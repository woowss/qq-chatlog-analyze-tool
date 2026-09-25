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

import config
from config import LOG_REDACT_NAMES, MAX_CONTENT_LENGTH, UPLOAD_FOLDER
from analyzer.deepseek_client import is_api_configured
from analyzer.logger import get_logger, mask_name
from webapp import store
from webapp.security import BYPASS_ENDPOINTS, _guard_post, wants_json
from web import ASSET_VERSION

logger = get_logger("app")


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


def inject_stats_flag():
    """导航栏需要的全局量在这里统一注入。

    - has_stats：当前是否有数据（统计已移出 session，不能在模板里直接判断）
    - api_ok：API Key 是否配好。原先由 7 个视图各自调一遍 is_api_configured()
      再传进模板，值完全相同；新增页面时漏传就会显示成"未配置 API Key"，
      属于看得见却想不到去查的误导。
    - asset_v：自有 JS/CSS 的版本号，模板用它破浏览器缓存（详见 web/__init__.py）
    """
    # is_group_chat：导航栏按模式切换文案与链接。取自 session（上传时写好的解析口径），
    # 不在这里重新解析文件——那意味着每个请求都要把几十 MB 的导出再读一遍。
    is_group = session.get("chat_mode") == "group"
    if "overview" in session:  # 兼容旧会话
        return {
            "overview": session["overview"],
            "has_stats": True,
            "api_ok": is_api_configured(),
            "asset_v": ASSET_VERSION,
            "is_group_chat": is_group,
        }
    return {
        "has_stats": bool(session.get("chat_hash")),
        "api_ok": is_api_configured(),
        "asset_v": ASSET_VERSION,
        "is_group_chat": is_group,
    }


def index():
    """首页 / 上传页面

    stats_error：上次上传的统计若在后台线程里失败，这里如实告诉用户
    （异步化之后没有 HTTP 响应能承载它，不说的话用户只会看到"上传成功却回首页"）。
    """
    return render_template("index.html", stats_error=store.stats_error(session.get("chat_hash", "")))


def health():
    """存活探针：只回一行 ok，不碰会话 / 日志 / 登录 / 清理链路。

    给反向代理与容器编排用。三条约束缺一不可：
    - 不能走 require_login：设了口令时探针会被 302 到登录页，永远判为不健康；
    - 不能建会话：探针可能每秒一次，每次都写一个 session 文件纯属浪费磁盘；
    - 不能写日志、不能触发清理：同上，高频调用会把日志刷爆。
    具体豁免见 webapp.security.BYPASS_ENDPOINTS。
    """
    return "ok", 200


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
    # 本次解析用的口径（private/group/two_party）：页面与统计缓存都靠它判断走哪条轨，
    # 不能在请求里重新判定——那意味着每个请求都重新解析一次大文件。
    session["chat_mode"] = chat.mode
    if old_hash and old_hash != new_hash:
        logger.info("同一会话上传了新文件，旧文件（%s…）的派生缓存已清理", old_hash[:8])

    # 本地统计：内容相同的文件直接复用上次结果（统计是确定性的，重算纯属浪费）
    if store._load_stats(new_hash, expect_mode=store.stats_mode_of(chat)) is None:
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
    """AJAX 上传（带选中的导出目录）走 JSON 响应，普通表单提交仍走 302 跳转。

    判定口径与"会话过期时该回 401 还是 302"共用 security.wants_json()，
    两处必须一致：否则会出现"上传接口认为自己在回 JSON、而登录守卫却给它 302"。
    """
    return wants_json()


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


def _is_group() -> bool:
    """当前会话是不是群聊记录（口径来自上传时写入的 chat_mode）"""
    return session.get("chat_mode") == "group"


def _group_ai_plan(stats: dict) -> dict:
    """群聊 AI 全量的调用计划：给用户一个"点下去要花多少次调用"的明确预期。

    群聊维度的成本结构与私聊不同：3 个群级维度**按月份**计费，成员画像**按人数**计费，
    所以只报"4 个维度"是不诚实的（例如 6 个月、10 位成员的群，实际是 18 + 10 = 28 次调用）。
    """
    # 月份数 = 出现过的 "YYYY-MM" 个数。原先写成一串 and/or 短路求值，
    # 依赖"空列表为假"来兜 0，读的人得在脑子里跑一遍才知道结果是不是数字。
    daily = stats.get("daily_counts") or []
    months = len({d["date"][:7] for d in daily}) if daily else 0
    members = min(
        stats["overview"].get("member_count", 0),
        int(getattr(config, "GROUP_AI_MAX_MEMBERS", 10) or 10),
    )
    return {"months": months, "members": members, "calls": months * 3 + members}


def _group_context(stats: dict) -> dict:
    """群聊页面共用的模板上下文。

    只放页面真正要用的键：群聊没有"对方"，因此不放 other_name 之类会让模板误用的字段。
    """
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
    """按当前记录类型渲染页面：群聊走群模板与群上下文，私聊走私模板与页面自己的上下文。

    这七个页面原先各自抄一遍"取统计 → 拿不到就跳首页 → 判断群聊 → 选模板"。抄漏一处
    的后果不是报错，而是**群聊记录渲染了私聊模板**：页面看起来完全正常，数字的含义
    却全错了（群聊里没有"对方"这个人）。收成一份之后，新增页面不可能漏掉这个分支。

    `private_context` 收的是**函数**而不是 dict：它可能很贵（词频要跑 jieba、
    表情原图要读缓存），而群聊页面根本用不到它——先算好再丢掉纯属浪费。
    """
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
    app.add_url_rule("/health", "health", health)
