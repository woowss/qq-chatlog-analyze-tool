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
# QQ 聊天记录分析工具 — Flask 主应用
# ========================================

import os
import secrets
import time
import uuid
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_session import Session

from config import FLASK_DEBUG, MAX_CONTENT_LENGTH, SECRET_KEY, SESSION_FILE_DIR, UPLOAD_FOLDER
from parser.qq_parser import load_chat
from analyzer.local_stats import (
    calc_overview,
    calc_daily_counts,
    calc_hourly_distribution,
    calc_weekly_distribution,
    calc_message_length_stats,
    calc_face_stats,
    calc_response_time,
    calc_exchange_rounds,
    calc_weekly_activity,
    calc_word_freq,
)
from analyzer.deepseek_client import (
    analyze_emotion,
    analyze_topics,
    analyze_relationship,
    analyze_habits,
    analyze_profile,
    is_api_configured,
)
from analyzer.logger import get_logger

logger = get_logger("app")

# ---------------------------------------------------------------------------
# Flask 应用初始化
# ---------------------------------------------------------------------------

app = Flask(__name__,
            template_folder="web/templates",
            static_folder="web/static",
            static_url_path="/static")
app.secret_key = SECRET_KEY
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# 服务端文件系统 session (避免 cookie 大小限制)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = SESSION_FILE_DIR
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SESSION_FILE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 请求日志中间件
# ---------------------------------------------------------------------------


@app.before_request
def log_request():
    """记录每个请求的方法/路径/来源IP"""
    ip = request.remote_addr or "127.0.0.1"
    logger.info("%s %s [%s]", request.method, request.path, ip)


@app.after_request
def log_response(response):
    """记录响应状态码 (只记非成功状态)"""
    if response.status_code >= 400:
        logger.warning("--> %s %s", response.status_code, request.path)
    return response


# ---------------------------------------------------------------------------
# CSRF / 来源校验
# ---------------------------------------------------------------------------


@app.before_request
def ensure_csrf_token():
    """确保会话中存在 CSRF token（session 服务端存储，攻击者无法读取）"""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


@app.context_processor
def inject_csrf_token():
    """向所有模板注入 csrf_token，供表单与 AJAX 请求使用"""
    return {"csrf_token": session.get("csrf_token", "")}


def _check_csrf() -> bool:
    """校验请求携带的 CSRF token 与会话一致"""
    provided = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "")
    expected = session.get("csrf_token", "")
    return bool(expected) and secrets.compare_digest(expected, provided)


