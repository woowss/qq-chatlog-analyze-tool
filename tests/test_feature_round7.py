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
"""功能优化轮 · 批1：语音进媒体口径、未识别元素探测、撤回计数、total_other_media 差集修复。

每条修复都做过反向验证（把实现改回旧行为，用例必须变红），断言里写明"钉的是哪条口径"。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

from _bootstrap import api_configured_patcher, bootstrap  # noqa: E402
from result_fixtures import ask as valid_ask  # noqa: E402

bootstrap()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import local_stats as ls  # noqa: E402
from analyzer.dialog import _message_line  # noqa: E402
from parser.qq_parser import CST, load_chat  # noqa: E402


def _el(msg_id, uid, name, ts_s, el_type, data, text=""):
    """构造一条"带结构化元素"的消息（新版导出器形态）"""
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": el_type,
        "content": {"text": text, "elements": [{"type": el_type, "data": data}]},
    }


def _text(msg_id, uid, name, ts_s, text):
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": "text",
        "content": {"text": text, "elements": [{"type": "text", "data": {"text": text}}]},
    }


def _load(msgs, self_uid="uA"):
    payload = json.dumps(
        {
            "chatInfo": {"name": "对方", "selfUid": self_uid, "selfName": "我"},
            "statistics": {
                "senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}],
                "totalMessages": len(msgs),
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        return load_chat(path)
    finally:
        os.remove(path)


BASE = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp())


class TestVoiceMedia(unittest.TestCase):
    """语音消息本体此前不在任何分派分支：以零正文混进计数、模型完全看不见。"""

    def test_voice_gets_kind_and_label(self):
        chat = _load([_el(1, "uB", "对方", BASE, "voice", {"duration": 12})])
        m = chat.messages[0]
        self.assertEqual(m.media_kind, "voice", "语音必须进媒体口径（kind 非空才不被当空正文）")
        self.assertIn("12", m.media_label)
        self.assertEqual(m.text, "", "占位符不进正文，避免污染词频与句长")

    def test_voice_dialog_line_has_mark(self):
        chat = _load([_el(1, "uB", "对方", BASE, "voice", {"duration": 5})])
        m = chat.messages[0]
        line = _message_line(m, "对方")
        self.assertIn("语音", line, "喂模型的对话行要标注语音，否则模型看不见它发生过")

    def test_voice_without_duration_still_labeled(self):
        chat = _load([_el(1, "uB", "对方", BASE, "voice", {"summary": ""})])
        self.assertEqual(chat.messages[0].media_kind, "voice")


class TestUnknownElements(unittest.TestCase):
    """未知元素类型不再被静默吞掉：计数、上报，且绝不静默把正文判死。"""

    def test_unknown_type_is_counted(self):
        chat = _load(
            [
                _text(1, "uA", "我", BASE, "早"),
                _el(2, "uB", "对方", BASE + 60, "brand_new_thing", {"x": 1}),
            ]
        )
        self.assertEqual(chat.unknown_element_types.get("brand_new_thing"), 1)
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["unknown_element_types"].get("brand_new_thing"), 1, "overview 要能拿到漂移信号")

    def test_all_unknown_message_falls_back_to_raw_text(self):
        # 导出器把 "text" 改名成新类型：结构化侧给不出正文，但原始文本在——不能丢。
        m = {
            "id": "1",
            "timestamp": BASE * 1000,
            "time": "2025-03-01 20:00:00",
            "sender": {"uid": "uB", "name": "对方"},
            "type": "renamed_text",
            "content": {"text": "记得吃饭", "elements": [{"type": "renamed_text", "data": {}}]},
        }
        chat = _load([m])
        self.assertEqual(chat.messages[0].text, "记得吃饭", "全未知元素时回落原始文本，正文不丢")

    def test_image_only_message_does_not_fall_back(self):
        # 有已识别元素（image）的消息不回落，以免把 "[图片]" 当成正文（旧口径钉住）。
        chat = _load([_el(1, "uB", "对方", BASE, "image", {"url": "x.jpg"}, text="[图片]")])
        self.assertEqual(chat.messages[0].text, "")


class TestRecallCounts(unittest.TestCase):
    """撤回标记已解析却零消费——补上按人计数（recalled 内容仍不进正文统计，那是正确的）。"""

    def test_recall_counted_per_party(self):
        msgs = [
            _text(1, "uA", "我", BASE, "在吗"),
            _text(2, "uB", "对方", BASE + 60, "在的"),
        ]
        msgs[1]["recalled"] = True
        msgs.append({**_text(3, "uA", "我", BASE + 120, "算了"), "recalled": True})
        ov = ls.calc_overview(_load(msgs))
        self.assertEqual(ov["total_recalls"], 2)
        self.assertEqual(ov["self_recalls"], 1)
        self.assertEqual(ov["other_recalls"], 1)

    def test_recalled_not_in_body_stats(self):
        # 反向锚点：撤回的那条不该进 total_messages（正文口径），只进撤回计数。
        msgs = [
            _text(1, "uA", "我", BASE, "你好"),
            {**_text(2, "uB", "对方", BASE + 60, "撤回我"), "recalled": True},
        ]
        ov = ls.calc_overview(_load(msgs))
        self.assertEqual(ov["total_messages"], 1)
        self.assertEqual(ov["total_recalls"], 1)


class TestOtherMediaFix(unittest.TestCase):
    """total_other_media 此前写死枚举三类，卡片/通话被解析被计数却漏进汇总（界面恒显示 0）。"""

    def test_card_and_call_now_included(self):
        chat = _load(
            [
                _el(1, "uB", "对方", BASE, "json", {"summary": "分享了一个链接"}),
                _el(2, "uB", "对方", BASE + 60, "av_record", {"summary": "通话 - 未接听"}),
            ]
        )
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_other_media"], 2, "卡片+通话都要进「其他」，旧枚举口径是 0")

    def test_no_double_count_with_voice(self):
        chat = _load(
            [
                _el(1, "uB", "对方", BASE, "voice", {"duration": 3}),
                _el(2, "uB", "对方", BASE + 60, "wallet", {"summary": "恭喜发财"}),
            ]
        )
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_voices"], 1)
        # 语音单独成项，不重复计入 other；other 只剩红包
        self.assertEqual(ov["total_other_media"], 1)


class TestStatsVersionPin(unittest.TestCase):
    def test_bumped(self):
        from webapp import store

        # 旧缓存里"其他=0"是错的，必须失效重算（只花本地 CPU，零 API 费用）。
        self.assertGreaterEqual(store.STATS_SCHEMA_VERSION, 6)
        self.assertGreaterEqual(store.GROUP_STATS_SCHEMA_VERSION, 4)


# ---------------------------------------------------------------------------
# 批 3：webapp 功能（保留期可配 / 删除当前聊天 / 结果导出导入 / 连通自检 / 群盐分轨）
# ---------------------------------------------------------------------------
import io  # noqa: E402
import zipfile  # noqa: E402
from unittest import mock  # noqa: E402

from webapp import store  # noqa: E402


def _touch_cache(name: str, content: str = '{"_created": 1}', age_days: int = 0) -> str:
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
            _touch_cache(f"emotion_{self.HASH}_model_fp123.json"),
            _touch_cache(f"vision_{self.HASH}_deadbeef.json"),
            _touch_cache(f"manifest_{self.HASH}.json", json.dumps({"months": ["mkey1", "mkey2"]})),
            _touch_cache("month_mkey1.json"),
            _touch_cache("month_mkey2.json"),
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


class TestRetentionSwitch(unittest.TestCase):
    """QQCHAT_CACHE_SLIDE_DAYS / QQCHAT_CACHE_MAX_DAYS 决定派生缓存的命运；0 = 不过期。"""

    def _aged(self, name: str, days: int):
        return _touch_cache(name, json.dumps({"_created": time.time() - days * 86400}), age_days=days)

    def test_default_still_collects(self):
        p = self._aged("emotion_hash_x.json", 40)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 30), mock.patch.object(cm, "CACHE_MAX_DAYS", 90):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertFalse(os.path.exists(p))

    def test_zero_means_never_expire(self):
        p = self._aged("emotion_hash_y.json", 400)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 0), mock.patch.object(cm, "CACHE_MAX_DAYS", 0):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertTrue(os.path.exists(p), "两条规则都关掉后，再老也不许删（用户显式要永久保留）")
        os.remove(p)

    def test_slide_off_hard_still_applies(self):
        p = self._aged("emotion_hash_z.json", 100)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 0), mock.patch.object(cm, "CACHE_MAX_DAYS", 90):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertFalse(os.path.exists(p), "关掉滑动不关掉绝对上限")


class TestGroupSaltSplit(unittest.TestCase):
    """QQCHAT_GROUP_CACHE_SALT：非空才并入群聊指纹；私聊指纹对它完全免疫。"""

    def test_empty_keeps_fingerprint_identical(self):
        from analyzer import group_client as gc

        base = gc.group_prompt_fingerprint()
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CACHE_SALT": ""}):
            self.assertEqual(gc.group_prompt_fingerprint(), base, "默认（空盐）时指纹逐字节不变")

    def test_group_salt_rotates_group_only(self):
        from analyzer import deepseek_client as dc
        from analyzer import group_client as gc

        base_g, base_p = gc.group_prompt_fingerprint(), dc.PROMPT_FINGERPRINT
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CACHE_SALT": "redo-group"}):
            self.assertNotEqual(gc.group_prompt_fingerprint(), base_g, "群盐应参与群聊指纹")
        from analyzer.deepseek_client import _prompt_fingerprint

        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CACHE_SALT": "redo-group"}):
            self.assertEqual(_prompt_fingerprint(), base_p, "群盐绝不能动私聊指纹（那会让私聊用户重新付费）")


class TestConnectionPing(unittest.TestCase):
    def test_unconfigured_says_so(self):
        from analyzer import deepseek_client as dc

        with mock.patch.object(dc, "DEEPSEEK_API_KEY", ""):
            ok, detail = dc.test_connection()
        self.assertFalse(ok)
        self.assertIn("API Key 未配置", detail)

    def test_error_detail_is_honest_and_bounded(self):
        from analyzer import deepseek_client as dc

        class _Completions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("Error code: 401 - " + "x" * 500)

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        with (
            mock.patch.object(dc, "DEEPSEEK_API_KEY", "sk-secret-not-logged"),
            mock.patch.object(dc, "_get_client", return_value=_Client()),
        ):
            ok, detail = dc.test_connection()
        self.assertFalse(ok)
        self.assertIn("RuntimeError", detail, "错误类型与文本就是排障线索，必须原样带回")
        self.assertLessEqual(len(detail), 260, "错误截断在预算内")
        self.assertNotIn("sk-secret", detail, "Key 绝不能出现在回显里")

    def test_success_reports_model_and_latency(self):
        from analyzer import deepseek_client as dc

        class _Completions:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] == 1, "自检必须是最小请求，不能顺手烧一次真实分析的钱"
                return object()

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        with (
            mock.patch.object(dc, "DEEPSEEK_API_KEY", "sk-test"),
            mock.patch.object(dc, "_get_client", return_value=_Client()),
        ):
            ok, detail = dc.test_connection()
        self.assertTrue(ok)
        self.assertIn("延迟", detail)


class TestTrends(unittest.TestCase):
    """calc_trends：逐月互发/句长、≥3 天冷场的重启与重启方、称呼变迁、首条消息。"""

    def _two_party_msgs(self):
        day = 86400
        return [
            _text(1, "uA", "我", BASE, "在吗"),
            _text(2, "uB", "对方", BASE + 60, "在的呀宝贝"),
            # 冷场 5 天后"我"先开口 → 一次重启（重启方=我）
            _text(3, "uA", "我", BASE + (5 * day), "最近好吗"),
            _text(4, "uB", "新备注", BASE + (5 * day) + 60, "挺好的"),
        ]

    def test_months_and_avg_len(self):
        chat = _load(self._two_party_msgs())
        months = ls.calc_trends(chat)["months"]
        self.assertEqual(len(months), 1, "都在同一个月")
        m0 = months[0]
        self.assertEqual((m0["self"], m0["other"]), (2, 2))
        # 句长只数非空正文：对方 = (5+3)/2，我 = (2+4)/2
        self.assertEqual(m0["avg_len_other"], 4.0)
        self.assertEqual(m0["avg_len_self"], 3.0)

    def test_restart_detection_and_who(self):
        chat = _load(self._two_party_msgs())
        r = ls.calc_trends(chat)["restarts"]
        self.assertEqual(r["threshold_days"], 3)
        self.assertEqual(r["count"], 1)
        self.assertEqual((r["self"], r["other"]), (1, 0), "冷场后是我先开口")
        # 5 天冷场但第二条消息晚 60 秒，间隔向下取整为 4 天（口径：完整天数才算）
        self.assertEqual(r["events"][0]["gap_days"], 4)

    def test_no_restart_below_threshold(self):
        day = 86400
        chat = _load([_text(1, "uA", "我", BASE, "早"), _text(2, "uB", "对方", BASE + 2 * day, "早")])
        self.assertEqual(ls.calc_trends(chat)["restarts"]["count"], 0, "隔 2 天不算冷场重启")

    def test_name_history_tracks_variants(self):
        chat = _load(self._two_party_msgs())
        nh = ls.calc_trends(chat)["name_history"]
        other_names = [n["name"] for n in nh["other"]]
        self.assertIn("对方", other_names)
        self.assertIn("新备注", other_names, "显示名变化必须留下轨迹（称呼变迁是关系信号）")
        changed = next(n for n in nh["other"] if n["name"] == "新备注")
        self.assertEqual(changed["count"], 1)

    def test_first_message_has_content(self):
        chat = _load(self._two_party_msgs())
        fm = ls.calc_trends(chat)["first_message"]
        self.assertEqual(fm["text"], "在吗")
        self.assertEqual(fm["who"], "self")

    def test_empty_chat_degrades(self):
        chat = _load([_text(1, "uA", "我", BASE, "x")])
        chat.messages = []
        tr = ls.calc_trends(chat)
        self.assertEqual(tr["months"], [])
        self.assertIsNone(tr["first_message"])

    def test_compute_stats_carries_trends(self):
        from webapp import store

        chat = _load(self._two_party_msgs())
        stats = store.compute_stats(chat)
        self.assertIn("trends", stats, "trends 必须进统计载荷（页面与导出共用同一份）")
        self.assertTrue(stats["trends"]["months"])


class TestMessagesApi(unittest.TestCase):
    """消息浏览/搜索 API（批 4）：过滤、分页、上下文定位、"看得见也搜得着"口径。"""

    @classmethod
    def setUpClass(cls):
        import io as _io

        import app as appmod
        from parser.qq_parser import CST

        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}

        base = int(datetime(2025, 1, 10, 20, 0, tzinfo=CST).timestamp())
        day = 86400
        msgs = [
            _text(1, "uA", "我", base, "今晚吃什么"),
            _text(2, "uB", "对方", base + 60, "火锅"),
            _text(3, "uA", "我", base + 2 * day, "好想念火锅"),
            _text(4, "uB", "对方", base + 60 * day, "下个月见面"),
            _el(5, "uB", "对方", base + 61 * day, "image", {"url": "x.jpg", "md5": "m1", "size": "1"}),
            {**_text(6, "uA", "我", base + 62 * day, "说错话了"), "recalled": True},
            {**_text(7, "uB", "系统消息", base + 63 * day, "对方撤回了一条消息"), "system": True},
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
            data={"file": (_io.BytesIO(payload), "chat.json")},
            headers=cls.headers,
            follow_redirects=True,
        )
        assert r.status_code == 200, f"上传失败: {r.status_code}"
        with cls.client.session_transaction() as sess:
            cls.chat_hash = sess["chat_hash"]

    @classmethod
    def tearDownClass(cls):
        with cls.client.session_transaction() as sess:
            fp = sess.get("filepath")
        if fp and os.path.exists(fp):
            os.remove(fp)
        store._purge_chat_caches(cls.chat_hash)

    def _get(self, **params):
        r = self.client.get("/api/messages", query_string=params)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return r.get_json()

    def test_all_listed_with_honest_flags(self):
        res = self._get()
        # 原始浏览"如实呈现"：撤回与系统消息也列出（打标而非隐藏）
        self.assertEqual(res["total"], 7)
        flags = {m["id"]: (m["recalled"], m["system"]) for m in res["messages"]}
        self.assertTrue(flags["6"][0], "撤回消息带标记")
        self.assertTrue(flags["7"][1], "系统消息带标记")

    def test_keyword_search(self):
        res = self._get(q="火锅")
        self.assertEqual(res["total"], 2, "两条正文含火锅")
        self.assertIn("3", {m["id"] for m in res["messages"]})
        self.assertNotIn("1", {m["id"] for m in res["messages"]})

    def test_filters_sender_month_range(self):
        self.assertEqual(self._get(sender="self")["total"], 3, "我发的 1/3/6（含撤回的）")
        res = self._get(month="2025-03")
        self.assertTrue(res["messages"])
        self.assertTrue(all(m["time"].startswith("2025-03") for m in res["messages"]))
        res = self._get(**{"from": "2025-03-12"})
        self.assertTrue(all(m["time"][:10] >= "2025-03-12" for m in res["messages"]))

    def test_pagination(self):
        res = self._get(per_page=3, page=2)
        self.assertEqual(len(res["messages"]), 3)
        self.assertEqual(res["pages"], 3)

    def test_around_returns_context_window(self):
        res = self._get(around="3")
        ids = [m["id"] for m in res["messages"]]
        self.assertIn("3", ids)
        self.assertIn("2", ids, "上下文窗口含前一条")

    def test_media_message_searchable(self):
        res = self._get(q="图片")
        self.assertEqual(res["total"], 1, "无正文的图片消息用占位标签可被搜到（看得见也搜得着）")
        self.assertEqual(res["messages"][0]["id"], "5")
        self.assertIn("图片", res["messages"][0]["text"])

    def test_page_renders(self):
        r = self.client.get("/messages")
        self.assertEqual(r.status_code, 200)
        self.assertIn("msgList", r.get_data(as_text=True))
        # 同一会话（私聊）下总括页也应正常渲染（模板只被冒烟覆盖过空会话分支）
        r = self.client.get("/recap")
        self.assertEqual(r.status_code, 200)
        self.assertIn("recapResult", r.get_data(as_text=True))


class TestStopwordsExternal(unittest.TestCase):
    """停用词外部化（评审遗留 #18）：内置词表永在，文件只做加法；改了文件词云要重算。"""

    def _freq(self, words):
        from datetime import timedelta

        msgs = []
        t = BASE
        for w in words:
            msgs.append(_text(len(msgs) + 1, "uA", "我", t, w))
            t += int(timedelta(minutes=1).total_seconds())
        return ls.calc_word_freq(_load(msgs))

    def test_builtin_never_disappears(self):
        out = self._freq(["因为 因为"])
        self.assertNotIn("因为", {x["word"] for x in out["self"]}, "内置停用词永远生效")

    def test_external_file_is_additive_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stop.txt")
            base_fp = ls.stopwords_fingerprint()
            with open(path, "w", encoding="utf-8") as f:
                f.write("# 我们的口头禅，从词云里摘掉\n加班\n")
            ls._STOP_CACHE.update(stamp="force-reload", words=frozenset(), text="")
            with mock.patch.dict(os.environ, {"QQCHAT_STOPWORD_FILE": path}):
                fp2 = ls.stopwords_fingerprint()
                self.assertNotEqual(fp2, base_fp, "文件改了，指纹必须变（词云才会重算）")
                out = self._freq(["加班 加班", "加班"])
                self.assertNotIn("加班", {x["word"] for x in out["self"]})
                self.assertEqual(out["stop_fp"], fp2)
            ls._STOP_CACHE.update(stamp="force-reload", words=frozenset(), text="")
            self.assertEqual(ls.stopwords_fingerprint(), base_fp, "撤掉文件回到内置口径")

    def test_freq_result_carries_fingerprint(self):
        self.assertIn("stop_fp", self._freq(["你好"]), "词频要带停用词指纹，懒算缓存靠它判新旧")

    def test_lazy_cache_recomputes_on_fingerprint_mismatch(self):
        """旧词频带着过期 stop_fp → _stats_with_word_freq 必须重算并盖上新指纹。"""
        import app as appmod
        from flask import session as flask_session
        from webapp import store as storemod

        stale = {
            "overview": {"total_messages": 1},
            "word_freq": {"self": [], "other": [], "stop_fp": "STALE00000"},
        }
        with appmod.app.test_request_context("/habits"):
            flask_session["filepath"] = __file__
            flask_session["chat_hash"] = "hashStop"
            with (
                mock.patch.object(
                    storemod, "_load_chat_cached", return_value=_load([_text(1, "uA", "我", BASE, "你好")])
                ),
                mock.patch.object(storemod, "_save_stats"),
            ):
                out = storemod._stats_with_word_freq(dict(stale), "hashStop")
        self.assertNotEqual(out["word_freq"]["stop_fp"], "STALE00000")

    def test_lazy_cache_hit_when_fresh(self):
        """指纹一致：命中缓存、绝不再分词（外部化不能把懒算的省钱效果打回去）。"""
        import app as appmod
        from flask import session as flask_session
        from webapp import store as storemod

        fresh_fp = ls.stopwords_fingerprint()
        stats = {
            "overview": {"total_messages": 1},
            "word_freq": {"self": [{"word": "你好", "count": 1}], "other": [], "stop_fp": fresh_fp},
        }
        with appmod.app.test_request_context("/habits"):
            flask_session["filepath"] = __file__  # 路径真实存在，唯一能短路的就是 stop_fp 命中
            with mock.patch.object(storemod, "_load_chat_cached", side_effect=AssertionError("不该重算")):
                out = storemod._stats_with_word_freq(dict(stats), "hashStop")
        self.assertEqual(out["word_freq"]["self"], [{"word": "你好", "count": 1}])


