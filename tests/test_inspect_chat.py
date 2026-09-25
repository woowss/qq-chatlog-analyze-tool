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
"""tools/inspect_chat.py 的 CLI 契约：能体检、能识别群聊、坏文件给人话而不是堆栈。

这个工具此前完全没有用例覆盖，而它是"拿到一份格式不确定的导出先看一眼"的第一入口——
崩掉或给错退出码，用户只会以为导出文件坏了。所以这里按 CLI 的真实契约测（子进程 + 退出码），
而不是 import 它的内部函数。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import bootstrap  # noqa: E402

bootstrap()

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "inspect_chat.py"


def _run(path) -> subprocess.CompletedProcess:
    # PYTHONIOENCODING 是必需的：子进程往**管道**写中文时，Windows 上 Python 会退回
    # 控制台代码页（cp936），而这里按 utf-8 解码——不设它，输出会变成一串替换字符，
    # 断言中文字符串必然假红（不是工具的问题，是"用什么编码读子进程"的问题）。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(TOOL), str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )


def _private_export() -> dict:
    base = 1704067200000  # 2024-01-01 08:00 CST 基线（合成夹具口径）
    msgs = [
        {
            "id": str(i),
            "timestamp": base + i * 3600_000,
            "time": "2024-01-01 08:00:00",
            "sender": {"uid": "u_self" if i % 2 == 0 else "u_other", "name": "我" if i % 2 == 0 else "对方"},
            "content": {"text": f"第{i}条", "elements": []},
        }
        for i in range(10)
    ]
    return {
        "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我", "type": "friend"},
        "statistics": {
            "senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}],
            "totalMessages": len(msgs),
        },
        "messages": msgs,
    }


def _group_export() -> dict:
    base = 1704067200000
    speakers = [("u_self", "我"), ("u_a", "阿明"), ("u_b", "小美")]
    msgs = []
    for i in range(12):
        uid, name = speakers[i % 3]
        elements = [{"type": "at", "data": {"uid": "u_b"}}] if i % 4 == 0 else []
        msgs.append(
            {
                "id": str(i),
                "timestamp": base + i * 600_000,
                "time": "2024-01-01 08:00:00",
                "sender": {"uid": uid, "name": name},
                "content": {"text": f"群消息{i}", "elements": elements},
            }
        )
    return {
        "chatInfo": {"name": "测试群", "selfUid": "u_self", "selfName": "我", "type": "group"},
        "statistics": {"senders": [{"uid": u, "name": n} for u, n in speakers], "totalMessages": len(msgs)},
        "messages": msgs,
    }


class TestInspectChatCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write(self, name: str, payload) -> Path:
        path = self.tmp / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def test_valid_private_export_exits_zero_and_reports_sections(self):
        proc = _run(self._write("private.json", _private_export()))
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout[-400:])
        self.assertIn("【格式】", proc.stdout)
        self.assertIn("对账", proc.stdout)

    def test_group_export_is_recognized_as_group(self):
        proc = _run(self._write("group.json", _group_export()))
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout[-400:])
        self.assertIn("group", proc.stdout)

    def test_invalid_file_reports_and_exits_one_without_traceback(self):
        """反向验证：把 tools/inspect_chat.py 里那段"缺顶层字段就返回"的判断删掉，
        这条立刻变红（它会走"群聊拒收就临时放行再试"的分支，把同一个 ValueError
        再抛一次变成 Traceback）。"""
        proc = _run(self._write("bad.json", {"nope": True}))
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, out[-400:])
        self.assertEqual(proc.returncode, 1, out[-400:])
        self.assertIn("缺少顶层字段", proc.stdout)
        self.assertIn("chatInfo", proc.stdout)

    def test_missing_messages_only_is_also_reported(self):
        proc = _run(self._write("half.json", {"chatInfo": {"selfUid": "u_self", "selfName": "我"}}))
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, out[-400:])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("messages", proc.stdout)


if __name__ == "__main__":
    unittest.main()
