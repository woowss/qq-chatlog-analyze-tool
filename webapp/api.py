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
"""AI 分析 API：发起/轮询/取消、结果缓存读取、用量与状态"""

import io
import hashlib
import os
import threading
import time
from bisect import bisect_left
from datetime import date

from flask import jsonify, request, send_file, session

from analyzer.deepseek_client import QuotaExhaustedError, is_api_configured, test_connection
from analyzer.logger import get_logger
from analyzer.result_schema import ResultValidationError, validate_result
from analyzer.usage import get_usage
from config import UPLOAD_FOLDER
from webapp import store
from webapp.analysis_scope import (
    apply_scope,
    normalize_scope,
    participant_labels,
    scope_identity,
    scope_summary,
)
from webapp.message_index import index_for
from webapp.jobs import (
    ALL_DIMENSION_NAMES,
    JOBS,
    analyze_func_for,
    dimensions_for_mode,
    is_group_dimension,
    is_private_only_dimension,
    JOBS_LOCK,
    _get_or_create_job,
    _fail_job,
    _prune_jobs,
    _run_analyze_all,
    _run_selected_plan,
    _run_job,
    _session_chat_file,
)
from webapp.security import _guard_post

logger = get_logger("app")


def _job_identity_payload(job_id):
    """Return the chat/scope identity frozen when this job was created."""
    with JOBS_LOCK:
        job = JOBS.get(job_id) or {}
        return {
            "chat_hash": job.get("chat_hash", ""),
            "scope_id": job.get("scope_id", ""),
        }


def _load_api_chat(filepath):
    """Keep parse/read failures inside the API's JSON error contract."""
    try:
        return store._load_chat_cached(filepath), None
    except FileNotFoundError:
        return None, ("聊天文件已被清理，请重新上传聊天记录", 410)
    except Exception as exc:
        logger.warning("聊天文件读取失败（%s）", type(exc).__name__)
        return None, ("聊天文件无法读取或格式已损坏，请重新上传聊天记录", 400)


def _start_analysis_thread(job_id, target, args, **kwargs):
    try:
        threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()
    except Exception as exc:
        logger.error("分析线程启动失败（%s）", type(exc).__name__)
        _fail_job(job_id, "分析任务启动失败，请稍后重试")
        return jsonify({"error": "分析任务启动失败，请稍后重试", **_job_identity_payload(job_id)}), 503
    return jsonify({"job": job_id, **_job_identity_payload(job_id)})


# 表情图抓取是同步且耗时的操作，加锁避免连点触发重复下载
_FACE_FETCH_LOCK = threading.Lock()


def _dimension_guard(dimension: str):
    """维度校验：必须存在于总表，且与当前会话的模式匹配。

    私聊文件跑群聊维度（或反之）不只是"没意义"——群聊维度会把"我 vs 对方"的数字
    当成群的数字讲给模型听，产出看着像结论、其实口径错位的东西。所以直接拒绝，
    并明确告诉用户当前是什么模式。

    recap 需要单独一条：`is_group_dimension()` 按"在不在群聊表里"判断，
    而 recap 既不在群聊表也不在私聊的 ANALYZE_FUNCS 表里（它是第三套独立注册表
    RECAP_DIMENSIONS），于是它被判成"非群聊维度"，群聊会话 `is_group=True` 与之
    相等 → 守卫**放行**。口径归属见 jobs.is_private_only_dimension。
    """
    if analyze_func_for(dimension) is None:
        return jsonify({"error": f"未知维度: {dimension}"}), 400
    is_group = session.get("chat_mode") == "group"
    if is_group and is_private_only_dimension(dimension):
        label = ALL_DIMENSION_NAMES.get(dimension, dimension)
        return jsonify({"error": f"「{label}」不适用于当前记录（这是群聊记录）"}), 400
    if is_group_dimension(dimension) != is_group:
        want = "群聊" if is_group else "私聊"
        label = ALL_DIMENSION_NAMES.get(dimension, dimension)
        return jsonify({"error": f"「{label}」不适用于当前记录（这是{want}记录）"}), 400
    return None