class TestRecapDimension(unittest.TestCase):
    """批 6：整体总括——独立维度族。**首要验收**：既有两族付费缓存的键一字不动。"""

    CHAT = None

    @classmethod
    def _chat(cls):
        if cls.CHAT is None:
            day = 86400
            msgs = []
            # 三个月、逐月节奏变化（第一月热、第三月冷）+ 一次 5 天冷场重启
            for month_idx, words in enumerate([("我想你了", "抱抱"), ("在忙", "好"), ("哦", "行吧")]):
                for j, w in enumerate(words):
                    msgs.append(
                        _text(
                            len(msgs) + 1,
                            "uA" if j % 2 == 0 else "uB",
                            "我" if j % 2 == 0 else "对方",
                            BASE + month_idx * 31 * day + j * 60,
                            w,
                        )
                    )
                msgs.append(
                    _text(
                        len(msgs) + 1, "uA", "我", BASE + (month_idx + 1) * 31 * day + 5 * day, "重启一下话题"
                    )
                )
            cls.CHAT = _load(msgs)
        return cls.CHAT

    def test_private_and_group_fingerprints_untouched(self):
        """recap 引入后，私聊/群聊指纹的**绝对值**必须与引入前一致。

        私聊绝对值另有 tests 的 PINNED_PRIVATE_FINGERPRINT 双保险；这里把群聊也钉住：
        任何"指纹常量元组被顺手加键"的改动都会在这里红，而不会等到用户重新付费那天。
        """
        from analyzer import deepseek_client as dc
        from analyzer import group_client as gc

        self.assertEqual(dc.PROMPT_FINGERPRINT, dc._prompt_fingerprint())
        self.assertEqual(gc.GROUP_PROMPT_FINGERPRINT, gc.group_prompt_fingerprint())
        self.assertNotEqual(dc.PROMPT_FINGERPRINT, gc.GROUP_PROMPT_FINGERPRINT)

    def test_recap_has_its_own_fingerprint_family(self):
        from analyzer import deepseek_client as dc
        from analyzer import recap_client as rc

        self.assertEqual(dc.fingerprint_for_dimension("recap"), rc.RECAP_PROMPT_FINGERPRINT)
        self.assertNotEqual(rc.RECAP_PROMPT_FINGERPRINT, dc.PROMPT_FINGERPRINT, "recap 不得寄生在私聊指纹下")
        self.assertEqual(dc.legacy_fingerprint_for_dimension("recap"), rc.RECAP_PROMPT_FINGERPRINT_LEGACY)
        # 缓存文件名里嵌的是 recap 指纹（族隔离在文件名层面可见）
        path = store._cache_path("recap", "hashR")
        self.assertIn(rc.RECAP_PROMPT_FINGERPRINT, path)
        self.assertNotIn(dc.PROMPT_FINGERPRINT, path)

    def test_recap_and_ask_fingerprint_cover_their_own_input(self):
        """recap/ask 的模型输入若改变，指纹必须跟着变——与私聊族同一条保证。

        这条不是"锦上添花的一致性"，而是本仓库已经踩过一次的坑：
        指纹哈希的是 **AST 归一的函数源码**，函数体里的模块级常量只是 Name 节点，
        所以"把抽样条数 800 改成 200"不改变任何节点形状。少了显式的 consts 段与上游
        函数名单，旧抽样算出的旧复盘会继续以"新配置"的名义命中 30 天，而缓存命中时
        _recap_digest 根本不会重跑——没有任何一处会发现输入变了。
        私聊族用 deepseek_client 的 "consts:" 块修过同类问题，并由
        tests/test_optimizations.py::test_input_budget_change_changes_fingerprint 钉住；
        这里给 recap/ask 补上同一条绳，避免第三次换代时再犯。
        """
        from unittest import mock

        from analyzer import dialog
        from analyzer import local_stats as ls
        from analyzer import recap_client as rc

        recap_base = rc.recap_prompt_fingerprint()
        ask_base = rc.ask_fingerprint()

        # ① 抽样预算：改变进模型的对话样本量
        with mock.patch.object(rc, "RECAP_SAMPLE_LINES", 200):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "改抽样条数必须换 recap 指纹")
            self.assertNotEqual(ask_base, rc.ask_fingerprint(), "改抽样条数必须换 ask 指纹")
        with mock.patch.object(rc, "RECAP_SAMPLE_CHARS", 1000):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "改抽样字符预算必须换指纹")

        # ② 摘要头行与"本地事实"段的数字来自 calc_overview：它必须在名单里
        with mock.patch.object(ls, "calc_overview", lambda chat: {"total_messages": 1}):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "calc_overview 口径变了必须换指纹")
            self.assertNotEqual(ask_base, rc.ask_fingerprint(), "calc_overview 口径变了必须换 ask 指纹")

        # ③ 抽样对话原文逐条经 dialog 成型：换格式=换模型输入
        with mock.patch.object(dialog, "_message_line", lambda *a, **k: "REFORMATTED"):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "对话行格式变了必须换指纹")

        # ④ 纯重排不许换键：这条保证"改注释/被格式化"不会让用户重新付费
        self.assertEqual(recap_base, rc.recap_prompt_fingerprint(), "无改动时指纹必须稳定")
        self.assertEqual(ask_base, rc.ask_fingerprint(), "无改动时 ask 指纹必须稳定")

    def test_recap_not_in_analyze_all(self):
        """ "一键全量"的清单与成本承诺不因 recap 改变（README 实测约 ¥6 的口径）。"""
        from analyzer import recap_client as rc
        from webapp import jobs

        dims = jobs.dimensions_for_mode(is_group=False)
        self.assertNotIn("recap", dims)
        self.assertEqual(len(dims), 5, "私聊全量仍是五维")
        self.assertEqual(jobs.analyze_func_for("recap"), rc.analyze_recap)
        self.assertEqual(jobs.dimension_unit("recap"), "次")
        self.assertIn("recap", jobs.ALL_DIMENSION_NAMES)

    def test_digest_is_cross_month_and_deterministic(self):
        from analyzer import recap_client as rc

        d1 = rc._recap_digest(self._chat())
        d2 = rc._recap_digest(self._chat())
        self.assertEqual(d1, d2, "同文件必得同输入——维度缓存按 chat_hash 寻址的立身之本")
        self.assertIn("逐月事实", d1, "recap 存在的意义就是让模型看见跨月对比")
        self.assertIn("冷场", d1)

    def test_analyze_recap_single_call_with_cancel_guard(self):
        from analyzer import deepseek_client as dc
        from analyzer import recap_client as rc

        calls = []

        def fake(system, user, max_tokens=None, tag=None, dim=None, **kw):
            calls.append({"system": system, "user": user, "max_tokens": max_tokens, "tag": tag, "dim": dim})
            return {"overall": "ok"}

        prog = []
        with mock.patch.object(dc, "_call_api", side_effect=fake):
            out = rc.analyze_recap(self._chat(), on_progress=lambda d, t: prog.append((d, t)))
        self.assertEqual(out, {"overall": "ok"})
        self.assertEqual(len(calls), 1, "总括只此一次调用")
        self.assertEqual(calls[0]["system"], rc.SYSTEM_PROMPT_RECAP)
        self.assertEqual(calls[0]["dim"], "recap", "思考模式与用量按 recap 口径记账")
        self.assertEqual(prog, [(0, 1), (1, 1)])

        # 取消/关闭：派发前拦下，一分钱不花
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("不该出网")):
            self.assertIsNone(rc.analyze_recap(self._chat(), should_cancel=lambda: True))

    def test_max_tokens_env_override_for_recap(self):
        from analyzer import deepseek_client as dc

        self.assertIn("recap", dc.MAX_TOKENS_BY_DIM)
        self.assertGreaterEqual(dc.MAX_TOKENS_BY_DIM["recap"], 4096, "开思考模式时低于 4096 必然截断丢结果")

    def test_recap_page_and_api_guard(self):
        """两个分支都显式打桩：与本机有没有 Key 无关（_bootstrap 保留真实环境）。"""
        import app as appmod
        from webapp import api as api_module

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        # 无聊天时页面回首页（不依赖 Key 状态）
        self.assertEqual(client.get("/recap").status_code, 302)
        # 显式"未配置"：报缺 Key；显式"已配置"：才轮到"请先上传"。
        # 不这么写的用例在本机（有 Key）与 CI（无 Key）会各绿一半——项目为此付过学费。
        with mock.patch.object(api_module, "is_api_configured", return_value=False):
            r = client.post("/api/analyze/recap", headers=headers)
            self.assertIn("API Key", r.get_json().get("error", ""))
        guard = api_configured_patcher()
        guard.start()
        try:
            r = client.post("/api/analyze/recap", headers=headers)
            self.assertIn("上传", r.get_json().get("error", ""))
        finally:
            guard.stop()


