# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#
"""后台分析任务：内存任务表、去重互斥、执行体

任务状态只是给前端轮询用的"直播信号"；结果的权威存储在磁盘缓存
（webapp.store），所以重启丢失无妨，内存条目也无需久留：
TTL 默认 15 分钟（QQCHAT_JOB_TTL_SECONDS 可调），另设条目数上限，
完成即过期——此前 TTL 1 小时且只在"新任务启动"时修剪，
没人再发起分析的话，profile 那种几十 KB 的结果会一直躺在内存里。
"""
import os
import threading
import time
import uuid

from flask import session

from config import JOB_TTL_SECONDS
from analyzer.deepseek_client import (
    QuotaExhaustedError,
    analyze_emotion, analyze_topics, analyze_relationship,
    analyze_habits, analyze_profile,
)
from analyzer.logger import get_logger
from webapp import store

logger = get_logger("app")

DIMENSION_NAMES = {
    "emotion": "情绪分析",
    "topics": "话题趋势",
    "relationship": "人际关系",
    "habits": "个人习惯",
    "profile": "人物锐评",
}

ANALYZE_FUNCS = {
    "emotion": analyze_emotion,
    "topics": analyze_topics,
    "relationship": analyze_relationship,
    "habits": analyze_habits,
    "profile": analyze_profile,
}

# 内存任务表：job_id -> 状态字典
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS_KEPT = 256          # 表长上限：超限按完成时间先淘汰已结束的条目


def _prune_jobs():
    """清掉过期条目；仍超上限就按完成时间淘汰已结束的（不碰 running）"""
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if v.get("finished_at", v.get("created", 0)) < cutoff]:
            JOBS.pop(jid, None)
        if len(JOBS) > MAX_JOBS_KEPT:
            finished = sorted(
                ((v.get("finished_at", 0), k) for k, v in JOBS.items()
                 if v.get("status") != "running"))
            for _, jid in finished[:len(JOBS) - MAX_JOBS_KEPT]:
                JOBS.pop(jid, None)


def _running_jobs_locked(sid: str, chat_hash: str) -> list[tuple[str, str]]:
    """（调用方须持有 JOBS_LOCK）返回该 session+文件的 running 任务 [(job_id, dim)]"""
    return [(jid, j.get("dim", "")) for jid, j in JOBS.items()
            if j["status"] == "running" and j.get("sid") == sid
            and j.get("chat_hash") == chat_hash]


def _get_or_create_job(sid: str, dimension: str, chat_hash: str, total: int,
                       conflict_dimension: str | None = None):
    """在**同一把锁内**完成"查重复用 → 建任务"，避免 check-then-act 之间插进第二个任务。

    conflict_dimension：与之互斥的维度名；传 "*" 表示"任意其他维度"。
    命中互斥时返回 conflict，调用方应拒绝该请求，而不是让同一批消息被分析两遍（双倍计费）。
    返回 (job_id, reused, conflict)。
    """
    with JOBS_LOCK:
        for jid, dim in _running_jobs_locked(sid, chat_hash):
            if dim == dimension:
                return jid, True, None
            if conflict_dimension == "*" or dim == conflict_dimension:
                return None, False, dim
        job_id = uuid.uuid4().hex
        job = {"status": "running", "dim": dimension, "done": 0, "total": total,
               "cancel": False, "chat_hash": chat_hash, "sid": sid,
               "created": time.time()}
        if dimension == "all":
            job["detail"] = ""
        JOBS[job_id] = job
        return job_id, False, None


def _session_chat_file():
    """返回当前 session 可用的聊天文件路径，或 (错误消息, 状态码)"""
    if "filepath" not in session:
        return None, ("请先上传聊天记录", 400)
    filepath = session["filepath"]
    if not os.path.exists(filepath):
        return None, ("会话文件已过期, 请重新上传", 400)
    return filepath, None


def _done_str(done: int, total: int) -> str:
    return "准备中" if total == 0 else f"{done}/{total} 月"


