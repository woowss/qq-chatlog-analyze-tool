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
"""页面路由：首页/上传/仪表盘/各分析页/报告导出"""

import os
import uuid

from flask import jsonify, redirect, render_template, request, send_file, session, url_for

from config import LOG_REDACT_NAMES, MAX_CONTENT_LENGTH, UPLOAD_FOLDER
from analyzer.deepseek_client import is_api_configured
from analyzer.logger import get_logger, mask_name
from webapp import store
from webapp.security import _guard_post

logger = get_logger("app")


def log_request():
    """记录每个请求的方法/路径/来源IP"""
    if request.path.startswith("/static/"):
        return  # 静态资源逐条记录只会淹没真正有用的日志
    ip = request.remote_addr or "127.0.0.1"
    logger.info("%s %s [%s]", request.method, request.path, ip)


def log_response(response):
    """记录响应状态码 (只记非成功状态)"""
    if response.status_code >= 400:
        logger.warning("--> %s %s", response.status_code, request.path)
    return response


def inject_stats_flag():
    """导航栏需要知道"当前是否有数据"，但统计已移出 session，这里统一注入"""
    if "overview" in session:  # 兼容旧会话
        return {"overview": session["overview"], "has_stats": True}
    return {"has_stats": bool(session.get("chat_hash"))}


def index():
    """首页 / 上传页面

    stats_error：上次上传的统计若在后台线程里失败，这里如实告诉用户
    （异步化之后没有 HTTP 响应能承载它，不说的话用户只会看到"上传成功却回首页"）。
    """
    return render_template(
        "index.html", api_ok=is_api_configured(), stats_error=store.stats_error(session.get("chat_hash", ""))
    )


def upload():
    """接收上传的 JSON 文件, 解析并存入 session

    请求线程只做两件事：流式落盘（顺带算哈希）与解析校验（坏文件要当场报 400）。
    十项统计交给 store.start_stats_job 后台算完落盘——浏览器少等一秒是一秒。
    """
    guard = _guard_post()
    if guard:
        return guard

    if "file" not in request.files:
        logger.warning("上传请求中没有 file 字段")
        return "请选择文件", 400

    file = request.files["file"]
    orig_name = file.filename
    # 大小写不敏感：导出器/浏览器给出的扩展名可能是 .JSON
    if orig_name == "" or not orig_name.lower().endswith(".json"):
        logger.warning("上传文件格式无效: %s", orig_name if not LOG_REDACT_NAMES else "<脱敏>")
        return "请选择有效的 .json 文件", 400

    # 旧文件信息先记下，等新文件解析成功后再处置（解析失败不伤及当前会话）
    old_path = session.get("filepath")
    old_hash = None
    if old_path and os.path.exists(old_path) and os.path.dirname(old_path) == UPLOAD_FOLDER:
        try:
            old_hash = store._chat_hash(old_path)
        except OSError:
            old_hash = None

    filename = f"{uuid.uuid4().hex}.json"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    try:
        size, new_hash = store.save_and_hash(file, filepath)
    except OSError as e:
        # 磁盘满/无写权限：给出可读原因，别抛一个 500 页面
        logger.error("上传文件写入失败: %s", e)
        return f"保存文件失败（磁盘空间或写入权限？）: {e}", 500
    if LOG_REDACT_NAMES:
        logger.info("文件已保存: %s (%d bytes, 原始文件名已脱敏)", filename, size)
    else:
        logger.info("文件已保存: %s (来自 %s, %d bytes)", filename, orig_name, size)

    try:
        chat = store._load_chat_cached(filepath)
        logger.info(
            "解析成功: %s <-> %s, %d 条消息, %d 天",
            mask_name(chat.self_name),
            mask_name(chat.other_name),
            len(chat.messages),
            chat.duration_days,
        )
        if chat.dropped_messages:
            # 这些消息的时间戳无法解析（缺失/null/非数值），已跳过而不是塞进 1970-01
            logger.warning("跳过 %d 条时间戳无效的消息（未计入统计与分析）", chat.dropped_messages)
    except Exception as e:
        logger.error("解析失败: %s", e)
        # 解析失败的孤儿文件立即删除；旧文件与缓存保持原样，会话不受影响
        try:
            os.remove(filepath)
        except OSError:
            pass
        return f"解析失败: {e}", 400

    # 新文件解析成功：处置旧文件。内容未变则保留其缓存（重传同文件应命中缓存省钱），
    # 内容变了才联动清除旧文件的派生缓存，避免敏感分析结果成为孤儿
    if old_path and old_path != filepath and os.path.exists(old_path):
        if old_hash and old_hash != new_hash:
            store._purge_chat_caches(old_hash)
        try:
            os.remove(old_path)
        except OSError:
            pass

    # 存入 session（只放小体积的会话元数据：统计结果落盘，见 store._save_stats）
    session["chat_name"] = chat.chat_name
    session["self_name"] = chat.self_name
    session["other_name"] = chat.other_name
    session["filepath"] = filepath
    session["chat_hash"] = new_hash
    session["total_messages"] = len(chat.messages)
    if old_hash and old_hash != new_hash:
        logger.info("同一会话上传了新文件，旧文件（%s…）的派生缓存已清理", old_hash[:8])

    # 本地统计：内容相同的文件直接复用上次结果（统计是确定性的，重算纯属浪费）
    if store._load_stats(new_hash) is None:
        logger.info("开始计算本地统计（后台线程）...")
        store.start_stats_job(chat, new_hash)
    else:
        logger.info("本地统计命中缓存，跳过重算")

    # WebUI 图片上传流程：告诉前端"看图需要哪些文件"，由浏览器从用户选中的
    # 导出目录里只挑这些上传（不搬整个 resources/，也不用改 .env 填路径）。
    wanted = []
    if store.vision_enabled():
        from parser.qq_parser import split_by_month
        from analyzer import vision

        wanted = vision.plan_wanted(split_by_month(chat))
    if _wants_json():
        return jsonify(
            {
                "ok": True,
                "next": url_for("dashboard"),
                "wanted_media": wanted,
                "total_messages": len(chat.messages),
            }
        )
    return redirect(url_for("dashboard"))


