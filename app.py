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
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_session import Session

from config import (
    ACCESS_PASSWORD,
    AI_CACHE_DIR,
    ALLOWED_ORIGINS,
    DEEPSEEK_MODEL,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    MAX_CONTENT_LENGTH,
    MONTH_CACHE_ENABLED,
    SECRET_KEY,
    SESSION_FILE_DIR,
    STATS_CACHE_DIR,
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
    CALL_MIN_INTERVAL,
    CONCURRENCY,
    MAX_TOKENS_BY_DIM,
    PROMPT_FINGERPRINT,
    QuotaExhaustedError,
    configure_month_cache,
    purge_month_cache,
    sweep_orphan_month_cache,
    thinking_budget_warnings,
    analyze_emotion,
    analyze_topics,
    analyze_relationship,
    analyze_habits,
    analyze_profile,
    is_api_configured,
    thinking_enabled,
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
app.config["SESSION_COOKIE_HTTPONLY"] = True          # 禁止 JS 读取会话 cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"         # 跨站请求不携带 cookie（CSRF 纵深防御）
Session(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SESSION_FILE_DIR, exist_ok=True)
os.makedirs(AI_CACHE_DIR, exist_ok=True)
os.makedirs(STATS_CACHE_DIR, exist_ok=True)

# 月份级缓存（增量分析）：把目录注入分析层，避免 analyzer 反向依赖本模块
configure_month_cache(AI_CACHE_DIR if MONTH_CACHE_ENABLED else "")


# ---------------------------------------------------------------------------
# 请求日志中间件
# ---------------------------------------------------------------------------


@app.before_request
def log_request():
    """记录每个请求的方法/路径/来源IP"""
    if request.path.startswith("/static/"):
        return          # 静态资源逐条记录只会淹没真正有用的日志
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


@app.context_processor
def inject_stats_flag():
    """导航栏需要知道"当前是否有数据"，但统计已移出 session，这里统一注入"""
    if "overview" in session:      # 兼容旧会话
        return {"overview": session["overview"], "has_stats": True}
    return {"has_stats": bool(session.get("chat_hash"))}


def _check_csrf() -> bool:
    """校验请求携带的 CSRF token 与会话一致

    注意：secrets.compare_digest 对含非 ASCII 字符的 str 会抛 TypeError，
    而 token 来自请求方（可被任意构造），必须先落到 bytes 再比较，
    否则一个中文 token 就能把 POST 打成 500。
    """
    provided = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "")
    expected = session.get("csrf_token", "")
    if not expected or not provided:
        return False
    try:
        return secrets.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except (UnicodeError, AttributeError, TypeError):
        return False


