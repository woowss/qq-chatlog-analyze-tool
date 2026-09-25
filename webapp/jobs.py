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
    analyze_emotion,
    analyze_topics,
    analyze_relationship,
    analyze_habits,
    analyze_profile,
    begin_run,
)
from analyzer.group_client import GROUP_DIMENSIONS
from analyzer.logger import get_logger
from analyzer.shutdown import shutdown_requested
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

# 群聊维度来自 analyzer/group_client.py（dim → (中文名, 执行函数, 进度单位)）。
# 两套维度**按会话模式二选一**：私聊文件请求群聊维度（或反之）会被 api 层拒绝，
# 因为把群聊维度跑在私聊数据上只会产出"我 vs 对方"式的错误结论。
GROUP_DIM_NAMES = {dim: info[0] for dim, info in GROUP_DIMENSIONS.items()}
GROUP_ANALYZE_FUNCS = {dim: info[1] for dim, info in GROUP_DIMENSIONS.items()}
ALL_DIMENSION_NAMES = {**DIMENSION_NAMES, **GROUP_DIM_NAMES}


def dimensions_for_mode(is_group: bool) -> list:
    """该模式该跑哪些维度（顺序即"一键全量"的执行顺序）。

    群聊把最贵的"成员画像"放在最后：用户先拿到便宜的群级结果，配额万一在中途耗尽，
    也已经有可用产出。
    """
    return list(GROUP_DIMENSIONS) if is_group else list(ANALYZE_FUNCS)


def analyze_func_for(dimension: str):
    """维度 → 执行函数（两套注册表合一，未知维度返回 None）"""
    return ANALYZE_FUNCS.get(dimension) or GROUP_ANALYZE_FUNCS.get(dimension)


def is_group_dimension(dimension: str) -> bool:
    return dimension in GROUP_ANALYZE_FUNCS


def dimension_unit(dimension: str) -> str:
    """进度单位：群聊成员画像是"人"，其余是"月"（前端提示文案用它）"""
    info = GROUP_DIMENSIONS.get(dimension)
    return info[2] if info else "月"


# 内存任务表：job_id -> 状态字典
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS_KEPT = 256  # 表长上限：超限按完成时间先淘汰已结束的条目
# running 条目的硬上限（兜底，**不参与 TTL 淘汰**）。
# 曾经 running 也按 created 走 TTL，而 _prune_jobs 就在轮询端点入口调用
# （webapp/api.py 的 api_analyze_job），于是一键全量分析（5 维度 × 多月 × 思考模式）
# 跑过默认 15 分钟后，会在**自己的轮询请求**里把自己删掉，此后每次轮询都 404，
# 用户看到的是"任务不存在（服务可能已重启）"——与实际不符（任务还在跑；结果因先落盘
# 而不会重复付费，但状态直播断了）。所以 running 只受这个远大于任何合理单次运行的
# 硬上限约束，用来兜住"线程异常死亡、没来得及写 finished_at"的滞留条目。
JOB_RUNNING_MAX_SECONDS = max(8 * JOB_TTL_SECONDS, 2 * 3600)


def _is_prunable(job: dict, now: float) -> bool:
    """该条目是否可淘汰"""
    if job.get("status") == "running":
        return now - job.get("created", 0) > JOB_RUNNING_MAX_SECONDS
    return job.get("finished_at", job.get("created", 0)) < now - JOB_TTL_SECONDS


def _prune_jobs():
    """清掉过期条目；仍超上限就按完成时间淘汰已结束的（running 不参与 TTL 淘汰）"""
    now = time.time()
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if _is_prunable(v, now)]:
            JOBS.pop(jid, None)
        if len(JOBS) > MAX_JOBS_KEPT:
            finished = sorted(
                ((v.get("finished_at", 0), k) for k, v in JOBS.items() if v.get("status") != "running")
            )
            for _, jid in finished[: len(JOBS) - MAX_JOBS_KEPT]:
                JOBS.pop(jid, None)


def _running_jobs_locked(sid: str, chat_hash: str) -> list[tuple[str, str]]:
    """（调用方须持有 JOBS_LOCK）返回该 session+文件的 running 任务 [(job_id, dim)]"""
    return [
        (jid, j.get("dim", ""))
        for jid, j in JOBS.items()
        if j["status"] == "running" and j.get("sid") == sid and j.get("chat_hash") == chat_hash
    ]


def _get_or_create_job(
    sid: str, dimension: str, chat_hash: str, total: int, conflict_dimension: str | None = None
):
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
        job = {
            "status": "running",
            "dim": dimension,
            "done": 0,
            "total": total,
            "cancel": False,
            "chat_hash": chat_hash,
            "sid": sid,
            "created": time.time(),
        }
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


def _done_str(done: int, total: int, unit: str = "月") -> str:
    return "准备中" if total == 0 else f"{done}/{total} {unit}"


def _fail_job(job_id: str, message: str) -> None:
    """把任务标记为失败（统一出口：免得每处都重复一遍 with JOBS_LOCK 样板）"""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j:
            j.update(status="error", error=message, finished_at=time.time())


def _job_cancelled(job_id: str) -> bool:
    with JOBS_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel"))


def _stop_requested(job_id: str) -> bool:
    """是否该停止继续派发新任务：用户点了取消，或进程正在关闭（Ctrl+C / SIGTERM）。

    两者必须分开看的地方在缓存写入：关闭时 _analyze_periods 会带着"已完成的部分
    月份"返回，把它当正常结果写进**维度**缓存，用户之后重跑就直接命中这份残缺结果，
    缺掉的月份再也不会补上。而用户主动取消是"我不要这次结果"，同样不该写。
    已经完成调用的月份本身已经落在月份缓存里，重跑不会重复付费。
    """
    return _job_cancelled(job_id) or shutdown_requested()