def api_analyze(dimension: str):
    """发起维度分析：命中缓存直接返回，否则启动后台任务并返回 job id"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    bad = _dimension_guard(dimension)
    if bad:
        return bad

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]

    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # 缓存命中（除非显式 refresh=1 强制重跑）
    refresh = request.args.get("refresh") == "1"
    if not refresh:
        cache_fp = store.analysis_cache_fingerprint(dimension, chat)
        cached = store._read_cache(dimension, chat_hash, fingerprint=cache_fp)
        if cached is not None:
            logger.info("%s 命中缓存，直接返回", ALL_DIMENSION_NAMES.get(dimension, dimension))
            return jsonify(
                {
                    "cached": True,
                    "chat_hash": chat_hash,
                    "scope_id": scope_identity(scope),
                    "result": cached,
                }
            )

    _prune_jobs()
    # 全量任务已覆盖本维度：拒绝而不是另起一个任务（否则同一维度会被分析两遍、双倍计费）
    job_id, reused, conflict = _get_or_create_job(
        session.sid,
        dimension,
        chat_hash,
        total=0,
        conflict_dimension="all",
        refresh=refresh,
        identity=scope_identity(scope),
        scope_id=scope_identity(scope),
    )
    if conflict:
        if conflict == dimension:
            logger.info("已有 %s 任务的刷新模式不同，拒绝复用", dimension)
            return jsonify({"error": "同维度分析正在运行，但刷新模式不同，请等它完成或先取消后重试"}), 409
        logger.info("已有全量任务在运行，拒绝重复启动 %s", dimension)
        return jsonify({"error": "一键全量分析正在运行，请等它完成或先取消（避免重复调用 API）"}), 409
    if reused:
        logger.info("复用进行中的 %s 任务 %s", dimension, job_id[:8])
        return jsonify({"job": job_id, "reused": True, **_job_identity_payload(job_id)})

    return _start_analysis_thread(
        job_id, _run_job, (job_id, dimension, filepath, chat_hash, refresh), scope=scope
    )


def api_analyze_job(job_id: str):
    """查询后台任务进度；完成时附带结果"""
    # 轮询是最高频的 API 入口，顺手在这里修剪过期任务：
    # 只在新任务启动时修剪的话，"跑完就不再发起分析"的进程会把结果一直挂在内存里
    _prune_jobs()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("sid") != session.sid:
            return jsonify({"error": "任务不存在"}), 404
        if not session.get("chat_hash") or j.get("chat_hash") != session.get("chat_hash"):
            return jsonify({"error": "任务不存在"}), 404
        resp = {
            "status": j["status"],
            "done": j["done"],
            "total": j["total"],
            "chat_hash": j.get("chat_hash", ""),
            "scope_id": j.get("scope_id", ""),
        }
        if j.get("detail"):
            resp["detail"] = j["detail"]
        is_plan = j.get("dim") == "plan" or j.get("summary") is not None
        if j.get("chat_hash") and store._is_recently_purged(j["chat_hash"]):
            # A completed job may outlive deletion of its chat in JOBS. Do not make
            # its in-memory model output retrievable after the cleanup boundary.
            # This applies to legacy single-dimension and all-dimension jobs too.
            j.update(
                status="cancelled",
                result={},
                results={},
                summary={},
                partial_results={},
                partial=True,
                error="聊天已清理，分析结果已丢弃",
            )
            resp.update(
                status="cancelled",
                result={},
                results={},
                summary={},
                partial_results={},
                partial=True,
                error=j["error"],
            )
            return jsonify(resp)
        if j["status"] == "done":
            resp["result"] = j["result"]
            if j.get("summary") is not None:
                resp["summary"] = j["summary"]
            if is_plan:
                resp["partial"] = bool(j.get("partial"))
                resp["partial_results"] = j.get("partial_results", {})
                resp["results"] = j.get("results", {})
            elif j.get("partial"):
                resp["partial"] = True
                resp["partial_results"] = j.get("partial_results", {})
        elif is_plan and j["status"] in {"error", "cancelled"}:
            for key in ("result", "summary", "partial_results", "results"):
                if key in j:
                    resp[key] = j[key]
            resp["partial"] = bool(j.get("partial", True))
            if j.get("error"):
                resp["error"] = j["error"]
            elif j["status"] == "cancelled":
                resp["error"] = "分析已取消"
        elif j["status"] == "error":
            resp["error"] = j.get("error", "未知错误")
            if j.get("partial"):
                resp["partial"] = True
                resp["result"] = j.get("result", {})
                resp["partial_results"] = j.get("partial_results", {})
    return jsonify(resp)


def _requested_plan_dimensions(values, is_group):
    """Parse the preview's explicit dimensions or the current mode's full set."""
    if isinstance(values, str):
        values = [values]
    if values is None:
        values = []
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
        return None
    if not values or values == ["all"]:
        return dimensions_for_mode(is_group)
    dimensions = list(
        dict.fromkeys(item.strip() for value in values for item in value.split(",") if item.strip())
    )
    if not dimensions:
        return dimensions_for_mode(is_group)
    for dimension in dimensions:
        if dimension not in ALL_DIMENSION_NAMES:
            return None
        if is_group_dimension(dimension) != is_group or (is_group and is_private_only_dimension(dimension)):
            return None
    return dimensions


def _build_current_analysis_plan(dimensions, refresh):
    filepath, err = _session_chat_file()
    if err:
        return None, (err[0], err[1])
    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return None, load_err
    try:
        chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return None, (str(exc), 400)
    from webapp.analysis_plan import build_analysis_plan

    plan = build_analysis_plan(chat, chat_hash, dimensions, refresh=refresh)
    plan["scope"] = scope
    plan["scope_summary"] = scope_summary(scope, participant_labels(raw_chat))
    plan["chat_hash"] = chat_hash
    plan["scope_id"] = scope_identity(scope)
    return plan, None


def api_analysis_plan():
    """Read-only preview of dimensions, unit caches, token ranges and optional cost."""
    is_group = session.get("chat_mode") == "group"
    dimensions = _requested_plan_dimensions(request.args.getlist("dimensions"), is_group)
    if dimensions is None:
        return jsonify({"error": "计划维度与当前聊天模式不匹配"}), 400
    refresh = request.args.get("refresh") == "1"
    plan, err = _build_current_analysis_plan(dimensions, refresh)
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify(plan)


def api_analysis_scope():
    """Read or replace the session's validated analysis scope."""
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    if request.method == "POST":
        guard = _guard_post()
        if guard:
            return jsonify({"error": guard[0]}), guard[1]
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "分析范围请求必须是 JSON 对象"}), 400
        raw_scope = payload.get("scope", {})
        try:
            scope = normalize_scope(raw_scope, chat)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if scope:
            session["analysis_scope"] = scope
        else:
            session.pop("analysis_scope", None)
    else:
        try:
            scope = normalize_scope(session.get("analysis_scope"), chat)
        except ValueError as exc:
            stored_scope = session.get("analysis_scope")
            if not isinstance(stored_scope, dict) or "message_ids" not in stored_scope:
                return jsonify({"error": f"当前分析范围已失效：{exc}。请重新设置范围。"}), 409
            # Old snippet scopes used message IDs, which may be ambiguous after duplicate IDs
            # are found. Keep the other validated filters and let the UI replace the snippet.
            recoverable = dict(stored_scope)
            recoverable.pop("message_ids", None)
            try:
                scope = normalize_scope(recoverable, chat)
            except ValueError:
                scope = {}
            warning = f"旧版消息片段无法精确定位（{exc}），请重新选择消息片段。"
        else:
            warning = ""
    names = participant_labels(chat)
    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    return jsonify(
        {
            "scope": scope,
            "scope_id": scope_identity(scope),
            "chat_hash": chat_hash,
            "active": bool(scope),
            "summary": scope_summary(scope, names),
            "warning": warning if request.method == "GET" else "",
            "senders": [{"uid": p.uid, "name": p.name, "is_self": p.is_self} for p in chat.participants()],
        }
    )