def _origin_allowed() -> bool:
    """校验浏览器 Origin 来源（存在时）。拒绝非白名单来源，防御跨站提交/DNS rebinding。

    curl 等非浏览器请求不带 Origin，交由 CSRF token 校验兜底。
    允许：回环地址、当前绑定地址 FLASK_HOST、以及显式配置的 ALLOWED_ORIGINS
    （局域网 IP / 自定义域名请写进 .env 的 ALLOWED_ORIGINS）。

    这里**故意不**把请求自带的 Host 计入白名单：DNS rebinding 场景下浏览器发出的
    Origin 与 Host 同为攻击者域名，一旦信任 Host，该校验就恒为 True、形同虚设。
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    allowed = {"localhost", "127.0.0.1", "::1", FLASK_HOST.lower()} | ALLOWED_ORIGINS
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

# 登录失败限流：同一 IP 在滑动窗口内失败达到上限后暂时拒绝，避免绑定局域网时被爆破
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _login_throttle_ok(ip: str) -> bool:
    """该 IP 是否仍允许尝试登录（只统计失败次数，成功即清零）"""
    now = time.time()
    with _login_lock:
        stamps = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        if stamps:
            _login_failures[ip] = stamps
        else:
            _login_failures.pop(ip, None)
        return len(stamps) < LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.time())
        if len(_login_failures) > 1000:      # 防止字典随扫描流量无限增长
            now = time.time()
            for key in [k for k, v in _login_failures.items()
                        if not v or now - v[-1] > LOGIN_WINDOW_SECONDS]:
                _login_failures.pop(key, None)


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


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
        ip = request.remote_addr or "127.0.0.1"
        if not _login_throttle_ok(ip):
            logger.warning("登录尝试过于频繁，暂时拒绝 [%s]", ip)
            return render_template(
                "login.html",
                error=f"尝试次数过多，请 {LOGIN_WINDOW_SECONDS // 60} 分钟后再试",
            ), 429
        # 登录接口自身豁免 CSRF（无 session 时先建 token）
        pwd = request.form.get("password", "")
        if hmac.compare_digest(pwd.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
            session["auth_ok"] = True
            _clear_login_failures(ip)
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/") or nxt.startswith("//"):  # 防开放重定向
                nxt = url_for("index")
            logger.info("登录成功 [%s]", ip)
            return redirect(nxt)
        _record_login_failure(ip)
        error = "口令错误，请重试"
        logger.warning("登录失败 [%s]", ip)
    return render_template("login.html", error=error)


# ---------------------------------------------------------------------------
# 临时文件清理（uploads/ 与 flask_session/ 只增不减，长期运行会占用磁盘）
# ---------------------------------------------------------------------------


def _cache_created_at(path: str) -> float:
    """缓存的创建时间：新格式写在 _created 字段里，旧格式回退到 mtime"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("_created"), (int, float)):
            return float(data["_created"])
    except (OSError, json.JSONDecodeError):
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _cleanup_old_files(max_age_seconds: int = 86400, cache_max_age: int = 30 * 86400,
                       cache_hard_max_age: int = 90 * 86400):
    """删除过期的临时文件。

    生命周期分层：
    - uploads/ 与 flask_session/：24h —— 原始聊天记录，敏感，尽快清；
    - ai_cache/ 与 stats_cache/：滑动 30 天 + 绝对 90 天。
      滑动窗口让"每周用几次"的人真正省到钱，但只看 mtime 的话，天天查看的结果
      永远不会过期，所以再加一条"创建超过 90 天必删"的硬上限，兑现隐私承诺。
    重新上传/删除聊天文件时其派生缓存会被联动清除（_purge_chat_caches）。
    """
    now = time.time()
    cleaned = 0
    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR):
        cleaned += _purge_dir(directory, lambda p: now - os.path.getmtime(p) > max_age_seconds)
    for directory in (AI_CACHE_DIR, STATS_CACHE_DIR):
        cleaned += _purge_dir(directory, lambda p: (
            now - os.path.getmtime(p) > cache_max_age
            or now - _cache_created_at(p) > cache_hard_max_age))
    try:
        cleaned += sweep_orphan_month_cache()
    except Exception as e:                       # 回收失败不影响主流程
        logger.warning("月份缓存回收失败: %s", e)
    if cleaned:
        logger.info("已清理 %d 个过期文件", cleaned)


def _purge_dir(directory: str, expired) -> int:
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and expired(path):
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


_last_cleanup = [0.0]


def _maybe_cleanup(interval_seconds: int = 3600) -> None:
    """长跑进程也要清理：原本只在 import 时跑一次，开着不关的实例永不回收临时文件"""
    now = time.time()
    if now - _last_cleanup[0] < interval_seconds:
        return
    _last_cleanup[0] = now
    try:
        _cleanup_old_files(max_age_seconds=86400)
    except Exception as e:                      # 清理失败不能影响上传
        logger.warning("定期清理失败: %s", e)


