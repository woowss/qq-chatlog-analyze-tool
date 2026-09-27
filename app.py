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
# 以及命令行入口 main()（console_scripts `qqchatlog` 与 `python app.py` 共用）。

import argparse
import os
import signal
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Optional, Sequence

from cachelib import FileSystemCache
from flask import Flask
from flask_session import Session

from config import (
    ACCESS_PASSWORD,
    AI_CACHE_DIR,
    ALLOWED_ORIGINS,
    CACHE_MAX_DAYS,
    CACHE_SLIDE_DAYS,
    COOKIE_SECURE,
    DEEPSEEK_MODEL,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    JOB_TTL_SECONDS,
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
    is_insecure_base_url,
    thinking_budget_warnings,
    thinking_enabled,
)
from analyzer.logger import get_logger
from analyzer.shutdown import request_shutdown
from analyzer.usage import flush as flush_usage

from web import STATIC_DIR, TEMPLATES_DIR
from webapp import api, cleanup, security, views

logger = get_logger("app")

#: 视为"本机、不经网络"的绑定地址（回环）。两个用途共用一份口径：
#: ① 非回环绑定必须设访问口令；② Secure cookie 的 auto 判定。
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_loopback(host: str) -> bool:
    """绑定地址是否为本机回环（本文件里"是否需要网络侧防护"的唯一口径来源）"""
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def _secure_cookie_enabled() -> bool:
    """会话 cookie 是否打 Secure 标志。

    auto（默认）跟着绑定地址走：回环不过网，恒 False 才不会把本机 http 访问
    也挡掉；一旦绑定到局域网/公网地址，就默认要求 HTTPS——明文 http 下 cookie
    会裸奔过网，中间人拿到 sid 等于拿到登录态。

    局域网明文 http 的用户需要在 .env 里显式设 QQCHAT_COOKIE_SECURE=false，
    启动横幅会提示这一点（否则症状是"登录成功却立刻被弹回登录页"，很难自查）。
    """
    if COOKIE_SECURE == "true":
        return True
    if COOKIE_SECURE == "false":
        return False
    return not _is_loopback(FLASK_HOST)


def create_app() -> Flask:
    """组装 Flask 应用：实例配置 → 服务端 session → 数据目录 → 月份缓存 → 各层注册"""
    # 模板/静态目录按 web 包的绝对路径解析，而不是相对工作目录的 "web/templates"：
    # pip 安装后包在 site-packages 下，从任何目录执行 qqchatlog 都要能找到它们。
    app = Flask(
        __name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR), static_url_path="/static"
    )
    app.secret_key = SECRET_KEY
    app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    # 服务端文件系统 session (避免 cookie 大小限制)
    # 会话存服务端文件：flask-session 0.8 起 "filesystem" 接口（以及 SESSION_FILE_DIR /
    # SESSION_FILE_THRESHOLD / SESSION_FILE_MODE / SESSION_USE_SIGNER）都已弃用，官方替代是
    # 直接把 cachelib 实例交给 SESSION_CACHELIB。旧接口内部用的就是同一个 cachelib.FileSystemCache
    # （threshold/mode 默认 500 / 0o600，这里显式写出），存取与 TTL 语义逐行等价，所以存储格式不变、
    # 已有会话继续可用。use_signer 的签名只防"会话 id 被篡改"，而 id 本身是 32 字节随机串、
    # 会话数据全在服务端，去掉它没有实际收益损失，正好跟上上游的移除计划。
    app.config["SESSION_TYPE"] = "cachelib"
    app.config["SESSION_CACHELIB"] = FileSystemCache(SESSION_FILE_DIR, threshold=500, mode=0o600)
    app.config["SESSION_PERMANENT"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # 禁止 JS 读取会话 cookie
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # 跨站请求不携带 cookie（CSRF 纵深防御）
    # Secure：非回环绑定默认开启（HTTPS 才回传会话 cookie）。详见 _secure_cookie_enabled
    app.config["SESSION_COOKIE_SECURE"] = _secure_cookie_enabled()
    Session(app)

    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR, AI_CACHE_DIR, STATS_CACHE_DIR):
        os.makedirs(directory, exist_ok=True)

    # 月份级缓存（增量分析）：把目录注入分析层，避免 analyzer 反向依赖本模块
    configure_month_cache(AI_CACHE_DIR if MONTH_CACHE_ENABLED else "")

    # 注册顺序即 before_request 执行顺序（与拆分前的 app.py 保持一致）：
    # log_request → track_active_chat → ensure_csrf_token → require_login
    # → periodic_cleanup → 路由分发
    # （track_active_chat 只读会话、不拦截，所以排在登录之前也无妨；它必须每个请求
    #   都跑一次，见 webapp/views.py 里那段说明）
    views.register(app)
    security.register(app)
    cleanup.register(app)
    api.register(app)
    return app


