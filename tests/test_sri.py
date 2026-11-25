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
"""SRI 自检：导出报告里的内联哈希常量，必须与本地 vendor 文件逐字节对得上

分享出去的 HTML 会把 /static/vendor/* 换成 jsDelivr 并带上 integrity。哈希一旦对不上，
浏览器是**静默拦掉**该资源（Bootstrap 样式、jQuery、ECharts 全没），不会有人报错——
所以这层只能靠测试钉死。本文件证明的是"内联常量 == 本地文件"；
"本地文件 == CDN 实际提供的字节"由 tools/verify_vendor_sri.py 联网复核
（2026-09-12 已按 sha256 逐条核对，并得到 data.jsdelivr.com 公布哈希的旁证）。
"""

import base64
import importlib.util
import sys
import unittest
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# VENDOR_CDN 表与导出代码 2026-09-12 起在 partial 里（私聊/群聊报告共用同一份导出机制）；
# 校验对象跟着搬，断言与语义不变。报告页渲染结果里是否带这些常量由 test_smoke 再验一遍。
REPORT_HTML = ROOT / "web" / "templates" / "_report_assets.html"
VENDOR_DIR = ROOT / "web" / "static" / "vendor"


def _load_sri_tool():
    """校验脚本也是"模板里那张表长什么样"的权威，测试复用它，免得两套解析各说各话"""
    path = ROOT / "tools" / "verify_vendor_sri.py"
    spec = importlib.util.spec_from_file_location("verify_vendor_sri", path)
    assert spec is not None and spec.loader is not None, f"加载不了 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sri = _load_sri_tool()


class TestReportSriConstants(unittest.TestCase):
    """报告导出的 vendor SRI 常量：本地文件哈希 vs 内联常量"""

    def setUp(self):
        self.table = sri.load_vendor_table()

    def test_inline_constants_match_local_vendor_files(self):
        """核心自检：浏览器实际会用的哈希，必须等于本地 vendor 文件的 sha384"""
        for name, entry in self.table.items():
            with self.subTest(vendor=name):
                path = VENDOR_DIR / name
                self.assertTrue(path.is_file(), f"{name} 在 {VENDOR_DIR} 下不存在")
                expected = sri.sri_of_file(path)
                self.assertEqual(
                    entry["integrity"],
                    expected,
                    f"{name}: 内联常量 {entry['integrity']} 与本地文件 {expected} 不一致；"
                    f"升级 vendor 后请跑 python tools/verify_vendor_sri.py --print 更新常量",
                )

    def test_constants_are_wellformed_sha384(self):
        """格式自检：截断/漏字符/写成 sha256 的常量在这里就红，不用等到浏览器静默拦"""
        for name, entry in self.table.items():
            with self.subTest(vendor=name):
                integrity = entry["integrity"]
                self.assertRegex(integrity, r"^sha384-[A-Za-z0-9+/]{64}$")
                raw = base64.b64decode(integrity.split("-", 1)[1], validate=True)
                self.assertEqual(len(raw), 48, "sha384 摘要应为 48 字节")

    def test_table_covers_exactly_the_files_base_html_loads(self):
        """两边一一对应：base.html 加了 vendor 文件却忘了 SRI，导出后会留本机路径"""
        expected = sri.app_vendor_files()
        self.assertEqual(sorted(self.table), expected)
        self.assertEqual(sri.coverage_issues(self.table, expected), [])

    def test_cdn_urls_are_pinned_jsdelivr_urls(self):
        """URL 自检：https + jsDelivr + 锁版本 + 末尾文件名与本地文件同名"""
        self.assertEqual(sri.url_issues(self.table), [])

    def test_export_code_rewrites_url_and_adds_integrity(self):
        """回归护栏：script/link 两处换 CDN 都必须调 markSri，漏一处 SRI 就等于没加"""
        text = REPORT_HTML.read_text(encoding="utf-8")
        self.assertIn("el.setAttribute('integrity', integrity)", text)
        self.assertIn("el.setAttribute('crossorigin', 'anonymous')", text)
        self.assertIn("s.setAttribute('src', asset.url)", text)
        self.assertIn("l.setAttribute('href', asset.url)", text)
        self.assertGreaterEqual(
            text.count("markSri("),
            3,
            "markSri 应出现 3 次（1 处定义 + script/link 两处调用），少了就是漏加 integrity",
        )
        self.assertEqual(
            text.count("VENDOR_CDN[vendorName("),
            2,
            "两处换 CDN 都要走 vendorName()，否则带 ?v= 的路径匹配不上映射表",
        )


class TestLocalizedAssets(unittest.TestCase):
    """前端资源本地化：页面不再伸手向 CDN；导出仍换回 CDN"""

    def test_vendor_files_present_and_nonempty(self):
        vendor = ROOT / "web" / "static" / "vendor"
        for name, min_size in (
            ("bootstrap.min.css", 100_000),
            ("bootstrap.bundle.min.js", 50_000),
            ("jquery.min.js", 50_000),
            ("echarts.min.js", 500_000),
            ("echarts-wordcloud.min.js", 10_000),
        ):
            p = vendor / name
            self.assertTrue(p.exists(), f"缺少本地化资源 {name}")
            self.assertGreater(p.stat().st_size, min_size, f"{name} 体积异常，疑似下载失败")

    def test_page_templates_have_no_runtime_cdn(self):
        for name in ("base.html", "login.html"):
            src = (ROOT / "web" / "templates" / name).read_text(encoding="utf-8")
            self.assertNotIn("cdn.jsdelivr", src, f"{name} 仍在运行时拉 CDN")

    def test_exported_report_rewrites_vendor_to_cdn(self):
        src = (ROOT / "web" / "templates" / "_report_assets.html").read_text(encoding="utf-8")
        self.assertIn("VENDOR_CDN", src, "导出必须把 vendor 路径换回 CDN，否则分享出去的报告没了样式")
        for name in ("bootstrap.min.css", "jquery.min.js", "echarts.min.js"):
            self.assertIn(name, src)

    def test_theme_toggle_has_view_transition(self):
        css = (ROOT / "web" / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn("@view-transition", css, "主题切换 reload 应带跨文档过渡，缓解整页闪跳")

    def test_report_cdn_map_covers_every_vendor_file(self):
        """base.html 引用的本地 vendor 资源必须都在导出映射表里。

        漏一个的后果很隐蔽：应用内一切正常，只有"下载 HTML 发给别人"时
        那份报告掉了样式/图表，而导出是纯前端拼接，没人会立刻发现。
        """

        base = (ROOT / "web" / "templates" / "base.html").read_text(encoding="utf-8")
        report = (ROOT / "web" / "templates" / "_report_assets.html").read_text(encoding="utf-8")
        refs = set(re.findall(r"filename='vendor/([\w.-]+)'", base))
        self.assertTrue(refs, "base.html 应引用本地化的 vendor 资源")
        for name in sorted(refs):
            self.assertIn(
                "'%s'" % name,
                report,
                f"{name} 不在 _report_assets.html 的 VENDOR_CDN 映射里，导出的报告会 404",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