def _wants_json() -> bool:
    """AJAX 上传（带选中的导出目录）走 JSON 响应，普通表单提交仍走 302 跳转"""
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in (
        request.headers.get("Accept") or ""
    )


def upload_too_large(error):
    """413：超过 MAX_CONTENT_LENGTH 时给出可执行的中文提示。

    Flask 默认回的是 Werkzeug 自带的英文页（实测正文只有
    "The data value transmitted exceeds the capacity limit."），既没说限制是多少，
    也没说怎么调——用户只看到"上传失败"，无从下手。
    """
    limit_mb = MAX_CONTENT_LENGTH // 1048576
    message = (
        f"上传文件超过 {limit_mb} MB 上限：可在 .env 中调大 QQCHAT_MAX_UPLOAD_MB 后重启，"
        "或在 QQChatExporter 里缩小导出范围（例如按月分批导出）"
    )
    logger.warning("上传被拒：超过 %d MB 上限（QQCHAT_MAX_UPLOAD_MB 可调）", limit_mb)
    if _wants_json():
        return jsonify({"error": message}), 413
    return message, 413


def _require_stats():
    """页面共用：取当前统计；没有则记一条日志并重定向回首页"""
    stats = store._current_stats()
    if stats is None:
        logger.info("会话无统计数据, 重定向到首页")
        return None, redirect(url_for("index"))
    return stats, None


def dashboard():
    """总览仪表盘"""
    stats, redir = _require_stats()
    if redir:
        return redir
    return render_template(
        "dashboard.html",
        overview=stats["overview"],
        daily_counts=stats.get("daily_counts"),
        hourly_dist=stats.get("hourly_dist"),
        weekly_dist=stats.get("weekly_dist"),
        length_stats=stats.get("length_stats"),
        exchange_rounds=stats.get("exchange_rounds"),
        milestones=stats.get("milestones"),
        api_ok=is_api_configured(),
        chat_name=session.get("chat_name"),
    )


def emotion():
    """情绪分析页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    return render_template("emotion.html", api_ok=is_api_configured(), overview=stats["overview"])


def relationship():
    """人际关系页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    return render_template(
        "relationship.html",
        api_ok=is_api_configured(),
        overview=stats["overview"],
        response_time=stats.get("response_time"),
        exchange_rounds=stats.get("exchange_rounds"),
    )


