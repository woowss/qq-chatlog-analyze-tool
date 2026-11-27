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
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import purge_marks  # noqa: E402
from webapp import store  # noqa: E402

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


class TestCascadePurgeReachesMonthCache(unittest.TestCase):
    """级联清理要在**同一次**调用里回收该聊天的月份缓存

    反向验证：把 store._purge_chat_caches 里的 purge_month_cache 挪回目录遍历之后，
    test_purge_removes_grace_expired_month_files_of_that_chat 立刻变红。

    为什么要专门钉住：生产环境 _MONTH_CACHE_DIR 就是 AI_CACHE_DIR（app.py 里
    configure_month_cache 收到的正是这个目录），而 manifest_{chat_hash}.json 也放在
    那里，并且**会被整段匹配认成"这个聊天的文件"**。清理循环若先跑，manifest 先被
    删掉，purge_month_cache 就再也读不到"这个聊天引用过哪些月份"，那些含聊天原句
    引用的月份文件只能等下一次 sweep_orphan_month_cache（最长一小时；进程若就此
    停止则要等下次启动）——用户已经看到"派生缓存已清理"，盘上却还留着。
    注意 tests/_bootstrap.py 把 QQCHAT_MONTH_CACHE 定死为 0，所以这个组合
    （月份缓存目录 == ai_cache/）只有显式 configure 才能覆盖到。
    """

    def test_purge_removes_grace_expired_month_files_of_that_chat(self):
        from analyzer import month_cache as mc
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "AI_CACHE_DIR", d):
                mc.configure_month_cache(d)  # 生产口径：月份缓存目录就是 ai_cache/
                try:
                    chat_hash = "hashCascade01"
                    key = mc._month_key("SYS", "USER-级联清理")
                    path = mc.month_cache_path(key)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump({"_created": 1.0, "self_emotion": "平静"}, f, ensure_ascii=False)
                    mc._record_month_usage(chat_hash, [key])
                    os.utime(path, (1.0, 1.0))  # 推过宽限期：这个文件本该被同步回收

                    store._purge_chat_caches(chat_hash)

                    self.assertFalse(
                        os.path.exists(path),
                        "该聊天的月份缓存必须随本次级联清理一起消失（它含聊天原句引用）："
                        "留到定期 sweep 才删，等于「删了聊天，敏感摘要还在盘上」",
                    )
                    self.assertFalse(
                        os.path.exists(mc._manifest_path(chat_hash)),
                        "manifest 是月份缓存的元数据，同样要清掉",
                    )
                finally:
                    mc.configure_month_cache("")

    def test_purge_keeps_month_files_still_referenced_by_another_chat(self):
        """反向：仍被别的聊天引用的月份文件不能被顺手删掉（删了要重新付费）"""
        from analyzer import month_cache as mc
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "AI_CACHE_DIR", d):
                mc.configure_month_cache(d)
                try:
                    shared = mc._month_key("SYS", "USER-共享月份")
                    path = mc.month_cache_path(shared)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump({"_created": 1.0, "x": 1}, f, ensure_ascii=False)
                    os.utime(path, (1.0, 1.0))
                    mc._record_month_usage("hashCascadeKeep", [shared])
                    mc._record_month_usage("hashCascadeDrop", [shared])

                    store._purge_chat_caches("hashCascadeDrop")

                    self.assertTrue(os.path.exists(path), "仍被另一个聊天引用的月份缓存必须留下")
                finally:
                    mc.configure_month_cache("")


