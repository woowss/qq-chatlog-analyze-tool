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
"""隐私护栏：三条"文档承诺了、实现曾经没做到"的规则，各配一条回归用例。

它们守的不是"某个函数返回什么"，而是**以后别再漏**：

1. 日志落盘前人名必须过 `mask_name`（源码级扫 analyzer/ 的 logger 调用）；
2. 缓存必须带 `_created`，且读取时不把它漏给调用方
   （否则"绝对 90 天"退化成可被命中无限续期的 mtime）；
3. 非回环绑定 + 未设口令必须**拒绝服务**，而不是放行——
   CLI 路径由启动检查拦住，这条兜住绕过 main() 的 WSGI 入口。

第 2 条覆盖**每一族**缓存文件：月份缓存与 manifest 由 v1.1.1 补齐，统计缓存
（stats_cache/，含双方昵称与高频词）此前是漏的——它同样在命中时续期 mtime，
少这个字段就等于该目录只有滑动 30 天、没有绝对 90 天。

第 1 条是源码级静态检查（不是运行时断言）：它按"日志调用附近是否出现 mask_name"
判断，多行调用按 3 行窗口一起看，避免把续行上的人名漏掉。
"""

import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 视为"人名"的表达式：出现在 logger 调用里就必须先过 mask_name
_NAMEISH = re.compile(r"\b(member\.name|display_name|chat_name|self_name|other_name)\b")


class TestLogRedactionGuard(unittest.TestCase):
    """analyzer/ 里往日志写人名，必须先 mask_name（与 webapp 层同一口径）"""

    def test_analyzer_logger_calls_mask_names(self):
        analyzer = os.path.join(ROOT, "analyzer")
        bad = []
        for fn in sorted(os.listdir(analyzer)):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(analyzer, fn)
            lines = io.open(path, encoding="utf-8", errors="ignore").read().splitlines()
            for i, line in enumerate(lines):
                if "logger." not in line:
                    continue
                # 多行调用：把本条起连续 3 行当一个语句看（续行上的人名不能漏）
                window = " ".join(lines[i : i + 3])
                if not _NAMEISH.search(window):
                    continue
                if "mask_name" in window:
                    continue
                bad.append("%s:%d %s" % (fn, i + 1, line.strip()[:90]))
        self.assertEqual(
            bad,
            [],
            "日志里的人名必须经 mask_name 脱敏（LOG_REDACT_NAMES=true 的承诺）：\n" + "\n".join(bad),
        )

    def test_mask_name_still_masks(self):
        """护栏的前提：mask_name 本身没被改坏（清单里其余用例只查调用点）"""
        from analyzer.deepseek_client import mask_name

        self.assertNotIn("甜", mask_name("阿甜"))


class TestCacheCreatedGuard(unittest.TestCase):
    """缓存的"绝对 90 天"硬上限依赖 _created；写入方漏写就会退化成 mtime"""

    def test_month_cache_writes_created_and_hides_it_from_callers(self):
        from analyzer import deepseek_client as dc

        with tempfile.TemporaryDirectory() as d:
            dc.configure_month_cache(d)
            try:
                key = "guardtest0001"
                result = {"emotion": {"summary": "x"}}
                dc._write_month_cache(key, result)

                raw = json.load(io.open(dc.month_cache_path(key), encoding="utf-8"))
                self.assertIn("_created", raw, "month 缓存必须写 _created，否则硬上限形同虚设")

                self.assertEqual(
                    dc._read_month_cache(key),
                    result,
                    "读取时必须把 _created 摘掉：调用方拿到的结果要与此前完全一致",
                )
            finally:
                dc.configure_month_cache("")

    def test_manifest_created_is_set_once(self):
        from analyzer import deepseek_client as dc

        with tempfile.TemporaryDirectory() as d:
            dc.configure_month_cache(d)
            try:
                dc._record_month_usage("chathash0001", ["m1"])
                p = dc._manifest_path("chathash0001")
                first = json.load(io.open(p, encoding="utf-8"))
                self.assertIn("_created", first)
                created = first["_created"]

                dc._record_month_usage("chathash0001", ["m2"])
                second = json.load(io.open(p, encoding="utf-8"))
                self.assertEqual(
                    second["_created"],
                    created,
                    "绝对上限看的是首次创建，重写 manifest 不该把它续期",
                )
                self.assertEqual(second["months"], ["m1", "m2"])
            finally:
                dc.configure_month_cache("")


