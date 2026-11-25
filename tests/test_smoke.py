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

    #: 逐页冒烟覆盖的页面清单（新增用户可见页面必须同步这里 + 下面那条一致性用例）
    SMOKE_PAGES = (
        "/",
        "/dashboard",
        "/emotion",
        "/relationship",
        "/habits",
        "/topics",
        "/profile",
        "/report",
        "/report/share",
        "/recap",
        "/messages",
    )

    def test_pages_render_clean(self):
        # 这份清单必须与 webapp/views.py 注册的用户可见页面同步。README 对外承诺
        # "全量单测含逐页冒烟"，漏一页就等于那一页的 500 / 模板异常 / emoji 回潮
        # 不在守卫范围内——而这三类恰好只有真渲染才暴露得出来。
        # /recap 与 /messages 上线时就漏在清单外（当时单独手测是 200，所以没人发现）。
        for path in self.SMOKE_PAGES:
            with self.subTest(page=path):
                r = self.client.get(path)
                body = r.get_data(as_text=True)
                self.assertEqual(r.status_code, 200, f"{path} 返回 {r.status_code}")
                self.assertNotIn("Traceback", body)
                self.assertEqual(_emoji_count(body), 0, f"{path} 出现了 emoji")
                self.assertIn("data-theme", body)  # 主题脚本

    def test_smoke_page_list_covers_every_html_route(self):
        """守卫自己也要防"漏了一页"：清单必须覆盖 views 注册的全部页面路由。

        少了这条，上面那条会一直全绿，同时新页面悄悄落在守卫之外——那比没有守卫更糟，
        因为它给出"已经覆盖"的错觉。判据取"url_map 里真实存在的 GET 页面"，
        而不是再抄一份常量清单（抄的这份正是会漏的东西）。
        """
        import app as appmod

        covered = set(self.SMOKE_PAGES)
        # 有意不在此列，各自都有理由：
        #   /login  —— 设了口令才有意义，未设时重定向，冒烟环境走的是那条分支
        #   /health —— 探针，故意绕开登录/会话/日志/清理整条中间件链
        excluded = {"/login", "/health"}
        html_pages = set()
        for rule in appmod.app.url_map.iter_rules():
            if "GET" not in rule.methods or "<" in rule.rule:
                continue  # 带参数的路由（/face/<key>）不是整页
            if rule.endpoint == "static" or rule.rule.startswith("/api/"):
                continue  # 静态资源与 JSON 端点由别的用例覆盖
            if rule.rule in excluded:
                continue
            html_pages.add(rule.rule)
        self.assertTrue(html_pages, "一个页面路由都没匹配到：这条守卫本身失效了")
        missing = html_pages - covered
        self.assertEqual(missing, set(), f"这些用户可见页面没进逐页冒烟清单：{sorted(missing)}")
        stale = covered - html_pages - {"/"}
        self.assertEqual(stale, set(), f"冒烟清单里有不再存在的路由：{sorted(stale)}")

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


ROOT = Path(__file__).resolve().parent.parent


