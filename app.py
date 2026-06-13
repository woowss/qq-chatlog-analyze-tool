# Copyright (C) 2026 Zhang Yangming. All Rights Reserved.
# Licensed under MIT License.
# SPDX-License-Identifier: MIT
#
# QQ 聊天记录分析工具 — Flask 主应用
# ========================================

import os
import uuid
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix

from config import UPLOAD_FOLDER, SECRET_KEY
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
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# 服务端文件系统 session (避免 cookie 大小限制)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.path.join(Path(__file__).parent, "flask_session")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)


# ---------------------------------------------------------------------------
# 请求日志中间件
# ---------------------------------------------------------------------------


@app.before_request
def log_request():
    """记录每个请求的方法/路径/来源IP"""
    from flask import request as req
    ip = req.remote_addr or "127.0.0.1"
    logger.info("%s %s [%s]", req.method, req.path, ip)


@app.after_request
def log_response(response):
    """记录响应状态码 (只记非成功状态)"""
    if response.status_code >= 400:
        logger.warning("--> %s %s", response.status_code, request.path)
    return response


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
    if "file" not in request.files:
        logger.warning("上传请求中没有 file 字段")
        return "请选择文件", 400

    file = request.files["file"]
    orig_name = file.filename
    if orig_name == "" or not orig_name.endswith(".json"):
        logger.warning("上传文件格式无效: %s", orig_name)
        return "请选择有效的 .json 文件", 400

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
    _p("  日志文件: logs/app.log (自动轮转, 保留 5x5MB)")
    _p(sep)

    logger.info("=" * 40)
    logger.info("应用启动 - http://localhost:5000")
    logger.info("API Key: %s", "已配置" if is_api_configured() else "未配置")
    logger.info("=" * 40)

    app.run(debug=True, host="127.0.0.1", port=5000)