def api_analyze_plan():
    """Recheck the preview before starting only the selected analysis units."""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "分析计划请求必须是 JSON 对象"}), 400
    is_group = session.get("chat_mode") == "group"
    raw_dimensions = payload.get("dimensions")
    if isinstance(raw_dimensions, str):
        raw_dimensions = [raw_dimensions]
    if raw_dimensions is None and "dimensions" in payload:
        return jsonify({"error": "dimensions 必须是字符串或字符串数组"}), 400
    if raw_dimensions is None:
        raw_dimensions = []
    if not isinstance(raw_dimensions, list) or any(not isinstance(item, str) for item in raw_dimensions):
        return jsonify({"error": "dimensions 必须是字符串或字符串数组"}), 400
    dimensions = _requested_plan_dimensions(raw_dimensions, is_group)
    if dimensions is None:
        return jsonify({"error": "计划维度与当前聊天模式不匹配"}), 400
    refresh = payload.get("refresh", False)
    if not isinstance(refresh, bool):
        return jsonify({"error": "refresh 必须是布尔值"}), 400
    current, err = _build_current_analysis_plan(dimensions, refresh)
    if err:
        return jsonify({"error": err[0]}), err[1]
    if payload.get("plan_id") != current["plan_id"]:
        return jsonify(
            {
                "changed": True,
                "error": "缓存或分析配置已变化，请检查更新后的计划再启动",
                "plan": current,
                "chat_hash": current["chat_hash"],
                "scope_id": current["scope_id"],
            }
        ), 409
    selected = payload.get("selected_units")
    if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected):
        return jsonify({"error": "至少选择一个分析单元"}), 400
    allowed = {unit["key"] for unit in current["units"] if unit["status"] != "无可分析消息"}
    if len(set(selected)) != len(selected) or not set(selected).issubset(allowed):
        return jsonify({"error": "所选单元不属于当前计划，请重新预览"}), 400
    needs_api = any(
        unit["key"] in selected and (unit["text_calls_max"] or unit["vision_calls_max"])
        for unit in current["units"]
    )
    if needs_api and not is_api_configured():
        return jsonify({"error": "API Key 未配置，请编辑 .env 文件"}), 400
    filepath, file_err = _session_chat_file()
    if file_err:
        return jsonify({"error": file_err[0]}), file_err[1]
    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    identity = hashlib.sha256(
        (current["plan_id"] + "\n" + "\n".join(sorted(selected))).encode("utf-8")
    ).hexdigest()
    _prune_jobs()
    job_id, reused, conflict = _get_or_create_job(
        session.sid,
        "plan",
        chat_hash,
        total=len(selected),
        conflict_dimension="*",
        refresh=refresh,
        identity=identity,
        scope_id=current["scope_id"],
    )
    if conflict:
        return jsonify({"error": "已有分析任务正在运行，请等它完成或先取消（避免重复调用 API）"}), 409
    if reused:
        return jsonify({"job": job_id, "reused": True, **_job_identity_payload(job_id)})
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(plan=current, selected_units=selected)
    return _start_analysis_thread(
        job_id,
        _run_selected_plan,
        (job_id, filepath, chat_hash, refresh, current, selected),
        scope=current.get("scope"),
    )


def api_analyze_cancel(job_id: str):
    """请求取消后台任务（协作式：已发出的 API 请求会自然结束，结果丢弃）"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("sid") != session.sid:
            return jsonify({"error": "任务不存在"}), 404
        j["cancel"] = True
    return jsonify({"status": "cancelling"})


def api_analysis_result(dimension: str):
    """读取已缓存的分析结果（页面加载时优先于 sessionStorage 使用）"""
    bad = _dimension_guard(dimension)
    if bad:
        return bad
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    cache_fp = store.analysis_cache_fingerprint(dimension, chat)
    cached = store._read_cache(dimension, chat_hash, fingerprint=cache_fp)
    stale_strategy = False
    if cached is None and not scope:
        cached = store._read_legacy_person_context_cache(dimension, chat_hash)
        stale_strategy = cached is not None
    if cached is None:
        return jsonify(
            {
                "error": "暂无该维度的分析结果",
                "chat_hash": chat_hash,
                "scope_id": scope_identity(scope),
            }
        ), 404
    return jsonify(
        {
            "cached": True,
            "chat_hash": chat_hash,
            "stale_strategy": stale_strategy,
            "scope_id": scope_identity(scope),
            "scope_summary": scope_summary(scope),
            "result": cached,
        }
    )


def api_analyze_all():
    """一键全量分析：五个维度顺序跑完，进度按维度汇报"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]

    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    refresh = request.args.get("refresh") == "1"
    is_group = session.get("chat_mode") == "group"
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        _scoped_chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    _prune_jobs()
    job_id, reused, conflict = _get_or_create_job(
        session.sid,
        "all",
        chat_hash,
        total=len(dimensions_for_mode(is_group)),
        conflict_dimension="*",
        refresh=refresh,
        identity=scope_identity(scope),
        scope_id=scope_identity(scope),
    )
    if reused:
        logger.info("复用进行中的一键全量任务 %s", job_id[:8])
        return jsonify({"job": job_id, "reused": True, **_job_identity_payload(job_id)})
    if conflict:
        if conflict == "all":
            logger.info("已有全量任务的刷新模式不同，拒绝复用")
            return jsonify({"error": "一键全量分析正在运行，但刷新模式不同，请等它完成或先取消后重试"}), 409
        # 已有单维度任务在跑：全量任务会把这些维度再跑一遍（重复计费），先拒绝
        logger.info("已有 %s 任务在运行，拒绝启动全量分析", conflict)
        return jsonify(
            {
                "error": f"已有「{ALL_DIMENSION_NAMES.get(conflict, conflict)}」任务在运行，"
                "请等它完成或先取消（避免重复调用 API）"
            }
        ), 409

    return _start_analysis_thread(
        job_id, _run_analyze_all, (job_id, filepath, chat_hash, refresh, is_group), scope=scope
    )