app = create_app()

# ---------------------------------------------------------------------------
# 启动时回收一次过期临时文件（覆盖 python app.py 与 flask --app app run 两种方式）
# ---------------------------------------------------------------------------
cleanup.startup_cleanup()


# ---------------------------------------------------------------------------
# 命令行入口（console_scripts: qqchatlog = "app:main"；`python app.py` 也走这里）
# ---------------------------------------------------------------------------


def _print_safe(msg: str) -> None:
    """打印启动信息：控制台编码表示不了时降级替换，别让一行生僻字把启动打崩"""
    enc = sys.stdout.encoding or "utf-8"
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode(enc, errors="replace").decode(enc))


def _package_version() -> str:
    """发行版本号：从安装元数据读取；源码直跑（没 pip install 过）时回退为 dev"""
    try:
        return version("qqchatlog")
    except PackageNotFoundError:
        return "dev"


def _startup_report() -> bool:
    """打印启动横幅与自检结果；返回 False 表示当前配置不允许启动（调用方以退出码 1 结束）"""
    sep = "=" * 50
    _print_safe(sep)
    _print_safe("  QQ 聊天记录分析工具")
    _print_safe(f"  访问地址: http://{FLASK_HOST}:{FLASK_PORT}")
    _print_safe(sep)
    if not is_api_configured():
        _print_safe("  [WARN] DeepSeek API Key 未配置")
        _print_safe("  请编辑项目根目录的 .env 文件填入 Key")
    else:
        _print_safe("  [OK] DeepSeek API 已配置")
        _print_safe(
            f"  模型: {DEEPSEEK_MODEL} · 并发 {CONCURRENCY} · 调用间隔 {CALL_MIN_INTERVAL}s"
            f"（多月份分析的排队下限 ≈ (月数-1)×{CALL_MIN_INTERVAL}s）"
        )
        thinking_dims = [d for d in MAX_TOKENS_BY_DIM if thinking_enabled(d)]
        if thinking_dims:
            _print_safe(f"  思考模式: {', '.join(thinking_dims)}")
        conflicts = thinking_budget_warnings()
        if conflicts:
            _print_safe("  [WARN] 思考模式与输出预算冲突，这些维度会因截断丢弃结果：")
            _print_safe(f"         {', '.join(conflicts)}")
            _print_safe(
                "         请在 .env 里调大对应维度的 LLM_MAX_TOKENS_<维度>（如 LLM_MAX_TOKENS_PROFILE），"
            )
            _print_safe("         或关闭该维度的思考模式")
        if is_insecure_base_url():
            _print_safe("  [WARN] DEEPSEEK_BASE_URL 是明文 http 且非本机地址：")
            _print_safe("         API Key 与聊天内容会以明文过网，建议改成 https 端点")
    _print_safe(f"  数据目录: {os.path.dirname(AI_CACHE_DIR)}（可用 QQCHAT_DATA_DIR 迁移，测试更安全）")
    _print_safe(f"  单次上传上限: {MAX_CONTENT_LENGTH // 1048576} MB（QQCHAT_MAX_UPLOAD_MB 可调）")
    _print_safe(f"  增量缓存: {'开（只分析新增月份）' if MONTH_CACHE_ENABLED else '关'}")
    if CACHE_SLIDE_DAYS <= 0 or CACHE_MAX_DAYS <= 0:
        _print_safe(
            "  [WARN] 派生缓存回收已手动关闭（QQCHAT_CACHE_SLIDE_DAYS / QQCHAT_CACHE_MAX_DAYS 为 0）："
        )
        _print_safe("         已付费的 AI 结果与统计将【永久留在本机】，不再到期自动回收")
    else:
        _print_safe(
            f"  派生缓存: 滑动 {CACHE_SLIDE_DAYS} 天 + 绝对 {CACHE_MAX_DAYS} 天上限"
            "（QQCHAT_CACHE_SLIDE_DAYS / QQCHAT_CACHE_MAX_DAYS 可调，0=不过期）"
        )
    _print_safe(f"  内存任务 TTL: {JOB_TTL_SECONDS}s · 结果本身永远先落盘（重启/超时不丢）")

    loopback = _is_loopback(FLASK_HOST)
    if not loopback and not ACCESS_PASSWORD:
        _print_safe("  [ERROR] 绑定到非回环地址必须设置 ACCESS_PASSWORD（见 .env.example）")
        _print_safe("  已拒绝启动，以免聊天记录与 AI 结果被局域网内陌生人访问")
        return False
    if ACCESS_PASSWORD:
        _print_safe("  [OK] 访问口令已启用")
    if not loopback:
        if ALLOWED_ORIGINS:
            _print_safe(f"  [OK] 允许的浏览器来源: {', '.join(sorted(ALLOWED_ORIGINS))}")
        else:
            _print_safe("  [WARN] 未设置 ALLOWED_ORIGINS：用局域网 IP 或域名打开页面时，")
            _print_safe("         上传与 AI 分析请求会被 403 拒绝（Origin 校验不信任请求 Host）")
        # Secure cookie 与"明文 http 局域网访问"互斥：必须把症状与解法一起说清，
        # 否则用户只会看到"登录成功却立刻被弹回登录页"，完全无从自查。
        if _secure_cookie_enabled():
            _print_safe("  [OK] 会话 cookie 已加 Secure：仅在 https 下回传（QQCHAT_COOKIE_SECURE=auto）")
            _print_safe("       若你用明文 http 访问局域网地址，登录将无法保持——请改用 https 反代，")
            _print_safe("       或设 QQCHAT_COOKIE_SECURE=false（明文传输口令与会话，风险自负）")
        else:
            _print_safe("  [WARN] 会话 cookie 未加 Secure（QQCHAT_COOKIE_SECURE=false）")
            _print_safe("         明文 http 下中间人可直接窃取会话 id 并接管登录态，建议改用 https")
    if FLASK_DEBUG:
        _print_safe("  [WARN] 调试模式已开启（调试器可执行任意代码，仅限本机开发）")

    _print_safe(sep)
    redact = "昵称与文件名已脱敏" if LOG_REDACT_NAMES else "含明文昵称（LOG_REDACT_NAMES=false）"
    _print_safe(f"  日志: {LOG_RETENTION_DAYS} 天轮转保留 · {redact}")
    _print_safe("  uploads/ flask_session/ ai_cache/ 过期文件启动时清理，之后每小时随请求去抖清理")
    _print_safe(sep)
    return True


