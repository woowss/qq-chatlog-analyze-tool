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

import base64
import hashlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webapp import store as storemod  # noqa: E402

import app as appmod  # noqa: E402


def _emoji_count(text: str) -> int:
    return sum(
        1
        for ch in text
        if 0x1F000 <= ord(ch) <= 0x1FAFF
        or 0x2600 <= ord(ch) <= 0x27BF
        or 0x2B00 <= ord(ch) <= 0x2BFF
        or 0xFE0F == ord(ch)
    )


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
            msgs.append(
                {
                    "id": str(i),
                    "timestamp": base + i * 3600_000,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((base + i * 3600_000) / 1000)),
                    "sender": {"uid": who, "name": "我" if who == "u_self" else "对方"},
                    "content": ["在吗", "在的", "今天好累", "早点睡", "晚安"][i % 5],
                }
            )
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
        ).encode("utf-8")
        r = cls.client.post("/upload", data={"file": (io.BytesIO(payload), "chat.json")}, headers=cls.headers)
        assert r.status_code == 302, f"上传失败: {r.status_code}"
        with cls.client.session_transaction() as sess:
            cls.filepath = sess.get("filepath")
            cls.chat_hash = sess.get("chat_hash")

    @classmethod
    def tearDownClass(cls):
        if cls.filepath and os.path.exists(cls.filepath):
            os.remove(cls.filepath)
        storemod._purge_chat_caches(cls.chat_hash)

    def test_pages_render_clean(self):
        for path in (
            "/",
            "/dashboard",
            "/emotion",
            "/relationship",
            "/habits",
            "/topics",
            "/profile",
            "/report",
        ):
            with self.subTest(page=path):
                r = self.client.get(path)
                body = r.get_data(as_text=True)
                self.assertEqual(r.status_code, 200, f"{path} 返回 {r.status_code}")
                self.assertNotIn("Traceback", body)
                self.assertEqual(_emoji_count(body), 0, f"{path} 出现了 emoji")
                self.assertIn("data-theme", body)  # 主题脚本

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

    def test_report_page_inlines_vendor_sri(self):
        """渲染结果里必须带与本地 vendor 文件一致的 SRI 常量

        分享出去的 HTML 把 /static/vendor/* 换成 CDN，靠 integrity 兜底：哈希错了浏览器
        静默拦掉资源（样式、图表全没）。tests/test_sri.py 校验模板里的常量，这里再按
        "真正渲染出来的页面"验一遍，防止常量被 Jinja/转义/替换弄丢。
        """
        body = self.client.get("/report").get_data(as_text=True)
        vendor = Path(appmod.__file__).resolve().parent / "web" / "static" / "vendor"
        files = sorted(p for p in vendor.iterdir() if p.suffix in (".css", ".js"))
        self.assertTrue(files, f"{vendor} 下没有 vendor 资源？")
        for path in files:
            with self.subTest(vendor=path.name):
                digest = base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()
                self.assertIn(f"sha384-{digest}", body, f"{path.name} 的 SRI 常量没出现在报告页")
        self.assertIn("crossorigin", body, "SRI 缺少 crossorigin，跨源资源会被跳过校验")

    def test_upload_page_skips_chart_scripts_but_analysis_pages_keep_them(self):
        """上传页没有图表：不该为它解析 MB 级的 ECharts 与两个自有脚本。

        反向验证：把 index.html 里那个 `{% block chart_scripts %}{% endblock %}`
        删掉，本条前半段立刻变红；反过来把 base.html 的块清空，后半段变红。
        """
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("vendor/jquery.min.js", index, "上传表单要用 jQuery，它不能被一起省掉")
        self.assertNotIn("echarts.min.js", index, "上传页没有图表，不该加载 ECharts")
        self.assertNotIn("js/analyze.js", index)
        self.assertNotIn("js/charts.js", index)

        dash = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("vendor/echarts.min.js", dash, "画图的页面必须仍然拿到图表脚本")
        self.assertRegex(dash, r"js/charts\.js\?v=\d+")
        self.assertRegex(dash, r"js/analyze\.js\?v=\d+")

    def test_missing_session_redirects_home(self):
        fresh = appmod.app.test_client()
        fresh.get("/")
        r = fresh.get("/dashboard")
        self.assertEqual(r.status_code, 302)

    def test_login_page_renders(self):
        """登录页此前没被任何用例覆盖：它不继承 base.html，模板出错不会被发现"""
        from webapp import security

        fresh = appmod.app.test_client()
        with mock.patch.object(security, "ACCESS_PASSWORD", "s3cret"):
            r = fresh.get("/login")
            body = r.get_data(as_text=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn('name="password"', body)
            self.assertNotIn("Traceback", body)
            self.assertEqual(_emoji_count(body), 0, "登录页出现了 emoji")
        # 未设置口令时登录页直接回首页
        self.assertEqual(fresh.get("/login").status_code, 302)

    def test_login_page_reports_error_without_traceback(self):
        from webapp import security

        fresh = appmod.app.test_client()
        with mock.patch.object(security, "ACCESS_PASSWORD", "s3cret"):
            r = fresh.post("/login", data={"password": "wrong"})
            body = r.get_data(as_text=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn("口令错误", body)
            self.assertNotIn("Traceback", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