def _origin_allowed() -> bool:
    """校验浏览器 Origin 来源（存在时）。拒绝非本机来源，防御跨站提交/DNS rebinding。

    curl 等非浏览器请求不带 Origin，交由 CSRF token 校验兜底。
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1")


def _guard_post():
    """POST 请求统一防护：Origin 校验 + CSRF token 校验"""
    if not _origin_allowed():
        logger.warning("拦截非本机来源请求: %s", request.headers.get("Origin"))
        return "非法来源", 403
    if not _check_csrf():
        logger.warning("CSRF 校验失败: %s %s", request.method, request.path)
        return "CSRF 校验失败", 400
    return None


# ---------------------------------------------------------------------------
# 临时文件清理（uploads/ 与 flask_session/ 只增不减，长期运行会占用磁盘）
# ---------------------------------------------------------------------------


def _cleanup_old_files(max_age_seconds: int = 86400):
    """删除超过 max_age_seconds 的临时文件"""
    now = time.time()
    cleaned = 0
    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR):
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
                    cleaned += 1
            except OSError:
                continue
    if cleaned:
        logger.info("已清理 %d 个过期临时文件", cleaned)


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """首页 / 上传页面"""
    return render_template("index.html", api_ok=is_api_configured())


@app.route("/upload", methods=["POST"])
def upload():
    """接收上传的 JSON 文件, 解析并存入 session"""
    guard = _guard_post()
    if guard:
        return guard

    if "file" not in request.files:
        logger.warning("上传请求中没有 file 字段")
        return "请选择文件", 400

    file = request.files["file"]
    orig_name = file.filename
    if orig_name == "" or not orig_name.endswith(".json"):
        logger.warning("上传文件格式无效: %s", orig_name)
        return "请选择有效的 .json 文件", 400

    # 删除上一次会话遗留的上传文件，避免孤儿文件堆积
    old_path = session.get("filepath")
    if old_path and os.path.exists(old_path) and os.path.dirname(old_path) == UPLOAD_FOLDER:
        try:
            os.remove(old_path)
        except OSError:
            pass

    filename = f"{uuid.uuid4().hex}.json"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    logger.info("文件已保存: %s (来自 %s, %d bytes)",
                filename, orig_name, os.path.getsize(filepath))

    try:
        chat = load_chat(filepath)
        logger.info("解析成功: %s <-> %s, %d 条消息, %d 天",
                    chat.self_name, chat.other_name,
                    len(chat.messages), chat.duration_days)
    except Exception as e:
        logger.error("解析失败: %s", e)
        return f"解析失败: {e}", 400

    # 存入 session
    session["chat_name"] = chat.chat_name
    session["self_name"] = chat.self_name
    session["other_name"] = chat.other_name
    session["filepath"] = filepath

    # 缓存本地统计结果
    logger.info("开始计算本地统计...")
    session["overview"] = calc_overview(chat)
    session["daily_counts"] = calc_daily_counts(chat)
    session["hourly_dist"] = calc_hourly_distribution(chat)
    session["weekly_dist"] = calc_weekly_distribution(chat)
    session["length_stats"] = calc_message_length_stats(chat)
    session["face_stats"] = calc_face_stats(chat)
    session["response_time"] = calc_response_time(chat)
    session["exchange_rounds"] = calc_exchange_rounds(chat)
    session["weekly_activity"] = calc_weekly_activity(chat)
    session["word_freq"] = calc_word_freq(chat, top_n=80)
    session["total_messages"] = len(chat.messages)
    logger.info("本地统计完成, 共 %d 项数据已缓存", len(session) - 4)

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """总览仪表盘"""
    if "overview" not in session:
        logger.info("session 无数据, 重定向到首页")
        return redirect(url_for("index"))
    return render_template(
        "dashboard.html",
        overview=session["overview"],
        daily_counts=session.get("daily_counts"),
        hourly_dist=session.get("hourly_dist"),
        weekly_dist=session.get("weekly_dist"),
        length_stats=session.get("length_stats"),
        exchange_rounds=session.get("exchange_rounds"),
        api_ok=is_api_configured(),
        chat_name=session.get("chat_name"),
    )


@app.route("/emotion")
def emotion():
    """情绪分析页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("emotion.html", api_ok=is_api_configured(),
                           overview=session["overview"])


@app.route("/relationship")
def relationship():
    """人际关系页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("relationship.html", api_ok=is_api_configured(),
                           overview=session["overview"],
                           response_time=session.get("response_time"),
                           exchange_rounds=session.get("exchange_rounds"))


@app.route("/habits")
def habits():
    """个人习惯页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("habits.html", api_ok=is_api_configured(),
                           overview=session["overview"],
                           face_stats=session.get("face_stats"),
                           length_stats=session.get("length_stats"),
                           weekly_activity=session.get("weekly_activity"),
                           word_freq=session.get("word_freq"))


@app.route("/topics")
def topics():
    """话题趋势页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("topics.html", api_ok=is_api_configured(),
                           overview=session["overview"])


@app.route("/profile")
def profile():
    """AI 人物锐评页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("profile.html", api_ok=is_api_configured(),
                           overview=session["overview"])


@app.route("/report")
def report():
    """全篇报告导出页"""
    if "overview" not in session:
        return redirect(url_for("index"))
    return render_template("report.html", api_ok=is_api_configured(),
                           overview=session["overview"],
                           daily_counts=session.get("daily_counts"),
                           hourly_dist=session.get("hourly_dist"),
                           weekly_dist=session.get("weekly_dist"),
                           length_stats=session.get("length_stats"),
                           face_stats=session.get("face_stats"),
                           response_time=session.get("response_time"),
                           exchange_rounds=session.get("exchange_rounds"),
                           weekly_activity=session.get("weekly_activity"),
                           word_freq=session.get("word_freq"))