#: 收到中断信号后，留给"正在跑的那一个月"的收尾时间（秒）。
#: 设 0 即恢复"按下就退出"的行为。
SHUTDOWN_GRACE_SECONDS = float(os.getenv("QQCHAT_SHUTDOWN_GRACE_SECONDS") or 5)


def _on_shutdown_signal(signum, _frame) -> None:
    """中断信号处理器：先停止派发新的付费调用，再退出。

    提成模块级函数（而不是留在 _install_shutdown_handler 里的闭包）是为了可测：
    用例可以直接调用它，不必真的给进程发信号（那样会打断测试进程本身）。

    两段式：
    1. 置位全局关闭标志。分析循环在每个派发点检查它，于是不再启动新月份，
       已经完成的月份结果照常落盘（月份级缓存在每个月完成时就写了）；
    2. 最多等 SHUTDOWN_GRACE_SECONDS 秒让进行中的那一个月收尾，然后抛
       KeyboardInterrupt —— Werkzeug 的 serve_forever 会吞掉它并关闭服务器，
       退出流程与原来一致。

    **第二次信号立即退出**：不加这条判断时，重复 Ctrl+C 会不断重新进入本函数，
    每次都睡满 GRACE（信号会打断 sleep 并重新派发），用户看到的是"按了没反应"，
    最后只能去杀进程——那正是这段等待想避免的事。用户明确催第二次时，
    就说明他不打算再等了，此时进行中的那个月照旧拿不回来，没必要再拖。

    顺带把 token 用量落盘（它按天累计在内存里，进程被硬杀就丢了）。
    """
    if not request_shutdown():
        logger.warning("再次收到中断信号（%s）：立即退出，不再等待进行中的月份", signum)
        flush_usage()
        raise KeyboardInterrupt
    logger.warning(
        "收到中断信号（%s）：不再发起新的分析请求，最多等 %.0f 秒让进行中的月份收尾"
        "（再按一次 Ctrl+C 可立即退出；也可用 QQCHAT_SHUTDOWN_GRACE_SECONDS=0 关掉这段等待）",
        signum,
        SHUTDOWN_GRACE_SECONDS,
    )
    flush_usage()
    if SHUTDOWN_GRACE_SECONDS > 0:
        time.sleep(SHUTDOWN_GRACE_SECONDS)
    raise KeyboardInterrupt