def _run_job(job_id: str, dimension: str, filepath: str, chat_hash: str) -> None:
    """后台线程执行分析：更新进度、支持取消、成功后写磁盘缓存"""
    dim_name = DIMENSION_NAMES.get(dimension, dimension)
    # 一次运行 = 这一个维度：把调用计数清零，让 LLM_MAX_CALLS_PER_RUN 从 0 起算。
    # 命中缓存的月份不发请求、不计入，所以上限只约束真正花钱的部分。
    begin_run()
    try:
        # 会话文件可能在"发起任务"与"后台线程开工"之间被 24 小时清理收走
        # （uploads/ 按 mtime 回收）。不特判的话它只是一个 FileNotFoundError，
        # 用户看到的是 "AI 分析失败: [Errno 2] No such file or directory: ..."。
        if not os.path.exists(filepath):
            logger.warning("%s 中止：会话文件已被清理（%s…）", dim_name, chat_hash[:8])
            _fail_job(job_id, "会话文件已被清理（上传文件只保留 24 小时），请重新上传聊天记录")
            return
        chat = store._load_chat_cached(filepath)
        func = analyze_func_for(dimension)
        if func is None:
            _fail_job(job_id, f"未知维度: {dimension}")
            return

        def on_progress(done: int, total: int):
            with JOBS_LOCK:
                j = JOBS.get(job_id)
                if j:
                    j.update(done=done, total=total)

        def should_cancel() -> bool:
            return _stop_requested(job_id)

        logger.info("开始 %s ...（后台任务 %s）", dim_name, job_id[:8])
        result = func(chat, on_progress=on_progress, should_cancel=should_cancel, chat_hash=chat_hash)
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
                j.update(
                    status="error",
                    error="分析未产生结果：可能全部月份失败，请查看日志",
                    finished_at=time.time(),
                )
            else:
                j.update(status="done", result=result, finished_at=time.time())
        if result and not cancelled:
            logger.info("%s 完成（任务 %s）", dim_name, job_id[:8])
    except Exception as e:
        logger.error("%s 失败: %s", dim_name, e)
        _fail_job(job_id, f"AI 分析失败: {e}")


def _run_analyze_all(
    job_id: str, filepath: str, chat_hash: str, refresh: bool, is_group: bool = False
) -> None:
    """一键全量分析：按维度顺序执行（维度内部已有月份级并发），
    已缓存的维度直接跳过（refresh 时强制重跑），单维度失败不阻断其余维度。

    is_group 默认 False（私聊维度集）：既有调用点与老测试不带这个参数时行为完全不变。
    """
    # 整个全量循环只清一次计数：这样 LLM_MAX_CALLS_PER_RUN 覆盖的是"一次点击的全量"，
    # 而不是每个维度各自一份额度（那样 5 个维度会把它放大 5 倍）。
    begin_run()
    try:
        # 同 _run_job：全量任务可能排在文件被清理之后才开工
        if not os.path.exists(filepath):
            logger.warning("一键全量分析中止：会话文件已被清理（%s…）", chat_hash[:8])
            _fail_job(job_id, "会话文件已被清理（上传文件只保留 24 小时），请重新上传聊天记录")
            return
        chat = store._load_chat_cached(filepath)
        dims = dimensions_for_mode(is_group)
        total = len(dims)
        summary: dict[str, str] = {}

        def should_cancel() -> bool:
            return _stop_requested(job_id)

        for idx, dim in enumerate(dims, 1):
            if should_cancel():
                break
            dim_name = ALL_DIMENSION_NAMES.get(dim, dim)
            with JOBS_LOCK:
                j = JOBS.get(job_id)
                if j:
                    j["detail"] = f"{idx}/{total} {dim_name}"
            if not refresh and store._read_cache(dim, chat_hash) is not None:
                summary[dim] = "cached"
            else:
                unit = dimension_unit(dim)

                def on_inner(done: int, tot: int, _dim=dim_name, _idx=idx, _unit=unit):
                    with JOBS_LOCK:
                        j = JOBS.get(job_id)
                        if j:
                            j["detail"] = f"{_idx}/{total} {_dim}（{_done_str(done, tot, _unit)}）"

                try:
                    result = analyze_func_for(dim)(
                        chat, on_progress=on_inner, should_cancel=should_cancel, chat_hash=chat_hash
                    )
                    if result and should_cancel():
                        # 取消/关闭打断的维度：不写缓存（残缺结果一旦落盘，重跑会命中它，
                        # 缺的月份就再也不会补上）；已完成的月份仍在月份缓存里，不重复付费
                        summary[dim] = "cancelled"
                    elif result:
                        store._write_cache(dim, chat_hash, result)
                        summary[dim] = "done"
                    else:
                        summary[dim] = "empty"
                except QuotaExhaustedError as e:
                    # 配额/限流致命：已完成的维度结果已落盘缓存，如实报告
                    summary[dim] = "aborted"
                    _fail_job(job_id, f"{e}（已完成维度：{len(summary)}，其结果已缓存）")
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
            # 这里已经在 JOBS_LOCK 里，不能再调 _stop_requested()/_job_cancelled()
            # ——JOBS_LOCK 是不可重入的 Lock，同线程二次获取会直接死锁。
            # j["cancel"] 现成可读，关闭标志则由无锁的 shutdown_requested() 给出。
            if j.get("cancel") or shutdown_requested():
                j.update(status="cancelled", finished_at=time.time())
            else:
                j.update(status="done", result=summary, finished_at=time.time())
        logger.info("一键全量分析完成（任务 %s）: %s", job_id[:8], summary)
    except Exception as e:
        logger.error("一键全量分析失败: %s", e)
        _fail_job(job_id, f"AI 分析失败: {e}")
