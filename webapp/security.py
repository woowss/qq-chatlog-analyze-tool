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
"""访问防护层：CSRF / Origin 校验、口令登录与登录限流

从 app.py 拆出（原"巨石"里的安全段落）。函数读取本模块全局的
ACCESS_PASSWORD / ALLOWED_ORIGINS / FLASK_HOST——测试打桩请打在
webapp.security 上，而不是 app 的重导出别名上。
"""

import hmac
import secrets
import threading
import time
from urllib.parse import urlparse

from flask import (
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import ACCESS_PASSWORD, ALLOWED_ORIGINS, FLASK_HOST, LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS
from analyzer.logger import get_logger

logger = get_logger("app")

PUBLIC_ENDPOINTS = {"login", "static"}

#: 豁免登录、会话、CSRF、日志与定期清理中间件的探针端点（如存活探针）
BYPASS_ENDPOINTS = frozenset({"health"})

# 登录失败限流：同 IP 在滑动窗口内超限后暂时拒绝（上限见 _LOGIN_FAILURES_MAX，防爆破/表膨胀）
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
_LOGIN_FAILURES_MAX = 1000


def _now() -> float:
    """当前时间（抽成函数，方便用例控制时间——不必真睡满一个窗口）"""
    return time.time()


# ---------------------------------------------------------------------------
# CSRF / Origin
# ---------------------------------------------------------------------------


def ensure_csrf_token():
    """确保会话中存在 CSRF token（豁免静态资源与探针端点，避免无 cookie 静态请求新建空会话覆盖登录态）。"""
    if request.endpoint in BYPASS_ENDPOINTS or request.endpoint == "static":
        return None
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


def inject_csrf_token():
    """向所有模板注入 csrf_token 与 auth_enabled（仅启用口令时渲染退出按钮）。"""
    return {"csrf_token": session.get("csrf_token", ""), "auth_enabled": bool(ACCESS_PASSWORD)}


def _check_csrf() -> bool:
    """校验请求携带的 CSRF token 与会话一致（转 UTF-8 bytes 再 compare_digest，防非 ASCII 抛错）。"""
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf_token", "")
    if not expected or not provided:
        return False
    try:
        return secrets.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except (UnicodeError, AttributeError, TypeError):
        return False


def _csrf_error_response():
    """CSRF 校验失败时返回含刷新引导的 400 错误。"""
    if not session.get("csrf_token"):
        msg = "CSRF 校验失败：会话已过期（长期未操作会被自动回收），请刷新页面（F5）后重试"
    else:
        msg = "CSRF 校验失败：请求缺少或携带了错误的 token，请刷新页面（F5）后重试"
    logger.warning("CSRF 校验失败: %s %s", request.method, request.path)
    return msg, 400


def _origin_allowed() -> bool:
    """校验浏览器 Origin 是否在白名单内（回环、FLASK_HOST 与 ALLOWED_ORIGINS；不信任请求 Host 头）。"""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    allowed = {"localhost", "127.0.0.1", "::1", FLASK_HOST.lower()} | ALLOWED_ORIGINS
    return host in allowed


def _safe_for_log(value, limit: int = 120) -> str:
    """把请求方的原始值安全地写进日志：去掉换行/控制字符，避免伪造日志行"""
    text = str(value or "")
    cleaned = "".join(ch for ch in text if ch.isprintable())
    return cleaned[:limit]


def _guard_post():
    """POST 请求统一防护：Origin 校验 + CSRF token 校验"""
    if not _origin_allowed():
        logger.warning("拦截非本机来源请求: %s", _safe_for_log(request.headers.get("Origin")))
        return "非法来源", 403
    if not _check_csrf():
        return _csrf_error_response()
    return None


# ---------------------------------------------------------------------------
# 可选访问口令（设置 ACCESS_PASSWORD 后生效；绑定非回环地址时强制要求）
# ---------------------------------------------------------------------------


def _prune_locked(ip: str, now: float) -> list:
    """（调用方须持 _login_lock）该地址在窗口内的失败时间戳，顺带清掉过期条目"""
    stamps = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if stamps:
        _login_failures[ip] = stamps
    else:
        _login_failures.pop(ip, None)
    return stamps


def _retry_after_locked(stamps: list, now: float) -> int:
    """（调用方须持 _login_lock）被限流时还要等多少秒（喂给 429 与页面提示）"""
    if len(stamps) < LOGIN_MAX_ATTEMPTS:
        return 0
    # 最早那次失败滑出窗口时，窗口内只剩 MAX-1 次，限流自动解除
    return max(1, int(LOGIN_WINDOW_SECONDS - (now - min(stamps))) + 1)


def _enforce_table_limit_locked(now: float) -> None:
    """（调用方须持 _login_lock）失败记录表的硬上限。

    窗口内的失败才计数，但扫描流量可以伪造大量**不同**地址，只按"过期"清理的话
    表会一直涨；超过上限就先清过期条目，仍然超限说明失败来自大量**新鲜**地址
    （例如端口扫描），于是按最近一次失败时间淘汰最旧的，保证这张表有硬上限。
    """
    if len(_login_failures) <= _LOGIN_FAILURES_MAX:
        return
    for key in [k for k, v in _login_failures.items() if not v or now - v[-1] > LOGIN_WINDOW_SECONDS]:
        _login_failures.pop(key, None)
    overflow = len(_login_failures) - _LOGIN_FAILURES_MAX
    if overflow > 0:
        for key in sorted(_login_failures, key=lambda k: _login_failures[k][-1])[:overflow]:
            _login_failures.pop(key, None)


def _register_login_attempt(ip: str) -> int:
    """占用一次登录尝试：**在同一把锁内**判超限并当场记下这次尝试。

    返回 0 表示可以继续校验口令；否则返回还应等待的秒数（调用方回 429 + Retry-After）。

    为什么必须一次加锁完成：原先"判超限"（_login_throttle_ok）与"记失败"
    （_record_login_failure）是两次独立加锁，N 个并发请求会同时看到"还没到上限"、
    各自记一次才超限——实际允许的尝试次数变成 上限 + N - 1，而那正是暴力破解最想要的
    窗口。开发服务器默认多线程，"并发请求恰好同时到达"不是理论问题。
    """
    now = _now()
    with _login_lock:
        stamps = _prune_locked(ip, now)
        if len(stamps) >= LOGIN_MAX_ATTEMPTS:
            return _retry_after_locked(stamps, now)
        stamps.append(now)
        _login_failures[ip] = stamps
        _enforce_table_limit_locked(now)
        return 0


def _login_throttle_ok(ip: str) -> bool:
    """该 IP 是否仍允许尝试登录（只读查询；真正的占用走 _register_login_attempt）"""
    now = _now()
    with _login_lock:
        return len(_prune_locked(ip, now)) < LOGIN_MAX_ATTEMPTS


def _login_retry_after(ip: str) -> int:
    """被限流时还要等多少秒（只读查询）"""
    now = _now()
    with _login_lock:
        return _retry_after_locked(_prune_locked(ip, now), now)


def _record_login_failure(ip: str) -> None:
    """记一次失败（保持既有语义：调用方已经自行判过超限）"""
    now = _now()
    with _login_lock:
        stamps = _prune_locked(ip, now)
        stamps.append(now)
        _login_failures[ip] = stamps
        _enforce_table_limit_locked(now)


def _clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


def add_security_headers(response):
    """统一安全响应头。

    前端把 AI 输出与聊天内容渲染进 DOM 时都做了 esc() 转义，这里再给浏览器一层
    默认约束：即便将来某个渲染路径漏了转义，注入也拿不到跨站资源。
    内联脚本/样式是这个项目的既有写法（主题初始化、ECharts 配置、模板内 onclick），
    所以 script-src/style-src 必须放行 'unsafe-inline'；但 default-src 仍锁死同源。
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # 用 SAMEORIGIN 而非 DENY：只挡跨站被框（防点击劫持），
    # 不阻断用户自己在同源页面里嵌入（例如把仪表盘放进自己的本地面板）
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self'",
    )
    return response


def _is_loopback() -> bool:
    """绑定地址是否为本机回环（与 app._is_loopback 同一口径）"""
    return (FLASK_HOST or "").strip().lower() in ("127.0.0.1", "localhost", "::1")


def wants_json() -> bool:
    """该请求是否期望 JSON 响应（AJAX / fetch）。

    同一个口径服务两处：上传接口回 302 还是回 JSON，以及**会话过期时该回 401
    还是回 302**。后者必须是 401——浏览器里的 jQuery 会静默跟随 302 去拿登录页，
    于是"请求成功、返回了一页 HTML"：发起分析的那条路径会报"未知响应格式"，
    轮询任务的那条路径读到 `s.status === undefined` 便继续排下一次轮询，
    **静默无限轮询**下去。两种症状都像"服务坏了"，真实原因只是会话过期。

    `/api/` 前缀一律算期望 JSON，不依赖客户端有没有声明——脚本调用方常常不带
    Accept，而它们最需要的是一个能判定的状态码。
    """
    if request.path.startswith("/api/"):
        return True
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in (
        request.headers.get("Accept") or ""
    )


def require_login():
    """未登录时重定向到登录页；未设置口令则不启用（仅限回环绑定）"""
    if not ACCESS_PASSWORD:
        # 未设口令 = 不启用登录，但**只在回环绑定下成立**：绑定到非回环又没口令，
        # 等于同网段任何人都能读到聊天分析与 /report。这里失败关闭，绝不敞开放行。
        # CLI 路径（qqchatlog / python app.py）在启动时已被 _startup_report 拦住，
        # 这条兜住的是绕过 main() 的入口（如 gunicorn app:app）。
        if not _is_loopback():
            logger.error(
                "绑定非回环地址（%s）却未设置 ACCESS_PASSWORD：拒绝所有请求。请设置口令，或改回 127.0.0.1。",
                FLASK_HOST,
            )
            abort(503, description="非回环绑定必须设置 ACCESS_PASSWORD")
        return None
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint in BYPASS_ENDPOINTS:
        return None
    if session.get("auth_ok"):
        return None
    if wants_json():
        logger.info("API 请求遇到过期会话，回 401: %s %s", request.method, request.path)
        return (
            jsonify(
                {
                    "error": "会话已过期（长期未操作会被自动回收），请刷新页面（F5）重新登录",
                    "auth": False,
                }
            ),
            401,
        )
    return redirect(url_for("login", next=request.path or "/"))


def _regenerate_session_id() -> None:
    """认证状态切换后轮换服务端 sid（须在 session 写入新键后调用，避免空 session 跳过 regenerate）。"""
    try:
        current_app.session_interface.regenerate(session)
    except AttributeError:
        logger.warning("当前会话后端不支持 session id 轮换，会话固定防护已降级")


def login():
    """口令登录页"""
    if not ACCESS_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not _origin_allowed():
            return "非法来源", 403
        ip = request.remote_addr or "127.0.0.1"
        wait = _register_login_attempt(ip)
        if wait:
            logger.warning("登录尝试过于频繁，暂时拒绝 [%s]（还需 %d 秒）", ip, wait)
            resp = make_response(
                render_template("login.html", error=f"尝试次数过多，请 {wait} 秒后再试"), 429
            )
            resp.headers["Retry-After"] = str(wait)
            return resp
        pwd = request.form.get("password", "")
        if hmac.compare_digest(pwd.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
            # 登录成功后重置内容、CSRF token 并轮换 sid（须在写入 session 之后调用）
            session.clear()
            session["auth_ok"] = True
            session["csrf_token"] = secrets.token_hex(32)
            _regenerate_session_id()
            _clear_login_failures(ip)
            nxt = request.args.get("next") or ""
            # 反斜杠先归一，防止 `/\\evil.com` 绕过协议相对 URL 检查
            safe_next = (nxt or "").strip().replace("\\", "/")
            parsed_next = urlparse(safe_next)
            if (
                not safe_next.startswith("/")
                or safe_next.startswith("//")
                or parsed_next.scheme
                or parsed_next.netloc
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in safe_next)
            ):
                safe_next = url_for("index")
            logger.info("登录成功 [%s]", ip)
            return redirect(safe_next)
        # 失败计数已在 _register_login_attempt 记录
        error = "口令错误，请重试"
        logger.warning("登录失败 [%s]", ip)
    return render_template("login.html", error=error)


def logout():
    """退出登录（仅 POST 且校验 Origin/CSRF）：清空并轮换作废旧 sid 存储，不重置限流计数。"""
    guard = _guard_post()
    if guard:
        return guard

    ip = request.remote_addr or "127.0.0.1"
    # 立即释放当前会话对缓存的活跃引用，避免继续阻塞清理
    from webapp import store

    store.forget_live_chat(session.get("chat_hash", "") or "", getattr(session, "sid", "") or "")
    session.clear()
    # regenerate() 内部用 `if session:` 判空，而只剩 _permanent 的空会话是 falsy：
    # 不先放一份内容，轮换会被静默跳过（登录路径踩过同一个坑，见 _regenerate_session_id）。
    # 放 CSRF token 而不是别的标记：登出之后的登录页本来就需要一份新的。
    session["csrf_token"] = secrets.token_hex(32)
    _regenerate_session_id()
    logger.info("已退出登录 [%s]", ip)
    return redirect(url_for("index"))


def register(app):
    """把防护挂钩到 Flask 实例（保持原始注册顺序）"""
    app.before_request(ensure_csrf_token)
    app.before_request(require_login)
    app.after_request(add_security_headers)
    app.context_processor(inject_csrf_token)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout, methods=["POST"])