class TestCacheRetentionFailsClosed(unittest.TestCase):
    """读不到时间的缓存按「该回收」处理，而不是「当它是刚创建的」

    反向验证：把 cleanup._cache_created_at 的 except 分支改回 return time.time()，
    或把 _cache_expired 的 getmtime 失败改回"异常抛出去、由调用方跳过"，
    下面两条立刻变红。

    方向为什么重要：这两个目录装的是含聊天内容的派生数据（月份整段结果、图片描述）。
    "判不出来就保留"等于给这类文件发了一张永久居留证，而它的生命周期本来有硬上限；
    反过来"判不出来就尝试删"最坏只是删不掉，调用方会留一行告警。
    """

    def test_unreadable_timestamps_degrade_to_epoch_not_now(self):
        """_created 与 mtime 都读不到时，取到的必须是 epoch（远古），不是 now（刚建）"""
        from webapp import cleanup as cleanupmod

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "emotion_deadbeef_deepseek-chat_abcdef123456.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            with (
                mock.patch.object(cleanupmod, "read_created_at", lambda _p: None),
                mock.patch.object(cleanupmod.os.path, "getmtime", side_effect=OSError("读不到")),
            ):
                self.assertEqual(
                    cleanupmod._cache_created_at(path),
                    0.0,
                    "取不到任何时间时必须当作远古文件（fail-closed），而不是当作刚创建"
                    "（那会让它永远逃过回收）",
                )

    def test_cache_with_unreadable_mtime_is_reclaimed(self):
        """端到端：ai_cache/ 里读不到 mtime 的文件要在本次清理里被真正删掉

        走完整的 cleanup_old_files()，只把**目标文件**的 getmtime 打桩成失败，
        其余路径仍用真实实现（否则会把上传目录、日志目录的判定一起打坏）。
        """
        from webapp import cleanup as cleanupmod
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with (
                mock.patch.object(cleanupmod, "AI_CACHE_DIR", d),
                mock.patch.object(store, "AI_CACHE_DIR", d),
            ):
                target = os.path.join(d, "emotion_deadbeef_deepseek-chat_abcdef123456.json")
                with open(target, "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "result": {"month_title": "旧结果"}}, f, ensure_ascii=False)
                # 「刚写过」的那个文件不能被连带删掉：清理只该回收过期内容
                fresh = os.path.join(d, "emotion_feedface_deepseek-chat_abcdef123456.json")
                with open(fresh, "w", encoding="utf-8") as f:
                    json.dump({"_created": time.time(), "result": {}}, f, ensure_ascii=False)

                real_getmtime = os.path.getmtime
                target_abs = os.path.abspath(target)

                def flaky_getmtime(path, _real=real_getmtime, _target=target_abs):
                    if os.path.abspath(path) == _target:
                        raise OSError("模拟：时间读不出来")
                    return _real(path)

                with mock.patch.object(cleanupmod.os.path, "getmtime", flaky_getmtime):
                    cleanupmod.cleanup_old_files()

                self.assertFalse(
                    os.path.exists(target),
                    "读不到时间的缓存文件必须被尝试回收（含聊天内容的派生数据不能永驻）",
                )
                self.assertTrue(os.path.exists(fresh), "同一轮清理不得误伤刚写入的缓存")


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


class _TempCacheDirs(unittest.TestCase):
    """把缓存目录整体搬进临时目录：这些用例都靠"目录外有个诱饵文件"来判成败。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r7-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ai = os.path.join(self.tmp, "ai_cache")
        self.stats = os.path.join(self.tmp, "stats_cache")
        os.makedirs(self.ai, exist_ok=True)
        os.makedirs(self.stats, exist_ok=True)
        for mod, attr, val in ((store, "AI_CACHE_DIR", self.ai), (store, "STATS_CACHE_DIR", self.stats)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)
        mc.configure_month_cache(self.ai)
        self.addCleanup(mc.configure_month_cache, "")
        # 把月份的"宽限期"归零：否则这些用例会因为"文件还年轻、本就不删"而**恒真**绿掉
        # （写这组用例时第一版就踩了：诱饵只老了 10 小时 < 默认 24 小时，断言 0 次删除
        # 看似"挡住了"，其实什么都没验——反向验证那一条当场把它抓了出来）。
        grace = mock.patch.object(mc, "MONTH_CACHE_GRACE_SECONDS", 0)
        grace.start()
        self.addCleanup(grace.stop)
        # 造出穿越路径的**中间目录**（键 "/../.." 拼出来的是 `month_/../../x.json`）。
        # 为什么必须有：POSIX 的路径解析要求中间每一段都真实存在，而 Windows 是先做
        # 词法归一、不需要。少了这一层，那两条"去掉闸门就该中招"的反向验证在 Linux 上
        # 会因为"路径压根解析不通"而失败（CI 就是 Linux）——那验的就不是闸门了。
        os.makedirs(os.path.join(self.ai, "month_"), exist_ok=True)
        purge_marks.clear()
        self.addCleanup(purge_marks.clear)

    def _malicious_bundle(self, chat_hash: str, key: str, leaf: str | None = None) -> io.BytesIO:
        buf = io.BytesIO()
        name = leaf or f"manifest_{chat_hash}.json"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps({"bundle": 1, "chat_hash": chat_hash}))
            zf.writestr(f"ai_cache/{name}", json.dumps({"months": [key]}))
        buf.seek(0)
        return buf


class TestMonthKeysAreNeverPaths(_TempCacheDirs):
    """`months` 是**磁盘内容**（且可由导入包写进来），绝不许拼进路径。

    这是本轮唯一的"能直接删/偷用户本机文件"的洞，所以用例分三层钉：
    形状闸、两个消费点（删除 / 导出）、以及"去掉闸就会中招"的反向验证。
    """

    HASH = "deadbeefdeadbeef"

    def _decoy(self) -> str:
        """缓存目录**之外**的诱饵文件（模拟别人的缓存 / 配置 / 凭据 json）。"""
        path = os.path.join(self.tmp, "decoy_target.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"secret": "must survive"}')
        old = time.time() - 10 * 3600
        os.utime(path, (old, old))  # 远早于任何宽限期（上面已把宽限期归零）
        return path

    #: `/../../decoy_target` → month_/../../decoy_target.json → 归一后逃出 ai_cache/
    KEY = "/../../decoy_target"

    def test_key_shape_gate_rejects_path_characters(self):
        """闸门本身：分隔符 / 上跳 / 盘符 / 空 / 超长一律拒绝，正常 key 照常放行。"""
        for bad in ("/../../x", "x/../../y", "..\\..\\z", "", "a" * 65, "a/b", None, 123, "c:d"):
            self.assertFalse(mc.is_safe_month_key(bad), f"{bad!r} 不该被当成合法月份键")
        for good in ("m1", "k1", "0" * 20, "abcDEF123_-"):
            self.assertTrue(mc.is_safe_month_key(good), f"{good!r} 是既有数据里出现过的形状")

    def test_month_cache_path_refuses_to_escape(self):
        """唯一的路径构造点拒绝拼路径（返回空串），而不是拼出一个能逃出目录的路径。"""
        self.assertEqual(mc.month_cache_path(self.KEY), "")
        # 合法键的行为逐字节不变
        self.assertTrue(mc.month_cache_path("m1").endswith(os.path.join("", "month_m1.json")))

    def test_manifest_reader_filters_unsafe_keys(self):
        """读 manifest 时按形状过滤：非法项当成不存在，绝不带到 os.path.join 上去。"""
        with open(os.path.join(self.ai, f"manifest_{self.HASH}.json"), "w", encoding="utf-8") as f:
            json.dump({"months": [self.KEY, "legit1", 7, None, "a/b"]}, f)
        keys = mc._manifest_keys_locked(f"manifest_{self.HASH}.json")
        self.assertEqual(keys, {"legit1"}, "只有形状合法的键能进引用集")

    def test_purge_never_touches_files_outside_the_cache_dir(self):
        """端到端：导入恶意 manifest → 级联清理 → 缓存目录外的诱饵必须还在。"""
        decoy = self._decoy()
        out = store.read_chat_bundle(self._malicious_bundle(self.HASH, self.KEY))
        self.assertNotIn("error", out, "恶意 manifest 本身会被写进来（形状合法）")

        removed = mc.purge_month_cache(self.HASH)
        self.assertEqual(removed, 0, "非法键不该产生任何一次删除")
        self.assertTrue(os.path.exists(decoy), "缓存目录之外的 .json 绝不许被删")

    def test_decoy_would_be_deleted_without_the_guard(self):
        """反向验证：把闸门拆掉，同一个攻击必须得手。

        没有这一条，上面的用例可能只是"恰好没删掉"（例如键拼错了、宽限期没越过）。
        这里显式 mock 掉形状判定，证明**正是它**挡住了删除。
        """
        decoy = self._decoy()
        store.read_chat_bundle(self._malicious_bundle(self.HASH, self.KEY))
        with mock.patch.object(mc, "is_safe_month_key", lambda _k: True):
            removed = mc.purge_month_cache(self.HASH)
        self.assertEqual(removed, 1, "去掉闸门时这一步确实会删掉诱饵")
        self.assertFalse(os.path.exists(decoy), "反向验证：闸门就是唯一的拦截点")

    def test_export_never_carries_files_outside_the_cache_dir(self):
        """第二个消费点：导出按同一个键 zf.write，会把任意 .json 的内容打进分享包。"""
        self._decoy()
        store.read_chat_bundle(self._malicious_bundle(self.HASH, self.KEY))

        entries = store.chat_bundle_files(self.HASH)["ai_cache"]
        self.assertFalse(
            [n for n in entries if "decoy" in n],
            f"导出清单不许出现逃出缓存目录的条目：{entries}",
        )

        buf = io.BytesIO()
        store.write_chat_bundle(self.HASH, buf)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        self.assertFalse([n for n in names if "decoy" in n], f"导出的包里不许夹带：{names}")

    def test_export_would_leak_without_the_guard(self):
        """反向验证（导出侧）：去掉闸门时，诱饵内容确实会被复制进结果包。"""
        decoy = self._decoy()
        store.read_chat_bundle(self._malicious_bundle(self.HASH, self.KEY))
        with mock.patch.object(mc, "is_safe_month_key", lambda _k: True):
            entries = store.chat_bundle_files(self.HASH)["ai_cache"]
        self.assertTrue([n for n in entries if "decoy" in n], "反向验证：闸门是唯一的拦截点")
        self.assertTrue(os.path.exists(decoy))

    def test_legitimate_month_migration_still_works(self):
        """不误伤：正常的月份文件仍要顺着 manifest 被收进包（增量迁移的依据）。"""
        legit = os.path.join(self.ai, "month_legit1.json")
        with open(legit, "w", encoding="utf-8") as f:
            f.write('{"result": 1}')
        out = store.read_chat_bundle(self._malicious_bundle(self.HASH, "legit1"))
        self.assertNotIn("error", out)
        self.assertIn("month_legit1.json", store.chat_bundle_files(self.HASH)["ai_cache"])


class TestMaliciousManifestCannotPoisonTheManifestFile(_TempCacheDirs):
    """写回 manifest 时也把非法键洗掉，别让"读旧 + 并新"把它继续留在盘上。"""

    def test_record_month_usage_drops_unsafe_keys(self):
        mc._record_month_usage("hashCleanup01", ["/../../evil", "good1"])
        with open(mc._manifest_path("hashCleanup01"), encoding="utf-8") as f:
            keys = json.load(f)["months"]
        self.assertEqual(keys, ["good1"])

    def test_preexisting_unsafe_keys_are_washed_on_rewrite(self):
        """磁盘上已有一份被篡改的 manifest：下一次写入顺手洗干净。"""
        path = mc._manifest_path("hashCleanup02")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"months": ["/../../evil", "keep1"]}, f)
        mc._record_month_usage("hashCleanup02", ["keep2"])
        with open(path, encoding="utf-8") as f:
            keys = json.load(f)["months"]
        self.assertEqual(keys, ["keep1", "keep2"], "非法项不留、合法项不丢")


class TestSecretsAreScrubbedFromOutwardFacingText(unittest.TestCase):
    """Key 走 Authorization 头、不进 URL，官方端点还会打码——但第三方网关常把它原样
    写进错误正文，而这些文本会回到浏览器（用户会截图求助）与 logs/（README 承诺
    日志可以整目录留存/贴给别人看）。

    **不读环境里那个真实的 Key**（CI 上没有 `.env`，key 是空串，于是"应当有 key"这类
    断言必挂、`assertNotIn("", detail)` 更是恒假——这正是 `_bootstrap.api_configured_patcher`
    的说明里点名过的那类"本机绿、CI 红"的坑，本文件第一版就踩了）。一律用打桩的
    合成 key，两个环境跑的是同一条用例。
    """

    #: 合成 key：**不是** _PLACEHOLDER_KEYS 里的占位符，所以会被当作真秘密处理
    FAKE_KEY = "sk-qqchatlogfakekey0123456789"

    def test_configured_key_is_replaced(self):
        with mock.patch.object(dc, "DEEPSEEK_API_KEY", self.FAKE_KEY):
            out = dc.scrub_secrets(f"upstream said: invalid key {self.FAKE_KEY} for model x")
        self.assertNotIn(self.FAKE_KEY, out, "本机配置的那个 Key 必须被抹掉")
        self.assertIn("<API_KEY>", out)
        self.assertIn("invalid key", out, "除 Key 之外的排障信息必须保留")

    def test_placeholder_key_is_not_a_secret(self):
        """占位符（.env.example 里那种"你的API_Key"）不是秘密，不必抹也不用报。"""
        with mock.patch.object(dc, "DEEPSEEK_API_KEY", "你的API_Key"):
            out = dc.scrub_secrets("bad key 你的API_Key rejected")
        self.assertIn("你的API_Key", out)

    def test_key_shaped_strings_are_redacted(self):
        token = "sk-abcdefghijklmnop1234"
        out = dc.scrub_secrets(f"401 unauthorized: {token}")
        self.assertNotIn(token, out)
        self.assertIn("<redacted>", out)

    def test_opencode_key_shaped_strings_are_redacted(self):
        token = "oc_sk_fake012345_abcdefghijklmnop"
        out = dc.scrub_secrets(f"401 unauthorized: {token}")
        self.assertNotIn(token, out)
        self.assertIn("<redacted>", out)

    def test_keyed_value_keeps_the_label(self):
        out = dc.scrub_secrets("apikey=abcdef1234567890 rejected")
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn("apikey=", out, "保留键名，只抹值（排障仍要看得懂是哪一项被拒）")

    def test_ordinary_error_text_is_left_alone(self):
        """不误伤：普通错误文本里的短标识、request-id、会话名不该被抹掉。"""
        text = "Error code: 429 - rate limit; request-id: abc123; session_id: sess-42"
        self.assertEqual(dc.scrub_secrets(text), text)

    def test_test_connection_does_not_echo_the_key(self):
        """`/api/status/test` 的响应会显示给用户：上游把 Key 回显进正文时不许带出去。"""
        leaky = (
            "Error code: 401 - {'error': {'message': 'bad key "
            f"sk-abcdefghijklmnop1234 / {self.FAKE_KEY}'}}}}"
        )

        class _Boom:
            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**_kw):
                        raise RuntimeError(leaky)

        with (
            mock.patch.object(dc, "DEEPSEEK_API_KEY", self.FAKE_KEY),
            mock.patch.object(dc, "is_api_configured", lambda: True),
            mock.patch.object(dc, "_get_client", lambda: _Boom()),
        ):
            ok, detail = dc.test_connection()
        self.assertFalse(ok)
        self.assertNotIn("sk-abcdefghijklmnop1234", detail, "长相像 Key 的字串必须被抹掉")
        self.assertNotIn(self.FAKE_KEY, detail, "本机配置的 Key 本身必须被抹掉")
        self.assertIn("401", detail, "错误码这类排障线索要保留")


class _FakeSessionCache:
    """最小会话后端替身：只实现 `has()`（真实后端是 cachelib.FileSystemCache）。"""

    def __init__(self, alive):
        self._alive = set(alive)
        self.seen = []

    def has(self, key):
        self.seen.append(key)
        return key in self._alive


class TestGhostLiveRefsDoNotBlockExplicitDelete(unittest.TestCase):
    """没点退出就关掉的会话会留下引用，把隐私清理挡住最长 24h，界面还说"仍被其它浏览器使用"。

    显式删除路径因此额外核对会话后端；换文件路径保持原样——那里误判的代价是删掉别人
    正在用的已付费结果，而内容寻址意味着闲置用户回头重传本该免费命中，两种代价不对称。
    """

    HASH = "ghosthash000001"

    def setUp(self):
        store.clear_live_chat_refs()
        self.addCleanup(store.clear_live_chat_refs)

    def test_dead_session_is_not_reported_as_still_using_the_chat(self):
        import app as appmod

        store.note_live_chat(self.HASH, "sid-ghost")
        cache = _FakeSessionCache(alive=[])
        with appmod.app.app_context(), mock.patch.dict(appmod.app.config, {"SESSION_CACHELIB": cache}):
            self.assertEqual(
                store.other_live_sessions(self.HASH, exclude_sid="me", require_live_session=True),
                [],
                "会话后端里已经没有这个 sid 了，它不该再挡住显式删除",
            )
            # 换文件路径保持原口径（保守：宁可少删）
            self.assertEqual(
                store.other_live_sessions(self.HASH, exclude_sid="me"),
                ["sid-ghost"],
                "换文件路径不看会话后端，行为逐字节不变",
            )
        self.assertIn("session:sid-ghost", cache.seen, "查的键必须带 flask-session 的前缀")

    def test_real_session_still_blocks(self):
        import app as appmod

        store.note_live_chat(self.HASH, "sid-real")
        cache = _FakeSessionCache(alive=["session:sid-real"])
        with appmod.app.app_context(), mock.patch.dict(appmod.app.config, {"SESSION_CACHELIB": cache}):
            self.assertEqual(
                store.other_live_sessions(self.HASH, exclude_sid="me", require_live_session=True),
                ["sid-real"],
                "真正还活着的会话必须继续被尊重（否则就是删别人正在看的结果）",
            )

    def test_without_a_session_backend_it_stays_conservative(self):
        """拿不到后端（无应用上下文等）时按"还活着"处理，绝不因为查不到就删。"""
        import app as appmod

        store.note_live_chat(self.HASH, "sid-unknown")
        self.assertIsNone(store.live_session_exists("sid-unknown"), "无上下文时如实回答'判断不了'")
        with appmod.app.app_context(), mock.patch.dict(appmod.app.config, {"SESSION_CACHELIB": None}):
            self.assertIsNone(store.live_session_exists("sid-unknown"))
            self.assertEqual(
                store.other_live_sessions(self.HASH, exclude_sid="me", require_live_session=True),
                ["sid-unknown"],
                "判断不了就该保守留着",
            )


def _f7_touch_cache(name: str, content: str = '{"_created": 1}', age_days: int = 0) -> str:
    path = os.path.join(store.AI_CACHE_DIR, name)
    os.makedirs(store.AI_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


class TestBundleRoundTrip(unittest.TestCase):
    """导出→清空→导入：已付费结果必须原样回来（跨机器不重复付费的闭环）。"""

    HASH = "aaaa1111bbbb2222"

    def setUp(self):
        self.files = [
            _f7_touch_cache(f"emotion_{self.HASH}_model_fp123.json"),
            _f7_touch_cache(f"vision_{self.HASH}_deadbeef.json"),
            _f7_touch_cache(f"manifest_{self.HASH}.json", json.dumps({"months": ["mkey1", "mkey2"]})),
            _f7_touch_cache("month_mkey1.json"),
            _f7_touch_cache("month_mkey2.json"),
        ]
        sp = store._stats_path(self.HASH)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            f.write('{"overview": {}}')
        self.files.append(sp)

    def tearDown(self):
        for p in self.files:
            try:
                os.remove(p)
            except OSError:
                pass

    def test_export_contains_month_files_via_manifest(self):
        names = store.chat_bundle_files(self.HASH)
        self.assertIn(f"emotion_{self.HASH}_model_fp123.json", names["ai_cache"])
        # 月份文件名字里没有 chat_hash——必须顺着 manifest 收，否则迁移后退化成整月重付费
        self.assertIn("month_mkey1.json", names["ai_cache"])
        self.assertIn("month_mkey2.json", names["ai_cache"])
        self.assertTrue(names["stats_cache"])

    def test_export_import_roundtrip(self):
        buf = io.BytesIO()
        count = store.write_chat_bundle(self.HASH, buf)
        self.assertGreaterEqual(count, 6)
        for p in self.files:
            os.remove(p)
        self.assertFalse(os.path.exists(self.files[0]))
        buf.seek(0)
        out = store.read_chat_bundle(buf)
        self.assertNotIn("error", out)
        self.assertGreaterEqual(out["written"], 6)
        for p in self.files:
            self.assertTrue(os.path.exists(p), f"{p} 应被导入恢复")

    def test_import_rejects_unsafe_entries(self):
        """两道判定分开钉：① 路径成分（叶子名 + 目录前缀）② 归属（声明哈希的段）。

        上一版这里断言"ai_cache/good.json 与 stats_hash.json 会被写入"——那正是问题
        所在：只校验名字的**形状**，等于允许往这两个目录里写任意键，而其中的内容会被
        当作已付费结果直接渲染。现在归属也要过：叶子必须内嵌 meta 声明的那份哈希。
        """
        declared = "abcd1234abcd1234"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("meta.json", json.dumps({"bundle": 1, "chat_hash": declared}))
            zf.writestr("../evil.json", "{}")  # 路径穿越
            zf.writestr("etc/passwd", "{}")  # 未知目录
            zf.writestr("ai_cache/../../x.json", "{}")  # 藏在合法目录里的穿越
            zf.writestr("ai_cache/good.json", '{"_created": 1}')  # 形状合法但不属于该聊天
            zf.writestr("stats_cache/stats_hash.json", "{}")  # 同上
            zf.writestr(f"ai_cache/emotion_{declared}_model_fp.json", '{"result": 1}')  # 该写的
        buf.seek(0)
        out = store.read_chat_bundle(buf)
        self.assertEqual(out["written"], 1, "只有既在允许目录、又属于声明那份聊天的条目能落盘")
        self.assertGreaterEqual(out["skipped"], 5)
        self.assertFalse(os.path.exists(os.path.join(store.AI_CACHE_DIR, "..", "evil.json")))
        self.assertFalse(os.path.exists(os.path.join(store.STATS_CACHE_DIR, "..", "x.json")))
        self.assertFalse(os.path.exists(os.path.join(store.AI_CACHE_DIR, "good.json")))


class TestBundleImportBinding(unittest.TestCase):
    """导入结果包必须绑定到它自己声明的那份聊天；坏条目不许把请求打成 500。

    这些文件是被当作**已付费的可信结果**读的：`_load_cache_file` 拿到 dict 就直接
    渲染成页面。所以 zip 若能任意挑键写入，一个不含任何聊天数据的包就能把
    `stats_<你当前聊天>.json` 换成任意内容，导入后冒充成用户的真实统计与
    "已付费 AI 结论"——本工具"AI 结论可回溯到本地事实"这条立论当场被打穿。
    路径穿越那一半上一轮已经挡住了（见上一条用例），这一半管的是**归属**。
    """

    H = "1111111111111111"
    OTHER = "2222222222222222"

    def setUp(self):
        # 这一族的用例都会真的往缓存目录里写文件（H / OTHER 是**合成**哈希，
        # 不是真聊天的键）。不留清理就会串味：前一条"确认后放行"的用例落了盘，
        # 后一条断言"坏条目不许发布"的用例就看到文件在那儿——看起来像校验失效，
        # 其实是上一条的残留。合成键可以安全直删，不必走级联清理。
        self._written = []

    def tearDown(self):
        for name, base in self._written:
            try:
                os.remove(os.path.join(base, name))
            except OSError:
                pass
        self._written.clear()

    def _bundle(self, entries: dict, declared: str) -> io.BytesIO:
        """造一个结果包，并把其中**可能被落盘**的条目登记进本用例的清理表。

        登记放在造夹具的地方而不是每条用例里：条目清单本来就在这儿，逐条再写一遍
        既啰嗦又漏得掉。
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps({"bundle": 1, "chat_hash": declared}))
            for name, blob in entries.items():
                zf.writestr(name, blob)
                head, _, leaf = name.rpartition("/")
                if head in ("ai_cache", "stats_cache"):
                    self._written.append(
                        (leaf, store.AI_CACHE_DIR if head == "ai_cache" else store.STATS_CACHE_DIR)
                    )
        buf.seek(0)
        return buf

    def _stats_blob(self, total: int = 999999) -> bytes:
        return json.dumps({"_created": 1.0, "overview": {"total_messages": total}}).encode()

    def test_entry_for_another_chat_is_refused(self):
        out = store.read_chat_bundle(
            self._bundle({f"stats_cache/stats_{self.OTHER}.json": self._stats_blob()}, self.H)
        )
        self.assertEqual(out["written"], 0, "声明了一份聊天，就不许写别人的键")
        self.assertGreaterEqual(out["skipped"], 1)
        self.assertFalse(
            os.path.exists(os.path.join(store.STATS_CACHE_DIR, f"stats_{self.OTHER}.json")),
            "被拒的条目不许留下文件",
        )

    def test_own_chat_entries_all_land_including_content_addressed_months(self):
        good = json.dumps({"_created": 1.0, "result": {"a": 1}}).encode()
        out = store.read_chat_bundle(
            self._bundle(
                {
                    f"ai_cache/emotion_{self.H}_deepseek-flash_deadbeef1234.json": good,
                    f"ai_cache/manifest_{self.H}.json": json.dumps(
                        {"_created": 1.0, "months": ["k1"]}
                    ).encode(),
                    "ai_cache/month_k1.json": good,  # 内容寻址：增量迁移必须跟包走
                    f"stats_cache/stats_{self.H}.json": self._stats_blob(3),
                    f"ai_cache/emotion_{self.OTHER}_deepseek-flash_deadbeef1234.json": good,
                },
                self.H,
            )
        )
        self.assertEqual(out["written"], 4, f"自己那份聊天的条目应全部落盘：{out}")
        self.assertEqual(out["skipped"], 1, "另一份聊天的维度缓存必须被拒")
        self.assertFalse(
            os.path.exists(
                os.path.join(store.AI_CACHE_DIR, f"emotion_{self.OTHER}_deepseek-flash_deadbeef1234.json")
            )
        )

    def test_unparsable_payload_is_skipped_not_published(self):
        """校验只管"能不能被读"，不管属于哪一族。

        逐族列 schema 一定会误拒合法导出（月份缓存是裸的 {期间: 结果} 映射、图片摘要
        顶层是 digest、统计顶层是 mode/_v/overview……），那比原漏洞更糟。所以这里
        只钉"非 JSON 不许发布"，并钉住"形状陌生但可解析"的内容照常导入。
        """
        out = store.read_chat_bundle(
            self._bundle(
                {
                    f"stats_cache/stats_{self.H}.json": b"not json at all",
                    f"ai_cache/emotion_{self.H}_m_fp.json": b"[1,2,3]",  # 不是 dict
                    f"ai_cache/weird_{self.H}_m_fp.json": b'{"brand_new": true}',
                },
                self.H,
            )
        )
        self.assertEqual(out["written"], 1, "只有可解析的 dict 条目该落盘")
        self.assertGreaterEqual(out["skipped"], 2)
        self.assertFalse(os.path.exists(os.path.join(store.STATS_CACHE_DIR, f"stats_{self.H}.json")))

    def _expect_written(self, head: str, leaf: str) -> str:
        """登记"这个条目会真的落盘"，交给 tearDown 清掉（手搭夹具的用例走这条）。"""
        self._written.append((leaf, store.AI_CACHE_DIR if head == "ai_cache" else store.STATS_CACHE_DIR))
        return leaf

    def test_missing_or_illegal_meta_chat_hash_refuses_the_whole_bundle(self):
        self._expect_written("stats_cache", f"stats_{self.H}.json")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"stats_cache/stats_{self.H}.json", self._stats_blob())
        buf.seek(0)
        self.assertIn("error", store.read_chat_bundle(buf), "没有 meta 就无从绑定，必须整体拒绝")
        for bad in ("", "../x", "deadbeef", "DEADBEEFDEADBEEF", self.H + ".json"):
            with self.subTest(declared=bad):
                res = store.read_chat_bundle(
                    self._bundle({f"stats_cache/stats_{self.H}.json": self._stats_blob()}, bad)
                )
                self.assertIn("error", res, f"非法 chat_hash {bad!r} 竟被接受")

    def test_corrupt_member_is_skipped_instead_of_raising(self):
        """坏 CRC 单条目失败要跳过，不许穿成 HTTP 500。

        `ZipExtFile` 校验 CRC 抛的是 `BadZipFile`，不是 OSError 的子类，原先的
        `except OSError` 接不住：整个请求变成一页 500，而前面已经 os.replace 掉的
        条目留下半包状态——用户既不知道导入了什么，也不知道失败了。
        """
        payload = self._stats_blob(1)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("meta.json", json.dumps({"bundle": 1, "chat_hash": self.H}))
            zf.writestr(f"stats_cache/stats_{self.H}.json", payload)
        raw = bytearray(buf.getvalue())
        at = raw.find(payload)
        self.assertGreater(at, 0, "夹具没在包里找到明文条目，本用例自身失效")
        raw[at + 5] = ord("9")  # 改一个字节 -> CRC 与内容对不上
        out = store.read_chat_bundle(io.BytesIO(bytes(raw)))  # 不得抛
        self.assertEqual(out.get("written", 0), 0, "CRC 坏了的内容不许发布成正式缓存")

    def test_overwriting_the_live_chat_requires_confirmation(self):
        """包声明的正是当前打开的聊天时先要一次确认，确认后放行。

        换机器恢复是合法用法（不能禁），但"随手导入一个来路不明的 zip 就把当前的
        结论换掉"必须被拦住——所以是二次确认而不是拒绝。
        """
        blob = self._bundle({f"stats_cache/stats_{self.H}.json": self._stats_blob(7)}, self.H)
        first = store.read_chat_bundle(io.BytesIO(blob.getvalue()), live_hash=self.H)
        self.assertTrue(first.get("need_confirm"), "覆盖用户正在看的聊天要先确认一次")
        self.assertNotIn("written", first)
        blob.seek(0)
        second = store.read_chat_bundle(blob, live_hash=self.H, confirm_overwrite=True)
        self.assertEqual(second.get("written"), 1, "确认后应放行（迁移的正常路径）")

    def test_other_chat_still_imports_without_a_confirm_prompt(self):
        blob = self._bundle({f"stats_cache/stats_{self.H}.json": self._stats_blob(7)}, self.H)
        out = store.read_chat_bundle(io.BytesIO(blob.getvalue()), live_hash=self.OTHER)
        self.assertFalse(out.get("need_confirm"), "不是当前聊天就不该多问一句")
        self.assertEqual(out.get("written"), 1)

    def test_unique_temp_names_are_still_matched_by_the_privacy_purge(self):
        """临时名唯一化之后，级联清理必须仍然认得它。

        改成 `.json.{pid}.{tid}.{rand}.tmp` 是为了不让两个并发写者互相删对方的半成品
        （多浏览器共用一份聊天是本项目明确支持的）。但 `_cache_belongs_to` 若还只剥
        一个死的 `.tmp`，哈希段就成了 `{hash}.json.1234…`，整段匹配判假 → 清理报称
        "已删"而那份含派生内容的残片留在盘上——上一轮刚修过的漏洞会随新命名原地复活。
        """
        a = store._tmp_sibling(f"stats_{self.H}.json")
        b = store._tmp_sibling(f"stats_{self.H}.json")
        self.assertNotEqual(a, f"stats_{self.H}.json.tmp", "临时名必须唯一")
        self.assertNotEqual(a, b, "两次调用不得给出同一个名字")
        self.assertTrue(store._cache_belongs_to(a, self.H), "新临时名必须仍被隐私清理认出")
        self.assertTrue(store._cache_belongs_to(f"stats_{self.H}.json.tmp", self.H), "旧格式要继续认")
        self.assertTrue(store._cache_belongs_to(f"emotion_{self.H}_m_fp.json.1.2.abcd.tmp", self.H))
        self.assertFalse(store._cache_belongs_to(a.replace(self.H, "0" * 16), self.H))


