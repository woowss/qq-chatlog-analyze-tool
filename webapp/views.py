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
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, send_file, session, url_for

import config
from config import LOG_REDACT_NAMES, MAX_CONTENT_LENGTH, UPLOAD_FOLDER
from analyzer.deepseek_client import is_api_configured
from analyzer.logger import get_logger, mask_name
from webapp import job_stream
from webapp import store
from webapp.security import BYPASS_ENDPOINTS, _guard_post, wants_json
from web import ASSET_VERSION

logger = get_logger("app")


def _analysis_page_identity():
    """Return a trustworthy identity for this rendered document without parsing chat data."""
    chat_hash = session.get("chat_hash")
    if not isinstance(chat_hash, str) or not chat_hash:
        return None
    scope = session.get("analysis_scope", {})
    if scope is None:  # normalize_scope(None, chat) is the valid full-chat scope.
        scope = {}
    if not isinstance(scope, dict):
        return None
    expected = {"date_from", "date_to", "target_uids", "allowed_uids", "message_positions"}
    if set(scope) - expected:
        return None
    # Older signed sessions may contain a sparse but otherwise canonical scope.
    # Fill fields the API normalizer always supplies, without opening/reparsing the chat.
    canonical_scope = {
        "date_from": scope.get("date_from", ""),
        "date_to": scope.get("date_to", ""),
        "target_uids": scope.get("target_uids"),
        "allowed_uids": scope.get("allowed_uids"),
        "message_positions": scope.get("message_positions"),
    }
    date_from, date_to = canonical_scope["date_from"], canonical_scope["date_to"]
    if date_from is None:
        date_from = canonical_scope["date_from"] = ""
    if date_to is None:
        date_to = canonical_scope["date_to"] = ""
    if not isinstance(date_from, str) or not isinstance(date_to, str):
        return None
    try:
        parsed_from = date.fromisoformat(date_from) if date_from else None
        parsed_to = date.fromisoformat(date_to) if date_to else None
    except ValueError:
        return None
    if (parsed_from and parsed_from.isoformat() != date_from) or (
        parsed_to and parsed_to.isoformat() != date_to
    ):
        return None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        return None
    for field in ("target_uids", "allowed_uids"):
        values = canonical_scope[field]
        if values is not None and (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
            or values != sorted(set(values))
        ):
            return None
    positions = canonical_scope["message_positions"]
    if positions is not None and (
        not isinstance(positions, list)
        or not positions
        or any(type(position) is not int or position < 0 for position in positions)
        or positions != sorted(set(positions))
    ):
        return None
    if not any(
        (
            date_from,
            date_to,
            canonical_scope["target_uids"],
            canonical_scope["allowed_uids"],
            canonical_scope["message_positions"],
        )
    ):
        canonical_scope = {}
    from webapp.analysis_scope import scope_identity

    return {"chat_hash": chat_hash, "scope_id": scope_identity(canonical_scope)}


def log_request():
    """记录每个请求的方法/路径/来源IP"""
    if request.path.startswith("/static/"):
        return  # 静态资源逐条记录只会淹没真正有用的日志
    if request.endpoint in BYPASS_ENDPOINTS:
        return  # 存活探针可能每秒被调一次，逐条记日志只会把日志刷爆
    ip = request.remote_addr or "127.0.0.1"
    logger.info("%s %s [%s]", request.method, request.path, ip)


def log_response(response):
    """记录响应状态码 (只记非成功状态)"""
    if response.status_code >= 400:
        logger.warning("--> %s %s", response.status_code, request.path)
    return response


def track_active_chat():
    """每次非豁免请求刷新当前会话对 chat_hash 的活跃引用（TTL 同 store.LIVE_CHAT_REF_TTL）。"""
    if request.endpoint in BYPASS_ENDPOINTS:
        return None
    chat_hash = session.get("chat_hash")
    if not chat_hash:
        return None
    sid = getattr(session, "sid", "") or ""
    store.note_live_chat(chat_hash, sid)
    return None


