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
# -*- coding: utf-8 -*-
"""会话后端自检：必须是 cachelib 后端，且不再碰 flask-session 的弃用配置

flask-session 0.8 起，`SESSION_TYPE="filesystem"` 与其配套的 `SESSION_FILE_DIR` /
`SESSION_FILE_THRESHOLD` / `SESSION_FILE_MODE`、以及 `SESSION_USE_SIGNER` 全部标记弃用，
官方替代是"把 cachelib 实例交给 SESSION_CACHELIB"。我们当前把 Flask-Session 精确锁在
0.8.0，所以告警不会立刻变成故障，但**下一次升级 Flask-Session 时旧接口会被移除**——
这层测试把"已经迁移完毕"钉住，避免以后又被改回去：

1. 配置用的是 cachelib 后端，且实例指向数据目录；
2. 弃用配置项一个都不在 app.config 里；
3. 导入 app 时不再出现来自 flask_session 的 DeprecationWarning（子进程验证，
   因为进程内 app 早已导入）；
4. 会话真的能写进那个目录、并且能读回来。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import io
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from cachelib import FileSystemCache  # noqa: E402

import app as appmod  # noqa: E402
import config as configmod  # noqa: E402
from webapp import security as securitymod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
#: 被迁移掉的弃用配置项：只要重新出现就说明有人把它们加回来了
DEPRECATED_KEYS = ("SESSION_FILE_DIR", "SESSION_FILE_THRESHOLD", "SESSION_FILE_MODE", "SESSION_USE_SIGNER")


class TestSessionBackend(unittest.TestCase):
    def test_uses_cachelib_backend_pointing_at_data_dir(self):
        self.assertEqual(appmod.app.config["SESSION_TYPE"], "cachelib")
        cache = appmod.app.config["SESSION_CACHELIB"]
        self.assertIsInstance(cache, FileSystemCache)
        # cachelib 没有公开的目录访问器，只能读私有 _path：这里只是确认"会话目录仍是数据目录下的
        # flask_session/"（清理逻辑按那个目录回收），真正的行为验证是下面的落盘用例。
        cache_dir = Path(getattr(cache, "_path", "") or "")
        self.assertEqual(
            cache_dir.resolve(),
            Path(configmod.SESSION_FILE_DIR).resolve(),
            "会话目录必须还是数据目录下的 flask_session/（清理逻辑按这个目录回收）",
        )

    def test_deprecated_session_knobs_are_gone(self):
        for key in DEPRECATED_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, appmod.app.config, f"{key} 已弃用，不要再用")

    def test_cookie_flags_kept(self):
        """迁移不能顺手把 cookie 防护丢了"""
        self.assertTrue(appmod.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(appmod.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertFalse(appmod.app.config["SESSION_PERMANENT"])

    def test_import_emits_no_flask_session_deprecation(self):
        """子进程里导入 app 并收集告警：来自 flask_session 的弃用告警必须为 0"""
        code = (
            "import warnings\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    import app  # noqa: F401\n"
            "    hits = [str(w.message) for w in caught\n"
            "            if 'flask_session' in str(w.filename) or 'use_signer' in str(w.message)]\n"
            "print('HITS=' + repr(hits))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": str(ROOT), "QQCHAT_DATA_DIR": tmp}
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=tmp, env=env, capture_output=True, text=True, timeout=180
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HITS=[]", result.stdout, f"仍在触发 flask-session 弃用告警: {result.stdout.strip()}")

    def test_session_round_trip_persists_on_disk(self):
        """真写一次会话：读得回来，而且数据目录里确实落了文件"""
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            sess["session_backend_probe"] = "v1"
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("session_backend_probe"), "v1")
        files = [p for p in Path(configmod.SESSION_FILE_DIR).glob("*") if p.is_file()]
        self.assertTrue(files, f"会话没有落到 {configmod.SESSION_FILE_DIR}")


class TestLoginRotatesSession(unittest.TestCase):
    """登录成功后轮换 CSRF token：认证态与匿名态不共用同一个 token"""

    def test_csrf_token_changes_after_login(self):
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            before = sess.get("csrf_token")
        self.assertTrue(before)
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            after = sess.get("csrf_token")
            self.assertTrue(sess.get("auth_ok"))
        self.assertNotEqual(before, after, "登录后 CSRF token 应轮换")


class TestSessionIdRotation(unittest.TestCase):
    """会话固定：认证状态升级时必须换一个 sid，且旧 sid 立刻失效"""

    def setUp(self):
        # 测试客户端走 http，Secure cookie 不会被回传——固定关掉以保证用例确定性
        self._secure = appmod.app.config["SESSION_COOKIE_SECURE"]
        appmod.app.config["SESSION_COOKIE_SECURE"] = False
        self.addCleanup(appmod.app.config.__setitem__, "SESSION_COOKIE_SECURE", self._secure)
        patcher = mock.patch.object(securitymod, "ACCESS_PASSWORD", "pw-under-test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sid_changes_after_successful_login(self):
        client = appmod.app.test_client()
        client.get("/")
        before = client.get_cookie("session")
        self.assertIsNotNone(before, "匿名访问后应当已经下发 session cookie")

        resp = client.post("/login", data={"password": "pw-under-test"})
        self.assertEqual(resp.status_code, 302)

        after = client.get_cookie("session")
        self.assertIsNotNone(after)
        self.assertNotEqual(before.value, after.value, "登录成功后必须轮换 session id（否则可被会话固定）")

    def test_old_sid_loses_access_after_login(self):
        """旧 sid 上不该带着"已登录"状态：换个客户端拿旧 sid 访问应被弹回登录页"""
        client = appmod.app.test_client()
        client.get("/")
        old_sid = client.get_cookie("session").value
        client.post("/login", data={"password": "pw-under-test"})

        attacker = appmod.app.test_client()
        attacker.set_cookie("session", old_sid, domain="localhost")
        resp = attacker.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

    def test_login_still_works_after_rotation(self):
        """轮换不能把新会话弄丢：登录后应能正常访问受保护页面"""
        client = appmod.app.test_client()
        client.get("/")
        client.post("/login", data={"password": "pw-under-test"})
        with client.session_transaction() as sess:
            self.assertTrue(sess.get("auth_ok"))
            self.assertTrue(sess.get("csrf_token"))


class TestSecureCookiePolicy(unittest.TestCase):
    """Secure cookie：回环不过网所以关掉，非回环默认要求 HTTPS，可显式覆盖"""

    def _enabled(self, host: str, mode: str) -> bool:
        with mock.patch.object(appmod, "FLASK_HOST", host), mock.patch.object(appmod, "COOKIE_SECURE", mode):
            return appmod._secure_cookie_enabled()

    def test_loopback_defaults_to_insecure(self):
        """回环绑定：恒 Secure 会让本机 http 访问直接登不上去，所以 auto 必须是 False"""
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertFalse(self._enabled(host, "auto"))

    def test_non_loopback_defaults_to_secure(self):
        """绑定到局域网/公网：明文 http 会让会话 id 裸奔，默认必须要求 HTTPS"""
        for host in ("0.0.0.0", "192.168.1.5", "10.0.0.7"):
            with self.subTest(host=host):
                self.assertTrue(self._enabled(host, "auto"))

    def test_explicit_override_wins(self):
        self.assertTrue(self._enabled("127.0.0.1", "true"))
        self.assertFalse(self._enabled("0.0.0.0", "false"))

    def test_app_config_matches_policy(self):
        """组装出来的 app 必须真的带上这个配置（不能只在 helper 里算）"""
        self.assertEqual(appmod.app.config["SESSION_COOKIE_SECURE"], appmod._secure_cookie_enabled())

    def test_invalid_mode_falls_back_to_auto(self):
        """拼错的值不能让"非回环要求 HTTPS"这条防线悄悄消失"""
        import config as configmod

        with mock.patch.dict(os.environ, {"QQCHAT_COOKIE_SECURE": "banana"}):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(
                    configmod.env_choice("QQCHAT_COOKIE_SECURE", "auto", ("auto", "true", "false")),
                    "auto",
                )
        self.assertIn("QQCHAT_COOKIE_SECURE", err.getvalue())
        with (
            mock.patch.object(appmod, "COOKIE_SECURE", "auto"),
            mock.patch.object(appmod, "FLASK_HOST", "0.0.0.0"),
        ):
            self.assertTrue(appmod._secure_cookie_enabled())


if __name__ == "__main__":
    unittest.main()