class TestFrontendRecoversAfterRestart(unittest.TestCase):
    """任务记录只在内存（TTL/重启会丢），但已完成的维度结果已落盘：

    轮询到 404 时应回读磁盘缓存，而不是把已经付过费的分析显示成失败。
    """

    def test_poll_falls_back_to_disk_cache_on_404(self):
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 404", src)
        self.assertIn("loadAnalysis(opts.dim", src, "404 时应回读磁盘结果")
        self.assertIn("opts.dim = dim", src, "startAnalyze 要把维度名传下去")

    def test_polling_backs_off_and_pauses_when_hidden(self):
        """固定 1.5s 的 setInterval 会让一次全量分析发出几百次请求"""
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(", src, "轮询不该再用固定间隔")
        self.assertIn("POLL_MAX_DELAY_MS", src, "应有退避上限")
        self.assertIn("visibilitychange", src, "后台标签页应降频、回到前台立刻补查")

    def test_charts_share_one_resize_listener(self):
        """每个图表各挂一个 resize 监听且从不移除：同一页面重绘 N 次就留下 N 个监听"""
        src = (ROOT / "web" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
        self.assertEqual(src.count("addEventListener('resize'"), 1, "resize 监听只应保留全局那一个")
        self.assertNotIn("window.addEventListener('resize', function() { chart.resize(); });", src)
        self.assertIn("function mountChart(", src)
        self.assertEqual(
            src.count("echarts.init("), 1, "只允许 mountChart 内部建实例（重绘要先 dispose 旧的）"
        )
        self.assertIn("dispose()", src)


class TestAssetCacheBusting(unittest.TestCase):
    """自有 JS/CSS 必须带版本参数，否则用户会长期跑到旧脚本"""

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.client = appmod.app.test_client()

    def test_own_assets_carry_version(self):
        """自有 JS/CSS 必须带 ?v= 破缓存，否则用户会长期跑到旧脚本。

        首页**不再加载图表脚本**（上传页没有图表，见 test_smoke 的逐页断言），
        所以这里分两处看：渲染出的首页验证 CSS，base.html 源码验证那两个图表脚本
        仍然挂着版本参数——少一个 `?v=`，改了前端却还在跑旧 JS，页面会直接不可用。
        """
        index = self.client.get("/").get_data(as_text=True)
        self.assertRegex(index, r"css/style\.css\?v=\d+")

        base = (ROOT / "web" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertRegex(base, r"js/charts\.js'\) \}\}\?v=\{\{ asset_v \}\}")
        self.assertRegex(base, r"js/analyze\.js'\) \}\}\?v=\{\{ asset_v \}\}")

    def test_vendor_assets_keep_their_own_filenames(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("vendor/jquery.min.js", body)
        self.assertNotIn("vendor/jquery.min.js?v=", body, "vendor 文件名自带版本，不必再挂参数")

    def test_version_is_numeric(self):
        from web import ASSET_VERSION

        self.assertRegex(ASSET_VERSION, r"^\d+$")


class TestApiOkInjectedGlobally(unittest.TestCase):
    """api_ok 由 context processor 统一注入；视图不再各传一份"""

    def test_pages_get_api_ok_from_context_processor(self):
        import app as appmod
        from flask import template_rendered

        captured = {}

        def record(_sender, **extra):
            # blinker 1.9 起接收者只能收 sender + 关键字参数，位置参数会直接 TypeError
            captured.update(extra.get("context") or {})

        template_rendered.connect(record, appmod.app)
        self.addCleanup(template_rendered.disconnect, record, appmod.app)

        with mock.patch.object(appmod.views, "is_api_configured", return_value=False):
            body = appmod.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("api_ok", captured, "模板上下文里必须有 api_ok")
        self.assertFalse(captured["api_ok"])
        self.assertIn("未配置 API Key", body, "页面要如实显示未配置状态")

    def test_views_no_longer_pass_it_manually(self):
        src = (ROOT / "webapp" / "views.py").read_text(encoding="utf-8")
        self.assertEqual(
            src.count("api_ok=is_api_configured()"),
            0,
            "视图里不该再逐个传 api_ok（由 context processor 注入）",
        )


class TestNetworkInterruptionRecovery(unittest.TestCase):
    """服务重启/断网时前端要回读缓存，而不是报"分析失败"诱导用户重跑"""

    def test_analyze_js_handles_status_zero(self):
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 0", src, "网络层失败要有单独分支")
        branch = src.split("xhr.status === 0")[1].split("opts.onError((xhr.responseJSON")[0]
        self.assertIn("loadAnalysis", branch, "网络中断时要尝试回读磁盘缓存")
        self.assertIn("不会重复付费", branch, "提示要说清「结果已保存」")

    def test_analyze_js_still_handles_404(self):
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 404", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
