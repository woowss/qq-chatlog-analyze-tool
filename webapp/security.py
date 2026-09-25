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

#: 完全不参与中间件链路的端点（存活探针）。它会被反代/容器编排高频调用：
#: 不该建会话（每次探针都写一个 session 文件纯属浪费）、不该要求登录（设了口令后
#: 探针永远被 302 到登录页，等于没有探针）、也不该写日志与触发清理。
#: 消费点：ensure_csrf_token / require_login / views.log_request / cleanup.register。
BYPASS_ENDPOINTS = frozenset({"health"})

# 登录失败限流：同一 IP 在滑动窗口内失败达到上限后暂时拒绝，避免绑定局域网时被爆破。
# 上限与窗口来自 config（`QQCHAT_LOGIN_MAX_ATTEMPTS` / `QQCHAT_LOGIN_WINDOW_SECONDS`），
# 之所以可调：计数器按 remote_addr 计，反代或 NAT 之后所有请求共享一个地址，
# 一个人连续输错会把所有人一起锁住（见 config.py 里那段注释）。
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
#: 失败记录表的硬上限。窗口内的失败才计数，但扫描流量可以伪造大量**不同**地址，
#: 只按"过期"清理的话表会一直涨；超过上限就按最近一次失败时间淘汰最旧的。
_LOGIN_FAILURES_MAX = 1000


def _now() -> float:
    """当前时间（抽成函数，方便用例控制时间——不必真睡满一个窗口）"""
    return time.time()


# ---------------------------------------------------------------------------
# CSRF / Origin
# ---------------------------------------------------------------------------


def ensure_csrf_token():
    """确保会话中存在 CSRF token（session 服务端存储，攻击者无法读取）"""
    if request.endpoint in BYPASS_ENDPOINTS:
        return None  # 探针不建会话：否则每次健康检查都在磁盘上留一个 session 文件
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


def inject_csrf_token():
    """向所有模板注入 csrf_token，供表单与 AJAX 请求使用

    顺带注入 auth_enabled：导航栏只应在**真的设了口令**时才渲染"退出登录"。
    没设口令时模板不输出任何东西，于是各页面的渲染结果与加这个按钮之前逐字节一致
    （私聊 8 页有对照用例钉着）。
    """
    return {"csrf_token": session.get("csrf_token", ""), "auth_enabled": bool(ACCESS_PASSWORD)}


def _check_csrf() -> bool:
    """校验请求携带的 CSRF token 与会话一致

    注意：secrets.compare_digest 对含非 ASCII 字符的 str 会抛 TypeError，
    而 token 来自请求方（可被任意构造），必须先落到 bytes 再比较，
    否则一个中文 token 就能把 POST 打成 500。
    """
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("csrf_token", "")
    if not expected or not provided:
        return False
    try:
        return secrets.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except (UnicodeError, AttributeError, TypeError):
        return False


def _csrf_error_response():
    """CSRF 校验失败时给用户可执行的提示。

    这里最常见的原因不是攻击，而是**会话文件被 24 小时回收**（清理任务按 mtime
    回收 flask_session/，而只读浏览不会刷新 mtime）：用户隔天回来点上传/分析，
    旧页面带的 token 对不上新会话，原先只回一句"CSRF 校验失败"，无从下手。
    """
    if not session.get("csrf_token"):
        msg = "CSRF 校验失败：会话已过期（长期未操作会被自动回收），请刷新页面（F5）后重试"
    else:
        msg = "CSRF 校验失败：请求缺少或携带了错误的 token，请刷新页面（F5）后重试"
    logger.warning("CSRF 校验失败: %s %s", request.method, request.path)
    return msg, 400


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


def _login_throttle_ok(ip: str) -> bool:
    """该 IP 是否仍允许尝试登录（只统计失败次数，成功即清零）"""
    now = _now()
    with _login_lock:
        stamps = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        if stamps:
            _login_failures[ip] = stamps
        else:
            _login_failures.pop(ip, None)
        return len(stamps) < LOGIN_MAX_ATTEMPTS


def _login_retry_after(ip: str) -> int:
    """被限流时还要等多少秒（喂给 429 的 Retry-After 与页面提示）"""
    now = _now()
    with _login_lock:
        stamps = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if len(stamps) < LOGIN_MAX_ATTEMPTS:
        return 0
    # 最早那次失败滑出窗口时，窗口内只剩 MAX-1 次，限流自动解除
    return max(1, int(LOGIN_WINDOW_SECONDS - (now - min(stamps))) + 1)