class TestAskAndDuplicateNotice(unittest.TestCase):
    """7-B 重复上传提示 + 7-C 自定义提问（同步单调用、问题级缓存、自动纳入隐私链路）。"""

    @classmethod
    def setUpClass(cls):
        import io as _io

        import app as appmod

        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        msgs = [
            _text(1, "uA", "我", BASE, "我们什么时候去看海"),
            _text(2, "uB", "对方", BASE + 60, "下个月吧"),
            _text(3, "uA", "我", BASE + 40 * 86400, "海还去看吗"),
            _text(4, "uB", "对方", BASE + 41 * 86400, "太忙了"),
        ]
        cls.payload = json.dumps(
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
            data={"file": (_io.BytesIO(cls.payload), "a.json")},
            headers=cls.headers,
            follow_redirects=True,
        )
        assert r.status_code == 200
        with cls.client.session_transaction() as sess:
            cls.chat_hash = sess["chat_hash"]

    @classmethod
    def tearDownClass(cls):
        with cls.client.session_transaction() as sess:
            fp = sess.get("filepath")
        if fp and os.path.exists(fp):
            os.remove(fp)
        store._purge_chat_caches(cls.chat_hash)

    def _ask(self, question, **extra):
        import app as appmod  # noqa: F401  确保装配完成

        body = {"question": question, **extra}
        return self.client.post(
            "/api/ask",
            json=body,
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": self.headers["X-CSRF-Token"]},
        )

    def test_duplicate_upload_is_announced(self):
        import io as _io

        r = self.client.post(
            "/upload",
            data={"file": (_io.BytesIO(self.payload), "a.json")},
            headers=self.headers,
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("同一份聊天记录", r.get_data(as_text=True), "重复上传必须如实告知'不会重复计费'")

    def test_ask_single_call_then_cache_hit(self):
        calls = []

        def fake(system, user, max_tokens=None, tag=None, dim=None, **kw):
            calls.append(tag)
            return {
                "answer": "原计划在五月，后来搁置了",
                "confidence": "medium",
                "evidence": ["2025-03 下个月吧"],
            }

        answer = "看海后来还去吗"
        guard = api_configured_patcher()
        guard.start()
        try:
            with mock.patch("analyzer.deepseek_client._call_api", side_effect=fake):
                r1 = self._ask(answer)
                self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True)[:200])
                j1 = r1.get_json()
                self.assertTrue(j1.get("ok"))
                self.assertEqual(j1["result"]["answer"], "原计划在五月，后来搁置了")
                self.assertEqual(len(calls), 1)
                # 同题再问：命中缓存，不再调用
                r2 = self._ask(answer)
                j2 = r2.get_json()
                self.assertTrue(j2.get("cached"), "同一问题重问必须免费命中")
                self.assertEqual(len(calls), 1, "缓存命中不该出网")
                # refresh 显式重跑才再调用
                r3 = self._ask(answer, refresh="1")
                self.assertTrue(r3.get_json().get("ok"))
                self.assertEqual(len(calls), 2)
        finally:
            guard.stop()

    def test_ask_question_length_guard(self):
        guard = api_configured_patcher()
        guard.start()
        try:
            r = self._ask("短")
            self.assertEqual(r.status_code, 400)
            r = self._ask("长" * 301)
            self.assertEqual(r.status_code, 400)
            self.assertIn("300", r.get_json()["error"])
        finally:
            guard.stop()

    def test_ask_cache_file_belongs_to_chat(self):
        """ask 文件名的 chat_hash 是完整段——级联清理/导出/回收自动覆盖它。"""
        with mock.patch("analyzer.deepseek_client._call_api", return_value=valid_ask()):
            guard = api_configured_patcher()
            guard.start()
            try:
                r = self._ask("这条缓存文件归谁管")
            finally:
                guard.stop()
            self.assertEqual(r.status_code, 200)
        names = os.listdir(store.AI_CACHE_DIR)
        ask_files = [n for n in names if n.startswith("ask_" + self.chat_hash + "_")]
        self.assertTrue(ask_files, "提问结果要落进 ai_cache/ 并带上会话哈希段")
        self.assertTrue(store._cache_belongs_to(ask_files[0], self.chat_hash))
        # 删干净，别把缓存带进后续用例
        for n in ask_files:
            os.remove(os.path.join(store.AI_CACHE_DIR, n))


