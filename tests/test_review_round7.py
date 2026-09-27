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
"""第三轮审阅修复的回归用例。

本轮修的是"审阅发现、但现有测试网不住"的一批问题，其中最重的一条是**路径穿越**：
导入的结果包里 `manifest_*.json` 的 `months` 键会被直接当成路径成分用，于是
`month_x/../../某文件` 归一后落在缓存目录之外——级联清理按它 `os.remove`（删掉用户
机器上任意 `.json`）、导出按它 `zf.write`（把任意 `.json` 的内容打进用户会分享的包）。
本文件的核心用例做的就是**先证明这个攻击成立，再证明它被挡住**（见下面
`TestMonthKeysAreNeverPaths.test_decoy_would_be_deleted_without_the_guard`）。

约定与仓库其它审查用例一致：每条都写明钉的是哪条口径，关键的几条同时给出
"反向验证"——即证明把修复去掉之后用例会变红（否则用例可能只是在描述现状）。
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime
from unittest import mock

from _bootstrap import api_configured_patcher, bootstrap  # noqa: E402

bootstrap()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_stats as gs  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import purge_marks  # noqa: E402
from parser.qq_parser import CST, load_chat  # noqa: E402
from webapp import store  # noqa: E402

BASE = 1704067200.0  # 2024-01-01 00:00:00 CST（合成夹具基线）


# ---------------------------------------------------------------------------
# 批 1：导入包的 months 键绝不许当路径用（本轮最重的一条）
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 批 2：解析层面对非字符串 type 字段不再崩
# ---------------------------------------------------------------------------
def _write_chat(payload) -> str:
    d = tempfile.mkdtemp(prefix="r7-parse-")
    path = os.path.join(d, "chat.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def _msg(msg_id, uid, name, ts_s, el_type, el_data=None, text=""):
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": "text",
        "content": {
            "text": text,
            "elements": [{"type": el_type, "data": el_data if el_data is not None else {"text": text}}],
        },
    }


class TestParserToleratesNonStringTypeFields(unittest.TestCase):
    """`type` 字段是 JSON 数字/对象时不许崩。

    症状不是"少一条消息"而是"整份文件传不上去"：upload 视图把异常当"解析失败"，
    **并删掉刚上传的文件**，界面上一句 `'int' object has no attribute 'strip'` 或
    `unhashable type: 'dict'`，与"文件格式"毫无关系。四种形状都实测复现过。
    """

    def _load(self, payload):
        path = _write_chat(payload)
        try:
            return load_chat(path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_message_type_as_object_or_array_does_not_crash(self):
        for bad_type in ({"a": 1}, ["text"], 7, None):
            with self.subTest(msg_type=bad_type):
                payload = {
                    "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
                    "statistics": {"senders": [{"uid": "uA", "name": "我"}]},
                    "messages": [
                        {**_msg(1, "uA", "我", BASE, "text", text="你好"), "type": bad_type},
                        _msg(2, "uB", "对方", BASE + 60, "text", text="在"),
                    ],
                }
                chat = self._load(payload)
                self.assertEqual(len(chat.messages), 2, "坏 type 不该让整份文件解析失败")

    def test_element_type_as_object_does_not_crash(self):
        """不可哈希的 el_type 会让 `el_type in MEDIA_KINDS` 抛 TypeError。"""
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
            "statistics": {"senders": [{"uid": "uA", "name": "我"}]},
            "messages": [
                _msg(1, "uA", "我", BASE, {"a": 1}, el_data={"text": "你好"}),
                _msg(2, "uB", "对方", BASE + 60, "text", text="在"),
            ],
        }
        chat = self._load(payload)
        self.assertEqual(len(chat.messages), 2)
        self.assertTrue(chat.unknown_element_types, "认不出的元素要如实记账，而不是崩")

    def test_chatinfo_type_as_number_does_not_crash(self):
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我", "type": 1},
            "statistics": {"senders": [{"uid": "uA", "name": "我"}]},
            "messages": [_msg(1, "uA", "我", BASE, "text", text="你好")],
        }
        chat = self._load(payload)
        self.assertEqual(chat.chat_type, "1")

    def test_non_object_top_level_is_rejected_as_a_format_error(self):
        """顶层是 null / 数字 / 数组：要给"格式不对"的人话，而不是 TypeError。"""
        for bad in (None, 123, [], "x"):
            with self.subTest(payload=bad):
                with self.assertRaises(ValueError) as ctx:
                    self._load(bad)
                self.assertIn("QQChatExporter", str(ctx.exception))

    def test_senders_name_as_number_does_not_crash(self):
        """`statistics.senders[].name` 是数字时，other_name 会带上 int 并让日志脱敏崩。

        链路：other_name=int → views 里 mask_name(chat.other_name) → logger 的
        `(name or "").strip()` → AttributeError → 上传被当成"解析失败"并删文件。

        消息里**故意不带 sender.name**：只有走 statistics.senders 那条兜底取值时，
        这个数字才会真的落到 other_name 上。消息自带名字的话，聚合名会先命中，
        这条用例就变成恒真的了（第一版就是这么写的，校验不了任何东西）。
        """
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
            "statistics": {"senders": [{"uid": "uB", "name": 12345}]},
            "messages": [_msg(1, "uB", "", BASE, "text", text="在")],
        }
        chat = self._load(payload)
        self.assertIsInstance(chat.other_name, str, "other_name 必须是字符串")
        self.assertEqual(chat.other_name, "12345", "数字名归一成字符串，而不是留在 int 上")
        from analyzer.logger import mask_name

        mask_name(chat.other_name)  # 不该抛：这正是上传路径上的那一步


# ---------------------------------------------------------------------------
# 批 3：称呼变迁按 strip 后的显示名归桶
# ---------------------------------------------------------------------------
class TestNameHistoryStripsDisplayNames(unittest.TestCase):
    """带尾空格的昵称不许被拆成两个桶——那会显示一次从未发生的改名。"""

    def _chat(self):
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
            "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "小明"}]},
            "messages": [
                _msg(1, "uA", "我", BASE, "text", text="早"),
                _msg(2, "uB", "小明", BASE + 60, "text", text="早"),
                _msg(3, "uB", "小明 ", BASE + 120, "text", text="在"),
                _msg(4, "uB", "   ", BASE + 180, "text", text="忙"),
            ],
        }
        path = _write_chat(payload)
        try:
            return load_chat(path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_trailing_space_and_blank_names_collapse(self):
        trends = ls.calc_trends(self._chat())
        other = trends["name_history"]["other"]
        names = [r["name"] for r in other]
        self.assertEqual(names, ["小明"], f"同一个人的昵称必须合成一桶：{names}")
        # 三条非空名里 "小明" ×2 与 "小明 " ×1 合并；"   " 那条不是名字（跳过），
        # 所以计数是 2 而不是 3——它的**正文**照常进词频/句长，只是不产生第三个名字。
        self.assertEqual(other[0]["count"], 2, "strip 后同一个名字的条数要合并")

    def test_reverse_verification_unstripped_names_would_split(self):
        """反向验证：不 strip 时同一个名字会裂成三桶（这就是被修掉的现象）。"""
        chat = self._chat()
        raw = {(m.sender_name or "") for m in chat.messages}
        self.assertGreater(len(raw), 1, "原始昵称里确实存在'小明'/'小明 '/'   '三种写法")
        self.assertNotIn("小明 ", [r["name"] for r in ls.calc_trends(chat)["name_history"]["other"]])


# ---------------------------------------------------------------------------
# 批 4：月份缓存的临时文件名必须唯一 + 补标记要认清理标记
# ---------------------------------------------------------------------------
class TestMonthCacheTempNamesAndRestamp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r7-month-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        mc.configure_month_cache(self.tmp)
        self.addCleanup(mc.configure_month_cache, "")
        purge_marks.clear()
        self.addCleanup(purge_marks.clear)

    def test_tmp_sibling_is_unique_and_still_recognizable(self):
        """固定 `{path}.tmp` 会被两个并发写者共踩；唯一名仍要被清理侧认得出来。"""
        path = mc.month_cache_path("concurrent1")
        names = {mc._tmp_sibling(path) for _ in range(50)}
        self.assertEqual(len(names), 50, "每个写者必须拿到各自的临时文件名")
        self.assertTrue(all(n.endswith(".tmp") for n in names))
        # 级联清理靠这个正则把临时文件认成"属于该聊天"，认不出来就会漏删敏感残留
        for n in names:
            self.assertTrue(store._TMP_SUFFIX_RE.search(os.path.basename(n)), n)
            self.assertTrue(store._cache_belongs_to(os.path.basename(n), "concurrent1"), n)

    def test_concurrent_writers_do_not_share_a_tmp_file(self):
        """两个线程写同一个键：最终文件必须是**完整的 JSON**（半截文件=丢掉一次付费结果）。"""
        key = "racekey0001"
        payloads = [{"result": {"n": i, "pad": "x" * 4000}} for i in range(2)]
        barrier = threading.Barrier(2)
        errors = []

        def worker(payload):
            try:
                barrier.wait(timeout=5)
                mc._write_month_cache(key, payload)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        with open(mc.month_cache_path(key), encoding="utf-8") as f:
            data = json.load(f)  # 半截文件会在这里抛 JSONDecodeError
        self.assertIn("n", data["result"])
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [], "成功的写入不该留下临时文件")

    def test_restamp_writes_thinking_mark_when_not_purged(self):
        """基线：没被清理过的老文件，读到就补标记（否则模式切换会一直串）。"""
        key = "legacy000001"
        path = mc.month_cache_path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"result": {"v": 1}}, f)  # 升级前的老文件：没有 _thinking

        mc._read_month_cache(key, expect_thinking=True, chat_hash="hashFresh001")
        with open(path, encoding="utf-8") as f:
            self.assertTrue(json.load(f).get("_thinking"), "基线行为：应当补上标记")

    def test_restamp_does_not_resurrect_a_purged_file(self):
        """被清理过的聊天：读侧不许把刚删掉的月份文件（含聊天原句引用）写回去。

        这是**读路径上的写**，只靠 `os.path.exists` 复查挡不住 TOCTOU；标记才是闸门。
        """
        key = "legacy000002"
        path = mc.month_cache_path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"result": {"v": 1}}, f)

        purge_marks.mark("hashPurged02")
        mc._read_month_cache(key, expect_thinking=True, chat_hash="hashPurged02")
        with open(path, encoding="utf-8") as f:
            self.assertIsNone(json.load(f).get("_thinking"), "被清理过就不许补标记（=不许写回）")

        # 反向验证：同一份文件、不打标记时必须会写回，证明上一行不是因为别的原因恒真
        purge_marks.unmark("hashPurged02")
        mc._read_month_cache(key, expect_thinking=True, chat_hash="hashPurged02")
        with open(path, encoding="utf-8") as f:
            self.assertTrue(json.load(f).get("_thinking"), "反向验证：没标记时确实会写回")

    def test_restamp_without_chat_hash_keeps_old_behaviour(self):
        """没传 chat_hash 的老调用方退化为只做 exists 复查，行为与从前一致。"""
        key = "legacy000003"
        path = mc.month_cache_path(key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"result": {"v": 1}}, f)
        mc._read_month_cache(key, expect_thinking=True)
        with open(path, encoding="utf-8") as f:
            self.assertTrue(json.load(f).get("_thinking"))


# ---------------------------------------------------------------------------
# 批 5：后台统计线程不许被 join 在未启动状态
# ---------------------------------------------------------------------------
class TestWaitForStatsDoesNotJoinAnUnstartedThread(unittest.TestCase):
    """`start_stats_job` 是"锁内登记、锁外 start"，窗口内 join 未启动线程会抛 RuntimeError。

    症状是仪表盘 500（`cannot join thread before it is started`），而它的成因恰好是
    本轮想消灭的"平白弹回首页"这条链上的另一环。修法是 start 移进锁内 + join 前查
    `is_alive()`，这里钉住后者（前者无法从外部稳定复现那个纳秒级窗口）。
    """

    HASH = "statsrace000001"

    def setUp(self):
        self.addCleanup(store._STATS_THREADS.pop, self.HASH, None)

    def test_wait_does_not_raise_on_registered_but_unstarted_thread(self):
        t = threading.Thread(target=lambda: None, name="stats-not-started")
        with store._STATS_LOCK:
            store._STATS_THREADS[self.HASH] = t
        try:
            store.wait_for_stats(self.HASH, timeout=0.01)  # 不许抛
        except RuntimeError as e:  # pragma: no cover - 失败路径
            self.fail(f"join 了未启动的线程：{e}")
        self.assertFalse(t.is_alive())

    def test_reverse_verification_joining_directly_does_raise(self):
        """反向验证：对未启动的线程直接 join 确实抛错，所以上面那道 `is_alive()` 是必需的。"""
        t = threading.Thread(target=lambda: None)
        with self.assertRaises(RuntimeError):
            t.join(0.01)


# ---------------------------------------------------------------------------
# 批 6：/api/ask 的配额错误必须回 JSON，而不是一页 500 HTML
# ---------------------------------------------------------------------------
class TestAskQuotaErrorIsJsonNot500(unittest.TestCase):
    """`_call_api` 在余额/配额/TPM 耗尽时**故意抛** QuotaExhaustedError。

    ask 原先没有 except，异常穿透视图 → Flask 的 500 HTML，"请充值"那句给人看的话
    被丢掉，前端只显示"提问失败（HTTP 500）"。其它 LLM 入口都由 _run_job 统一兜住。
    """

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        msgs = [
            _msg(1, "uA", "我", BASE, "text", text="我们什么时候去看海"),
            _msg(2, "uB", "对方", BASE + 60, "text", text="下个月吧"),
        ]
        payload = json.dumps(
            {
                "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
                "statistics": {
                    "senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}],
                    "totalMessages": len(msgs),
                },
                "messages": msgs,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        r = cls.client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "a.json")},
            headers=cls.headers,
            follow_redirects=True,
        )
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        with cls.client.session_transaction() as sess:
            cls.chat_hash = sess["chat_hash"]

    @classmethod
    def tearDownClass(cls):
        store._purge_chat_caches(cls.chat_hash)

    def _ask(self, question):
        return self.client.post(
            "/api/ask",
            json={"question": question},
            headers=self.headers,
        )

    def test_quota_error_returns_429_json_with_the_human_message(self):
        guard = api_configured_patcher()
        guard.start()
        self.addCleanup(guard.stop)
        msg = "套餐额度耗尽，请充值后再试"
        with mock.patch(
            "analyzer.recap_client.answer_question",
            side_effect=dc.QuotaExhaustedError(msg),
        ):
            r = self._ask("看海后来还去吗")
        self.assertEqual(r.status_code, 429, f"必须是可判定的 429，实际 {r.status_code}")
        self.assertEqual(r.mimetype, "application/json", "JSON 端点不许回 HTML 错误页")
        body = r.get_json()
        self.assertIn(msg, body.get("error", ""), "写给人的那句'请充值'不能被丢掉")
        self.assertTrue(body.get("quota"))

    def test_other_failures_are_not_disguised_as_quota_errors(self):
        """不误伤：非配额异常照旧走 500，不许被当成"额度问题"给用户看错方向。

        （Flask 的 test_client 默认把未捕获异常转成 500 响应，所以这里断言状态码与
        响应体，而不是 assertRaises：原实现下 QuotaExhaustedError 走的正是这条路，
        用户拿到的是一页 HTML 500 与丢失的"请充值"。）
        """
        guard = api_configured_patcher()
        guard.start()
        self.addCleanup(guard.stop)
        # 这条路会走 Flask 的兜底异常处理并**故意**打一条 traceback：把它静音，
        # 免得 CI 日志里出现一段看着像失败的堆栈（用例本身断言的是 500 响应）。
        quiet = mock.patch(
            "flask.app.Flask.logger",
            new_callable=mock.PropertyMock,
            return_value=logging.getLogger("r7-silent-flask"),
        )
        quiet.start()
        self.addCleanup(quiet.stop)
        # PROPAGATE_EXCEPTIONS 显式钉成 False：别的用例可能把 app.testing 打开过而没还原，
        # 那时 Flask 会把异常直接抛出来（而不是转成 500），这条用例就会随执行顺序变红——
        # 实测在整套里跑就是这样（单文件跑会绿）。用例要自己决定这件事。
        import app as appmod

        prop = mock.patch.dict(appmod.app.config, {"PROPAGATE_EXCEPTIONS": False})
        prop.start()
        self.addCleanup(prop.stop)
        with mock.patch(
            "analyzer.recap_client.answer_question",
            side_effect=ValueError("boom"),
        ):
            r = self._ask("看海后来还去吗")
        self.assertEqual(r.status_code, 500)
        self.assertNotEqual(r.mimetype, "application/json", "普通异常不该被包装成配额响应")


# ---------------------------------------------------------------------------
# 批 7：互动矩阵按"生效上限"缓存（大群不再每人重算一遍 n×n）
# ---------------------------------------------------------------------------
class TestInteractionMatrixIsMemoisedPerEffectiveLimit(unittest.TestCase):
    """`_member_context` 是"每位成员一次调用"，每次都要不截断的矩阵。

    不缓存时几千人的群每分析一位成员就重算 n×n（实测 n=3000 单次 4.4s / 376MB）。
    键取**已解析的生效上限**而不是入参 top_k：配置是调用时读的，用 top_k 当键会让
    "改了配置仍拿回旧上限的矩阵"（既有用例 test_matrix_limit_reads_config_at_call_time
    钉的就是这条语义）。
    """

    def _chat(self, n_members=4):
        msgs = []
        seq = 0
        for i in range(n_members):
            uid = f"u{i}"
            for _ in range(n_members - i):
                msgs.append(_msg(seq, uid, uid, BASE + seq * 2, "text", text="话"))
                seq += 1
        payload = {
            "chatInfo": {"name": "摸鱼群", "selfUid": "u0", "selfName": "u0", "type": "group"},
            "statistics": {"senders": [{"uid": f"u{i}", "name": f"u{i}"} for i in range(n_members)]},
            "messages": msgs,
        }
        path = _write_chat(payload)
        try:
            return load_chat(path)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_same_limit_returns_the_cached_object(self):
        chat = self._chat()
        first = gs.calc_interaction_matrix(chat, top_k=0)
        second = gs.calc_interaction_matrix(chat, top_k=0)
        self.assertIs(first, second, "同一个生效上限必须命中缓存（否则成员画像里就是白算）")

    def test_different_limits_are_cached_separately(self):
        chat = self._chat()
        full = gs.calc_interaction_matrix(chat, top_k=0)
        capped = gs.calc_interaction_matrix(chat, top_k=2)
        self.assertIsNot(full, capped)
        self.assertEqual(len(full["members"]), 4)
        self.assertEqual(len(capped["members"]), 2)
        # 再取一次：两者都还在缓存里，互不覆盖
        self.assertIs(gs.calc_interaction_matrix(chat, top_k=0), full)
        self.assertIs(gs.calc_interaction_matrix(chat, top_k=2), capped)

    def test_config_is_still_read_at_call_time(self):
        """缓存键必须是**解析后的**上限：改配置后不许拿回旧上限的结果。"""
        chat = self._chat()
        with mock.patch("analyzer.group_stats.config.GROUP_MATRIX_MEMBERS", 2):
            self.assertEqual(gs.calc_interaction_matrix(chat)["matrix_limit"], 2)
        with mock.patch("analyzer.group_stats.config.GROUP_MATRIX_MEMBERS", 4):
            self.assertEqual(gs.calc_interaction_matrix(chat)["matrix_limit"], 4)

    def test_cache_is_per_chat_object(self):
        """两份不同的聊天不许互相命中（缓存挂在 chat 对象上，不挂在模块上）。"""
        a, b = self._chat(4), self._chat(3)
        self.assertIsNot(gs.calc_interaction_matrix(a, top_k=0), gs.calc_interaction_matrix(b, top_k=0))


# ---------------------------------------------------------------------------
# 批 8：对外可见的错误文本必须先洗掉 API Key
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 批 9：幽灵引用不许把"删除本聊天"挡住
# ---------------------------------------------------------------------------
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


if __name__ == "__main__":
    unittest.main()