def _face_assets(stats: dict, chat_hash: str) -> tuple[dict, dict, bool]:
    """收集本次聊天的表情 → (emoji 映射, 已缓存表情图映射, 表情图功能是否开启)。

    只读缓存/本地表情包，**不在页面渲染时联网**；联网抓取由用户在页面上显式触发。
    """
    from analyzer.face_emoji import emoji_map
    from analyzer import face_images as fi

    face_stats = stats.get("face_stats") or {}
    names = list((face_stats.get("self") or {}).keys()) + list((face_stats.get("other") or {}).keys())
    emojis = emoji_map(names)
    images: dict = {}
    if fi.enabled():
        filepath = session.get("filepath")
        if filepath and os.path.exists(filepath):
            try:
                chat = store._load_chat_cached(filepath)
                faces = fi.collect_cached(chat, chat_hash)
                have = fi.ensure(faces, allow_network=False)  # 只用已有缓存
                images = fi.url_map(have)
            except Exception as e:  # 表情图是锦上添花，失败不能拖垮页面
                logger.warning("表情图映射失败（回退 emoji）: %s", e)
    return emojis, images, fi.enabled()


def habits():
    """个人习惯页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    chat_hash = session.get("chat_hash", "")
    stats = store._stats_with_word_freq(stats, chat_hash)
    emojis, images, faces_on = _face_assets(stats, chat_hash)
    return render_template(
        "habits.html",
        api_ok=is_api_configured(),
        overview=stats["overview"],
        face_stats=stats.get("face_stats") or {},
        face_emoji=emojis,
        face_images=images,
        face_images_enabled=faces_on,
        length_stats=stats.get("length_stats"),
        weekly_activity=stats.get("weekly_activity"),
        word_freq=stats.get("word_freq"),
    )


def topics():
    """话题趋势页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    return render_template("topics.html", api_ok=is_api_configured(), overview=stats["overview"])


def profile():
    """AI 人物锐评页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    return render_template("profile.html", api_ok=is_api_configured(), overview=stats["overview"])


def report():
    """全篇报告导出页"""
    stats, redir = _require_stats()
    if redir:
        return redir
    stats = store._stats_with_word_freq(stats, session.get("chat_hash", ""))
    emojis, images, _faces_on = _face_assets(stats, session.get("chat_hash", ""))
    return render_template(
        "report.html",
        api_ok=is_api_configured(),
        overview=stats["overview"],
        daily_counts=stats.get("daily_counts"),
        hourly_dist=stats.get("hourly_dist"),
        weekly_dist=stats.get("weekly_dist"),
        length_stats=stats.get("length_stats"),
        face_stats=stats.get("face_stats") or {},
        face_emoji=emojis,
        face_images=images,
        response_time=stats.get("response_time"),
        exchange_rounds=stats.get("exchange_rounds"),
        weekly_activity=stats.get("weekly_activity"),
        word_freq=stats.get("word_freq"),
    )


def face_image(key: str):
    """提供缓存里的表情原图（键是受控格式，路径穿越无从谈起）"""
    from analyzer import face_images

    path = face_images.serve_path(key)
    if not path:
        return "未缓存该表情图", 404
    resp = send_file(path, max_age=30 * 86400)
    resp.headers["Cache-Control"] = "public, max-age=2592000"
    return resp


def register(app):
    """页面路由注册（端点名与旧 app.py 完全一致，url_for/模板比较不受影响）"""
    app.before_request(log_request)
    app.after_request(log_response)
    app.context_processor(inject_stats_flag)
    app.register_error_handler(413, upload_too_large)
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/upload", "upload", upload, methods=["POST"])
    app.add_url_rule("/dashboard", "dashboard", dashboard)
    app.add_url_rule("/emotion", "emotion", emotion)
    app.add_url_rule("/relationship", "relationship", relationship)
    app.add_url_rule("/habits", "habits", habits)
    app.add_url_rule("/topics", "topics", topics)
    app.add_url_rule("/profile", "profile", profile)
    app.add_url_rule("/report", "report", report)
    app.add_url_rule("/face/<key>", "face_image", face_image)