def _install_shutdown_handler() -> None:
    """把中断处理器挂到 SIGINT / SIGTERM 上（行为见 _on_shutdown_signal）"""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_shutdown_signal)
        except (ValueError, OSError, AttributeError):
            # 非主线程、或平台不支持该信号：保持默认行为即可，不影响启动
            continue


def main(argv: Optional[Sequence[str]] = None) -> int:
    """启动 Web 服务，返回进程退出码（0 = 正常结束，1 = 配置或端口问题拒绝启动）

    绑定地址/端口/调试开关仍由 .env 与环境变量决定，命令行只提供 --version：
    Origin 白名单在 webapp/security.py 里是按 import 时的 FLASK_HOST 算的，
    运行时改监听地址会让校验与真实地址对不上，所以这里不开 --host/--port。
    """
    parser = argparse.ArgumentParser(prog="qqchatlog", description="QQ 聊天记录分析工具")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_package_version()}")
    parser.parse_args(argv)

    if not _startup_report():
        return 1

    _install_shutdown_handler()

    logger.info("=" * 40)
    logger.info("应用启动 - http://%s:%d", FLASK_HOST, FLASK_PORT)
    logger.info("API Key: %s", "已配置" if is_api_configured() else "未配置")
    logger.info("调试模式: %s", FLASK_DEBUG)
    logger.info("=" * 40)

    try:
        app.run(debug=FLASK_DEBUG, host=FLASK_HOST, port=FLASK_PORT)
    except OSError as e:
        _print_safe(f"  [ERROR] 启动失败: {e}")
        _print_safe(f"  端口 {FLASK_PORT} 可能被占用（Windows 上 5000 常被 AirPlay/Hyper-V 占用），")
        _print_safe("  可在 .env 中设置 FLASK_PORT=5001 换一个端口")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
