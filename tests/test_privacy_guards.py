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
import time
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


class TestFingerprintMigration(unittest.TestCase):
    """指纹公式从"源码原文"改成"AST 归一"时，既有缓存必须继续可用并迁移到新键。

    这是"改公式不能让用户重新付费"的唯一保障。反向验证：把 _read_cache 里的旧键回退
    分支删掉 → 维度缓存那条红；把 _analyze_periods 的 _keys_for 回退删掉 → 月份那条红。
    """

    def test_legacy_dimension_cache_is_read_and_migrated(self):
        from webapp import store

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "AI_CACHE_DIR", d):
                chat_hash = "hashMigrate01"
                result = {"month_title": "《旧指纹写下的结果》"}
                legacy_path = store._cache_path("emotion", chat_hash, legacy=True)
                current_path = store._cache_path("emotion", chat_hash)
                self.assertNotEqual(legacy_path, current_path, "前提：新旧指纹确实不同")
                with open(legacy_path, "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "result": result}, f, ensure_ascii=False)

                self.assertEqual(
                    store._read_cache("emotion", chat_hash),
                    result,
                    "旧指纹命名的缓存必须照常命中，否则用户为同样的分析重新付费",
                )
                self.assertTrue(os.path.exists(current_path), "命中旧文件后应当改名到当前键")
                self.assertFalse(os.path.exists(legacy_path), "改名而不是复制：不留第二份敏感内容")

    def test_legacy_month_cache_is_read_and_migrated(self):
        from analyzer import deepseek_client as dc
        from analyzer import month_cache as mc

        with tempfile.TemporaryDirectory() as d:
            mc.configure_month_cache(d)
            try:
                prompt = "以下是某月的对话数据：\n\n[01-01 08:00] 我: 在吗"
                new_key = mc._month_key("SYS", prompt)
                legacy_key = mc._month_key("SYS", prompt, dc.PROMPT_FINGERPRINT_LEGACY)
                self.assertNotEqual(new_key, legacy_key, "前提：新旧指纹给出的键确实不同")
                with open(mc.month_cache_path(legacy_key), "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "self_emotion": "平静"}, f, ensure_ascii=False)

                calls = []

                def fake_api(*_a, **_kw):
                    calls.append(1)
                    return {"self_emotion": "不该被调用"}

                with mock.patch.object(dc, "_call_api", side_effect=fake_api):
                    out = dc._analyze_periods(
                        {"2024-01": []},
                        "SYS",
                        lambda _p, _m: prompt,
                        max_tokens=16,
                        tag="emotion",
                        chat_hash="hashMigrate02",
                    )
                self.assertEqual(calls, [], "旧键里的月份结果可用时不该再调用 API")
                self.assertEqual(out["2024-01"]["self_emotion"], "平静")
                self.assertTrue(os.path.exists(mc.month_cache_path(new_key)), "应改名到当前键")
                self.assertFalse(os.path.exists(mc.month_cache_path(legacy_key)))
                # 迁移后的新键必须被 manifest 记账，否则宽限期后会被当孤儿删掉（白付费）
                manifest = json.load(io.open(mc._manifest_path("hashMigrate02"), encoding="utf-8"))
                self.assertIn(new_key, manifest["months"])
            finally:
                mc.configure_month_cache("")

    def test_ast_normalization_ignores_comments_and_formatting(self):
        """AST 归一只丢与模型输入无关的差异，逻辑改动照样换键

        三个变体用**同名**的嵌套函数：函数名也在 AST 里，不同名会被判为"逻辑变了"，
        那是刻意的行为（改名 = 换键），不是这条用例要验的东西。
        嵌套还顺带覆盖了 dedent：getsource 给的是带缩进的片段，不 dedent 会解析失败
        并静默退回原文。
        """
        from analyzer.deepseek_client import _hashed_source

        def make_with_comment():
            def sample(x):
                # 这条注释不该影响哈希
                return x + 1

            return sample

        def make_without_comment():
            def sample(x):
                return x + 1

            return sample

        def make_with_other_logic():
            def sample(x):
                return x + 2

            return sample

        commented = make_with_comment()
        plain = make_without_comment()
        other = make_with_other_logic()
        self.assertEqual(commented.__name__, plain.__name__, "前提：三个变体同名（改名本来就该换键）")
        self.assertEqual(
            _hashed_source(commented),
            _hashed_source(plain),
            "注释与空白不应影响指纹（否则改个错别字就要用户重新付费）",
        )
        self.assertNotEqual(
            _hashed_source(plain),
            _hashed_source(other),
            "逻辑变了必须换键：否则新格式会顶着旧缓存返回",
        )
        dump = _hashed_source(plain)
        self.assertIn("FunctionDef", dump, "返回值应当是 ast.dump 的形态，而不是退回的原文")
        self.assertNotIn("return x + 1", dump, "AST dump 里不会保留原始代码文本")

    def test_legacy_vision_digest_is_read_and_migrated(self):
        """图片摘要的键里也含指纹：公式一改，既有摘要缓存同样不能丢。

        直接驱动 vision.digest（而不是手工重演 os.replace）：摘要缓存的键、改名、
        以及"不再调模型"三件事要一起验。迁移一旦失效，这里会走到 _call_vision——
        测试环境没有 API Key，它返回空串，于是断言失败而不是真的出网。
        """
        from analyzer import vision

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(vision, "AI_CACHE_DIR", d):
                images = [{"key": "img-fingerprint-1", "path": "x", "size": 1, "mime": "image/png"}]
                key = vision._images_key(images)
                legacy_key = vision._images_key(images, legacy=True)
                self.assertNotEqual(key, legacy_key, "前提：新旧指纹给出的键确实不同")
                legacy_path = vision._cache_path("hashMigrate03", legacy_key)
                with open(legacy_path, "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "digest": "旧指纹写下的图片摘要"}, f, ensure_ascii=False)

                with (
                    mock.patch.object(vision, "available", lambda *_a, **_kw: True),
                    mock.patch.object(vision, "pick_images", lambda *_a, **_kw: images),
                    mock.patch.object(vision, "_memo_get", lambda *_a, **_kw: None),
                    mock.patch.object(vision, "_memo_put", lambda *_a, **_kw: None),
                ):
                    text = vision.digest([], chat_hash="hashMigrate03", label="测试")

                self.assertEqual(text, "旧指纹写下的图片摘要", "旧键里的摘要必须照常返回")
                self.assertTrue(os.path.exists(vision._cache_path("hashMigrate03", key)), "应改名到当前键")
                self.assertFalse(os.path.exists(legacy_path), "改名而不是复制：不留第二份图片描述")

    def test_legacy_group_dimension_cache_is_read_and_migrated(self):
        """群聊维度的缓存文件名里嵌的是**群聊**指纹：私聊那条用例覆盖不到它

        群聊维度（member_profiles 等）走 fingerprint_for_dimension 的群聊分支，
        旧键同样要认、要改名、要不留第二份。
        """
        from analyzer import group_client as gc
        from webapp import store

        self.assertNotEqual(
            gc.GROUP_PROMPT_FINGERPRINT,
            gc.GROUP_PROMPT_FINGERPRINT_LEGACY,
            "前提：群聊新旧指纹确实不同",
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(store, "AI_CACHE_DIR", d):
                chat_hash = "hashMigrateGroup"
                result = {"members": ["旧指纹写下的群聊结果"]}
                legacy_path = store._cache_path("member_profiles", chat_hash, legacy=True)
                current_path = store._cache_path("member_profiles", chat_hash)
                self.assertNotEqual(legacy_path, current_path, "前提：群聊维度确实落在群聊指纹上")
                self.assertIn(gc.GROUP_PROMPT_FINGERPRINT_LEGACY, legacy_path)
                with open(legacy_path, "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "result": result}, f, ensure_ascii=False)

                self.assertEqual(
                    store._read_cache("member_profiles", chat_hash),
                    result,
                    "旧群聊指纹命名的缓存必须照常命中，否则群聊用户为同样的分析重新付费",
                )
                self.assertTrue(os.path.exists(current_path), "命中旧文件后应当改名到当前键")
                self.assertFalse(os.path.exists(legacy_path), "改名而不是复制：不留第二份敏感内容")

    def test_legacy_group_month_cache_is_read_and_migrated(self):
        """群聊月份缓存键里也是群聊指纹：旧键读到即迁移，并按**当前键**记进 manifest"""
        from analyzer import deepseek_client as dc
        from analyzer import group_client as gc
        from analyzer import month_cache as mc

        with tempfile.TemporaryDirectory() as d:
            mc.configure_month_cache(d)
            try:
                prompt = "以下是某月的群聊对话数据：\n\n[01-01 08:00] 小明: 在吗"
                group_fp = gc.GROUP_PROMPT_FINGERPRINT
                new_key = mc._month_key("SYS", prompt, group_fp)
                legacy_key = mc._month_key("SYS", prompt, gc.GROUP_PROMPT_FINGERPRINT_LEGACY)
                self.assertNotEqual(new_key, legacy_key, "前提：群聊新旧指纹给出的键确实不同")
                self.assertNotEqual(
                    new_key,
                    mc._month_key("SYS", prompt),
                    "前提：群聊月份键与私聊月份键不是同一个键（两类缓存互不干扰）",
                )
                with open(mc.month_cache_path(legacy_key), "w", encoding="utf-8") as f:
                    json.dump({"_created": 1.0, "group_emotion": "平静"}, f, ensure_ascii=False)

                calls = []

                def fake_api(*_a, **_kw):
                    calls.append(1)
                    return {"group_emotion": "不该被调用"}

                with mock.patch.object(dc, "_call_api", side_effect=fake_api):
                    out = dc._analyze_periods(
                        {"2024-01": []},
                        "SYS",
                        lambda _p, _m: prompt,
                        max_tokens=16,
                        tag="group_emotion",
                        chat_hash="hashMigrateGroup2",
                        fingerprint=group_fp,
                    )
                self.assertEqual(calls, [], "旧键里的群聊月份结果可用时不该再调用 API")
                self.assertEqual(out["2024-01"]["group_emotion"], "平静")
                self.assertTrue(os.path.exists(mc.month_cache_path(new_key)), "应改名到当前键")
                self.assertFalse(os.path.exists(mc.month_cache_path(legacy_key)))
                manifest = json.load(io.open(mc._manifest_path("hashMigrateGroup2"), encoding="utf-8"))
                self.assertIn(new_key, manifest["months"], "迁移后必须按当前键记账，否则会被当孤儿删掉")
            finally:
                mc.configure_month_cache("")


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


if __name__ == "__main__":
    unittest.main()
