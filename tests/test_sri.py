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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT_HTML = ROOT / "web" / "templates" / "report.html"
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
                    entry["integrity"], expected,
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
            text.count("markSri("), 3,
            "markSri 应出现 3 次（1 处定义 + script/link 两处调用），少了就是漏加 integrity",
        )
        self.assertEqual(
            text.count("VENDOR_CDN[vendorName("), 2,
            "两处换 CDN 都要走 vendorName()，否则带 ?v= 的路径匹配不上映射表",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