# 模块导入时即清理（覆盖 python app.py 与 flask --app app run 两种启动方式）
_cleanup_old_files(max_age_seconds=86400)
_last_cleanup[0] = time.time()


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

    # 旧文件信息先记下，等新文件解析成功后再处置（解析失败不伤及当前会话）
    old_path = session.get("filepath")
    old_hash = None
    if old_path and os.path.exists(old_path) and os.path.dirname(old_path) == UPLOAD_FOLDER:
        try:
            old_hash = _chat_hash(old_path)
        except OSError:
            old_hash = None

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
    new_hash = _chat_hash(filepath)
    if old_path and old_path != filepath and os.path.exists(old_path):
        if old_hash and old_hash != new_hash:
            _purge_chat_caches(old_hash)
        try:
            os.remove(old_path)
        except OSError:
            pass

    # 存入 session（只放小体积的会话元数据：统计结果落盘，见 _save_stats）
    session["chat_name"] = chat.chat_name
    session["self_name"] = chat.self_name
    session["other_name"] = chat.other_name
    session["filepath"] = filepath
    session["chat_hash"] = new_hash
    session["total_messages"] = len(chat.messages)
    if old_hash and old_hash != new_hash:
        logger.info("同一会话上传了新文件，旧文件（%s…）的派生缓存已清理", old_hash[:8])

    # 本地统计：内容相同的文件直接复用上次结果（统计是确定性的，重算纯属浪费）
    stats = _load_stats(new_hash)
    if stats is None:
        logger.info("开始计算本地统计...")
        t0 = time.time()
        stats = {
            "overview": calc_overview(chat),
            "daily_counts": calc_daily_counts(chat),
            "hourly_dist": calc_hourly_distribution(chat),
            "weekly_dist": calc_weekly_distribution(chat),
            "length_stats": calc_message_length_stats(chat),
            "face_stats": calc_face_stats(chat),
            "response_time": calc_response_time(chat),
            "exchange_rounds": calc_exchange_rounds(chat),
            "weekly_activity": calc_weekly_activity(chat),
            "milestones": calc_milestones(chat),
            # word_freq 由"说话习惯"页按需计算（jieba 分词占了统计耗时的大头）
        }
        _save_stats(new_hash, stats)
        logger.info("本地统计完成（%.0f ms），已落盘复用", (time.time() - t0) * 1000)
    else:
        logger.info("本地统计命中缓存，跳过重算")
    _maybe_cleanup()

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """总览仪表盘"""
    stats = _current_stats()
    if stats is None:
        logger.info("会话无统计数据, 重定向到首页")
        return redirect(url_for("index"))
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


@app.route("/emotion")
def emotion():
    """情绪分析页"""
    stats = _current_stats()
    if stats is None:
        return redirect(url_for("index"))
    return render_template("emotion.html", api_ok=is_api_configured(),
                           overview=stats["overview"])


@app.route("/relationship")
def relationship():
    """人际关系页"""
    stats = _current_stats()
    if stats is None:
        return redirect(url_for("index"))
    return render_template("relationship.html", api_ok=is_api_configured(),
                           overview=stats["overview"],
                           response_time=stats.get("response_time"),
                           exchange_rounds=stats.get("exchange_rounds"))


@app.route("/habits")
def habits():
    """个人习惯页"""
    stats = _stats_with_word_freq(_current_stats(), session.get("chat_hash", ""))
    if stats is None:
        return redirect(url_for("index"))
    return render_template("habits.html", api_ok=is_api_configured(),
                           overview=stats["overview"],
                           face_stats=stats.get("face_stats"),
                           length_stats=stats.get("length_stats"),
                           weekly_activity=stats.get("weekly_activity"),
                           word_freq=stats.get("word_freq"))


@app.route("/topics")
def topics():
    """话题趋势页"""
    stats = _current_stats()
    if stats is None:
        return redirect(url_for("index"))
    return render_template("topics.html", api_ok=is_api_configured(),
                           overview=stats["overview"])


@app.route("/profile")
def profile():
    """AI 人物锐评页"""
    stats = _current_stats()
    if stats is None:
        return redirect(url_for("index"))
    return render_template("profile.html", api_ok=is_api_configured(),
                           overview=stats["overview"])


@app.route("/report")
def report():
    """全篇报告导出页"""
    stats = _stats_with_word_freq(_current_stats(), session.get("chat_hash", ""))
    if stats is None:
        return redirect(url_for("index"))
    return render_template("report.html", api_ok=is_api_configured(),
                           overview=stats["overview"],
                           daily_counts=stats.get("daily_counts"),
                           hourly_dist=stats.get("hourly_dist"),
                           weekly_dist=stats.get("weekly_dist"),
                           length_stats=stats.get("length_stats"),
                           face_stats=stats.get("face_stats"),
                           response_time=stats.get("response_time"),
                           exchange_rounds=stats.get("exchange_rounds"),
                           weekly_activity=stats.get("weekly_activity"),
                           word_freq=stats.get("word_freq"))


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


