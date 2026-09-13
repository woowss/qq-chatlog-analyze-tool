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
"""群聊前端与集成测试（M3）：HTTP 层、离线、不联网

钉住四件事：
1. 群聊记录走的是**群聊模板**（七个页面都渲染成功、导航是群聊文案）；
2. 报告页仍带齐 vendor 的 SRI 常量（导出机制抽成 partial 后最容易漏的就是它）；
3. 私聊页面**不受影响**（导航仍是私聊文案，报告仍是私聊模板）；
4. 成本预提示按"月份 × 3 + 成员数"给出调用次数（群聊的计费结构与私聊不同，
   只显示"4 个维度"是不诚实的）。
"""

import base64
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import api_configured_patcher  # noqa: E402
from _bootstrap import bootstrap  # noqa: E402
from _stats import ensure_stats  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser.qq_parser as qp  # noqa: E402
from webapp import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "group_5p.json"


def _emoji_count(text: str) -> int:
    return sum(
        1
        for ch in text
        if 0x1F000 <= ord(ch) <= 0x1FAFF
        or 0x2600 <= ord(ch) <= 0x27BF
        or 0x2B00 <= ord(ch) <= 0x2BFF
        or ord(ch) == 0xFE0F
    )


class GroupClientMixin:
    """建一个已上传群聊 fixture 的测试客户端（闸门临时打开）"""

    @classmethod
    def _make_client(cls, group: bool):
        import app as appmod

        payload = None
        if group:
            payload = json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8")), ensure_ascii=False)
        else:
            base = 1740900000000
            msgs = [
                {
                    "id": str(i),
                    "timestamp": base + i * 3600_000,
                    "time": "2025-03-02 12:00:00",
                    "sender": {"uid": "u_self" if i % 2 else "u_other", "name": "我" if i % 2 else "对方"},
                    "content": ["在吗", "在的", "今天好累", "早点睡"][i % 4],
                }
                for i in range(40)
            ]
            payload = json.dumps(
                {
                    "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
                    "statistics": {
                        "senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}],
                        "totalMessages": len(msgs),
                    },
                    "messages": msgs,
                },
                ensure_ascii=False,
            )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        with mock.patch.object(qp, "GROUP_TRACK_READY", True):
            resp = client.post(
                "/upload",
                data={"file": (io.BytesIO(payload.encode("utf-8")), "chat.json")},
                headers=headers,
            )
        assert resp.status_code == 302, resp.status_code
        with client.session_transaction() as sess:
            chat_hash = sess.get("chat_hash")
            filepath = sess.get("filepath")
        # 页面渲染要求统计已落盘。后台线程是异步的，而 wait_for_stats 在没有线程在跑时
        # 会直接返回（同一份 fixture 的缓存可能刚被别的用例清掉），所以这里用 ensure_stats
        # 把"统计可用"变成同步保证，不赌线程时序。详见 tests/_stats.py。
        ensure_stats(filepath, chat_hash)
        return client, headers, chat_hash


PAGES = ("/", "/dashboard", "/emotion", "/relationship", "/habits", "/topics", "/profile", "/report")