def _record_login_failure(ip: str) -> None:
    now = _now()
    with _login_lock:
        _login_failures.setdefault(ip, []).append(now)
        if len(_login_failures) > _LOGIN_FAILURES_MAX:
            # 先清过期条目；仍然超限说明失败来自大量**新鲜**地址（例如端口扫描），
            # 就按最近一次失败时间淘汰最旧的，保证这张表有硬上限。
            for key in [k for k, v in _login_failures.items() if not v or now - v[-1] > LOGIN_WINDOW_SECONDS]:
                _login_failures.pop(key, None)
            overflow = len(_login_failures) - _LOGIN_FAILURES_MAX
            if overflow > 0:
                for key in sorted(_login_failures, key=lambda k: _login_failures[k][-1])[:overflow]:
                    _login_failures.pop(key, None)


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
    """认证状态升级后轮换服务端 session id（防御会话固定）。

    调用时机有硬要求：必须**在会话已经写入内容之后**。flask-session 的
    ``regenerate()`` 内部用 ``if session:`` 做前置判断，而只剩 ``_permanent``
    的空会话在 ``ServerSideSession.__bool__`` 下是 falsy——放在 session.clear()
    之后、写入 auth_ok 之前调用会被静默跳过，看起来"轮换过了"其实没有。

    为什么必须轮换：``session.clear()`` 只清空服务端**内容**，浏览器 cookie 里的
    sid 原样不变。攻击者若能预先固定住受害者的 sid（明文 HTTP 嗅探、或诱导受害者
    点击带 Set-Cookie 的响应），该 sid 一旦登录就直接是已认证状态。
    ``regenerate()`` 会删掉旧 sid 的存储、生成新 sid 并置 ``modified=True``，
    于是响应会重下 cookie——旧 sid 随即失效。
    """
    try:
        current_app.session_interface.regenerate(session)
    except AttributeError:
        # 换用不支持 regenerate 的会话后端（或更老的 flask-session）时降级：
        # 内容与 CSRF token 已轮换，只是 sid 复用——记一条便于排查。
        logger.warning("当前会话后端不支持 session id 轮换，会话固定防护已降级")


def login():
    """口令登录页"""
    if not ACCESS_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "127.0.0.1"
        if not _login_throttle_ok(ip):
            wait = _login_retry_after(ip)
            logger.warning("登录尝试过于频繁，暂时拒绝 [%s]（还需 %d 秒）", ip, max(1, wait))
            # Retry-After 是给脚本/客户端看的（浏览器会忽略）；页面提示用同一份秒数，
            # 免得用户只看到"请稍后再试"却不知道要等多久。
            resp = make_response(
                render_template("login.html", error=f"尝试次数过多，请 {max(1, wait)} 秒后再试"), 429
            )
            resp.headers["Retry-After"] = str(max(1, wait))
            return resp
        # 登录接口自身豁免 CSRF（无 session 时先建 token）
        pwd = request.form.get("password", "")
        if hmac.compare_digest(pwd.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
            # 登录成功即换一份会话内容：丢弃匿名阶段的残留，并轮换 CSRF token 与 sid
            session.clear()
            session["auth_ok"] = True
            session["csrf_token"] = secrets.token_hex(32)
            # 必须排在写入 auth_ok/CSRF 之后：regenerate() 用 `if session:` 判空，
            # 空会话会被它静默跳过（详见函数注释）。
            _regenerate_session_id()
            _clear_login_failures(ip)
            nxt = request.args.get("next") or ""
            # 防开放重定向：反斜杠先归一为斜杠再判断。浏览器把 `/\evil.com` 当作
            # 协议相对地址（等价于 //evil.com），只查 "//" 会被这样绕过。
            safe_next = nxt.replace("\\", "/")
            if not safe_next.startswith("/") or safe_next.startswith("//"):
                safe_next = url_for("index")
            logger.info("登录成功 [%s]", ip)
            return redirect(safe_next)
        _record_login_failure(ip)
        error = "口令错误，请重试"
        logger.warning("登录失败 [%s]", ip)
    return render_template("login.html", error=error)


def logout():
    """退出登录：清空服务端会话内容，并让浏览器丢弃会话 cookie。

    只接受 POST 且必须过 `_guard_post()`（Origin + CSRF）：

    - 不接受 GET，因为登出是**改状态**的操作，而任意第三方页面都能用
      `<img src="http://127.0.0.1:5000/logout">` 触发它（登出 CSRF 危害有限，
      但本项目的口径是"改状态就过 _guard_post"，这里不开口子）；
    - `session.clear()` 只清了服务端内容，浏览器上的 sid 还在，所以显式
      `delete_cookie`。会话文件仍会被 24 小时回收，但那不是用户能依赖的时点。

    注意这里**不**清登录失败限流的计数：登出本身与该地址的失败历史无关。
    """
    guard = _guard_post()
    if guard:
        return guard

    ip = request.remote_addr or "127.0.0.1"
    session.clear()
    logger.info("已退出登录 [%s]", ip)
    response = redirect(url_for("index"))
    response.delete_cookie(current_app.config.get("SESSION_COOKIE_NAME") or "session")
    return response


def register(app):
    """把防护挂钩到 Flask 实例（保持原始注册顺序）"""
    app.before_request(ensure_csrf_token)
    app.before_request(require_login)
    app.after_request(add_security_headers)
    app.context_processor(inject_csrf_token)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout, methods=["POST"])