def api_faces_fetch():
    """抓取表情原图到本地缓存（可选功能；只在用户点击时联网）。

    抓不到就如实报告：离线、404、超时都会被逐条跳过，界面继续用 emoji/文字渲染。
    抓取是同步且要几十秒的，用一把锁避免连点造成重复下载。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    from analyzer import face_images

    if not face_images.enabled():
        return jsonify({"error": "表情图功能未开启：请在 .env 中设置 QQCHAT_FACE_IMAGES=true"}), 403

    if not _FACE_FETCH_LOCK.acquire(blocking=False):
        return jsonify({"error": "正在抓取中，请等这次结束"}), 409
    try:
        filepath = session.get("filepath")
        if not filepath or not os.path.exists(filepath):
            return jsonify({"error": "请先上传聊天记录"}), 400
        chat_hash = session.get("chat_hash", "")
        chat = store._load_chat_cached(filepath)
        faces = face_images.collect_cached(chat, chat_hash)
        total = len(faces)
        before = len(face_images.url_map(face_images.ensure(faces, allow_network=False)))
        # 带上时长上限：抓取同步跑在请求线程里，不设预算时 300 张 × 6s 超时能挂住半小时
        have = face_images.ensure(faces, allow_network=True, max_seconds=face_images.FETCH_BUDGET_SECONDS)
        after = len(face_images.url_map(have))
        return jsonify(
            {
                "total": total,
                "available": after,
                "fetched": max(0, after - before),
                # 仍未拿到图的（超级表情 + 本次没抓完的），前端据此如实提示
                "pending": max(0, total - after),
                "budget_seconds": face_images.FETCH_BUDGET_SECONDS,
            }
        )
    finally:
        _FACE_FETCH_LOCK.release()


def api_media_upload():
    """接收 WebUI 上传的图片副本（只挑"看图需要的那几十张"）。

    安全与容量约束：
    - 只接受图片扩展名，按 basename 平铺存到 uploads/media/<chat_hash>/，
      彻底规避 ../ 之类的路径穿越（导出器的文件名自带 md5 前缀，重名极罕见）；
    - 单文件大小与总量都有上限，超限跳过而不是写爆磁盘；
    - chat_hash 必须与当前会话一致，防止往别人的目录里塞文件。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    from analyzer import vision

    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    # 前端会上报它认为的 chat_hash：不一致说明页面还停在旧会话上（用户中途换过文件），
    # 这时接收图片副本只会写进一个已经没人引用的目录，所以直接拒绝。
    # 不传该字段是允许的（老客户端/脚本），此时以 session 里的哈希为准。
    form_hash = (request.form.get("chat_hash") or "").strip()
    if form_hash and form_hash != chat_hash:
        return jsonify({"error": "会话与文件不匹配，请刷新页面重新上传"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"saved": 0, "skipped": 0})

    target_dir = vision.session_media_dir(chat_hash)
    os.makedirs(target_dir, exist_ok=True)
    saved = skipped = 0
    total_bytes = 0
    for fs in files[: vision.MEDIA_UPLOAD_MAX_FILES]:
        name = os.path.basename((fs.filename or "").replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in vision.SUPPORTED_EXT:
            skipped += 1
            continue
        data = fs.read(vision.VISION_MAX_BYTES + 1)
        if not data or len(data) > vision.VISION_MAX_BYTES:
            skipped += 1
            continue
        if total_bytes + len(data) > vision.MEDIA_UPLOAD_MAX_BYTES:
            skipped += 1
            continue
        path = os.path.join(target_dir, name)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            logger.warning("图片副本写入失败 %s: %s", name, e)
            skipped += 1
            continue
        total_bytes += len(data)
        saved += 1
    if saved:
        logger.info("已接收 %d 张图片副本（%.1f MB），供图片理解使用", saved, total_bytes / 1048576)
    return jsonify({"saved": saved, "skipped": skipped, "bytes": total_bytes})


def api_usage():
    """LLM token 用量统计（按天 × 维度聚合，仅数字无聊天内容）"""
    return jsonify(get_usage())


def api_status():
    """API 配置状态"""
    status = is_api_configured()
    logger.debug("API 状态查询: %s", "已配置" if status else "未配置")
    return jsonify({"api_ok": status})


def api_status_test():
    """连通性实测：真实发一个 1-token 请求，把端点/Key/模型名的错误当场说清。

    POST 而不是 GET：它会打一次网络请求、可能产生费用（1 token），不能被
    预取或爬虫触发；错误详情原样回传（Key 永远不会出现在里面）。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    ok, detail = test_connection()
    return jsonify({"ok": ok, "detail": detail})


def api_chat_delete():
    """主动删除当前聊天：上传原件 + 全部派生缓存（统计/维度/月份/图片摘要/图片副本）。

    此前级联清理的唯一触发点是"换一份新文件"——想"看完就清、不留隐私"只能先随便
    传个文件顶一下，或手删目录再重启。清理逻辑复用与换文件同一口径：先松开本会话
    引用，仍有其他会话在用这份内容时**不**删它们共享的已付费结果（与换文件路径一致）。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "当前会话没有聊天记录"}), 400
    sid = getattr(session, "sid", "") or ""
    removed = 0
    filepath = session.get("filepath") or ""
    # 只回收本工具写进 uploads/ 的副本（与换文件路径同一条守卫）
    if filepath and os.path.dirname(filepath) == UPLOAD_FOLDER and os.path.exists(filepath):
        try:
            os.remove(filepath)
            removed += 1
        except OSError as e:
            logger.warning("上传副本删除失败（可能仍残留敏感内容）: %s", e)
    store.forget_live_chat(chat_hash, sid)
    # 显式删除：额外核对会话后端，别让"没点退出就关掉的浏览器"留下幽灵引用把清理挡住
    shared = store.other_live_sessions(chat_hash, exclude_sid=sid, require_live_session=True)
    if shared:
        logger.info("该聊天仍被其它 %d 个会话使用，本次仅解除本会话引用", len(shared))
    else:
        removed += store._purge_chat_caches(chat_hash)
    for key in (
        "chat_hash",
        "filepath",
        "chat_name",
        "self_name",
        "other_name",
        "total_messages",
        "chat_mode",
        "overview",  # 旧会话兼容键
        "analysis_scope",
    ):
        session.pop(key, None)
    return jsonify({"ok": True, "removed": removed, "kept_for_other_sessions": bool(shared)})