class TestGroupPages(GroupClientMixin, unittest.TestCase):
    """群聊记录：七个页面都渲染成功，且用的是群聊模板"""

    @classmethod
    def setUpClass(cls):
        # 成本预估区块只在"已配置 API Key"时渲染：本机 .env 有真 Key、CI 没有。
        # 不显式打这个补丁，本组用例就只在本机绿（原因见 tests/_bootstrap.py）。
        cls._api_guard = api_configured_patcher()
        cls._api_guard.start()
        cls.addClassCleanup(cls._api_guard.stop)
        cls.client, cls.headers, cls.chat_hash = cls._make_client(group=True)

    @classmethod
    def tearDownClass(cls):
        store._purge_chat_caches(cls.chat_hash)

    def test_all_pages_render(self):
        for path in PAGES:
            with self.subTest(page=path):
                resp = self.client.get(path)
                body = resp.get_data(as_text=True)
                self.assertEqual(resp.status_code, 200, f"{path} 返回 {resp.status_code}")
                self.assertNotIn("Traceback", body)

    def test_group_nav_and_no_private_nav(self):
        body = self.client.get("/dashboard").get_data(as_text=True)
        for label in ("群仪表盘", "群情绪", "群关系", "成员活跃", "群话题", "成员画像", "群报告"):
            self.assertIn(label, body, f"群聊导航缺少 {label}")
        # 私聊文案不得出现（"习惯""锐评"是私聊专属标签）
        self.assertNotIn(">锐评<", body)
        self.assertNotIn(">习惯<", body)

    def test_dashboard_shows_group_sections_and_cost_plan(self):
        body = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("同时在线高峰", body)
        self.assertIn("互动热力矩阵", body)
        self.assertIn("成员活跃时段", body)
        self.assertIn("本次预计调用", body)
        # 5 人 fixture 跨 3 个月：3×3 + 5 = 14 次
        self.assertIn("<strong>14</strong>", body)
        for dim in ("group_dynamics", "group_topics", "group_emotion", "member_profiles"):
            self.assertIn(f'data-dim="{dim}"', body)

    def test_group_pages_load_group_charts(self):
        for path in ("/dashboard", "/relationship", "/habits", "/topics", "/emotion", "/profile", "/report"):
            with self.subTest(page=path):
                body = self.client.get(path).get_data(as_text=True)
                self.assertIn("js/group_charts.js", body, f"{path} 没加载群聊图表脚本")

    def test_no_emoji_in_templates(self):
        """模板里不得有 emoji（与私聊页面同一条规矩；数据里的 emoji 不算）"""
        for path in ("/dashboard", "/relationship", "/habits", "/topics", "/emotion", "/profile"):
            with self.subTest(page=path):
                body = self.client.get(path).get_data(as_text=True)
                self.assertEqual(_emoji_count(body), 0, f"{path} 的模板里出现了 emoji")

    def test_group_report_inlines_vendor_sri(self):
        """群报告也要能导出：vendor 的 SRI 常量必须在页面里（partial 抽出去后最易漏）"""
        body = self.client.get("/report").get_data(as_text=True)
        vendor = ROOT / "web" / "static" / "vendor"
        for path in sorted(p for p in vendor.iterdir() if p.suffix in (".css", ".js")):
            digest = base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()
            with self.subTest(vendor=path.name):
                self.assertIn(f"sha384-{digest}", body, f"{path.name} 的 SRI 常量不在群报告里")
        self.assertIn("crossorigin", body)
        # Jinja 的 tojson 默认把中文转成 \uXXXX 转义序列：断言要按转义后的形态写，
        # 否则"下载文件名带不带群名"这条检查会因为编码细节而恒假。
        escaped = json.dumps("QQ群聊分析报告", ensure_ascii=True)[1:-1]
        self.assertIn(escaped, body, "下载文件名没带上群名")

    def test_group_report_has_group_sections(self):
        body = self.client.get("/report").get_data(as_text=True)
        for marker in ("群概览", "成员活跃度", "互动关系图", "群里程碑", "成员画像"):
            self.assertIn(marker, body)


class TestPrivatePagesUnaffected(GroupClientMixin, unittest.TestCase):
    """私聊记录：导航、模板、报告都不受群聊改动影响"""

    @classmethod
    def setUpClass(cls):
        cls.client, cls.headers, cls.chat_hash = cls._make_client(group=False)

    @classmethod
    def tearDownClass(cls):
        store._purge_chat_caches(cls.chat_hash)

    def test_private_nav_labels(self):
        body = self.client.get("/dashboard").get_data(as_text=True)
        for label in ("仪表盘", "情绪", "关系", "习惯", "话题", "锐评", "报告"):
            self.assertIn(label, body)
        self.assertNotIn("群仪表盘", body)
        self.assertNotIn("成员画像", body)

    def test_private_pages_do_not_load_group_charts(self):
        for path in PAGES:
            with self.subTest(page=path):
                body = self.client.get(path).get_data(as_text=True)
                self.assertNotIn("group_charts.js", body, f"{path} 不该加载群聊脚本")

    def test_private_report_still_private(self):
        body = self.client.get("/report").get_data(as_text=True)
        self.assertIn("双方数据对比", body)
        self.assertNotIn("群概览", body)

    def test_private_analysis_endpoints_unchanged(self):
        """只验证"校验与缓存读取"这一层，**不真的发起分析**：

        早先这里 POST 了 /api/analyze/emotion，请求返回 200 之后后台任务照跑，
        测试于是真的调用了 API（实测数千 tokens）。发任务那条路径由 conftest 的
        网络护栏兜底，用例本身则不该去碰它。
        """
        self.assertEqual(self.client.post("/api/analyze/notadim", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get("/api/analysis/emotion").status_code, 404)  # 尚未分析


class TestUploadPageClearsBothTracks(GroupClientMixin, unittest.TestCase):
    """上传页要同时清理私聊与群聊的 sessionStorage 键（否则换文件后可能显示旧结果）"""

    def test_index_clears_all_dimension_keys(self):
        import app as appmod

        client = appmod.app.test_client()
        body = client.get("/").get_data(as_text=True)
        for dim in (
            "emotion",
            "relationship",
            "habits",
            "topics",
            "profile",
            "group_dynamics",
            "group_topics",
            "group_emotion",
            "member_profiles",
        ):
            with self.subTest(dim=dim):
                self.assertIn(f"'{dim}'", body, f"上传页没清理 {dim} 的会话缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
