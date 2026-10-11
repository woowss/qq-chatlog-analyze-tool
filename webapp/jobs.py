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
    AnalysisIncompleteError,
    QuotaExhaustedError,
    begin_run,
    end_run,
    scrub_secrets,
)
from analyzer import deepseek_client as dc
from analyzer import group_client as gc
from analyzer import recap_client
from analyzer.result_schema import ResultValidationError, validate_dimension_result
from analyzer.group_client import GROUP_DIMENSIONS as GROUP_DIMENSIONS
from analyzer.recap_client import RECAP_DIMENSIONS as RECAP_DIMENSIONS
from analyzer import purge_marks
from analyzer.cache_policy import content_cache_policy
from analyzer.logger import get_logger
from analyzer.shutdown import shutdown_requested
from webapp import store
from webapp import analysis_catalog
from webapp.analysis_scope import apply_scope

logger = get_logger("app")

DIMENSION_NAMES = {
    key: item.label
    for key, item in analysis_catalog.ANALYSIS_CATALOG.items()
    if item.mode == "private" and item.include_in_all
}
ANALYZE_FUNCS = {
    key: item.runner
    for key, item in analysis_catalog.ANALYSIS_CATALOG.items()
    if item.mode == "private" and item.include_in_all
}

# 群聊维度来自 analyzer/group_client.py（dim → (中文名, 执行函数, 进度单位)）。
# 两套维度**按会话模式二选一**：私聊文件请求群聊维度（或反之）会被 api 层拒绝，
# 因为把群聊维度跑在私聊数据上只会产出"我 vs 对方"式的错误结论。
GROUP_DIM_NAMES = {
    key: item.label for key, item in analysis_catalog.ANALYSIS_CATALOG.items() if item.mode == "group"
}
GROUP_ANALYZE_FUNCS = {
    key: item.runner for key, item in analysis_catalog.ANALYSIS_CATALOG.items() if item.mode == "group"
}
# 「整体总括」（analyzer/recap_client.py）：私聊专用、单次调用、独立缓存族。
# **刻意不进** ANALYZE_FUNCS——dimensions_for_mode 就是"一键全量"的清单，
# README 对全量成本有实测承诺（约 ¥6），悄悄把新维度塞进去等于改了承诺；
# recap 走独立按钮，成本（一次调用）在页面上写清。
RECAP_ANALYZE_FUNCS = {
    key: item.runner for key, item in analysis_catalog.ANALYSIS_CATALOG.items() if item.private_only
}
ALL_DIMENSION_NAMES = {key: item.label for key, item in analysis_catalog.ANALYSIS_CATALOG.items()}


def dimensions_for_mode(is_group: bool) -> list:
    """该模式该跑哪些维度（顺序即"一键全量"的执行顺序）。

    群聊把最贵的"成员画像"放在最后：用户先拿到便宜的群级结果，配额万一在中途耗尽，
    也已经有可用产出。
    私聊的全量清单不含 recap（理由见 RECAP_ANALYZE_FUNCS 上方注释）。
    """
    mode = "group" if is_group else "private"
    return [
        key
        for key, item in analysis_catalog.ANALYSIS_CATALOG.items()
        if item.mode == mode
        and item.include_in_all
        and (key in (GROUP_ANALYZE_FUNCS if is_group else ANALYZE_FUNCS))
    ]


def analyze_func_for(dimension: str):
    """维度 → 执行函数（三套注册表合一，未知维度返回 None）"""
    return (
        ANALYZE_FUNCS.get(dimension)
        or GROUP_ANALYZE_FUNCS.get(dimension)
        or RECAP_ANALYZE_FUNCS.get(dimension)
    )


def is_group_dimension(dimension: str) -> bool:
    return analysis_catalog.mode_for(dimension) == "group"