class TestStatsCacheCreatedGuard(unittest.TestCase):
    """统计缓存与月份缓存同属敏感派生数据，`_created` 一条都不能漏。

    反向验证：把 store._save_stats 里写 `_created` 的那两行删掉，
    test_stats_cache_writes_created_and_hides_it_from_callers 立刻变红；
    把它改成每次都用 time.time()，test_stats_rewrite_does_not_renew_created 变红。
    """

    def test_stats_cache_writes_created_and_hides_it_from_callers(self):
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "STATS_CACHE_DIR", d):
                stats = {"overview": {"total_messages": 3}, "daily_counts": []}
                store._save_stats("guardstats01", stats)

                raw = json.load(io.open(store._stats_path("guardstats01"), encoding="utf-8"))
                self.assertIn("_created", raw, "统计缓存必须写 _created，否则绝对 90 天上限形同虚设")
                self.assertEqual(
                    list(raw)[:1],
                    ["_created"],
                    "_created 必须是第一个键：清理任务只扫文件头，不整份解析这些文件",
                )

                loaded = store._load_stats("guardstats01")
                self.assertNotIn("_created", loaded, "读取时要把 _created 摘掉：模板不该看到这个字段")
                self.assertEqual(loaded["overview"], {"total_messages": 3})

    def test_stats_rewrite_does_not_renew_created(self):
        """词频是懒算后回写同一个文件的：重写必须保留首次创建时间。

        否则"每次有人打开习惯页"都把 90 天硬上限往后推一格，
        含高频词与昵称的统计缓存就永远不会消失。
        """
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "STATS_CACHE_DIR", d):
                store._save_stats("guardstats02", {"overview": {}})
                path = store._stats_path("guardstats02")
                old = json.load(io.open(path, encoding="utf-8"))["_created"] - 86400 * 10
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"_created": old, "overview": {}, "mode": "private", "_v": 4}, f)

                store._save_stats("guardstats02", {"overview": {"total_messages": 9}, "word_freq": {}})
                after = json.load(io.open(path, encoding="utf-8"))
                self.assertEqual(after["_created"], old, "重写统计缓存不该续期绝对上限")
                self.assertEqual(after["overview"], {"total_messages": 9})

    def test_cleanup_reads_created_from_both_ends_without_parsing_body(self):
        """有界扫描：头部（新格式）、尾部（历史遗留的月份缓存）、以及跨边界都要正确。"""
        from webapp import store

        win = store._CREATED_WINDOW
        with tempfile.TemporaryDirectory() as d:
            head = os.path.join(d, "head.json")
            with open(head, "w", encoding="utf-8") as f:
                f.write('{"_created": 1700000000.5, "result": "' + "x" * 5000 + '"}')

            tail = os.path.join(d, "tail.json")
            with open(tail, "w", encoding="utf-8") as f:
                f.write('{"emotion": "' + "x" * (win * 3) + '", "_created": 1699999999.25}')

            # 时间戳恰好跨过窗口末尾：头部会匹配到一个被切断的数值，
            # 此时必须改用锚在 EOF 的尾部窗口，否则会把新文件算成远古文件而提前删掉。
            cut = os.path.join(d, "cut.json")
            stamp = '"_created": 1700000000.5}'
            pad_len = win - 14 - len('{"pad": "') - len('", ')
            with open(cut, "w", encoding="utf-8") as f:
                f.write('{"pad": "' + "p" * pad_len + '", ' + stamp)

            self.assertEqual(store.read_created_at(head), 1700000000.5, "头部键（新格式）")
            self.assertEqual(store.read_created_at(tail), 1699999999.25, "尾部键（历史遗留格式）")
            self.assertEqual(store.read_created_at(cut), 1700000000.5, "跨窗口边界时不得取到截断值")

            missing = os.path.join(d, "none.json")
            with open(missing, "w", encoding="utf-8") as f:
                f.write('{"result": "no stamp here"}')
            self.assertIsNone(store.read_created_at(missing), "没有该字段时返回 None，由调用方回退 mtime")
            self.assertIsNone(store.read_created_at(os.path.join(d, "不存在.json")))


class TestNonLoopbackFailClosed(unittest.TestCase):
    """非回环绑定又没设口令 = 零认证：必须拒绝服务，绝不敞开放行"""

    def test_serves_503_when_non_loopback_without_password(self):
        from webapp import security

        import app as appmod

        client = appmod.app.test_client()
        with (
            mock.patch.object(security, "ACCESS_PASSWORD", ""),
            mock.patch.object(security, "FLASK_HOST", "0.0.0.0"),
        ):
            resp = client.get("/report")
        self.assertEqual(
            resp.status_code,
            503,
            "非回环 + 未设口令时必须 503（失败关闭），否则 WSGI 部署等于零认证",
        )

    def test_loopback_without_password_still_serves(self):
        from webapp import security

        import app as appmod

        client = appmod.app.test_client()
        with (
            mock.patch.object(security, "ACCESS_PASSWORD", ""),
            mock.patch.object(security, "FLASK_HOST", "127.0.0.1"),
        ):
            resp = client.get("/")
        self.assertNotEqual(
            resp.status_code,
            503,
            "回环绑定下不设口令是既有的正常用法，不能被这条护栏误伤",
        )


if __name__ == "__main__":
    unittest.main()