# ---------------------------------------------------------------------------
# 已解析聊天数据的进程内复用（一次全量分析原本要为每个维度重新解析一遍）
# ---------------------------------------------------------------------------

_CHAT_CACHE: dict[tuple, object] = {}
_CHAT_CACHE_LOCK = threading.Lock()


def _load_chat_cached(filepath: str):
    """按 (路径, mtime, 大小) 复用已解析的 ChatData；只保留最近一份，避免大文件堆积"""
    try:
        st = os.stat(filepath)
        key = (os.path.abspath(filepath), st.st_mtime_ns, st.st_size)
    except OSError:
        return load_chat(filepath)
    with _CHAT_CACHE_LOCK:
        cached = _CHAT_CACHE.get(key)
    if cached is not None:
        return cached
    chat = load_chat(filepath)
    with _CHAT_CACHE_LOCK:
        _CHAT_CACHE.clear()
        _CHAT_CACHE[key] = chat
    return chat


# ---------------------------------------------------------------------------
# 本地统计结果的磁盘缓存（内容寻址，与 AI 缓存同生命周期）
# ---------------------------------------------------------------------------


# 统计结果的结构版本：字段形状变了就 +1，老缓存会被判为过期并重算（避免模板 500）
STATS_SCHEMA_VERSION = 2


def _stats_path(chat_hash: str) -> str:
    return os.path.join(STATS_CACHE_DIR, f"stats_{chat_hash}.json")


def _load_stats(chat_hash: str):
    if not chat_hash:
        return None
    try:
        with open(_stats_path(chat_hash), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or "overview" not in data
            or data.get("_v") != STATS_SCHEMA_VERSION):
        return None
    try:
        os.utime(_stats_path(chat_hash), None)
    except OSError:
        pass
    return data