def inject_stats_flag():
    """向模板全局注入导航状态（has_stats、api_ok、asset_v、is_group_chat、analysis_page_identity）。"""
    is_group = session.get("chat_mode") == "group"
    analysis_page_identity = _analysis_page_identity()
    if "overview" in session:  # 兼容旧会话
        return {
            "overview": session["overview"],
            "has_stats": True,
            "api_ok": is_api_configured(),
            "asset_v": ASSET_VERSION,
            "is_group_chat": is_group,
            "analysis_page_identity": analysis_page_identity,
        }
    return {
        "has_stats": bool(session.get("chat_hash")),
        "api_ok": is_api_configured(),
        "asset_v": ASSET_VERSION,
        "is_group_chat": is_group,
        "analysis_page_identity": analysis_page_identity,
    }


def index():
    """首页 / 上传页面，并透传后台统计失败原因。"""
    return render_template("index.html", stats_error=store.stats_error(session.get("chat_hash", "")))


def health():
    """存活探针：豁免登录、会话、日志与清理中间件（见 webapp.security.BYPASS_ENDPOINTS）。"""
    from analyzer.shutdown import shutdown_requested

    if shutdown_requested():
        return "shutting down", 503
    return "ok", 200


def upload():
    """接收上传的 JSON 文件，解析校验后写会话并触发后台本地统计。"""
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

    # 旧文件信息先记下，等新文件解析成功后再处置（解析失败不伤及当前会话）。
    # 哈希**取会话里那份**，不是重新读旧文件算：读盘算哈希的前提是"旧文件还在"，
    # 而 uploads/ 按 mtime 24 小时回收，会话却因为每个请求都重下 cookie 而一直续期。
    # 于是"标签页开着放了几天、回来再传另一份聊天"是常态路径：那时旧文件早被收走了，
    # old_hash 取不到 → 整段级联清理被跳过 → 上一条聊天的维度缓存（含聊天原句引用）、
    # 月份文件、图片摘要、统计结果一条都没删，而界面已经按"换文件即清理"对外承诺过
    # （README「重新上传时，旧文件及其派生缓存立即删除」）。会话里那份哈希就是上一轮
    # 自己算出来写进去的，读盘纯属多余的依赖。
    old_path = session.get("filepath")
    old_hash = session.get("chat_hash", "") or ""
    sid = getattr(session, "sid", "") or ""

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
        if chat.is_group_chat:
            # 群聊没有单一"对方"：日志按"群名 + 成员数"记，避免把群名当成某个人
            logger.info(
                "解析成功（群聊）: %s, %d 位成员, %d 条消息, %d 天",
                mask_name(chat.chat_name),
                len(chat.participants()),
                len(chat.messages),
                chat.duration_days,
            )
        else:
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

    # 换文件时释放当前会话引用；若已无其它活跃会话共用同一哈希，则联动清理其派生缓存（内容不变仅提示命中缓存）
    switching = bool(old_hash) and old_hash != new_hash
    if not switching and old_hash == new_hash:
        flash(
            "这份内容与当前会话是同一份聊天记录：统计与已付费的 AI 结果都会直接命中缓存，不会重复计费。",
            "info",
        )
    if switching:
        store.forget_live_chat(old_hash, sid)
        job_stream.purge_session(sid, old_hash)
        holders = store.other_live_sessions(old_hash, exclude_sid=sid)
        if holders:
            logger.info("旧聊天仍被其它 %d 个会话使用中，本次不清理其派生缓存", len(holders))
        else:
            store._purge_chat_caches(old_hash)
            logger.info("同一会话上传了新文件，旧文件（%s…）的派生缓存已清理", old_hash[:8])
    if old_path and old_path != filepath and os.path.exists(old_path):
        # 仅删除 uploads 目录内的托管副本，防止误删会话外的路径
        if os.path.dirname(old_path) == UPLOAD_FOLDER:
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
    # Message IDs and date/member choices belong to the uploaded chat identity.
    session.pop("analysis_scope", None)
    session["total_messages"] = len(chat.messages)
    # 缓存解析口径供后续请求分支判断，避免重复解析大文件
    session["chat_mode"] = chat.mode
    store.note_live_chat(new_hash, sid)
    # 无条件撤销清理标记（含统计缓存命中的路径），避免后续分析任务误判取消
    store._unmark_purged(new_hash)

    # 本地统计：内容相同的文件直接复用上次结果
    if store._load_stats(new_hash, expect_mode=store.stats_mode_of(chat)) is None:
        logger.info("开始计算本地统计（后台线程）...")
        store.start_stats_job(chat, new_hash)
    else:
        logger.info("本地统计命中缓存，跳过重算")

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
    """AJAX 请求走 JSON 响应（与 security.wants_json 共用同一判定口径）。"""
    return wants_json()