def _run_job(job_id: str, dimension: str, filepath: str, chat_hash: str) -> None:
    """后台线程执行分析：更新进度、支持取消、成功后写磁盘缓存"""
    dim_name = DIMENSION_NAMES.get(dimension, dimension)
    try:
        chat = store._load_chat_cached(filepath)
        func = ANALYZE_FUNCS[dimension]

        def on_progress(done: int, total: int):
            with JOBS_LOCK:
                j = JOBS.get(job_id)
                if j:
                    j.update(done=done, total=total)

        def should_cancel() -> bool:
            with JOBS_LOCK:
                return bool(JOBS.get(job_id, {}).get("cancel"))

        logger.info("开始 %s ...（后台任务 %s）", dim_name, job_id[:8])
        result = func(chat, on_progress=on_progress, should_cancel=should_cancel,
                      chat_hash=chat_hash)
        cancelled = should_cancel()
        # 先落盘缓存，再对外置 done：否则前端轮询到 done 立刻请求
        # /api/analysis/<dim> 时可能读不到缓存，反而重新发起一次付费分析
        if result and not cancelled:
            store._write_cache(dimension, chat_hash, result)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if cancelled:
                j.update(status="cancelled", finished_at=time.time())
            elif not result:
                j.update(status="error",
                         error="分析未产生结果：可能全部月份失败，请查看日志",
                         finished_at=time.time())
            else:
                j.update(status="done", result=result, finished_at=time.time())
        if result and not cancelled:
            logger.info("%s 完成（任务 %s）", dim_name, job_id[:8])
    except Exception as e:
        logger.error("%s 失败: %s", dim_name, e)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j.update(status="error", error=f"AI 分析失败: {e}", finished_at=time.time())


def _run_analyze_all(job_id: str, filepath: str, chat_hash: str, refresh: bool) -> None:
    """一键全量分析：按维度顺序执行（维度内部已有月份级并发），
    已缓存的维度直接跳过（refresh 时强制重跑），单维度失败不阻断其余维度。"""
    try:
        chat = store._load_chat_cached(filepath)
        dims = list(ANALYZE_FUNCS)
        total = len(dims)
        summary: dict[str, str] = {}

        def should_cancel() -> bool:
            with JOBS_LOCK:
                return bool(JOBS.get(job_id, {}).get("cancel"))

        for idx, dim in enumerate(dims, 1):
            if should_cancel():
                break
            dim_name = DIMENSION_NAMES.get(dim, dim)
            with JOBS_LOCK:
                j = JOBS.get(job_id)
                if j:
                    j["detail"] = f"{idx}/{total} {dim_name}"
            if not refresh and store._read_cache(dim, chat_hash) is not None:
                summary[dim] = "cached"
            else:
                def on_inner(done: int, tot: int, _dim=dim_name, _idx=idx):
                    with JOBS_LOCK:
                        j = JOBS.get(job_id)
                        if j:
                            j["detail"] = f"{_idx}/{total} {_dim}（{_done_str(done, tot)}）"
                try:
                    result = ANALYZE_FUNCS[dim](chat, on_progress=on_inner,
                                                should_cancel=should_cancel,
                                                chat_hash=chat_hash)
                    if result:
                        store._write_cache(dim, chat_hash, result)
                        summary[dim] = "done"
                    else:
                        summary[dim] = "empty"
                except QuotaExhaustedError as e:
                    # 配额/限流致命：已完成的维度结果已落盘缓存，如实报告
                    summary[dim] = "aborted"
                    with JOBS_LOCK:
                        j = JOBS.get(job_id)
                        if j:
                            j.update(status="error",
                                     error=f"{e}（已完成维度：{len(summary)}，其结果已缓存）",
                                     finished_at=time.time())
                    return
                except Exception as e:
                    logger.error("一键全量分析 %s 失败: %s", dim_name, e)
                    summary[dim] = "error"
            with JOBS_LOCK:
                j = JOBS.get(job_id)
                if j:
                    j.update(done=idx, total=total)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if j.get("cancel"):
                j.update(status="cancelled", finished_at=time.time())
            else:
                j.update(status="done", result=summary, finished_at=time.time())
        logger.info("一键全量分析完成（任务 %s）: %s", job_id[:8], summary)
    except Exception as e:
        logger.error("一键全量分析失败: %s", e)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j.update(status="error", error=f"AI 分析失败: {e}", finished_at=time.time())
