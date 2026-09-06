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

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_session import Session

from config import (
    ACCESS_PASSWORD,
    AI_CACHE_DIR,
    DEEPSEEK_MODEL,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    MAX_CONTENT_LENGTH,
    SECRET_KEY,
    SESSION_FILE_DIR,
    UPLOAD_FOLDER,
)
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
    calc_milestones,
)
from analyzer.usage import get_usage
from analyzer.deepseek_client import (
    QuotaExhaustedError,
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
os.makedirs(AI_CACHE_DIR, exist_ok=True)


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
    """校验浏览器 Origin 来源（存在时）。拒绝非本机/非本站来源，防御跨站提交/DNS rebinding。

    curl 等非浏览器请求不带 Origin，交由 CSRF token 校验兜底。
    允许：回环地址、当前绑定地址 FLASK_HOST、以及请求 Host 本身（局域网 IP 访问场景）。
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    request_host = (request.host or "").split(":")[0].lower()
    allowed = {"localhost", "127.0.0.1", FLASK_HOST.lower(), request_host}
    return host in allowed


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
# 可选访问口令（设置 ACCESS_PASSWORD 后生效；绑定非回环地址时强制要求）
# ---------------------------------------------------------------------------

PUBLIC_ENDPOINTS = {"login", "static"}


@app.before_request
def require_login():
    """未登录时重定向到登录页；未设置口令则不启用"""
    if not ACCESS_PASSWORD:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS or session.get("auth_ok"):
        return None
    return redirect(url_for("login", next=request.path or "/"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """口令登录页"""
    if not ACCESS_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        # 登录接口自身豁免 CSRF（无 session 时先建 token）
        pwd = request.form.get("password", "")
        if hmac.compare_digest(pwd.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
            session["auth_ok"] = True
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):  # 防开放重定向
                nxt = url_for("index")
            logger.info("登录成功 [%s]", request.remote_addr)
            return redirect(nxt)
        error = "口令错误，请重试"
        logger.warning("登录失败 [%s]", request.remote_addr)
    return render_template("login.html", error=error)


# ---------------------------------------------------------------------------
# 临时文件清理（uploads/ 与 flask_session/ 只增不减，长期运行会占用磁盘）
# ---------------------------------------------------------------------------


def _cleanup_old_files(max_age_seconds: int = 86400):
    """删除超过 max_age_seconds 的临时文件（含 AI 缓存——缓存内容源自聊天记录，属敏感数据）"""
    now = time.time()
    cleaned = 0
    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR, AI_CACHE_DIR):
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


# 模块导入时即清理（覆盖 python app.py 与 flask --app app run 两种启动方式）
_cleanup_old_files(max_age_seconds=86400)


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
        # 解析失败的孤儿文件立即删除，不留到 24h 过期清理
        try:
            os.remove(filepath)
        except OSError:
            pass
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
    session["milestones"] = calc_milestones(chat)
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
        milestones=session.get("milestones"),
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
# AI 分析 API：服务端结果缓存 + 异步任务（进度/取消）
# ---------------------------------------------------------------------------

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

# 内存任务表：job_id -> 状态字典。结果落盘缓存后，任务记录仅供前端轮询，
# 服务重启丢失无妨（重跑会命中磁盘缓存）。
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600


def _prune_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        for jid in [k for k, v in JOBS.items() if v.get("finished_at", v.get("created", 0)) < cutoff]:
            JOBS.pop(jid, None)


def _chat_hash(filepath: str) -> str:
    """聊天文件内容哈希（前 16 位），作为缓存键的一部分"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cache_path(dimension: str, chat_hash: str) -> str:
    return os.path.join(AI_CACHE_DIR, f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}.json")


def _read_cache(dimension: str, chat_hash: str):
    try:
        with open(_cache_path(dimension, chat_hash), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(dimension: str, chat_hash: str, result) -> None:
    try:
        with open(_cache_path(dimension, chat_hash), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("AI 缓存写入失败: %s", e)


def _run_job(job_id: str, dimension: str, filepath: str, chat_hash: str) -> None:
    """后台线程执行分析：更新进度、支持取消、成功后写磁盘缓存"""
    dim_name = DIMENSION_NAMES.get(dimension, dimension)
    try:
        chat = load_chat(filepath)
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
        result = func(chat, on_progress=on_progress, should_cancel=should_cancel)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if j.get("cancel"):
                j.update(status="cancelled", finished_at=time.time())
            elif not result:
                j.update(status="error",
                         error="分析未产生结果：可能全部月份失败，请查看日志",
                         finished_at=time.time())
            else:
                j.update(status="done", result=result, finished_at=time.time())
        if result and not should_cancel():
            _write_cache(dimension, chat_hash, result)
            logger.info("%s 完成（任务 %s）", dim_name, job_id[:8])
    except Exception as e:
        logger.error("%s 失败: %s", dim_name, e)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j.update(status="error", error=f"AI 分析失败: {e}", finished_at=time.time())


def _session_chat_file():
    """返回当前 session 可用的聊天文件路径，或 (错误消息, 状态码)"""
    if "filepath" not in session:
        return None, ("请先上传聊天记录", 400)
    filepath = session["filepath"]
    if not os.path.exists(filepath):
        return None, ("会话文件已过期, 请重新上传", 400)
    return filepath, None


@app.route("/api/analyze/<dimension>", methods=["POST"])
def api_analyze(dimension: str):
    """发起维度分析：命中缓存直接返回，否则启动后台任务并返回 job id"""
    guard = _guard_post()
    if guard:
        return jsonify({"error": guard[0]}), guard[1]

    if dimension not in ANALYZE_FUNCS:
        return jsonify({"error": f"未知维度: {dimension}"}), 400

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置, 请编辑 .env 文件"}), 400

    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]

    chat_hash = _chat_hash(filepath)

    # 缓存命中（除非显式 refresh=1 强制重跑）
    if request.args.get("refresh") != "1":
        cached = _read_cache(dimension, chat_hash)
        if cached is not None:
            logger.info("%s 命中缓存，直接返回", DIMENSION_NAMES.get(dimension, dimension))
            return jsonify({"cached": True, "result": cached})

    _prune_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "dim": dimension, "done": 0, "total": 0,
            "cancel": False, "chat_hash": chat_hash, "sid": session.sid,
            "created": time.time(),
        }
    threading.Thread(target=_run_job, args=(job_id, dimension, filepath, chat_hash),
                     daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/api/analyze-job/<job_id>")
def api_analyze_job(job_id: str):
    """查询后台任务进度；完成时附带结果"""
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


@app.route("/api/analyze-job/<job_id>/cancel", methods=["POST"])
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


@app.route("/api/analysis/<dimension>")
def api_analysis_result(dimension: str):
    """读取已缓存的分析结果（页面加载时优先于 sessionStorage 使用）"""
    if dimension not in ANALYZE_FUNCS:
        return jsonify({"error": f"未知维度: {dimension}"}), 400
    filepath, err = _session_chat_file()
    if err:
        return jsonify({"error": err[0]}), err[1]
    cached = _read_cache(dimension, _chat_hash(filepath))
    if cached is None:
        return jsonify({"error": "暂无该维度的分析结果"}), 404
    return jsonify({"cached": True, "result": cached})


def _run_analyze_all(job_id: str, filepath: str, chat_hash: str, refresh: bool) -> None:
    """一键全量分析：按维度顺序执行（维度内部已有月份级并发），
    已缓存的维度直接跳过（refresh 时强制重跑），单维度失败不阻断其余维度。"""
    try:
        chat = load_chat(filepath)
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
            if not refresh and _read_cache(dim, chat_hash) is not None:
                summary[dim] = "cached"
            else:
                def on_inner(done: int, tot: int, _dim=dim_name, _idx=idx):
                    with JOBS_LOCK:
                        j = JOBS.get(job_id)
                        if j:
                            j["detail"] = f"{_idx}/{total} {_dim}（{_done_str(done, tot)}）"
                try:
                    result = ANALYZE_FUNCS[dim](chat, on_progress=on_inner,
                                                should_cancel=should_cancel)
                    if result:
                        _write_cache(dim, chat_hash, result)
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


def _done_str(done: int, total: int) -> str:
    return "准备中" if total == 0 else f"{done}/{total} 月"


@app.route("/api/analyze-all", methods=["POST"])
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

    chat_hash = _chat_hash(filepath)
    refresh = request.args.get("refresh") == "1"

    _prune_jobs()
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "dim": "all", "done": 0, "total": len(ANALYZE_FUNCS),
            "detail": "", "cancel": False, "chat_hash": chat_hash, "sid": session.sid,
            "created": time.time(),
        }
    threading.Thread(target=_run_analyze_all,
                     args=(job_id, filepath, chat_hash, refresh), daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/api/usage")
def api_usage():
    """LLM token 用量统计（按天 × 维度聚合，仅数字无聊天内容）"""
    return jsonify(get_usage())


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
    _p(f"  访问地址: http://{FLASK_HOST}:{FLASK_PORT}")
    _p(sep)
    if not is_api_configured():
        _p("  [WARN] DeepSeek API Key 未配置")
        _p("  请编辑项目根目录的 .env 文件填入 Key")
    else:
        _p("  [OK] DeepSeek API 已配置")

    loopback = FLASK_HOST in ("127.0.0.1", "localhost", "::1")
    if not loopback and not ACCESS_PASSWORD:
        _p("  [ERROR] 绑定到非回环地址必须设置 ACCESS_PASSWORD（见 .env.example）")
        _p("  已拒绝启动，以免聊天记录与 AI 结果被局域网内陌生人访问")
        sys.exit(1)
    if ACCESS_PASSWORD:
        _p("  [OK] 访问口令已启用")
    if FLASK_DEBUG:
        _p("  [WARN] 调试模式已开启（调试器可执行任意代码，仅限本机开发）")

    _p(sep)
    _p("  日志文件: logs/app.log (自动轮转, 保留 5x5MB)")
    _p("  uploads/ flask_session/ ai_cache/ 中超 24h 的旧文件启动时自动清理")
    _p(sep)

    logger.info("=" * 40)
    logger.info("应用启动 - http://%s:%d", FLASK_HOST, FLASK_PORT)
    logger.info("API Key: %s", "已配置" if is_api_configured() else "未配置")
    logger.info("调试模式: %s", FLASK_DEBUG)
    logger.info("=" * 40)

    try:
        app.run(debug=FLASK_DEBUG, host=FLASK_HOST, port=FLASK_PORT)
    except OSError as e:
        _p(f"  [ERROR] 启动失败: {e}")
        _p(f"  端口 {FLASK_PORT} 可能被占用（Windows 上 5000 常被 AirPlay/Hyper-V 占用），")
        _p("  可在 .env 中设置 FLASK_PORT=5001 换一个端口")
        sys.exit(1)