def upload_too_large(error):
    """413：超过 MAX_CONTENT_LENGTH 时返回可读中文限制与调整建议。"""
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


def _is_group() -> bool:
    """当前会话是不是群聊记录（口径来自上传时写入的 chat_mode）"""
    return session.get("chat_mode") == "group"


def _group_ai_plan(stats: dict) -> dict:
    """计算群聊全量分析预期调用数（3 个群维度按月、成员画像按人数）。"""
    daily = stats.get("daily_counts") or []
    months = len({d["date"][:7] for d in daily}) if daily else 0
    members = min(
        stats["overview"].get("member_count", 0),
        int(getattr(config, "GROUP_AI_MAX_MEMBERS", 10) or 10),
    )
    return {"months": months, "members": members, "calls": months * 3 + members}


def _group_context(stats: dict) -> dict:
    """群聊页面共用的模板上下文（不含私聊专用字段 other_name）。"""
    return {
        "overview": stats["overview"],
        "member_activity": stats.get("member_activity") or [],
        "interaction": stats.get("interaction") or {},
        "member_hourly": stats.get("member_hourly") or {},
        "daily_counts": stats.get("daily_counts"),
        "hourly_dist": stats.get("hourly_dist"),
        "weekly_dist": stats.get("weekly_dist"),
        "weekly_activity": stats.get("weekly_activity"),
        "length_stats": stats.get("length_stats"),
        "face_stats": stats.get("face_stats") or {},
        "milestones": stats.get("milestones") or {},
        "ai_plan": _group_ai_plan(stats),
        "chat_name": session.get("chat_name"),
    }


def _render_by_mode(group_template: str, private_template: str, private_context):
    """按 chat_mode 分发群/私聊模板；private_context 延迟求值，群聊路径不触发计算。"""
    stats, redir = _require_stats()
    if redir:
        return redir
    if _is_group():
        return render_template(group_template, **_group_context(stats))
    return render_template(private_template, **private_context(stats))


def dashboard():
    """总览仪表盘"""
    return _render_by_mode(
        "group_dashboard.html",
        "dashboard.html",
        lambda stats: {
            "overview": stats["overview"],
            "daily_counts": stats.get("daily_counts"),
            "hourly_dist": stats.get("hourly_dist"),
            "weekly_dist": stats.get("weekly_dist"),
            "length_stats": stats.get("length_stats"),
            "exchange_rounds": stats.get("exchange_rounds"),
            "milestones": stats.get("milestones"),
            "trends": stats.get("trends") or {},
            "chat_name": session.get("chat_name"),
            # 只在 two_party（旧逃生阀）下渲染一条如实提示；正常私聊时模板不会输出任何东西，
            # 因此私聊页面渲染结果逐字节不变（tests/test_group_foundation.py 有对照用例）。
            "chat_mode": session.get("chat_mode", "private"),
        },
    )


def emotion():
    """情绪分析页"""
    return _render_by_mode(
        "group_emotion.html",
        "emotion.html",
        lambda stats: {"overview": stats["overview"]},
    )


def recap():
    """整体总括页（私聊专用：群聊会话访问时回到群仪表盘，与维度守卫同口径）"""
    stats, redir = _require_stats()
    if redir:
        return redir
    if _is_group():
        return redirect(url_for("dashboard"))
    return render_template("recap.html", overview=stats["overview"])