class TestFingerprintMigrationChain(unittest.TestCase):
    """7-E 迁移链扩展：单代 if → 有序链 + 读侧循环。现值零变化，未来换代不再断层。"""

    def test_current_values_untouched(self):
        """链式机制：群聊/recap 仍各只有一代旧键；私聊已有两代（中位数换代）。

        私聊那条从"单代"变成两代是有意的：_conversation_stats 的 median_gap 改用
        local_stats._median（上中位 → 真中位），改变了喂给模型的那行文本，
        所以必须换键；换出去的那一代压进链头，让沿链迁移继续读得到它。
        这正好反过来验证了链式改造的价值：第三次换代不再断层。
        """
        from analyzer import deepseek_client as dc
        from analyzer import group_client as gc
        from analyzer import recap_client as rc

        self.assertEqual(
            dc.legacy_fingerprints_for_dimension("emotion"),
            (*dc.PRIVATE_FINGERPRINT_GENERATIONS, dc.PROMPT_FINGERPRINT_LEGACY),
        )
        self.assertEqual(len(dc.legacy_fingerprints_for_dimension("emotion")), 2, "私聊应为两代")
        self.assertEqual(
            dc.legacy_fingerprints_for_dimension("group_dynamics"), (gc.GROUP_PROMPT_FINGERPRINT_LEGACY,)
        )
        self.assertEqual(dc.legacy_fingerprints_for_dimension("recap"), (rc.RECAP_PROMPT_FINGERPRINT_LEGACY,))
        # 链头是"上一代的当前键"（AST 归一时代写的），链尾是更早的原文公式
        self.assertEqual(dc.legacy_fingerprints_for_dimension("emotion")[0], "f4bd6aa06d52")
        self.assertEqual(dc.legacy_fingerprints_for_dimension("emotion")[-1], dc.PROMPT_FINGERPRINT_LEGACY)
        # 单值入口取链头（老调用点语义保持："先试最近的那一代"）
        self.assertEqual(
            dc.legacy_fingerprint_for_dimension("emotion"), dc.PRIVATE_FINGERPRINT_GENERATIONS[0]
        )
        self.assertEqual(
            dc.legacy_fingerprint_for_dimension("group_dynamics"), gc.GROUP_PROMPT_FINGERPRINT_LEGACY
        )
        # 链里不许混进当前键自己：那等于白读一次自己，还会把"命中"误报成"迁移成功"
        for dim in ("emotion", "profile", "group_dynamics", "recap"):
            self.assertNotIn(dc.fingerprint_for_dimension(dim), dc.legacy_fingerprints_for_dimension(dim))

    def test_median_now_matches_local_stats_algorithm(self):
        """给模型看的"回复间隔中位数"与界面卡用的是同一个算法（口径唯一）。

        就地 `s[n//2]` 在偶数样本上取的是**上中位**：[10,20,30,40] 会报成 30（真值 25），
        两句长度 [1,9] 报成 9（真值 5）。local_stats 为同一个毛病专门顶过
        STATS_SCHEMA_VERSION，而模型侧这句漏了——同一个词在两处算出两个数。
        """
        from analyzer import local_stats as ls
        from analyzer.dialog import _conversation_stats
        from parser.qq_parser import Message

        def mk(i, uid, ts):
            return Message(
                id=str(i),
                timestamp=ts,
                time_str="x",
                sender_uid=uid,
                sender_name=uid,
                text="t",
                raw_text="t",
                msg_type="text",
                has_image=False,
                is_reply=False,
                media_kind="",
                media_label="",
                media_bytes=0,
                media_id="",
                media_path="",
                media_w=0,
                media_h=0,
                face_url="",
                face_ids=[],
                face_names=[],
                image_bytes=0,
                image_count=0,
                image_ids=[],
                recalled=False,
                system=False,
                reply_to_id="",
                reply_to_uid="",
                mentions=[],
                mentions_all=False,
            )

        # 换人且都在同一段内 → gaps = [10, 20, 30, 40]，偶数样本
        msgs = [
            mk(0, "uA", 1_700_000_000_000),
            mk(1, "uB", 1_700_000_010_000),
            mk(2, "uA", 1_700_000_030_000),
            mk(3, "uB", 1_700_000_060_000),
            mk(4, "uA", 1_700_000_100_000),
        ]
        got = _conversation_stats(msgs, "uA")["median_gap"]
        self.assertEqual(got, 25.0, f"偶数样本必须取中间两数平均，实际 {got}（30 = 上中位，就是原来的错法）")
        self.assertEqual(got, ls._median(sorted([10.0, 20.0, 30.0, 40.0])), "与 local_stats 同口径")
        # 奇数样本两种取法本来就一致，顺手钉住别把它改坏
        odd = _conversation_stats(msgs[:4], "uA")["median_gap"]
        self.assertEqual(odd, ls._median(sorted([10.0, 20.0, 30.0])))

    def test_read_cache_walks_multi_generation_chain(self):
        """链里放**两代**假旧键，_read_cache 必须挨代找到并搬到当前键。

        反向验证：旧实现只认一代——写在中止于第一代，第二代永远读不到，本条变红。
        """
        from analyzer import deepseek_client as dc
        from webapp import store as storemod

        chat_hash = "chash-chain-001"
        dim = "emotion"
        gen1 = "ffffffffffff"
        gen0 = "eeeeeeeeeeee"
        # 旧键文件名必须复刻 _cache_path 的真实口径（思考模式开启时带 _think 后缀）
        suffix = "_think" if dc.thinking_enabled(dim) else ""
        legacy_payload = {"_created": time.time(), "result": {"self_emotion": "旧代产出"}}
        path0 = os.path.join(
            storemod.AI_CACHE_DIR, f"{dim}_{chat_hash}_{storemod.DEEPSEEK_MODEL}_{gen0}{suffix}.json"
        )
        try:
            os.makedirs(storemod.AI_CACHE_DIR, exist_ok=True)
            with open(path0, "w", encoding="utf-8") as f:
                json.dump(legacy_payload, f)
            # 打桩在**消费侧**的绑定名上（store 是 import 期绑定的，patch 源模块不影响它——
            # 这正是本项目 _bootstrap.api_configured_patcher 文档里写过的同一课）
            with mock.patch.object(storemod, "legacy_fingerprints_for_dimension", return_value=(gen1, gen0)):
                got = storemod._read_cache(dim, chat_hash)
            self.assertIsNotNone(got, "链上的每一代都要被尝试")
            self.assertEqual(got["self_emotion"], "旧代产出")
            self.assertFalse(os.path.exists(path0), "迁移是改名不是复制（敏感内容不留两份）")
            self.assertTrue(os.path.exists(storemod._cache_path(dim, chat_hash)))
        finally:
            for p in (storemod._cache_path(dim, chat_hash), path0):
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestJobHistory(unittest.TestCase):
    """7-D 任务历史：白名单字段、新→旧、读取端点与 /api/ 前缀守卫。"""

    def setUp(self):
        # 账目落在 LOG_DIR，用临时目录顶掉它，免得用例之间、以及与真实数据目录之间串味
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = self._tmp_obj.name
        self.addCleanup(self._tmp_obj.cleanup)

    def test_fields_whitelisted_and_newest_first(self):
        from webapp import store as storemod

        # max_age_days=0：本用例测的是"字段白名单 + 排序"，用的 t=1.0/2.0 是 1970 年的
        # 合成时间戳，会被年龄过滤正当清掉。关掉年龄这一维，两条保证各自单独可测。
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(storemod, "LOG_DIR", d):
                storemod.append_job_history(
                    {
                        "t": 1.0,
                        "dim": "emotion",
                        "status": "done",
                        "done": 3,
                        "total": 3,
                        "chat": "abc123456789",
                        "error": "不该进来的文本/路径",
                    },
                    max_age_days=0,
                )
                storemod.append_job_history(
                    {"t": 2.0, "dim": "all", "status": "cancelled", "done": 1, "total": 5, "chat": "def456"},
                    max_age_days=0,
                )
                hist = storemod.read_job_history()
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["dim"], "all", "新→旧排序")
        self.assertNotIn("error", hist[1], "错误文本（可能含路径）绝不进历史账目")
        self.assertEqual(hist[1]["chat"], "abc123456789")

    def test_ancient_timestamps_are_expired_by_default(self):
        """默认口径下，1970 年的 t 会被年龄过滤清掉（上一条用例的成因，钉成契约）。"""
        from webapp import store as storemod

        with tempfile.TemporaryDirectory() as d:
            with (
                mock.patch.object(storemod, "LOG_DIR", d),
                mock.patch.object(storemod, "LOG_RETENTION_DAYS", 7),
            ):
                storemod.append_job_history({"t": 1.0, "dim": "emotion", "status": "done", "chat": "abc"})
                hist = storemod.read_job_history()
        self.assertEqual(hist, [], "远超保留天数的条目默认就该被回收")

    def test_history_endpoint_lists(self):
        import app as appmod

        client = appmod.app.test_client()
        r = client.get("/api/jobs/history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("history", r.get_json())

    def test_new_endpoints_registered_under_api_prefix(self):
        import app as appmod

        rules = {rule.rule for rule in appmod.app.url_map.iter_rules()}
        for path in (
            "/api/ask",
            "/api/jobs/history",
            "/api/messages",
            "/api/chat/delete",
            "/api/export",
            "/api/import",
            "/api/status/test",
        ):
            self.assertIn(path, rules)

    def test_privacy_wipe_erases_the_ledger_row(self):
        """「删除本聊天」必须连这份账目一起擦掉。

        账目只有 (时间, 维度, 状态, 进度, chat_hash[:12])，不含聊天内容——但
        chat_hash 在本项目里**就是聊天的身份**（所有缓存都按它寻址）。
        接口对用户承诺的是"上传原件 + 全部派生缓存"，漏了这个文件就等于
        承诺之后仍留着"这台机器在那天分析过这份内容"的记录。
        """
        from webapp import store as storemod

        chat_hash = "abcdef0123456789"
        with mock.patch.object(storemod, "LOG_DIR", self.tmp):
            storemod.append_job_history(
                {
                    "t": time.time(),
                    "dim": "recap",
                    "status": "done",
                    "done": 1,
                    "total": 1,
                    "chat": chat_hash[:12],
                }
            )
            storemod.append_job_history(
                {
                    "t": time.time(),
                    "dim": "emotion",
                    "status": "done",
                    "done": 2,
                    "total": 2,
                    "chat": "111111111111",
                }
            )
            path = storemod._job_history_path()
            with open(path, encoding="utf-8") as f:
                self.assertIn(chat_hash[:12], f.read())

            removed = storemod.purge_job_history(chat_hash)
            with open(path, encoding="utf-8") as f:
                body = f.read()
        self.assertEqual(removed, 1, "该聊天的那一行该被删掉")
        self.assertNotIn(chat_hash[:12], body, "删除后不许留下哈希前缀")
        self.assertIn("111111111111", body, "别人那份聊天的账目不许被牵连（按整段前缀比对）")

    def test_purge_chat_caches_reaches_the_ledger(self):
        """走真实的级联清理入口，而不是只测 purge_job_history 本身。

        单测函数自己是不够的：账目此前逃过的正是 `_purge_chat_caches` 这一层
        （它删缓存、删月份、删图片副本，唯独不知道 logs/ 里还有这份账）。
        """
        from webapp import store as storemod

        chat_hash = "deadbeefcafebabe"
        with mock.patch.object(storemod, "LOG_DIR", self.tmp):
            storemod.append_job_history(
                {
                    "t": time.time(),
                    "dim": "topics",
                    "status": "done",
                    "done": 1,
                    "total": 1,
                    "chat": chat_hash[:12],
                }
            )
            storemod._purge_chat_caches(chat_hash)
            body = open(storemod._job_history_path(), encoding="utf-8").read()
        self.assertNotIn(chat_hash[:12], body, "级联清理后账目里不该再有这份聊天")

    def test_ledger_rows_expire_with_log_retention_days(self):
        """账目行要按 LOG_RETENTION_DAYS 过期。

        它住在 LOG_DIR 里，README 对这个目录的承诺是"按天轮转保留 N 天"；
        但定期清理的日志那一段只认 `app.log.` 前缀（cleanup.py），
        所以这个文件**永远不会**被按龄回收，只能自己实现。少了这一条，
        轻度使用者删掉聊天前的记录可以静静躺上好几个月。
        """
        from webapp import store as storemod

        old_stamp = time.time() - 9 * 86400
        with (
            mock.patch.object(storemod, "LOG_DIR", self.tmp),
            mock.patch.object(storemod, "LOG_RETENTION_DAYS", 7),
        ):
            storemod.append_job_history(
                {
                    "t": old_stamp,
                    "dim": "emotion",
                    "status": "done",
                    "done": 1,
                    "total": 1,
                    "chat": "oldoldold000",
                }
            )
            storemod.append_job_history(
                {
                    "t": time.time(),
                    "dim": "topics",
                    "status": "done",
                    "done": 1,
                    "total": 1,
                    "chat": "newnewnew000",
                }
            )
            body = open(storemod._job_history_path(), encoding="utf-8").read()
        self.assertNotIn("oldoldold000", body, "超过保留天数的条目必须被回收")
        self.assertIn("newnewnew000", body, "保留期内的条目必须还在")

    def test_line_cap_still_holds_after_the_age_filter(self):
        """对折上限不能被新加的年龄过滤弄失效（两者是互补的两道界）。"""
        from webapp import store as storemod

        with (
            mock.patch.object(storemod, "LOG_DIR", self.tmp),
            mock.patch.object(storemod, "LOG_RETENTION_DAYS", 0),
        ):
            for i in range(storemod.JOB_HISTORY_MAX_LINES + 60):
                storemod.append_job_history(
                    {
                        "t": time.time(),
                        "dim": "emotion",
                        "status": "done",
                        "done": 1,
                        "total": 1,
                        "chat": f"c{i:06d}",
                    }
                )
            lines = [
                x
                for x in open(storemod._job_history_path(), encoding="utf-8").read().splitlines()
                if x.strip()
            ]
        self.assertLessEqual(len(lines), storemod.JOB_HISTORY_MAX_LINES, "账目必须有上界")

    def test_malformed_and_foreign_rows_are_survivable(self):
        """坏行/非 dict 行不许让清理或读取抛异常（账目是辅助信息，不该反噬主流程）。"""
        from webapp import store as storemod

        path = os.path.join(self.tmp, "job_history.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write("[1,2,3]\n")
            f.write(json.dumps({"t": "not-a-number", "dim": "emotion", "chat": "x" * 12}) + "\n")
            f.write("\n")
        with mock.patch.object(storemod, "LOG_DIR", self.tmp):
            self.assertEqual(storemod.purge_job_history("yyyy"), 0)
            hist = storemod.read_job_history()
            # 时间戳不是数字的行：年龄过滤放行（判不出来就不删），读侧照实返回
            storemod.append_job_history({"t": time.time(), "dim": "all", "status": "done", "chat": "z" * 12})
            kept = open(storemod._job_history_path(), encoding="utf-8").read()
        self.assertEqual(len(hist), 1, "读侧跳过坏行，但可解析的行要返回")
        self.assertEqual(hist[0]["dim"], "emotion")
        self.assertIn("not-a-number", kept, "判不出时间的行不许被年龄过滤误删")
        self.assertIn("not json", kept, "无关的坏行不许在一次无关清理里被顺手销毁")


if __name__ == "__main__":
    unittest.main()