def api_export_chat():
    """导出当前聊天的统计 + 已付费 AI 结果为 zip（备份/换机器迁移）。

    月份缓存按 manifest 一起打包——只带维度文件的话，新机器重跑会整月重新付费。
    GET 无副作用；文件经会话鉴权与 SameSite=Lax 双重保护（跨站 <img> 拿不到 cookie）。
    """
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    buf = io.BytesIO()
    count = store.write_chat_bundle(chat_hash, buf)
    if not count:
        return jsonify({"error": "该聊天还没有可导出的缓存（统计尚未算好？）"}), 404
    buf.seek(0)
    from datetime import datetime

    name = f"qqchatlog_export_{chat_hash[:8]}_{datetime.now():%Y%m%d-%H%M}.zip"
    logger.info("导出 %d 个结果文件（聊天 %s…）", count, chat_hash[:8])
    return send_file(buf, as_attachment=True, download_name=name, mimetype="application/zip")


def api_import_chat():
    """导入本工具导出的结果包：恢复已付费分析（跨机器不换血）。

    只按文件名落回 ai_cache/ 与 stats_cache/ 两个目录，条目路径一律不信；
    导入后若当前会话还没有聊天、而包里 meta 的 chat_hash 与随后上传的内容一致，
    分析会直接命中——这正是"迁移不重复付费"的完整闭环。

    两道判定（见 store.read_chat_bundle）：
    - **归属**：包里每个条目必须属于 meta 声明的那份聊天。少了这条，一个不含任何
      聊天数据的 zip 就能把 `stats_<你当前聊天>.json` 写成任意内容，导入后被当作
      真实统计与"已付费 AI 结论"渲染——本工具"结论可回溯到本地事实"的立论基础；
    - **确认**：包声明的正是当前会话打开的这份聊天时先要一次二次确认（409 +
      need_confirm）。换机器恢复是合法用法，但那也是"冒充当前聊天结果"最省事的路，
      所以让用户点两下，而不是静默覆盖正在看的数据。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    file = request.files.get("file")
    if file is None or not (file.filename or "").lower().endswith(".zip"):
        return jsonify({"error": "请选择本工具导出的 .zip 结果包"}), 400
    form = request.form
    confirm = str(form.get("confirm") or "") in ("1", "true", "on")
    out = store.read_chat_bundle(
        file.stream, confirm_overwrite=confirm, live_hash=session.get("chat_hash") or ""
    )
    if out.get("need_confirm"):
        return jsonify(out), 409
    if "error" in out:
        return jsonify({"error": out["error"]}), 400
    logger.info("导入结果包：写入 %d、跳过 %d", out["written"], out["skipped"])
    return jsonify({"ok": True, **out})


def _browse_body(m) -> str:
    """浏览视图下的"这条消息说了什么"：正文优先，空正文时由媒体/表情占位补出。

    同一份拼法同时供**展示**与**搜索**用（口径唯一）：此前搜索只查 m.text 的话，
    纯图片/纯语音这类"没有正文但有内容"的消息就永远搜不到——而它们在界面上
    明明显示着 [图片]/[语音] 标签，症状是"看得见、搜不着"。
    """
    if m.text:
        return m.text
    bits = []
    if m.media_kind:
        from parser.qq_parser import MEDIA_KINDS

        kind = MEDIA_KINDS.get(m.media_kind, m.media_kind)
        bits.append(f"[{kind}{':' + m.media_label if m.media_label else ''}]")
    if m.has_image:
        bits.append(f"[图片×{m.image_count or 1}]")
    if m.face_names:
        bits.append("[表情:" + "、".join(m.face_names) + "]")
    if m.face_ids:
        bits.append(f"[表情×{len(m.face_ids)}]")
    return " ".join(bits)


def _fmt_browse_message(m, self_uid: str, position: int | None = None) -> dict:
    """单条消息 → 浏览视图（原文浏览是"如实呈现"：撤回与系统消息也列出，打标而非隐藏）。

    正文与媒体口径沿用解析结果：text 不含占位符（那是统计口径的干净设计），
    界面上用 media_label / 表情名补出"这条消息里发生过什么"。
    """
    result = {
        "id": m.id,
        "time": m.time_str,
        "name": m.sender_name,
        "uid": m.sender_uid,
        "is_self": bool(self_uid) and m.sender_uid == self_uid,
        "text": _browse_body(m),
        "is_reply": m.is_reply,
        "recalled": m.recalled,
        "system": m.system,
        "mentions_all": m.mentions_all,
    }
    if position is not None:
        result["position"] = position
    return result


def _match_browse(m, q: str, side: str, month: str, dt_from: str, dt_to: str, self_uid: str) -> bool:
    """过滤谓词：一条消息是否进结果集。q 匹配正文、媒体标签与表情名（大小写不敏感）。"""
    if month and not m.time_str.startswith(month):
        return False
    if dt_from and m.time_str[:10] < dt_from:
        return False
    if dt_to and m.time_str[:10] > dt_to:
        return False
    if side == "self" and m.sender_uid != self_uid:
        return False
    if side == "other" and m.sender_uid == self_uid:
        return False
    if side and side != "self" and side != "other" and m.sender_uid != side:
        # 群聊按成员 uid 过滤（前端下拉传的就是 uid）；与 self/other 两条分支互不冲突
        return False
    if q:
        hay = " ".join(
            x for x in (_browse_body(m), m.media_label, " ".join(m.face_names), m.sender_name) if x
        ).lower()
        if q.lower() not in hay:
            return False
    return True


def api_messages():
    """原始消息浏览与搜索（数据全在本机内存/磁盘，零 API 成本）。

    参数：q 关键词 | sender（self/other 或群成员 uid）| month=YYYY-MM |
    from/to=YYYY-MM-DD | page（1 起）| per_page（≤200）| around=消息 id（定位上下文，
    忽略分页，返回其前后各 ~10 条）| around_pos=消息位置（精确定位重复 ID）|
    context=1（从原始时间线展开上下文）。
    这是"AI 结论可回溯"的入口：锐评引用了某句话，用户从此能搜到原文看上下文。
    """
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    try:
        chat = store._load_chat_cached(filepath)
    except Exception as e:  # noqa: BLE001 —— 坏文件当场说清，不抛 500 堆栈
        return jsonify({"error": f"读取聊天记录失败: {e}"}), 500

    args = request.args
    q = (args.get("q") or "").strip()
    sender = (args.get("sender") or "").strip()
    month = (args.get("month") or "").strip()
    dt_from = (args.get("from") or "").strip()
    dt_to = (args.get("to") or "").strip()
    ts_from_raw = (args.get("ts_from") or "").strip()
    ts_to_raw = (args.get("ts_to") or "").strip()
    try:
        page = max(1, int(args.get("page") or 1))
        # 下限 1（不是 10）：客户端要小页（分页预览、调试）时，钳到 10 会让"第 2 页"
        # 因 lo 超过总数而翻出空列表——尊重合理的小 per_page，只卡上限。
        per_page = min(200, max(1, int(args.get("per_page") or 100)))
        ts_from = int(ts_from_raw) if ts_from_raw else None
        ts_to = int(ts_to_raw) if ts_to_raw else None
    except ValueError:
        return jsonify({"error": "page/per_page/ts_from/ts_to 必须是数字"}), 400
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        return jsonify({"error": "时间片段的开始不能晚于结束"}), 400

    index = index_for(chat, _browse_body, _match_browse)
    around = (args.get("around") or "").strip()
    around_pos_raw = (args.get("around_pos") or "").strip()
    scope_only_context = args.get("scope_only") == "1"
    try:
        around_pos = int(around_pos_raw) if around_pos_raw else None
    except ValueError:
        return jsonify({"error": "around_pos 必须是数字"}), 400
    if around_pos is not None and args.get("context") != "1":
        return jsonify({"error": "around_pos 仅支持原始时间线上下文查询"}), 400
    context_mode = bool(around or around_pos_raw) and args.get("context") == "1"
    if context_mode and around_pos is not None and not 0 <= around_pos < len(chat.messages):
        return jsonify({"error": "找不到该消息的上下文"}), 404
    context_chat = chat
    context_source_positions = None
    if context_mode and scope_only_context and session.get("analysis_scope") is not None:
        try:
            context_chat, context_scope = apply_scope(chat, session.get("analysis_scope"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if context_scope:
            context_source_positions = context_chat._analysis_source_positions
    positions = (
        range(len(context_chat.messages)) if context_mode else index.query(q, sender, month, dt_from, dt_to)
    )
    if not context_mode and (ts_from is not None or ts_to is not None):
        positions = [
            pos
            for pos in positions
            if (ts_from is None or chat.messages[pos].timestamp >= ts_from)
            and (ts_to is None or chat.messages[pos].timestamp <= ts_to)
        ]
    total = len(positions)
    if around or around_pos is not None:
        # 搜索结果点开的上下文要回到原始时间线，否则前后 10 条都会是相同关键词命中，
        # 看不到真正相邻的对话。旧调用仍沿用过滤后定位；新 UI 显式请求 context=1。
        idx = None
        if context_source_positions is not None and around_pos is not None:
            try:
                idx = context_source_positions.index(around_pos)
            except ValueError:
                return jsonify({"error": "找不到该消息的分析范围内上下文"}), 404
        elif context_source_positions is not None and around:
            idx = next(
                (pos for pos, message in enumerate(context_chat.messages) if message.id == around),
                None,
            )
        elif around_pos is not None:
            idx = around_pos
        if idx is None and context_source_positions is not None:
            return jsonify({"error": "找不到该消息的分析范围内上下文"}), 404
        if idx is None:
            for pos in index.ids.get(around, ()):
                offset = bisect_left(positions, pos)
                if offset < len(positions) and positions[offset] == pos:
                    idx = offset
                    break
        if idx is None:
            return jsonify({"error": "找不到该消息的上下文"}), 404
        lo = max(0, idx - 10)
        window_positions = list(positions[lo : idx + 11])
        formatted = []
        for pos in window_positions:
            source_pos = context_source_positions[pos] if context_source_positions is not None else pos
            formatted.append(_fmt_browse_message(context_chat.messages[pos], chat.self_uid, source_pos))
        return jsonify(
            {
                "total": total,
                "around": around,
                "around_position": around_pos,
                "messages": formatted,
            }
        )

    lo = (page - 1) * per_page
    return jsonify(
        {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "messages": [
                _fmt_browse_message(chat.messages[pos], chat.self_uid, pos)
                for pos in positions[lo : lo + per_page]
            ],
            "senders": [
                {"uid": p.uid, "name": p.name, "raw_name": p.raw_name, "is_self": p.is_self}
                for p in chat.participants()
            ],
            "chat_mode": session.get("chat_mode", "private"),
        }
    )


def api_keyword_timeline():
    """Aggregate keyword hits and session-like segments from the existing browse index."""
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    q = (request.args.get("q") or "").strip()
    sender = (request.args.get("sender") or "").strip()
    month = (request.args.get("month") or "").strip()
    dt_from = (request.args.get("from") or "").strip()
    dt_to = (request.args.get("to") or "").strip()
    if not q:
        return jsonify({"error": "请输入关键词后再生成时间线"}), 400
    if len(q) > 200:
        return jsonify({"error": "关键词最多 200 个字符"}), 400
    chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        if month and (len(month) != 7 or date.fromisoformat(month + "-01").strftime("%Y-%m") != month):
            raise ValueError("月份格式应为 YYYY-MM")
        start = date.fromisoformat(dt_from) if dt_from else None
        end = date.fromisoformat(dt_to) if dt_to else None
        dt_from = start.isoformat() if start else ""
        dt_to = end.isoformat() if end else ""
        if start and end and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        index = index_for(chat, _browse_body, _match_browse)
        result = index.timeline(q, side=sender, month=month, dt_from=dt_from, dt_to=dt_to)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


def api_period_compare():
    """Compare two date ranges using local statistics only."""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    payload = request.get_json(silent=True) or {}
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("a", {}), dict)
        or not isinstance(payload.get("b", {}), dict)
    ):
        return jsonify({"error": "时段参数格式无效"}), 400
    chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        from analyzer.period_compare import compare_periods

        result = compare_periods(chat, payload.get("a") or {}, payload.get("b") or {})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


def api_report_share():
    """Return a sanitized static report copy without touching the source caches."""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("options", {}), dict):
        return jsonify({"error": "脱敏选项格式无效"}), 400
    chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        mode = store.stats_mode_of(chat)
        stats = store._load_stats(session.get("chat_hash", ""), expect_mode=mode, read_only=True)
        if stats is None:
            return jsonify({"error": "本地统计尚未准备好，请稍后重试"}), 409
        from webapp.share_report import render_share_html

        html, warnings, options = render_share_html(chat, stats, payload.get("options"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "html": html,
            "filename": "聊天分析分享报告.html",
            "warnings": warnings,
            "options": options,
        }
    )


def api_evidence(dimension: str):
    """Read-only verification of the current session's cached citations."""
    bad = _dimension_guard(dimension)
    if bad:
        return bad
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    chat_hash = session.get("chat_hash") or store._chat_hash(filepath)
    cached = store._read_cache(
        dimension, chat_hash, fingerprint=store.analysis_cache_fingerprint(dimension, chat)
    )
    stale_strategy = False
    if cached is None and not scope:
        cached = store._read_legacy_person_context_cache(dimension, chat_hash)
        stale_strategy = cached is not None
    if cached is None:
        return jsonify({"error": "暂无该维度的分析结果"}), 404
    from webapp.evidence import verify_sources

    result = verify_sources(
        chat,
        dimension,
        cached,
        index_for(chat, _browse_body, _match_browse),
        _fmt_browse_message,
        person_context_enabled=False if stale_strategy else None,
    )
    if stale_strategy:
        result["stale_strategy"] = True
    result["scope_id"] = scope_identity(scope)
    result["scope_summary"] = scope_summary(scope)
    return jsonify(result)


