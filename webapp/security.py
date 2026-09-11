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

from flask import redirect, render_template, request, session, url_for

from config import ACCESS_PASSWORD, ALLOWED_ORIGINS, FLASK_HOST
from analyzer.logger import get_logger

logger = get_logger("app")

PUBLIC_ENDPOINTS = {"login", "static"}

# 登录失败限流：同一 IP 在滑动窗口内失败达到上限后暂时拒绝，避免绑定局域网时被爆破
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CSRF / Origin
# ---------------------------------------------------------------------------


def ensure_csrf_token():
    """确保会话中存在 CSRF token（session 服务端存储，攻击者无法读取）"""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


def inject_csrf_token():
    """向所有模板注入 csrf_token，供表单与 AJAX 请求使用"""
    return {"csrf_token": session.get("csrf_token", "")}


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


def require_login():
    """未登录时重定向到登录页；未设置口令则不启用"""
    if not ACCESS_PASSWORD:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS or session.get("auth_ok"):
        return None
    return redirect(url_for("login", next=request.path or "/"))


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


def register(app):
    """把防护挂钩到 Flask 实例（保持原始注册顺序）"""
    app.before_request(ensure_csrf_token)
    app.before_request(require_login)
    app.context_processor(inject_csrf_token)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
