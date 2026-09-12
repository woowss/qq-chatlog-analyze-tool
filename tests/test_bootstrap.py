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
"""测试自身两条保证的回归测试（tests/_bootstrap.py）

1. 数据目录不依赖系统临时目录可写：%TEMP% 不可用时退到仓库内，而不是直接把测试卡死在
   PermissionError 上。
2. 未配置 API Key 时，任何真实 LLM 客户端入口都被哨兵切断——**在 unittest 下也要生效**。
   这条曾经只由 tests/conftest.py（pytest fixture）提供，而 CI 跑的是 unittest，
   于是护栏在 CI 上等于不存在。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap
from _bootstrap import BlockedLlmCall, bootstrap

bootstrap()

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


class TestDataDirBootstrap(unittest.TestCase):
    """数据目录的创建必须在 %TEMP% 不可用时也能成功"""

    def test_falls_back_inside_repo_when_system_temp_unusable(self):
        """系统临时目录建不出来时，退到仓库内 tests/.tmp_baseline/"""
        real_mkdtemp = tempfile.mkdtemp

        def fake_mkdtemp(prefix=None, dir=None):
            # 只在"没指定目录"（= 想用系统临时目录）时失败；退路本身必须真的能建
            if dir is None:
                raise PermissionError("模拟只读 %TEMP%")
            return real_mkdtemp(prefix=prefix, dir=dir)

        with mock.patch.object(tempfile, "mkdtemp", side_effect=fake_mkdtemp):
            data_dir = _bootstrap._make_data_dir()

        self.addCleanup(shutil.rmtree, data_dir, True)
        self.assertIn("tmp_baseline", data_dir, "应当退到仓库内的 .tmp_baseline")
        self.assertTrue(Path(data_dir).is_dir())
        self.assertTrue(os.access(data_dir, os.W_OK), "退路目录必须真的可写")

    def test_bootstrap_is_idempotent_and_usable(self):
        """重复调用不换目录：16 个测试文件各调一次，必须指向同一个目录"""
        first = bootstrap()
        second = bootstrap()
        self.assertEqual(first, second)
        self.assertEqual(os.environ["QQCHAT_DATA_DIR"], first)
        self.assertTrue(Path(first, "tmp").is_dir(), "tempfile 根应指向数据目录下的 tmp/")
        self.assertTrue(os.access(first, os.W_OK))


class TestLlmNetworkGuard(unittest.TestCase):
    """真实 LLM 调用护栏：unittest 下同样要拦住没有 Key 的出网"""

    def test_guard_blocks_client_when_no_key_configured(self):
        """没有配置 Key 时，_get_client 必须是"一调用就炸"的哨兵

        护栏只在"未配置 Key"时安装（配置了真 Key 是本机排查用的合法场景，见 _bootstrap），
        所以本机配了真 Key 时这条不负责拦截。

        "是谁装的"不能写死：pytest 下 conftest.py 的 autouse fixture 也会换掉 `_get_client`
        ——那是比本模块更早的一道护栏，谁先装都算拦住了，不该因此判红。
        """
        from analyzer import deepseek_client as dc

        if dc.is_api_configured():
            self.skipTest("本机已配置真实 API Key：护栏按设计不安装")

        with self.assertRaises(AssertionError):  # 任一护栏都必须让调用响亮失败
            dc._get_client()
        if dc._get_client is _bootstrap._forbidden_client:
            # 本模块的护栏（unittest 跑法）：断言到具体异常类型与提示
            with self.assertRaises(BlockedLlmCall):
                dc._get_client()

    def test_guard_installed_in_fresh_interpreter(self):
        """干净子进程（无 Key）里，护栏必须自动生效

        这条正是 CI 的姿势：`python -m unittest` + 没有 Key。以前 conftest.py 的
        pytest fixture 在这种跑法下根本不加载，于是本机配了 Key 就可能真的出网花钱。

        为什么路径在子进程里现算（而不是 f-string 塞进去）：`python -c` 会把 cwd 放进
        sys.path[0]，一旦子进程 cwd 落在仓库内，`import config` 就会拿到**仓库里的**
        config.py —— 它按文件位置 load_dotenv()，于是开发机的真 Key 又回来了，
        这个"无 Key"用例反而会读到 Key 而失败。把 cwd 显式放到 sys.path 最前并清掉
        PYTHONPATH，才是真正干净的"无 Key"环境。

        还要把 `DEEPSEEK_API_KEY` 从子进程环境里摘掉：本进程一旦 import 过 config，
        load_dotenv() 就把 .env 读进了 os.environ，子进程会原样继承——那样"无 Key"只是
        自我感觉良好（这两个坑各踩了一次，才写成现在这样）。
        """
        probe = (
            "import os, sys\n"
            "outside = os.getcwd()\n"
            "sys.path.insert(0, outside)\n"
            "sys.path.insert(0, r'{tests}')\n"
            "from _bootstrap import bootstrap, _forbidden_client\n"
            "bootstrap()\n"
            "from analyzer import deepseek_client as dc\n"
            "assert dc.__file__.startswith(r'{repo}'), '导到了别处的 deepseek_client'\n"
            "assert not dc.is_api_configured(), '子进程里不该有真 Key'\n"
            "assert dc._get_client is _forbidden_client, '护栏没装上'\n"
            "print('GUARD-ACTIVE')\n"
        ).format(tests=str(TESTS_DIR), repo=str(REPO_ROOT))

        with tempfile.TemporaryDirectory() as outside:
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("QQCHAT_DATA_DIR", "PYTHONPATH", "DEEPSEEK_API_KEY")
            }
            env["PYTHONIOENCODING"] = "utf-8"
            env["QQCHAT_DATA_DIR"] = outside  # 仓库外：那里没有 .env，子进程必然无 Key
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=outside,
                env=env,
                capture_output=True,
                # 必须显式指定编码：子进程输出中文，而 Windows 下 text=True 默认按 GBK 解码，
                # 于是读取线程抛 UnicodeDecodeError，stdout 直接变成空串（比断言失败更难查）。
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        self.assertIn("GUARD-ACTIVE", result.stdout, f"stdout={result.stdout!r} stderr={result.stderr!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
