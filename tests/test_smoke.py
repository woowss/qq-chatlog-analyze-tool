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
"""页面冒烟测试：上传一份聊天记录后逐页渲染，确保没有 500 / 模板异常 / emoji 回潮

这一层原来靠每次手工跑脚本验证，现在固化成测试，改模板或改路由都会立刻暴露问题。
"""
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

# 测试隔离：数据目录指向临时目录，绝不碰真实 uploads/ai_cache/session
import tempfile as _tempfile
os.environ.setdefault("QQCHAT_DATA_DIR", _tempfile.mkdtemp(prefix="qqchatlog-test-"))
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def _emoji_count(text: str) -> int:
    return sum(1 for ch in text
               if 0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF
               or 0x2B00 <= ord(ch) <= 0x2BFF or 0xFE0F == ord(ch))


class TestPageSmoke(unittest.TestCase):
    """逐页渲染：状态码、无异常堆栈、无 emoji、导航与主题脚本存在"""

    @classmethod
    def setUpClass(cls):
        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.token = sess["csrf_token"]
        cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": cls.token}

        base = int(time.time() * 1000) - 30 * 86400_000
        msgs = []
        for i in range(40):
            who = "u_self" if i % 2 else "u_other"
            msgs.append({
                "id": str(i), "timestamp": base + i * 3600_000,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((base + i * 3600_000) / 1000)),
                "sender": {"uid": who, "name": "我" if who == "u_self" else "对方"},
                "content": ["在吗", "在的", "今天好累", "早点睡", "晚安"][i % 5],
            })
        payload = json.dumps({
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}],
                           "totalMessages": len(msgs)},
            "messages": msgs,
        }, ensure_ascii=False).encode("utf-8")
        r = cls.client.post("/upload", data={"file": (io.BytesIO(payload), "chat.json")},
                            headers=cls.headers)
        assert r.status_code == 302, f"上传失败: {r.status_code}"
        with cls.client.session_transaction() as sess:
            cls.filepath = sess.get("filepath")
            cls.chat_hash = sess.get("chat_hash")

    @classmethod
    def tearDownClass(cls):
        if cls.filepath and os.path.exists(cls.filepath):
            os.remove(cls.filepath)
        appmod._purge_chat_caches(cls.chat_hash)

    def test_pages_render_clean(self):
        for path in ("/", "/dashboard", "/emotion", "/relationship",
                     "/habits", "/topics", "/profile", "/report"):
            with self.subTest(page=path):
                r = self.client.get(path)
                body = r.get_data(as_text=True)
                self.assertEqual(r.status_code, 200, f"{path} 返回 {r.status_code}")
                self.assertNotIn("Traceback", body)
                self.assertEqual(_emoji_count(body), 0, f"{path} 出现了 emoji")
                self.assertIn("data-theme", body)          # 主题脚本

    def test_api_endpoints(self):
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        usage = self.client.get("/api/usage").get_json()
        self.assertIn("total", usage)
        self.assertIn("cost", usage["total"], "用量接口应带费用估算")
        self.assertEqual(self.client.get("/api/analysis/emotion").status_code, 404)  # 尚未分析
        self.assertEqual(self.client.post("/api/analyze/notadim", headers=self.headers).status_code, 400)

    def test_navigation_visible_after_upload(self):
        body = self.client.get("/dashboard").get_data(as_text=True)
        for label in ("仪表盘", "情绪", "关系", "习惯", "话题", "锐评", "报告"):
            self.assertIn(label, body, f"导航缺少 {label}")

    def test_missing_session_redirects_home(self):
        fresh = appmod.app.test_client()
        fresh.get("/")
        r = fresh.get("/dashboard")
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
