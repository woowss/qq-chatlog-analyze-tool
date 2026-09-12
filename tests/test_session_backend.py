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

from cachelib import FileSystemCache

import app as appmod
import config as configmod

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
        self.assertEqual(cache_dir.resolve(), Path(configmod.SESSION_FILE_DIR).resolve(),
                         "会话目录必须还是数据目录下的 flask_session/（清理逻辑按这个目录回收）")

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
            result = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=180)
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


if __name__ == "__main__":
    unittest.main()