# ---------------------------------------------------------------------------
# AI 分析 API
# ---------------------------------------------------------------------------


DIMENSION_NAMES = {
    "emotion": "情绪分析",
    "topics": "话题趋势",
    "relationship": "人际关系",
    "habits": "个人习惯",
    "profile": "人物锐评",
}


@app.route("/api/analyze/<dimension>", methods=["POST"])
def api_analyze(dimension: str):
    """调用 DeepSeek 分析指定维度"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    dim_name = DIMENSION_NAMES.get(dimension, dimension)

    if "filepath" not in session:
        logger.warning("AI 分析请求但 session 无文件, dim=%s", dim_name)
        return jsonify({"error": "请先上传聊天记录"}), 400

    if not is_api_configured():
        logger.warning("AI 分析请求但 API Key 未配置, dim=%s", dim_name)
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath = session["filepath"]
    if not os.path.exists(filepath):
        logger.error("session 文件已丢失: %s", filepath)
        return jsonify({"error": "会话文件已过期, 请重新上传"}), 400

    try:
        chat = load_chat(filepath)
    except Exception as e:
        logger.error("重载聊天记录失败: %s", e)
        return jsonify({"error": str(e)}), 500

    func_map = {
        "emotion": analyze_emotion,
        "topics": analyze_topics,
        "relationship": analyze_relationship,
        "habits": analyze_habits,
        "profile": analyze_profile,
    }

    if dimension not in func_map:
        logger.warning("未知分析维度: %s", dimension)
        return jsonify({"error": f"未知维度: {dimension}"}), 400

    logger.info("开始 %s ... (%d 条消息, 共 %d 个月)",
                dim_name, chat.total_count,
                len(set(m.time_str[:7] for m in chat.messages)))

    try:
        result = func_map[dimension](chat)
        if not result:
            logger.error("%s 无任何结果", dim_name)
            return jsonify({"error": "分析未产生结果：可能单月消息过多超出模型上下文或 API 报错，请查看日志排查"}), 500
        month_count = len(result) if isinstance(result, dict) else "?"
        logger.info("%s 完成, 返回 %s 个月的数据", dim_name, month_count)
        return jsonify(result)
    except Exception as e:
        logger.error("%s 失败: %s", dim_name, e)
        return jsonify({"error": f"AI 分析失败: {e}"}), 500


@app.route("/api/status")
def api_status():
    """API 配置状态"""
    status = is_api_configured()
    logger.debug("API 状态查询: %s", "已配置" if status else "未配置")
    return jsonify({"api_ok": status})


if __name__ == "__main__":
    import sys
    enc = sys.stdout.encoding or "utf-8"

    def _p(msg: str):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode(enc, errors="replace").decode(enc))

    sep = "=" * 50
    _p(sep)
    _p("  QQ 聊天记录分析工具")
    _p("  访问地址: http://localhost:5000")
    _p(sep)
    if not is_api_configured():
        _p("  [WARN] DeepSeek API Key 未配置")
        _p("  请编辑项目根目录的 .env 文件填入 Key")
    else:
        _p("  [OK] DeepSeek API 已配置")
    _p(sep)
    _p("  调试模式: %s" % ("开" if FLASK_DEBUG else "关"))
    _p("  日志文件: logs/app.log (自动轮转, 保留 5x5MB)")
    _p(sep)

    # 启动时清理超过 24 小时的上传文件与 session 文件
    _cleanup_old_files(max_age_seconds=86400)

    logger.info("=" * 40)
    logger.info("应用启动 - http://localhost:5000")
    logger.info("API Key: %s", "已配置" if is_api_configured() else "未配置")
    logger.info("调试模式: %s", FLASK_DEBUG)
    logger.info("=" * 40)

    app.run(debug=FLASK_DEBUG, host="127.0.0.1", port=5000)
