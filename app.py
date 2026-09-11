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
# QQ 聊天记录分析工具 — 应用组装入口
# ====================================
# 路由与基础设施在 webapp/ 包（分层见 webapp/__init__.py 的说明）。
# 本文件负责：创建并配置 Flask 实例、启动期一次性回收、测试兼容的重导出面、
# 以及 `python app.py` 的启动横幅。

import os
import sys

from flask import Flask
from flask_session import Session

from config import (
    ACCESS_PASSWORD,
    AI_CACHE_DIR,
    ALLOWED_ORIGINS,
    DEEPSEEK_MODEL,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    LOG_REDACT_NAMES,
    LOG_RETENTION_DAYS,
    MAX_CONTENT_LENGTH,
    MONTH_CACHE_ENABLED,
    SECRET_KEY,
    SESSION_FILE_DIR,
    STATS_CACHE_DIR,
    UPLOAD_FOLDER,
)
from analyzer.deepseek_client import (
    CALL_MIN_INTERVAL,
    CONCURRENCY,
    MAX_TOKENS_BY_DIM,
    configure_month_cache,
    is_api_configured,
    thinking_budget_warnings,
    thinking_enabled,
)
from analyzer.logger import get_logger

from webapp import api, cleanup, jobs, security, store, views

logger = get_logger("app")


def create_app() -> Flask:
    """组装 Flask 应用：实例配置 → 服务端 session → 数据目录 → 月份缓存 → 各层注册"""
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

    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR, AI_CACHE_DIR, STATS_CACHE_DIR):
        os.makedirs(directory, exist_ok=True)

    # 月份级缓存（增量分析）：把目录注入分析层，避免 analyzer 反向依赖本模块
    configure_month_cache(AI_CACHE_DIR if MONTH_CACHE_ENABLED else "")

    # 注册顺序即 before_request 执行顺序（与拆分前的 app.py 保持一致）：
    # log_request → ensure_csrf_token → require_login → periodic_cleanup → 路由分发
    views.register(app)
    security.register(app)
    cleanup.register(app)
    api.register(app)
    return app


app = create_app()

# 测试兼容的重导出面：历史测试以 `import app as appmod` 为入口直接触碰这些符号。
# 函数/对象是同一份（不是副本），直接调用完全等价；但 mock.patch.object 必须打在
# 定义模块（webapp.store / webapp.security / webapp.cleanup / webapp.api）上才生效。
_chat_hash = store._chat_hash
save_and_hash = store.save_and_hash
_load_chat_cached = store._load_chat_cached
_stats_path = store._stats_path
_load_stats = store._load_stats
_save_stats = store._save_stats
_delete_stats = store._delete_stats
_current_stats = store._current_stats
_stats_with_word_freq = store._stats_with_word_freq
_cache_path = store._cache_path
_read_cache = store._read_cache
_write_cache = store._write_cache
_purge_chat_caches = store._purge_chat_caches
start_stats_job = store.start_stats_job
wait_for_stats = store.wait_for_stats

_cache_created_at = cleanup._cache_created_at
_cleanup_old_files = cleanup.cleanup_old_files
_purge_dir = cleanup._purge_dir
_maybe_cleanup = cleanup.maybe_cleanup

JOBS = jobs.JOBS
JOBS_LOCK = jobs.JOBS_LOCK
JOB_TTL_SECONDS = jobs.JOB_TTL_SECONDS
DIMENSION_NAMES = jobs.DIMENSION_NAMES
ANALYZE_FUNCS = jobs.ANALYZE_FUNCS
_prune_jobs = jobs._prune_jobs
_get_or_create_job = jobs._get_or_create_job
_run_job = jobs._run_job
_run_analyze_all = jobs._run_analyze_all
_session_chat_file = jobs._session_chat_file

_check_csrf = security._check_csrf
_origin_allowed = security._origin_allowed
_guard_post = security._guard_post
_login_failures = security._login_failures
_login_lock = security._login_lock
_login_throttle_ok = security._login_throttle_ok
_record_login_failure = security._record_login_failure
_clear_login_failures = security._clear_login_failures
LOGIN_MAX_ATTEMPTS = security.LOGIN_MAX_ATTEMPTS
LOGIN_WINDOW_SECONDS = security.LOGIN_WINDOW_SECONDS
PUBLIC_ENDPOINTS = security.PUBLIC_ENDPOINTS

# ---------------------------------------------------------------------------
# 启动时回收一次过期临时文件（覆盖 python app.py 与 flask --app app run 两种方式）
# ---------------------------------------------------------------------------
cleanup.startup_cleanup()


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
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
    _p(f"  内存任务 TTL: {JOB_TTL_SECONDS}s · 结果本身永远先落盘（重启/超时不丢）")

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
    redact = "昵称与文件名已脱敏" if LOG_REDACT_NAMES else "含明文昵称（LOG_REDACT_NAMES=false）"
    _p(f"  日志: {LOG_RETENTION_DAYS} 天轮转保留 · {redact}")
    _p("  uploads/ flask_session/ ai_cache/ 过期文件启动时清理，之后每小时随请求去抖清理")
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