def relationship():
    """人际关系页"""
    return _render_by_mode(
        "group_relations.html",
        "relationship.html",
        lambda stats: {
            "overview": stats["overview"],
            "response_time": stats.get("response_time"),
            "exchange_rounds": stats.get("exchange_rounds"),
        },
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
    """个人习惯页（群聊时是成员活跃页，不做表情原图映射：那一套是"我和对方"的两人对比视图）"""

    def _private(stats):
        chat_hash = session.get("chat_hash", "")
        stats = store._stats_with_word_freq(stats, chat_hash)
        emojis, images, faces_on = _face_assets(stats, chat_hash)
        return {
            "overview": stats["overview"],
            "face_stats": stats.get("face_stats") or {},
            "face_emoji": emojis,
            "face_images": images,
            "face_images_enabled": faces_on,
            "length_stats": stats.get("length_stats"),
            "weekly_activity": stats.get("weekly_activity"),
            "word_freq": stats.get("word_freq"),
        }

    return _render_by_mode("group_activity.html", "habits.html", _private)


def topics():
    """话题趋势页"""
    return _render_by_mode(
        "group_topics.html",
        "topics.html",
        lambda stats: {"overview": stats["overview"]},
    )


def profile():
    """AI 人物锐评页"""
    return _render_by_mode(
        "group_profiles.html",
        "profile.html",
        lambda stats: {"overview": stats["overview"]},
    )


def report():
    """全篇报告导出页"""

    def _private(stats):
        chat_hash = session.get("chat_hash", "")
        stats = store._stats_with_word_freq(stats, chat_hash)
        emojis, images, _faces_on = _face_assets(stats, chat_hash)
        return {
            "overview": stats["overview"],
            "daily_counts": stats.get("daily_counts"),
            "hourly_dist": stats.get("hourly_dist"),
            "weekly_dist": stats.get("weekly_dist"),
            "length_stats": stats.get("length_stats"),
            "face_stats": stats.get("face_stats") or {},
            "face_emoji": emojis,
            "face_images": images,
            "response_time": stats.get("response_time"),
            "exchange_rounds": stats.get("exchange_rounds"),
            "weekly_activity": stats.get("weekly_activity"),
            "word_freq": stats.get("word_freq"),
        }

    return _render_by_mode("group_report.html", "report.html", _private)


def report_share_builder():
    """Show the independent share-copy editor; the source report stays available."""
    if not session.get("chat_hash"):
        return redirect(url_for("index"))
    return render_template("share_report_builder.html")


def face_image(key: str):
    """提供缓存里的表情原图（键是受控格式，路径穿越无从谈起）"""
    from analyzer import face_images

    path = face_images.serve_path(key)
    if not path:
        return "未缓存该表情图", 404
    resp = send_file(path, max_age=30 * 86400)
    resp.headers["Cache-Control"] = "public, max-age=2592000"
    return resp


def messages_page():
    """原始消息浏览与搜索页（群聊/私聊共用一个模板：这里没有"对方"，只有发言人列表）。

    页面本身只渲染骨架与当前身份，真正的消息由前端按需拉 /api/messages（分页）。
    与其它页一致：无统计就回首页。
    """
    _, redir = _require_stats()
    if redir:
        return redir
    return render_template(
        "messages.html",
        chat_name=session.get("chat_name"),
        chat_mode=session.get("chat_mode", "private"),
        self_name=session.get("self_name"),
        other_name=session.get("other_name"),
    )


def register(app):
    """页面路由注册（端点名与旧 app.py 完全一致，url_for/模板比较不受影响）"""
    app.before_request(log_request)
    app.before_request(track_active_chat)
    app.after_request(log_response)
    app.context_processor(inject_stats_flag)
    app.register_error_handler(413, upload_too_large)
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/upload", "upload", upload, methods=["POST"])
    app.add_url_rule("/dashboard", "dashboard", dashboard)
    app.add_url_rule("/messages", "messages_page", messages_page)
    app.add_url_rule("/recap", "recap", recap)
    app.add_url_rule("/emotion", "emotion", emotion)
    app.add_url_rule("/relationship", "relationship", relationship)
    app.add_url_rule("/habits", "habits", habits)
    app.add_url_rule("/topics", "topics", topics)
    app.add_url_rule("/profile", "profile", profile)
    app.add_url_rule("/report", "report", report)
    app.add_url_rule("/report/share", "report_share", report_share_builder)
    app.add_url_rule("/face/<key>", "face_image", face_image)
    app.add_url_rule("/health", "health", health)