def api_job_history():
    """最近完成的分析任务列表（落盘账目，跨重启仍在）。只含维度/状态/计数——不含聊天内容。"""
    return jsonify({"history": store.read_job_history()})


#: 同题互斥：(chat_hash, 问题哈希) -> 持有该问题的 sid。
#: 提问的缓存是**调用返回之后**才写的（见 api_ask 结尾），所以两个并发相同问题
#: 都会读不到缓存、都去发一次真实调用 —— 用户点两下就是两次付费，正是本文件
#: _get_or_create_job 那条规则（"拒绝而不是另起一个任务，否则同一维度被分析两遍、
#: 双倍计费"）要防的事。提问走同步路径、进不了任务表，所以在这里补一道同口径的锁。
#: 带 TTL 兜底：持有者被硬杀时不许把这个问题永久锁死。
_ASK_IN_FLIGHT: dict = {}
_ASK_LOCK = threading.Lock()
_ASK_STALE_SECONDS = 600.0
_ASK_FILTERED_INPUT_CACHE_VERSION = "filtered-input-duration-v2"


def _ask_key_fingerprint(question: str) -> str:
    import hashlib

    return hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:16]


def _ask_cache_question(question: str, chat) -> str:
    """Key ask answers by the messages/statistics actually sent to the model.

    Target-only scopes do not change ask input, so they retain the full-chat key.
    Filtered scopes include a versioned identity because duration-based statistics
    changed when the scope stopped inheriting the export's full-chat duration.
    """
    input_scope_id = getattr(chat, "analysis_input_scope_id", "")
    if not input_scope_id:
        return question
    return f"{question}\0{_ASK_FILTERED_INPUT_CACHE_VERSION}:{input_scope_id}"