class TestLogPrivacy(unittest.TestCase):
    """日志：脱敏助手 + 保留天数来自配置"""

    def test_mask_name_keeps_first_char(self):
        import analyzer.logger as L

        with mock.patch.object(L, "LOG_REDACT_NAMES", True):
            self.assertEqual(L.mask_name("阿甜"), "阿*")
            self.assertEqual(L.mask_name("甜"), "*")  # 单字也不泄露全名
            self.assertEqual(L.mask_name(""), "*")
        with mock.patch.object(L, "LOG_REDACT_NAMES", False):
            self.assertEqual(L.mask_name("阿甜"), "阿甜")

    def test_timed_rotating_handler_with_retention(self):
        from logging.handlers import TimedRotatingFileHandler
        import analyzer.logger as L
        import config

        base = L.get_logger()
        handlers = [h for h in base.handlers if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(handlers), 1, "文件 handler 必须是按天轮转且只有一个")
        self.assertEqual(handlers[0].backupCount, config.LOG_RETENTION_DAYS)
        self.assertLessEqual(handlers[0].backupCount, 90, "日志保留必须有上界，5×5MB 式无限留存不得回潮")


class TestPathContainment(unittest.TestCase):
    """导出文件是可以被构造的：其中的路径字段不能把读取范围带出允许的目录"""

    def test_vision_refuses_path_outside_media_root(self):
        from analyzer import vision
        from parser.qq_parser import Message

        base = tempfile.mkdtemp(prefix="qqchatlog-traversal-")
        media_root = os.path.join(base, "exports")
        os.makedirs(media_root, exist_ok=True)
        outside = os.path.join(base, "private.png")  # 媒体根目录之外的图片
        with open(outside, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        inside = os.path.join(media_root, "ok.png")
        with open(inside, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"y" * 32)
        try:
            with mock.patch.object(vision, "MEDIA_ROOT", media_root):
                self.assertIsNone(vision.resolve("../private.png"), "不得解析到媒体根目录之外的文件")
                self.assertIsNone(vision.resolve("..\\private.png"))
                self.assertIsNone(vision.resolve("resources/../../private.png"))
                self.assertEqual(vision.resolve("ok.png"), inside)
            # 也不该被挑进"要送给模型的图片"
            msg = Message(
                id="1",
                timestamp=1704067200000,
                time_str="",
                sender_name="我",
                sender_uid="uA",
                text="",
                raw_text="",
                msg_type="type_3",
                has_image=True,
                is_reply=False,
                media_path="../private.png",
                media_w=1080,
                media_h=2400,
                media_id="X",
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", media_root),
                mock.patch.object(vision, "VISION_MIN_SIDE", 100),
            ):
                self.assertEqual(vision.pick_images([msg], limit=5), [])
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)

    def test_face_pack_refuses_path_outside_pack_dir(self):
        from analyzer import face_images as fi

        pack = tempfile.mkdtemp(prefix="qqchatlog-pack2-")
        base = os.path.dirname(pack)
        evil = os.path.join(base, "evil.gif")
        with open(evil, "wb") as f:
            f.write(b"GIF89a" + b"z" * 8)
        try:
            self.assertIsNone(
                fi.local_pack_path("n1", "../../evil", local_dir=pack), "表情名里的 ../ 不能被带出表情包目录"
            )
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)
            if os.path.exists(evil):
                os.remove(evil)

    def test_origin_header_is_sanitized_in_logs(self):
        """Origin 是请求方给的：写日志前要清掉换行/控制字符，避免伪造日志行。

        真实 HTTP 头里发不出裸换行（Werkzeug 也会拒绝），但 U+2028 这类分隔符
        能进来并且在很多查看器里渲染成换行，所以清洗放在服务端做。
        """
        import app as appmod
        from webapp import security

        self.assertEqual(security._safe_for_log("http://a\n[ERROR] 伪造"), "http://a[ERROR] 伪造")
        self.assertEqual(security._safe_for_log("http://a\u2028伪造"), "http://a伪造")
        self.assertEqual(security._safe_for_log("http://a\t伪造"), "http://a伪造")
        self.assertEqual(len(security._safe_for_log("x" * 500)), 120, "日志长度要有上限")

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        r = client.post("/upload", data={"csrf_token": token}, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403, "非白名单来源仍必须被拦下")

    def test_vision_and_face_pack_refuse_symlinks_escaping_root(self):
        from analyzer import face_images as fi
        from analyzer import vision

        base = tempfile.mkdtemp(prefix="qqchatlog-symlink-")
        media_root = os.path.join(base, "media")
        pack_root = os.path.join(base, "faces")
        os.makedirs(media_root, exist_ok=True)
        os.makedirs(pack_root, exist_ok=True)
        outside_img = os.path.join(base, "secret.png")
        outside_gif = os.path.join(base, "secret.gif")
        with open(outside_img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"a" * 16)
        with open(outside_gif, "wb") as f:
            f.write(b"GIF89a" + b"b" * 16)
        try:
            try:
                os.symlink(outside_img, os.path.join(media_root, "escape.png"))
                os.symlink(outside_gif, os.path.join(pack_root, "escape.gif"))
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不支持创建符号链接")
            with mock.patch.object(vision, "MEDIA_ROOT", media_root):
                self.assertIsNone(vision.resolve("escape.png"), "指向 MEDIA_ROOT 之外的符号链接必须拒绝")
            self.assertIsNone(
                fi.local_pack_path("n1", "escape", local_dir=pack_root),
                "指向 QQCHAT_FACE_DIR 之外的符号链接必须拒绝",
            )
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_face_download_blocks_foreign_redirects_and_non_webp_riff(self):
        from analyzer import face_images as fi

        handler = fi._AllowedRedirectHandler()
        req = urllib.request.Request("https://qzonestyle.gtimg.cn/qzone/em/e100.gif")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/steal.gif")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://qzonestyle.gtimg.cn/downgrade.gif")

        self.assertFalse(fi._looks_like_image(b"RIFF\x00\x00\x00\x00AVI "))
        self.assertTrue(fi._looks_like_image(b"RIFF\x00\x00\x00\x00WEBP"))

    def test_codex_memory_is_not_tracked_by_git(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("非 git 检出环境")
        out = subprocess.run(
            ["git", "ls-files", ".codex-memory"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "", ".codex-memory 不应纳入版本库")


if __name__ == "__main__":
    unittest.main()
