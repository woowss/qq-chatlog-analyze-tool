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


class TestFrontendGuards(unittest.TestCase):
    """前端模板与图表静态回归护栏"""

    ROOT = Path(__file__).resolve().parent.parent
    CHARTS = ROOT / "web" / "static" / "js" / "group_charts.js"
    EMOTION = ROOT / "web" / "templates" / "group_emotion.html"
    TOPICS = ROOT / "web" / "templates" / "group_topics.html"
    ASSETS = ROOT / "web" / "templates" / "_report_assets.html"
    GROUP_REPORT = ROOT / "web" / "templates" / "group_report.html"

    @staticmethod
    def _code_of(text):
        """只留代码行：注释里会引用旧写法作为反面教材，混进来会把断言变成永真"""
        return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))

    def _heatmap_body(self):
        src = self._code_of(self.CHARTS.read_text(encoding="utf-8"))
        return src.split("function renderInteractionHeatmap")[1].split("\nfunction ")[0]

    def test_heatmap_tooltip_matches_axis_convention(self):
        """tooltip 的「谁先说 / 谁接话」必须与坐标轴、数据三方一致

        服务端是 directed[先说者][接话者]，图里压成 [列, 行, 值] → x=列=接话者、
        y=行=先说者。写反之后卡片标题与两条坐标轴都是对的，只有鼠标气泡说反，
        而用户信的是气泡。
        反向验证：把「说完」那一方从 value[1] 改回 value[0]，本条变红。
        """
        body = self._heatmap_body()
        # 绑定与使用要一起看：气泡里的「说完」必须取自 value[1]（行 = 先说的人）。
        # 只匹配一整句字符串的话，改个变量名就能骗过这条用例。
        self.assertRegex(
            body, r"var\s+row\s*=\s*esc\(names\[p\.value\[1\]\]\)", "row 必须绑定到 value[1]（行）"
        )
        self.assertRegex(
            body, r"var\s+col\s*=\s*esc\(names\[p\.value\[0\]\]\)", "col 必须绑定到 value[0]（列）"
        )
        self.assertRegex(body, r"row\s*\+\s*' 说完", "「说完」的一方必须是 row")
        self.assertNotIn("col + ' 说完", body, "把 col 说成「先开口的人」就是本轮修的那个反义 bug")
        self.assertIn("接了", body, "接话次数仍要在气泡里报出来")

    def test_mention_matrix_uses_its_own_wording(self):
        """@点名矩阵复用同一张热力图时，文案与行列语义都要换一套

        反向验证：删掉 mention 分支（回到只会说「说完/接了」），本条变红。
        """
        body = self._heatmap_body()
        self.assertIn("mention", body, "函数需要区分接话矩阵与 @ 矩阵")
        self.assertIn("@了", body, "@矩阵要说 @，不能沿用「说完/接了」")
        self.assertIn("mode: 'mention'", self.TOPICS.read_text(encoding="utf-8"), "调用点要显式声明模式")

    def test_emotion_page_guards_missing_container(self):
        """未配 Key 时 #sec-emotion 不渲染，但回调仍会拿到旧结果 —— 必须判空

        反向验证：去掉模板里的 `if (box)`、恢复直接把 getElementById 传进渲染函数，
        本条变红。那不只丢 AI 小节：同一个回调里排在后面的 mountChart('emotionTrend')
        会被一个 TypeError 一起带走，本地统计的走势图同样空掉。
        """
        src = self._code_of(self.EMOTION.read_text(encoding="utf-8"))
        self.assertNotIn(
            "renderGroupEmotion(document.getElementById(",
            src,
            "容器判空前不要直接把 getElementById 传进渲染函数",
        )
        self.assertIn("if (box)", src, "AI 小节要判空后再渲染")
        self.assertLess(
            src.index("if (box)"),
            src.index("mountChart('emotionTrend')"),
            "判空要在走势图之前，否则一个 null 把本地图也带走",
        )

    def test_report_snapshot_freezes_charts_before_stripping_scripts(self):
        """导出快照必须先把 canvas 冻结成图片，再剥脚本

        canvas 的像素不参与 outerHTML 序列化，而画图的代码又被剥掉了 —— 顺序反了
        或根本没冻结，分享出去的群报告就是四个全白的图表区域。
        反向验证：删掉 `freezeCharts(clone)` 这一行，本条变红。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn("function freezeCharts", code, "导出快照要有图表冻结步骤")
        self.assertIn("getInstanceByDom", code, "要用活实例取快照")
        self.assertIn("getDataURL", code)
        # 断言必须落在**调用点**上。第一版写的是 index("freezeCharts(clone)")，
        # 而这串字符同样出现在 `function freezeCharts(clone) {` 那一行里 —— 删掉调用
        # 也照样"绿"，是一条没有鉴别力的假守卫（反向验证脚本把它抓出来了）。
        self.assertRegex(code, r"\n\s*freezeCharts\(clone\);", "buildReportSnapshot 里要真的调用冻结")
        call = code.index("freezeCharts(clone);")
        self.assertGreater(call, code.index("cloneNode(true)"), "冻结要作用在克隆体上")
        self.assertLess(call, code.index("/static/js/"), "必须先冻结再剥脚本（脚本没了就画不出来了）")

    def test_report_snapshot_drops_live_render_scripts_but_keeps_theme(self):
        """分享版剥掉「活渲染块」与导出机制，但必须留住首屏的主题初始化

        反向验证：把内联脚本整体保留（旧行为只删 CSRF 那一块），收件人打开导出件
        会在第一行 ReferenceError；而误删主题初始化会让分享件打开时先闪一下浅色。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn("window\\.CSRF_TOKEN", code, "含 token 的脚本仍要按内容精确剔除")
        self.assertIn("data-theme", code, "主题初始化必须留在分享版里")
        self.assertIn("{ s.remove(); return; }", code, "非主题的脚本要整体摘掉，而不是留一份跑不动的")

    def test_report_snapshot_strips_csrf_input_and_uses_surface_bg(self):
        """剥了脚本不许漏剥表单里的 hidden csrf_token；冻结底色取卡片底而不是页面底

        反向验证：①删掉摘 input[name=csrf_token]/form.nav-logout 的那一行，第一段变红
        （登出表单在 base.html 里带 hidden token，与 window.CSRF_TOKEN 同值，README
        承诺"导出剥掉 CSRF token"就落空）；②把 freezeCharts 的取色改回 --bs-body-bg，
        第二段变红（图表都在 .card 里，暗色主题下按页面底填色，每张导出图自带一块
        比卡片更深的矩形）。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn('input[name="csrf_token"]', code, "hidden csrf_token 要整体摘掉")
        self.assertIn("form.nav-logout", code, "登出表单连壳一起摘，别只留个空 form")
        self.assertIn("--surface", code, "冻结底色取卡片底")
        self.assertNotIn("--bs-body-bg", code, "页面底不是图表的容身色")


class TestFrontendRegressionGuards(unittest.TestCase):
    """前端踩过的坑：用静态断言守住，避免改回去"""

    JS = ROOT / "web" / "static" / "js" / "charts.js"
    # 导出分享版报告的代码（VENDOR_CDN 表、CDN 重写、表情图内联、快照构建）2026-09-12 起
    # 抽到 partial：私聊报告与群聊报告共用同一份实现（两处各存一份必然漏改一处，
    # 而 SRI 对不上时浏览器是静默拦掉资源）。断言与语义不变，只是校验对象跟着代码走。
    REPORT = ROOT / "web" / "templates" / "_report_assets.html"
    INDEX = ROOT / "web" / "templates" / "index.html"

    def test_heatmap_uses_theme_tokens_not_hardcoded_colors(self):
        """热力图曾写死浅色（#6b7280/#e4e7eb/#ffffff）：深色主题下格子描边发白"""
        src = self.JS.read_text(encoding="utf-8")
        heatmap = src.split("function renderHeatmapChart")[1].split("function render")[0]
        # 注释里会提到旧写法作为说明，只检查真正的代码行
        code = "\n".join(ln for ln in heatmap.splitlines() if not ln.strip().startswith("//"))
        self.assertNotIn("#ffffff", code)
        self.assertNotIn("#6b7280", code)
        self.assertNotIn("#e4e7eb", code)
        self.assertIn("T.axis", code)
        self.assertIn("T.surface", code)

    def test_chart_entry_points_guard_missing_dom(self):
        """图表入口都要能在节点缺失时安全返回，否则一处 TypeError 会打断整块渲染"""
        src = self.JS.read_text(encoding="utf-8")
        for fn in (
            "renderEmotionCharts",
            "renderTopicsCharts",
            "renderRelationshipInsight",
            "renderHabitsInsight",
        ):
            body = src.split("function %s(" % fn)[1].split("\nfunction ")[0]
            self.assertIn("if (!data) return;", body, f"{fn} 缺少 data 判空")

    def test_report_keeps_style_link_when_inlining_failed(self):
        """样式没取到时不能把 <link> 也删掉，否则导出的报告只剩 Bootstrap"""
        # 导出机制在 partial（_report_assets.html），而 asList 这类"渲染期兜底"仍在
        # report.html 的渲染脚本里——两处都要看，才覆盖这条用例原本想守的东西。
        report_tpl = ROOT / "web" / "templates" / "report.html"
        src = self.REPORT.read_text(encoding="utf-8") + report_tpl.read_text(encoding="utf-8")
        self.assertIn("if (cssText) {", src)
        # 删 <link> 必须发生在这个守卫之内：取不到样式就保留原链接，别让报告裸奔
        self.assertLess(
            src.index("if (cssText) {"),
            src.index('link[href^="/static/css/"]'),
            "删样式表链接的代码要在 cssText 守卫之内",
        )
        self.assertIn("asList", src, "模型返回值先兜底成数组，避免一处 TypeError 打断报告")

    def test_report_replaces_unavailable_face_images(self):
        src = self.REPORT.read_text(encoding="utf-8")
        self.assertIn("replaceChild", src, "内联失败的表情图要降级成文字，不能留裂图")
        self.assertIn("getAttribute('alt')", src, "降级文字取自 alt（表情名）")

    def test_index_reports_partial_upload_failure(self):
        """分批上传失败时不能静默跳转，要如实说明还剩多少没传"""
        src = self.INDEX.read_text(encoding="utf-8")
        self.assertIn("中途失败", src)
        self.assertIn("服务端跳过", src)


class TestRouteSurfaceUnchanged(unittest.TestCase):
    """webapp 拆分是搬家不是改建：路由与端点名必须原样"""

    def test_all_endpoints_registered(self):
        import app as appmod

        endpoints = {r.endpoint for r in appmod.app.url_map.iter_rules()}
        for e in (
            "index",
            "upload",
            "dashboard",
            "emotion",
            "relationship",
            "habits",
            "topics",
            "profile",
            "report",
            "login",
            "static",
            "api_analyze",
            "api_analyze_job",
            "api_analyze_cancel",
            "api_analysis_result",
            "api_analyze_all",
            "api_usage",
            "api_status",
        ):
            self.assertIn(e, endpoints, f"端点 {e} 在拆分后消失了")

    def test_url_rules_point_where_expected(self):
        import app as appmod

        rules = {r.rule: r.endpoint for r in appmod.app.url_map.iter_rules()}
        self.assertEqual(rules["/api/analyze-all"], "api_analyze_all")
        self.assertEqual(rules["/upload"], "upload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