def _ask_acquire(chat_hash: str, question: str) -> tuple:
    """占用这个问题；返回 (token, busy_sid)。busy_sid 非空表示已有人在问同一题。"""
    sid = getattr(session, "sid", "") or ""
    key = (chat_hash, _ask_key_fingerprint(question))
    now = time.time()
    with _ASK_LOCK:
        holder = _ASK_IN_FLIGHT.get(key)
        if holder and now - holder[1] < _ASK_STALE_SECONDS:
            return None, holder[2]
        token = f"{sid}:{now}:{os.getpid()}"
        _ASK_IN_FLIGHT[key] = (token, now, sid)
        # 有界：只清过期项，避免长期跑下来这张表一直涨
        for k in [k for k, v in _ASK_IN_FLIGHT.items() if now - v[1] > _ASK_STALE_SECONDS]:
            _ASK_IN_FLIGHT.pop(k, None)
        return token, None


def _ask_release(chat_hash: str, question: str, token) -> None:
    key = (chat_hash, _ask_key_fingerprint(question))
    with _ASK_LOCK:
        holder = _ASK_IN_FLIGHT.get(key)
        # 只解自己占的那把：超时被别人接管后，迟到的持有者不许把别人的锁放掉
        if holder and holder[0] == token:
            _ASK_IN_FLIGHT.pop(key, None)


def _private_only_guard(label: str):
    """recap / ask 是私聊专用：群聊会话直接拒。

    这两族吃的同一份摘要（recap_client._recap_digest）结构性地是二人关系口径：
    头行写"参与者：{self_name}（我）与 {other_name}"，抽样对话里每一条非本人消息
    都被标成 other_name，而群聊文件里 other_name 是**群名**（parser 的兜底取值）。
    于是 N 个成员的话被合并成一个人格、"我 vs 大家"被讲成二人关系——正是
    _dimension_guard 文档里说的"产出看着像结论、其实口径错位"。
    /recap 页面早就按这个口径把群聊会话重定向走了（views.recap），ask 之前漏了，
    而且每漏一次就真花一次钱（约 15 万字符的输入）。
    """
    if session.get("chat_mode") == "group":
        return jsonify({"error": f"「{label}」不适用于当前记录（这是群聊记录）"}), 400
    return None