def _save_stats(chat_hash: str, stats: dict) -> None:
    if not chat_hash:
        return
    os.makedirs(STATS_CACHE_DIR, exist_ok=True)
    path = _stats_path(chat_hash)
    tmp = f"{path}.tmp"
    payload = dict(stats)
    payload["_v"] = STATS_SCHEMA_VERSION
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("统计缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _delete_stats(chat_hash: str) -> None:
    if not chat_hash:
        return
    try:
        os.remove(_stats_path(chat_hash))
    except OSError:
        pass


def _current_stats():
    """当前会话的统计数据（从磁盘读；没有则返回 None，路由据此回首页）"""
    return _load_stats(session.get("chat_hash", ""))


def _stats_with_word_freq(stats: dict, chat_hash: str):
    """词频按需计算并写回统计缓存：jieba 分词占统计耗时的大头（5 万条约 0.9 秒）"""
    if stats is None or stats.get("word_freq"):
        return stats
    filepath = session.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return stats
    try:
        chat = _load_chat_cached(filepath)
        stats["word_freq"] = calc_word_freq(chat, top_n=80)
        _save_stats(chat_hash, stats)
    except Exception as e:                      # 词频失败不该拖垮页面
        logger.error("词频统计失败: %s", e)
        stats.setdefault("word_freq", {"self": [], "other": []})
    return stats



def _cache_path(dimension: str, chat_hash: str) -> str:
    # 键含提示词/格式指纹：PROMPT_FINGERPRINT 由 SYSTEM_PROMPT_* 与对话格式化函数
    # 自动哈希而来，改了提示词或输入格式后旧缓存自动失效（不再依赖人工 bump 版本号）。
    # 键含思考模式：同一模型开关 thinking 前后的结果差异很大，必须分开存放，
    # 否则切换 LLM_THINKING(_DIMS) 后会命中另一种模式的旧结果（看起来"没区别"）。
    # 非思考模式不加后缀，保持既有缓存键兼容。
    suffix = "_think" if thinking_enabled(dimension) else ""
    return os.path.join(AI_CACHE_DIR,
                        f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}_{PROMPT_FINGERPRINT}{suffix}.json")


def _purge_chat_caches(chat_hash: str) -> int:
    """删除某聊天文件的全部缓存（跨版本/模型）。聊天源文件被删时联动调用，
    避免派生的分析结果（含聊天内容摘要）成为孤儿残留。"""
    if not chat_hash:
        return 0
    removed = 0
    try:
        entries = os.listdir(AI_CACHE_DIR)
    except OSError:
        return 0
    for name in entries:
        if f"_{chat_hash}_" in name:
            try:
                os.remove(os.path.join(AI_CACHE_DIR, name))
                removed += 1
            except OSError:
                pass
    # 本地统计缓存
    try:
        os.remove(_stats_path(chat_hash))
        removed += 1
    except OSError:
        pass
    # 月份级缓存：删 manifest，并回收不再被其他聊天引用的月份文件
    removed += purge_month_cache(chat_hash)
    return removed


def _read_cache(dimension: str, chat_hash: str):
    path = _cache_path(dimension, chat_hash)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # 新格式带创建时间（供绝对上限清理用）；旧格式直接就是结果本身
    if isinstance(data, dict) and "_created" in data and "result" in data:
        data = data["result"]
    # 命中即续期：清理任务按 mtime 判断过期，只读不写会让天天用的缓存
    # 在 30 天后照样被删掉（然后重新花钱分析）
    try:
        os.utime(path, None)
    except OSError:
        pass
    return data


def _write_cache(dimension: str, chat_hash: str, result) -> None:
    """原子写入：临时文件 + os.replace，避免进程中断留下半截 JSON。

    记录创建时间：命中读取会刷新 mtime（滑动窗口续期），若只有 mtime，
    天天查看的结果将永远不会被回收，与"保留 30 天"的隐私承诺不符。
    """
    path = _cache_path(dimension, chat_hash)
    tmp = f"{path}.tmp"
    payload = {"_created": time.time(), "result": result}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("AI 缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _run_job(job_id: str, dimension: str, filepath: str, chat_hash: str) -> None:
    """后台线程执行分析：更新进度、支持取消、成功后写磁盘缓存"""
    dim_name = DIMENSION_NAMES.get(dimension, dimension)
    try:
        chat = _load_chat_cached(filepath)
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
        result = func(chat, on_progress=on_progress, should_cancel=should_cancel,
                    chat_hash=chat_hash)
        cancelled = should_cancel()
        # 先落盘缓存，再对外置 done：否则前端轮询到 done 立刻请求
        # /api/analysis/<dim> 时可能读不到缓存，反而重新发起一次付费分析
        if result and not cancelled:
            _write_cache(dimension, chat_hash, result)
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            if cancelled:
                j.update(status="cancelled", finished_at=time.time())
            elif not result:
                j.update(status="error",
                         error="分析未产生结果：可能全部月份失败，请查看日志",
                         finished_at=time.time())
            else:
                j.update(status="done", result=result, finished_at=time.time())
        if result and not cancelled:
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


def _running_jobs_locked(sid: str, chat_hash: str) -> list[tuple[str, str]]:
    """（调用方须持有 JOBS_LOCK）返回该 session+文件的 running 任务 [(job_id, dim)]"""
    return [(jid, j.get("dim", "")) for jid, j in JOBS.items()
            if j["status"] == "running" and j.get("sid") == sid
            and j.get("chat_hash") == chat_hash]


def _get_or_create_job(sid: str, dimension: str, chat_hash: str, total: int,
                       conflict_dimension: str | None = None):
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
        job = {"status": "running", "dim": dimension, "done": 0, "total": total,
               "cancel": False, "chat_hash": chat_hash, "sid": sid,
               "created": time.time()}
        if dimension == "all":
            job["detail"] = ""
        JOBS[job_id] = job
        return job_id, False, None


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

    chat_hash = session.get("chat_hash") or _chat_hash(filepath)

    # 缓存命中（除非显式 refresh=1 强制重跑）
    if request.args.get("refresh") != "1":
        cached = _read_cache(dimension, chat_hash)
        if cached is not None:
            logger.info("%s 命中缓存，直接返回", DIMENSION_NAMES.get(dimension, dimension))
            return jsonify({"cached": True, "result": cached})

    _prune_jobs()
    # 全量任务已覆盖本维度：拒绝而不是另起一个任务（否则同一维度会被分析两遍、双倍计费）
    job_id, reused, conflict = _get_or_create_job(
        session.sid, dimension, chat_hash, total=0, conflict_dimension="all")
    if conflict:
        logger.info("已有全量任务在运行，拒绝重复启动 %s", dimension)
        return jsonify({"error": "一键全量分析正在运行，请等它完成或先取消（避免重复调用 API）"}), 409
    if reused:
        logger.info("复用进行中的 %s 任务 %s", dimension, job_id[:8])
        return jsonify({"job": job_id, "reused": True})

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
    cached = _read_cache(dimension, session.get("chat_hash") or _chat_hash(filepath))
    if cached is None:
        return jsonify({"error": "暂无该维度的分析结果"}), 404
    return jsonify({"cached": True, "result": cached})


def _run_analyze_all(job_id: str, filepath: str, chat_hash: str, refresh: bool) -> None:
    """一键全量分析：按维度顺序执行（维度内部已有月份级并发），
    已缓存的维度直接跳过（refresh 时强制重跑），单维度失败不阻断其余维度。"""
    try:
        chat = _load_chat_cached(filepath)
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
                                                should_cancel=should_cancel,
                                                chat_hash=chat_hash)
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

    chat_hash = session.get("chat_hash") or _chat_hash(filepath)
    refresh = request.args.get("refresh") == "1"

    _prune_jobs()
    job_id, reused, conflict = _get_or_create_job(
        session.sid, "all", chat_hash, total=len(ANALYZE_FUNCS), conflict_dimension="*")
    if reused:
        logger.info("复用进行中的一键全量任务 %s", job_id[:8])
        return jsonify({"job": job_id, "reused": True})
    if conflict:
        # 已有单维度任务在跑：全量任务会把这些维度再跑一遍（重复计费），先拒绝
        logger.info("已有 %s 任务在运行，拒绝启动全量分析", conflict)
        return jsonify({"error": f"已有「{DIMENSION_NAMES.get(conflict, conflict)}」任务在运行，"
                                 "请等它完成或先取消（避免重复调用 API）"}), 409

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
        _p(f"  模型: {DEEPSEEK_MODEL} · 并发 {CONCURRENCY} · 调用间隔 {CALL_MIN_INTERVAL}s"
           f"（多月份分析的排队下限 ≈ (月数-1)×{CALL_MIN_INTERVAL}s）")
        thinking_dims = [d for d in MAX_TOKENS_BY_DIM if thinking_enabled(d)]
        if thinking_dims:
            _p(f"  思考模式: {', '.join(thinking_dims)}")
        conflicts = thinking_budget_warnings()
        if conflicts:
            _p("  [WARN] 思考模式与输出预算冲突，这些维度会因截断丢弃结果：")
            _p(f"         {', '.join(conflicts)}")
            _p("         请调大 MAX_TOKENS_BY_DIM（analyzer/deepseek_client.py）或关闭对应维度的思考模式")
    _p(f"  数据目录: {os.path.dirname(AI_CACHE_DIR)}（可用 QQCHAT_DATA_DIR 迁移，测试更安全）")
    _p(f"  增量缓存: {'开（只分析新增月份）' if MONTH_CACHE_ENABLED else '关'}")

    loopback = FLASK_HOST in ("127.0.0.1", "localhost", "::1")
    if not loopback and not ACCESS_PASSWORD:
        _p("  [ERROR] 绑定到非回环地址必须设置 ACCESS_PASSWORD（见 .env.example）")
        _p("  已拒绝启动，以免聊天记录与 AI 结果被局域网内陌生人访问")
        sys.exit(1)
    if ACCESS_PASSWORD:
        _p("  [OK] 访问口令已启用")
    if not loopback:
        if ALLOWED_ORIGINS:
            _p(f"  [OK] 允许的浏览器来源: {', '.join(sorted(ALLOWED_ORIGINS))}")
        else:
            _p("  [WARN] 未设置 ALLOWED_ORIGINS：用局域网 IP 或域名打开页面时，")
            _p("         上传与 AI 分析请求会被 403 拒绝（Origin 校验不信任请求 Host）")
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
