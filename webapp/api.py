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

import os
import threading

from flask import jsonify, request, session

from analyzer.deepseek_client import is_api_configured
from analyzer.logger import get_logger
from analyzer.usage import get_usage
from webapp import store
from webapp.jobs import (
    ALL_DIMENSION_NAMES,
    JOBS,
    analyze_func_for,
    dimensions_for_mode,
    is_group_dimension,
    JOBS_LOCK,
    _get_or_create_job,
    _prune_jobs,
    _run_analyze_all,
    _run_job,
    _session_chat_file,
)
from webapp.security import _guard_post

logger = get_logger("app")

# 表情图抓取是同步且耗时的操作，加锁避免连点触发重复下载
_FACE_FETCH_LOCK = threading.Lock()


def _dimension_guard(dimension: str):
    """维度校验：必须存在于总表，且与当前会话的模式匹配。

    私聊文件跑群聊维度（或反之）不只是"没意义"——群聊维度会把"我 vs 对方"的数字
    当成群的数字讲给模型听，产出看着像结论、其实口径错位的东西。所以直接拒绝，
    并明确告诉用户当前是什么模式。
    """
    if analyze_func_for(dimension) is None:
        return jsonify({"error": f"未知维度: {dimension}"}), 400
    is_group = session.get("chat_mode") == "group"
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

    # 缓存命中（除非显式 refresh=1 强制重跑）
    if request.args.get("refresh") != "1":
        cached = store._read_cache(dimension, chat_hash)
        if cached is not None:
            logger.info("%s 命中缓存，直接返回", ALL_DIMENSION_NAMES.get(dimension, dimension))
            return jsonify({"cached": True, "result": cached})

    _prune_jobs()
    # 全量任务已覆盖本维度：拒绝而不是另起一个任务（否则同一维度会被分析两遍、双倍计费）
    job_id, reused, conflict = _get_or_create_job(
        session.sid, dimension, chat_hash, total=0, conflict_dimension="all"
    )
    if conflict:
        logger.info("已有全量任务在运行，拒绝重复启动 %s", dimension)
        return jsonify({"error": "一键全量分析正在运行，请等它完成或先取消（避免重复调用 API）"}), 409
    if reused:
        logger.info("复用进行中的 %s 任务 %s", dimension, job_id[:8])
        return jsonify({"job": job_id, "reused": True})

    threading.Thread(target=_run_job, args=(job_id, dimension, filepath, chat_hash), daemon=True).start()
    return jsonify({"job": job_id})


def api_analyze_job(job_id: str):
    """查询后台任务进度；完成时附带结果"""
    # 轮询是最高频的 API 入口，顺手在这里修剪过期任务：
    # 只在新任务启动时修剪的话，"跑完就不再发起分析"的进程会把结果一直挂在内存里
    _prune_jobs()
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("sid") != session.sid:
            return jsonify({"error": "任务不存在"}), 404
        resp = {"status": j["status"], "done": j["done"], "total": j["total"]}
        if j.get("detail"):
            resp["detail"] = j["detail"]
        if j["status"] == "done":
            resp["result"] = j["result"]
        elif j["status"] == "error":
            resp["error"] = j.get("error", "未知错误")
    return jsonify(resp)


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
    cached = store._read_cache(dimension, session.get("chat_hash") or store._chat_hash(filepath))
    if cached is None:
        return jsonify({"error": "暂无该维度的分析结果"}), 404
    return jsonify({"cached": True, "result": cached})


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

    _prune_jobs()
    job_id, reused, conflict = _get_or_create_job(
        session.sid, "all", chat_hash, total=len(dimensions_for_mode(is_group)), conflict_dimension="*"
    )
    if reused:
        logger.info("复用进行中的一键全量任务 %s", job_id[:8])
        return jsonify({"job": job_id, "reused": True})
    if conflict:
        # 已有单维度任务在跑：全量任务会把这些维度再跑一遍（重复计费），先拒绝
        logger.info("已有 %s 任务在运行，拒绝启动全量分析", conflict)
        return jsonify(
            {
                "error": f"已有「{ALL_DIMENSION_NAMES.get(conflict, conflict)}」任务在运行，"
                "请等它完成或先取消（避免重复调用 API）"
            }
        ), 409

    threading.Thread(
        target=_run_analyze_all, args=(job_id, filepath, chat_hash, refresh, is_group), daemon=True
    ).start()
    return jsonify({"job": job_id})


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


def register(app):
    """API 路由注册（端点名与旧 app.py 一致）"""
    app.add_url_rule("/api/analyze/<dimension>", "api_analyze", api_analyze, methods=["POST"])
    app.add_url_rule("/api/analyze-job/<job_id>", "api_analyze_job", api_analyze_job)
    app.add_url_rule(
        "/api/analyze-job/<job_id>/cancel", "api_analyze_cancel", api_analyze_cancel, methods=["POST"]
    )
    app.add_url_rule("/api/analysis/<dimension>", "api_analysis_result", api_analysis_result)
    app.add_url_rule("/api/analyze-all", "api_analyze_all", api_analyze_all, methods=["POST"])
    app.add_url_rule("/api/media", "api_media_upload", api_media_upload, methods=["POST"])
    app.add_url_rule("/api/faces/fetch", "api_faces_fetch", api_faces_fetch, methods=["POST"])
    app.add_url_rule("/api/usage", "api_usage", api_usage)
    app.add_url_rule("/api/status", "api_status", api_status)