def api_ask():
    """自定义提问：一次同步调用 + 问题级缓存（重问同一题免费）。

    与维度不同，它是"一问一答"的即席请求：单次调用没有多月份进度要直播，
    同步返回比塞进任务表更诚实（前端转圈等待，超过一分钟属正常）。
    缓存文件名含问题哈希、落在同一 ai_cache/ 目录——级联清理、结果导出、
    保留期回收全部自动覆盖，不给"提问"单造一套生命周期。
    """
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]
    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return jsonify({"error": "请先上传聊天记录"}), 400
    bad = _private_only_guard("提问角")
    if bad:
        return bad
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "提问请求必须是 JSON 对象"}), 400
    else:
        payload = request.form
    question = str(payload.get("question") or "").strip()
    if len(question) < 2:
        return jsonify({"error": "问题太短（至少 2 个字）"}), 400
    if len(question) > 300:
        return jsonify({"error": "问题过长（上限 300 字）——把大问题拆成两问，答案质量也更好"}), 400
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    raw_chat, load_err = _load_api_chat(filepath)
    if load_err:
        return jsonify({"error": load_err[0]}), load_err[1]
    try:
        chat, scope = apply_scope(raw_chat, session.get("analysis_scope"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # Cache and deduplicate by effective model input; target-only filters do not
    # affect ask's messages or digest and therefore share the full-chat entry.
    cache_question = _ask_cache_question(question, chat)
    refresh = str(payload.get("refresh") or "") in ("1", "true", "on")
    if not refresh:
        cached = store.ask_cache_read(chat_hash, cache_question)
        if cached is not None:
            return jsonify(
                {
                    "cached": True,
                    "chat_hash": chat_hash,
                    "scope_id": scope_identity(scope),
                    "result": cached,
                }
            )
    # 同题互斥：答案缓存是调用之后才写的，所以并发同题会各付一次钱（见 _acquire 说明）
    token, busy = _ask_acquire(chat_hash, cache_question)
    if busy is not None:
        return (
            jsonify(
                {
                    "error": "同一个问题正在分析中，请等这一次返回后再问（避免重复计费）",
                    "pending": True,
                    "chat_hash": chat_hash,
                    "scope_id": scope_identity(scope),
                }
            ),
            409,
        )
    # 释放在 finally 里：手写每条返回路径都放一次，正是本项目反复修的那类漏
    # （漏一条就把这个问题永久锁在 _ASK_STALE_SECONDS 里，用户看到的是"这个问题
    # 一直说正在分析中"）。异常路径同理。
    try:
        from analyzer import recap_client

        result = recap_client.answer_question(chat, question, chat_hash)
        if not result:
            return jsonify(
                {
                    "error": "这次提问没有拿到结果（材料不足或输出被截断），换个问法再试",
                    "chat_hash": chat_hash,
                    "scope_id": scope_identity(scope),
                }
            ), 502
        try:
            validate_result("ask", result)
        except ResultValidationError as e:
            logger.warning("提问结果结构无效（字段 %s）", e.path)
            return jsonify(
                {
                    "error": f"提问结果结构无效（字段 {e.path}），请重试",
                    "chat_hash": chat_hash,
                    "scope_id": scope_identity(scope),
                }
            ), 502
        store.ask_cache_write(chat_hash, cache_question, result)
        return jsonify(
            {
                "ok": True,
                "chat_hash": chat_hash,
                "scope_id": scope_identity(scope),
                "result": result,
            }
        )
    except QuotaExhaustedError as e:
        # 这条路径原先没有 except：而 _call_api 在 402/余额不足、401/403 配额、TPM 耗尽时
        # 是**故意抛** QuotaExhaustedError 的，异常于是穿透视图，用户拿到一页 Flask 的
        # 500 HTML，而那句专门写给人的"额度耗尽，请充值"被丢掉了——前端只显示
        # "提问失败（HTTP 500）"，完全无从下手。is_api_configured() 只查 Key 是否存在，
        # 所以"Key 有效但余额为 0"的第一次提问必定撞在这里。其它 LLM 入口都由 _run_job
        # 统一兜住这类异常，只有 ask 漏了。
        logger.warning("提问因配额/限流中止: %s", e)
        return jsonify(
            {
                "error": str(e),
                "quota": True,
                "chat_hash": chat_hash,
                "scope_id": scope_identity(scope),
            }
        ), 429
    finally:
        _ask_release(chat_hash, cache_question, token)


def register(app):
    """API 路由注册（端点名与旧 app.py 一致）"""
    app.add_url_rule("/api/analyze/<dimension>", "api_analyze", api_analyze, methods=["POST"])
    app.add_url_rule("/api/analyze-job/<job_id>", "api_analyze_job", api_analyze_job)
    app.add_url_rule(
        "/api/analyze-job/<job_id>/cancel", "api_analyze_cancel", api_analyze_cancel, methods=["POST"]
    )
    app.add_url_rule("/api/analysis/<dimension>", "api_analysis_result", api_analysis_result)
    app.add_url_rule("/api/analysis-plan", "api_analysis_plan", api_analysis_plan)
    app.add_url_rule("/api/analysis-scope", "api_analysis_scope", api_analysis_scope, methods=["GET", "POST"])
    app.add_url_rule("/api/analyze-plan", "api_analyze_plan", api_analyze_plan, methods=["POST"])
    app.add_url_rule("/api/analyze-all", "api_analyze_all", api_analyze_all, methods=["POST"])
    app.add_url_rule("/api/media", "api_media_upload", api_media_upload, methods=["POST"])
    app.add_url_rule("/api/faces/fetch", "api_faces_fetch", api_faces_fetch, methods=["POST"])
    app.add_url_rule("/api/usage", "api_usage", api_usage)
    app.add_url_rule("/api/status", "api_status", api_status)
    app.add_url_rule("/api/status/test", "api_status_test", api_status_test, methods=["POST"])
    app.add_url_rule("/api/chat/delete", "api_chat_delete", api_chat_delete, methods=["POST"])
    app.add_url_rule("/api/export", "api_export_chat", api_export_chat)
    app.add_url_rule("/api/import", "api_import_chat", api_import_chat, methods=["POST"])
    app.add_url_rule("/api/ask", "api_ask", api_ask, methods=["POST"])
    app.add_url_rule("/api/messages", "api_messages", api_messages)
    app.add_url_rule("/api/messages/timeline", "api_keyword_timeline", api_keyword_timeline)
    app.add_url_rule("/api/stats/compare", "api_period_compare", api_period_compare, methods=["POST"])
    app.add_url_rule("/api/report/share", "api_report_share", api_report_share, methods=["POST"])
    app.add_url_rule("/api/evidence/<dimension>", "api_evidence", api_evidence)
    app.add_url_rule("/api/jobs/history", "api_job_history", api_job_history)