def is_private_only_dimension(dimension: str) -> bool:
    """该维度是否"只适用于两人私聊"——群聊会话一律不许跑。

    为什么不能直接用 `not is_group_dimension(dim)` 代替：recap 既不在 ANALYZE_FUNCS
    也不在 GROUP_DIMENSIONS（它是第三套独立注册表 RECAP_DIMENSIONS），所以
    `is_group_dimension("recap")` 是 False，`False != True` 就放行了群聊会话。
    而 recap 与私聊五维吃的是**同一份**摘要，那份摘要是二人关系口径
    （"参与者：我 与 对方"，每条非本人消息都标成 other_name；群聊文件里
    other_name 是群名），拿它跑群聊就是"我 vs 全群"被讲成二人关系——与
    _dimension_guard 存在的目的完全同类，且每跑一次就真花一次钱。
    """
    return analysis_catalog.is_private_only(dimension)


def dimension_unit(dimension: str) -> str:
    """进度单位：群聊成员画像是"人"、recap 是"次"，其余是"月"（前端提示文案用它）"""
    return analysis_catalog.unit_for(dimension)


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
    sid: str,
    dimension: str,
    chat_hash: str,
    total: int,
    conflict_dimension: str | None = None,
    *,
    refresh: bool = False,
    identity: str | None = None,
    scope_id: str | None = None,
):
    """在**同一把锁内**完成"查重复用 → 建任务"，避免 check-then-act 之间插进第二个任务。

    conflict_dimension：与之互斥的维度名；传 "*" 表示"任意其他维度"。
    命中互斥时返回 conflict，调用方应拒绝该请求，而不是让同一批消息被分析两遍（双倍计费）。
    同维度只有刷新模式相同才能复用；模式不同也返回该维度作为 conflict，避免刷新请求
    被普通任务吞掉。检查与创建在同一把锁内完成，模式冲突不会另起并行付费任务。
    返回 (job_id, reused, conflict)。
    """
    with JOBS_LOCK:
        for jid, dim in _running_jobs_locked(sid, chat_hash):
            if dim == dimension:
                if identity is not None and JOBS[jid].get("identity") != identity:
                    return None, False, dim
                if scope_id is not None and JOBS[jid].get("scope_id", "") != scope_id:
                    return None, False, dim
                if JOBS[jid].get("refresh", False) != refresh:
                    return None, False, dim
                return jid, True, None
            if conflict_dimension == "*" or dim == conflict_dimension or dim == "plan":
                return None, False, dim
        job_id = uuid.uuid4().hex
        job = {
            "status": "running",
            "dim": dimension,
            "done": 0,
            "total": total,
            "cancel": False,
            "refresh": refresh,
            "chat_hash": chat_hash,
            "sid": sid,
            "created": time.time(),
            "identity": identity,
            "scope_id": scope_id or "",
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
            if j.get("dim") == "plan":
                j.setdefault("result", {})
                j.setdefault("results", {})
                j.setdefault("summary", {})
                j.setdefault("partial_results", {})
                j.setdefault("partial", True)


def _validated_partial_result(dimension: str, exc: Exception) -> dict | None:
    """Return only schema-valid unit results attached to an incomplete run."""
    result = getattr(exc, "partial_result", None)
    if not isinstance(result, dict) or not result:
        return None
    try:
        validate_dimension_result(dimension, result)
    except ResultValidationError as validation_error:
        logger.warning("%s 部分结果结构无效（字段 %s），已丢弃", dimension, validation_error.path)
        return None
    return result


def _save_partial_job_result(job_id: str, dimension: str, exc: Exception) -> None:
    """Keep completed independent members visible when a later member fails."""
    result = _validated_partial_result(dimension, exc)
    if result is None:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job and not store._is_recently_purged(job.get("chat_hash", "")):
            job.update(result=result, partial_results=result, partial=True)


def _job_cancelled(job_id: str) -> bool:
    with JOBS_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel"))


def _job_chat_hash(job_id: str) -> str:
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return j.get("chat_hash", "") if j else ""


def _stop_requested(job_id: str) -> bool:
    """是否该停止继续派发新任务：用户点了取消、进程正在关闭，或这个聊天刚被清掉。

    三者必须分开看的地方在缓存写入：关闭时 _analyze_periods 会带着"已完成的部分
    月份"返回，把它当正常结果写进**维度**缓存，用户之后重跑就直接命中这份残缺结果，
    缺掉的月份再也不会补上。而用户主动取消是"我不要这次结果"，同样不该写。
    已经完成调用的月份本身已经落在月份缓存里，重跑不会重复付费。

    "刚被清掉"这一条原先是缺的：分析动辄跑几分钟，用户在中间换了一份聊天，级联清理
    把派生数据全删了，而这一轮跑完照样把维度结果、月份文件、manifest 依次写回盘上——
    清理报称"已删除"的敏感数据原地复活，且已无人引用。把它做成停止条件，一处就同时
    管住三处：不再派发新月份、不写月份缓存、不写维度缓存（状态记为 cancelled）。
    """
    return _job_cancelled(job_id) or shutdown_requested() or purge_marks.is_marked(_job_chat_hash(job_id))


def _run_job(
    job_id: str,
    dimension: str,
    filepath: str,
    chat_hash: str,
    refresh: bool = False,
    scope=None,
) -> None:
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
        chat, _scope = apply_scope(store._load_chat_cached(filepath), scope)
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
        with content_cache_policy(refresh, should_cancel):
            result = func(chat, on_progress=on_progress, should_cancel=should_cancel, chat_hash=chat_hash)
        cancelled = should_cancel()
        # 先落盘缓存，再对外置 done：否则前端轮询到 done 立刻请求
        # /api/analysis/<dim> 时可能读不到缓存，反而重新发起一次付费分析
        if result and not cancelled:
            validate_dimension_result(dimension, result)
            cache_fp = store.analysis_cache_fingerprint(dimension, chat)
            store._write_cache(dimension, chat_hash, result, fingerprint=cache_fp)
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
        # 任务历史落盘（只记白名单字段，跨重启可查；错误文本不进这份账）
        store.append_job_history(
            {
                "t": j.get("finished_at"),
                "dim": dimension,
                "status": j.get("status"),
                "done": j.get("done", 0),
                "total": j.get("total", 0),
                "chat": (chat_hash or "")[:12],
            }
        )
        if result and not cancelled:
            logger.info("%s 完成（任务 %s）", dim_name, job_id[:8])
    except ResultValidationError as e:
        logger.warning("%s 结果结构无效（字段 %s）", dim_name, e.path)
        _save_partial_job_result(job_id, dimension, e)
        _fail_job(job_id, f"{dim_name} 结果结构无效（字段 {e.path}），请重试失败项")
    except Exception as e:
        # 洗一遍再落日志：上游/网关的错误正文里可能夹带 API Key（见 scrub_secrets）
        logger.error("%s 失败: %s", dim_name, scrub_secrets(e))
        _save_partial_job_result(job_id, dimension, e)
        # _fail_job 的文本会经 /api/analyze-job 回给浏览器，同样先洗（用户会截图求助）
        _fail_job(job_id, f"AI 分析失败: {scrub_secrets(e)}")
    finally:
        # 与 begin_run 配对（即使中途 return/抛异常也要释放）：漏掉的后果是
        # _run_depth 永远 >0，之后每次 begin_run 都不再清零，LLM_MAX_CALLS_PER_RUN
        # 会悄悄变成"进程启动以来的累计"，把正常的一次分析也掐断。
        end_run()


def _run_analyze_all(
    job_id: str, filepath: str, chat_hash: str, refresh: bool, is_group: bool = False, scope=None
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
        chat, _scope = apply_scope(store._load_chat_cached(filepath), scope)
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
            cache_fp = store.analysis_cache_fingerprint(dim, chat)
            if not refresh and store._read_cache(dim, chat_hash, fingerprint=cache_fp) is not None:
                summary[dim] = "cached"
            else:
                unit = dimension_unit(dim)

                def on_inner(done: int, tot: int, _dim=dim_name, _idx=idx, _unit=unit):
                    with JOBS_LOCK:
                        j = JOBS.get(job_id)
                        if j:
                            j["detail"] = f"{_idx}/{total} {_dim}（{_done_str(done, tot, _unit)}）"

                try:
                    with content_cache_policy(refresh, should_cancel):
                        result = analyze_func_for(dim)(
                            chat, on_progress=on_inner, should_cancel=should_cancel, chat_hash=chat_hash
                        )
                    if result and should_cancel():
                        # 取消/关闭打断的维度：不写缓存（残缺结果一旦落盘，重跑会命中它，
                        # 缺的月份就再也不会补上）；已完成的月份仍在月份缓存里，不重复付费
                        summary[dim] = "cancelled"
                    elif result:
                        validate_dimension_result(dim, result)
                        store._write_cache(dim, chat_hash, result, fingerprint=cache_fp)
                        summary[dim] = "done"
                    else:
                        summary[dim] = "empty"
                except QuotaExhaustedError as e:
                    # 配额/限流致命：已完成的维度结果已落盘缓存，如实报告
                    summary[dim] = "aborted"
                    _fail_job(job_id, f"{e}（已完成维度：{len(summary)}，其结果已缓存）")
                    return
                except ResultValidationError as e:
                    logger.warning("一键全量分析 %s 结果结构无效（字段 %s）", dim_name, e.path)
                    summary[dim] = "invalid"
                except Exception as e:
                    logger.error("一键全量分析 %s 失败: %s", dim_name, scrub_secrets(e))
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
            # j["cancel"] 现成可读，关闭标志则由无锁的 shutdown_requested() 给出；
            # 第三个停止条件（刚被清理）同理从手头直接判 —— 循环就是因此中断的，
            # 这里漏判会把"清理后整轮作废"如实发生的任务标成 done。
            if j.get("cancel") or shutdown_requested() or purge_marks.is_marked(j.get("chat_hash", "")):
                j.update(status="cancelled", finished_at=time.time())
            else:
                j.update(status="done", result=summary, finished_at=time.time())
            hist = {
                "t": j.get("finished_at"),
                "dim": "all",
                "status": j.get("status"),
                "done": j.get("done", 0),
                "total": j.get("total", 0),
                "chat": (j.get("chat_hash") or "")[:12],
            }
        store.append_job_history(hist)
        logger.info("一键全量分析完成（任务 %s）: %s", job_id[:8], summary)
    except Exception as e:
        logger.error("一键全量分析失败: %s", scrub_secrets(e))
        _fail_job(job_id, f"AI 分析失败: {scrub_secrets(e)}")
    finally:
        # 与 _run_job 同理：全量循环里的 return（配额/花费上限中止）也必须释放运行边界
        end_run()


def _plan_result_slice(dimension: str, cached: dict, selected_units: list[str]) -> dict:
    """Select unit values from a complete dimension cache without changing it."""
    if dimension == "recap":
        return cached
    if not isinstance(cached, dict):
        return {}
    unit_type = {
        "habits": "person",
        "profile": "person",
        "member_profiles": "member",
        "emotion": "month",
        "topics": "month",
        "relationship": "month",
        "group_dynamics": "month",
        "group_topics": "month",
        "group_emotion": "month",
    }.get(dimension)
    if unit_type is None:
        return {}
    result = {}
    for unit_key in selected_units:
        parts = unit_key.split(":", 2)
        if len(parts) != 3 or parts[0] != dimension or parts[1] != unit_type:
            continue
        item_key = parts[2]
        if item_key in cached:
            result[item_key] = cached[item_key]
    return result


def _run_selected_dimension(chat, dimension, unit_keys, on_progress, should_cancel, chat_hash):
    """Execute only selected units of a dimension, using its normal unit cache path."""
    from webapp import analysis_plan

    if dimension in {"habits", "profile"}:
        system_prompt, template, sample_size, stratified = analysis_plan._person_template(dimension)
        result = {}
        for index, unit_key in enumerate(unit_keys, 1):
            if should_cancel():
                break
            person_key = unit_key.rsplit(":", 1)[-1]
            messages = [
                message
                for message in chat.messages
                if (message.sender_uid == chat.self_uid) == (person_key == "self")
            ]
            target_uids = getattr(chat, "analysis_target_uids", None)
            if target_uids is not None:
                if person_key == "self":
                    messages = messages if chat.self_uid in target_uids else []
                else:
                    messages = [message for message in messages if message.sender_uid in target_uids]
            if not messages:
                raise AnalysisIncompleteError("人物画像计划已变化，请重新预览")
            display_name = chat.self_name if person_key == "self" else chat.other_name
            value = dc._analyze_person(
                system_prompt,
                sample_size,
                messages,
                display_name,
                template,
                max_tokens=dc.MAX_TOKENS_BY_DIM[dimension],
                tag=dimension,
                stratified=stratified,
                chat_hash=chat_hash,
                context_messages=chat.messages if dimension == "profile" else None,
                speaker_name_of=(
                    lambda message: chat.self_name if message.sender_uid == chat.self_uid else chat.other_name
                )
                if dimension == "profile"
                else None,
                scope_id=getattr(chat, "analysis_scope_id", ""),
            )
            if value:
                result[person_key] = value
            on_progress(index, len(unit_keys), display_name)
        return result

    if dimension == "member_profiles":
        import analyzer.group_prompts as gp

        members = {member.uid: member for member in gc.select_ai_members(chat)}
        template = gc._member_prompt_template()
        result = {}
        used_keys = set()
        failures = 0
        quota_error = None
        try:
            for index, unit_key in enumerate(unit_keys, 1):
                if should_cancel():
                    break
                uid = unit_key.rsplit(":", 1)[-1]
                member = members.get(uid)
                if member is None:
                    failures += 1
                    on_progress(index, len(unit_keys), uid)
                    continue
                try:
                    value = gc._analyze_member(
                        chat,
                        member,
                        gp.GROUP_SYSTEM_PROMPT_MEMBER_PROFILE,
                        template,
                        max_tokens=dc.MAX_TOKENS_BY_DIM[dimension],
                        tag=dimension,
                        chat_hash=chat_hash,
                        used_keys=used_keys,
                    )
                except QuotaExhaustedError as exc:
                    quota_error = exc
                    break
                except (AnalysisIncompleteError, ResultValidationError):
                    value = None
                if value:
                    result[uid] = value
                else:
                    failures += 1
                on_progress(index, len(unit_keys), member.name)
        finally:
            # Successful member files exist before the next member can fail; register
            # them on every exit so the orphan sweep cannot discard paid work.
            gc._record_member_usage(chat_hash, used_keys)
        if quota_error is not None:
            quota_error.partial_result = result
            raise quota_error
        if failures and not should_cancel():
            error = AnalysisIncompleteError("成员画像分析未取得全部成员的完整结果；已完成的成员可从缓存复用")
            error.partial_result = result
            raise error
        return result

    if dimension == "recap":
        return recap_client.analyze_recap(
            chat,
            on_progress=lambda done, total: on_progress(done, total, "整体总括"),
            should_cancel=should_cancel,
            chat_hash=chat_hash,
        )

    months = chat.months()
    selected_months = {
        key.rsplit(":", 1)[-1]: months[key.rsplit(":", 1)[-1]]
        for key in unit_keys
        if key.rsplit(":", 1)[-1] in months
    }
    if dimension in {"emotion", "topics", "relationship"}:
        systems = {
            "emotion": dc.SYSTEM_PROMPT_EMOTION,
            "topics": dc.SYSTEM_PROMPT_TOPICS,
            "relationship": dc.SYSTEM_PROMPT_RELATIONSHIP,
        }
        result = dc._analyze_periods(
            selected_months,
            systems[dimension],
            lambda period, messages: dc._month_prompt(chat, period, messages, chat_hash),
            max_tokens=dc.MAX_TOKENS_BY_DIM[dimension],
            tag=dimension,
            on_progress=lambda done, total: on_progress(done, total, "月份"),
            should_cancel=should_cancel,
            chat_hash=chat_hash,
        )
        if dimension == "topics":
            for row in result.values():
                dc._normalize_topic_weights(row)
        elif dimension == "emotion":
            for row in result.values():
                dc._clamp_int(row, "self_intensity", 0, 10)
                dc._clamp_int(row, "other_intensity", 0, 10)
        else:
            for row in result.values():
                dc._clamp_int(row, "closeness_score", 1, 10)
                dc._clamp_float(row, "initiator_ratio_self", 0.0, 1.0, 0.5)
        return result

    import analyzer.group_prompts as gp

    systems = {
        "group_dynamics": gp.GROUP_SYSTEM_PROMPT_DYNAMICS,
        "group_topics": gp.GROUP_SYSTEM_PROMPT_TOPICS,
        "group_emotion": gp.GROUP_SYSTEM_PROMPT_EMOTION,
    }
    result = gc._analyze_periods(
        selected_months,
        systems[dimension],
        lambda period, messages: gc._group_month_prompt(chat, period, messages, chat_hash),
        max_tokens=dc.MAX_TOKENS_BY_DIM[dimension],
        tag=dimension,
        on_progress=lambda done, total: on_progress(done, total, "月份"),
        should_cancel=should_cancel,
        chat_hash=chat_hash,
        fingerprint=gc.group_prompt_fingerprint(),
    )
    if dimension == "group_topics":
        for row in result.values():
            dc._normalize_topic_weights(row)
    elif dimension == "group_emotion":
        for row in result.values():
            dc._clamp_int(row, "group_intensity", 0, 10)
            for item in row.get("member_emotions") or []:
                if isinstance(item, dict):
                    dc._clamp_int(item, "intensity", 0, 10)
    return result


def _run_selected_plan(job_id, filepath, chat_hash, refresh, plan, selected_units, scope=None):
    """Plan job executor; partial dimensions never enter the complete dimension cache."""
    begin_run()
    try:
        if not os.path.exists(filepath):
            _fail_job(job_id, "会话文件已被清理（上传文件只保留 24 小时），请重新上传聊天记录")
            return
        chat, _scope = apply_scope(store._load_chat_cached(filepath), scope)
        all_units = {unit["key"]: unit for unit in plan["units"]}
        grouped = {dimension: [] for dimension in plan["dimensions"]}
        for key in selected_units:
            grouped[all_units[key]["dimension"]].append(key)
        total = sum(len(keys) for keys in grouped.values())
        summary = {}
        results = {}
        errors = {}
        quota_exhausted = False
        completed = 0

        def should_cancel():
            return _stop_requested(job_id)

        for dimension in plan["dimensions"]:
            keys = grouped[dimension]
            if not keys:
                summary[dimension] = "未选择"
                continue
            if should_cancel():
                summary[dimension] = "cancelled"
                continue
            if quota_exhausted:
                summary[dimension] = "aborted"
                continue
            dim_units = [
                unit["key"]
                for unit in plan["units"]
                if unit["dimension"] == dimension and unit["status"] != "无可分析消息"
            ]
            all_selected = set(keys) == set(dim_units)
            cache_fp = store.analysis_cache_fingerprint(dimension, chat)
            cached = None if refresh else store._read_cache(dimension, chat_hash, fingerprint=cache_fp)
            if cached is not None:
                result = cached if all_selected else _plan_result_slice(dimension, cached, keys)
                results[dimension] = result
                summary[dimension] = "cached" if all_selected else "partial"
                completed += len(keys)
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if job:
                        job.update(done=completed, detail=ALL_DIMENSION_NAMES.get(dimension, dimension))
                continue

            def on_progress(done, unit_total, detail, _base=completed, _dimension=dimension):
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if job:
                        job.update(
                            done=min(total, _base + done),
                            total=total,
                            detail=(
                                f"{ALL_DIMENSION_NAMES.get(_dimension, _dimension)}："
                                f"{detail} {done}/{unit_total}"
                            ),
                        )

            try:
                if all_selected:
                    func = analyze_func_for(dimension)

                    def on_full_progress(done, unit_total, _dimension=dimension):
                        on_progress(done, unit_total, _dimension)

                    with content_cache_policy(refresh, should_cancel):
                        result = func(
                            chat,
                            on_progress=on_full_progress,
                            should_cancel=should_cancel,
                            chat_hash=chat_hash,
                        )
                else:
                    with content_cache_policy(refresh, should_cancel):
                        result = _run_selected_dimension(
                            chat, dimension, keys, on_progress, should_cancel, chat_hash
                        )
                if result:
                    validate_dimension_result(dimension, result)
                if should_cancel():
                    summary[dimension] = "cancelled"
                    if result:
                        results[dimension] = result
                elif result:
                    if all_selected:
                        store._write_cache(dimension, chat_hash, result, fingerprint=cache_fp)
                        summary[dimension] = "done"
                    else:
                        summary[dimension] = "partial"
                    results[dimension] = result
                else:
                    summary[dimension] = "empty"
                    errors[dimension] = "未返回分析结果，请重试"
            except QuotaExhaustedError as exc:
                summary[dimension] = "aborted"
                errors[dimension] = scrub_secrets(exc)
                partial_result = _validated_partial_result(dimension, exc)
                if partial_result is not None:
                    results[dimension] = partial_result
                quota_exhausted = True
            except (AnalysisIncompleteError, ResultValidationError) as exc:
                summary[dimension] = "error"
                errors[dimension] = scrub_secrets(exc)
                partial_result = _validated_partial_result(dimension, exc)
                if partial_result is not None:
                    results[dimension] = partial_result
                logger.warning("计划分析 %s 未完成：%s", dimension, scrub_secrets(exc))
            except Exception as exc:
                summary[dimension] = "error"
                errors[dimension] = scrub_secrets(exc)
                logger.error("计划分析 %s 失败：%s", dimension, scrub_secrets(exc))
            completed += len(keys)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    job.update(done=completed, total=total)

        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            # Recheck at the point the terminal result is attached. Cleanup marks
            # before deleting files; checking before taking JOBS_LOCK leaves a race
            # where cleanup clears/redacts this job and this thread then restores
            # its partial results. Read the job's frozen chat identity while holding
            # the lock, and inspect the cancel flag directly (should_cancel would
            # acquire JOBS_LOCK again).
            purged = purge_marks.is_marked(job.get("chat_hash", chat_hash))
            cancelled = bool(job.get("cancel")) or shutdown_requested() or purged
            if purged:
                # Cache cleanup is a data deletion boundary, not a user request to
                # keep the in-memory partial model output available to the old job.
                results.clear()
            failed = any(value in {"error", "aborted", "empty"} for value in summary.values())
            partial = (
                cancelled
                or failed
                or any(value in {"partial", "未选择", "cancelled"} for value in summary.values())
            )
            status = "cancelled" if cancelled else "error" if failed else "done"
            error_message = None
            if cancelled:
                error_message = (
                    "聊天已清理，分析结果已丢弃" if purged else "分析已取消（此前完成的结果已保留）"
                )
            elif errors:
                error_message = "计划分析未完整完成：" + "；".join(
                    f"{ALL_DIMENSION_NAMES.get(dimension, dimension)}：{message}"
                    for dimension, message in errors.items()
                )
            job.update(
                status=status,
                result=summary if len(plan["dimensions"]) > 1 else results.get(plan["dimensions"][0], {}),
                results=results,
                summary=summary,
                partial_results={
                    key: value
                    for key, value in results.items()
                    if summary.get(key) in {"partial", "cancelled", "error", "aborted"}
                },
                partial=partial,
                error=error_message,
                finished_at=time.time(),
            )
            history = {
                "t": job["finished_at"],
                "dim": "plan",
                "status": job["status"],
                "done": job.get("done", 0),
                "total": job.get("total", 0),
                "chat": (chat_hash or "")[:12],
            }
        store.append_job_history(history)
    except Exception as exc:
        logger.error("计划分析失败：%s", scrub_secrets(exc))
        _fail_job(job_id, f"AI 分析失败: {scrub_secrets(exc)}")
    finally:
        end_run()
