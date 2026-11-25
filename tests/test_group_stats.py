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
"""群聊本地统计的口径测试（M1）

重点不是"数字好看"，而是三件事：
1. **对账**：成员条数之和 + 未知条数 == 统计口径总条数。对不上账的报表没人敢用。
2. **口径唯一**：接话判定必须走 is_session_start（间隔超过 SESSION_GAP_MINUTES 算新段），
   不能出现第二个间隔阈值——否则同一份数据在不同页面会给出不同的"互动次数"。
3. **互不串味**：私聊与群聊两套统计缓存按 mode 隔离；同一份文件切换模式后不得命中
   另一种口径的结果（这类错最难发现：数字看着正常，含义已经变了）。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from parser.qq_parser import ChatData, Message, load_chat  # noqa: E402
from analyzer import group_stats as gs  # noqa: E402
from analyzer.local_stats import SESSION_GAP_MINUTES  # noqa: E402
from webapp import store  # noqa: E402

CST = timezone(timedelta(hours=8))
BASE = datetime(2025, 3, 1, 20, 0, tzinfo=CST)


def _msg(uid: str, name: str, minutes: int, text: str = "内容") -> Message:
    ts = int((BASE + timedelta(minutes=minutes)).timestamp() * 1000)
    return Message(
        id=f"{uid}-{minutes}",
        timestamp=ts,
        time_str=(BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


def _group(messages: list, name: str = "摸鱼群", self_uid: str = "uA", self_name: str = "我") -> ChatData:
    return ChatData(
        chat_name=name,
        self_name=self_name,
        other_name=name,
        self_uid=self_uid,
        other_uid="",
        messages=sorted(messages, key=lambda m: m.timestamp),
        is_group_chat=True,
        mode="group",
    )


class TestMemberActivity(unittest.TestCase):
    """成员活跃度：对账 + 排序 + 名字唯一"""

    def _chat(self):
        return _group(
            [
                _msg("uA", "我", 0, "12345"),
                _msg("uB", "小明", 1, "1234567890"),
                _msg("uB", "小明", 2, "abc"),
                _msg("uC", "小红", 3, "x"),
                _msg("", "", 4, "没有发送者"),
            ]
        )

    def test_counts_reconcile_with_total(self):
        """成员条数之和 + 未知条数 == 统计口径总条数（对账）"""
        chat = self._chat()
        activity = gs.calc_member_activity(chat)
        unknown = sum(1 for m in chat.statistical() if not m.sender_uid)
        self.assertEqual(sum(a["msg_count"] for a in activity) + unknown, len(chat.statistical()))

    def test_ordering_and_fields(self):
        activity = gs.calc_member_activity(self._chat())
        self.assertEqual([a["uid"] for a in activity], ["uB", "uA", "uC"])
        top = activity[0]
        self.assertEqual(top["name"], "小明")
        self.assertEqual(top["msg_count"], 2)
        self.assertEqual(top["char_count"], len("1234567890") + len("abc"))
        self.assertEqual(top["avg_chars"], 6.5)
        self.assertEqual(top["active_days"], 1)
        self.assertFalse(top["is_self"])
        self.assertTrue(activity[1]["is_self"])
        # share 的分母是**全部**统计口径消息（含无归属的那条），所以成员份额之和是 0.8：
        # 4 条有主 + 1 条未知 = 5。这条断言同时钉住"未知不摊到任何人头上"。
        self.assertAlmostEqual(sum(a["share"] for a in activity), 0.8, places=3)

    def test_names_are_unique(self):
        chat = _group([_msg("uA", "小明", 0), _msg("uB", "小明", 1), _msg("uA", "小明", 2)])
        names = [a["name"] for a in gs.calc_member_activity(chat)]
        self.assertEqual(sorted(names), ["小明#uA", "小明#uB"])

    def test_repeated_member_activity_reads_reuse_the_parsed_chat_cache(self):
        chat = self._chat()
        with mock.patch.object(gs, "_statistical", wraps=gs._statistical) as statistical:
            first = gs.calc_member_activity(chat)
            second = gs.calc_member_activity(chat)
        self.assertIs(first, second)
        statistical.assert_called_once_with(chat)


class TestInteractionMatrix(unittest.TestCase):
    """互动矩阵：谁接了谁的话（口径必须与 SESSION_GAP_MINUTES 一致）"""

    def test_directed_counts(self):
        # A→B、B→A、A→C
        chat = _group(
            [_msg("uA", "我", 0), _msg("uB", "小明", 1), _msg("uA", "我", 2), _msg("uC", "小红", 3)]
        )
        m = gs.calc_interaction_matrix(chat)
        idx = {p["uid"]: i for i, p in enumerate(m["members"])}
        self.assertEqual(len(m["members"]), 3)
        self.assertEqual(m["directed"][idx["uA"]][idx["uB"]], 1)
        self.assertEqual(m["directed"][idx["uB"]][idx["uA"]], 1)
        self.assertEqual(m["directed"][idx["uA"]][idx["uC"]], 1)
        self.assertEqual(m["undirected"][idx["uA"]][idx["uC"]], 1)
        # 无向边把 A→B 与 B→A 合成一条：(A,B)=2、(A,C)=1
        self.assertEqual(len(m["edges"]), 2)
        values = sorted(e["value"] for e in m["edges"])
        self.assertEqual(values, [1, 2])
        totals = {t["uid"]: t for t in m["totals"]}
        # 会话是 A(0) B(1) A(2) C(3)：A 被 B、C 各接一次；A 只接了 B 一次
        self.assertEqual(totals["uA"]["replied_by"], 2)  # 别人接我话的次数（行和）
        self.assertEqual(totals["uA"]["replies_to"], 1)  # 我接别人话的次数（列和）
        self.assertEqual(totals["uB"]["replies_to"], 1)
        self.assertEqual(totals["uC"]["replied_by"], 0, "最后一条消息没有被任何人接话")
        self.assertFalse(m["truncated"])
        self.assertEqual(m["dropped"], 0)

    def test_same_speaker_consecutive_is_not_a_reply(self):
        chat = _group([_msg("uA", "我", 0), _msg("uA", "我", 1), _msg("uB", "小明", 2)])
        m = gs.calc_interaction_matrix(chat)
        self.assertEqual(sum(sum(row) for row in m["directed"]), 1)

    def test_gap_beyond_session_is_not_a_reply(self):
        """超过 SESSION_GAP_MINUTES 的换人不是"接话"，而是新话题的开口"""
        inside = _group([_msg("uA", "我", 0), _msg("uB", "小明", SESSION_GAP_MINUTES - 1)])
        outside = _group([_msg("uA", "我", 0), _msg("uB", "小明", SESSION_GAP_MINUTES + 1)])
        self.assertEqual(sum(sum(r) for r in gs.calc_interaction_matrix(inside)["directed"]), 1)
        self.assertEqual(sum(sum(r) for r in gs.calc_interaction_matrix(outside)["directed"]), 0)

    def test_unknown_sender_is_counted_separately(self):
        chat = _group([_msg("uA", "我", 0), _msg("", "", 1), _msg("uB", "小明", 2)])
        m = gs.calc_interaction_matrix(chat)
        self.assertEqual(m["unknown_replies"], 2)  # A→未知、未知→B
        self.assertEqual(sum(sum(r) for r in m["directed"]), 0)
        self.assertNotIn("__unknown__", [x["uid"] for x in m["members"]])

    def test_top_k_truncation_is_reported(self):
        msgs = []
        for i, uid in enumerate(["uA", "uB", "uC", "uD"]):
            for k in range(4 - i):  # 条数递减，确保截断顺序确定
                msgs.append(_msg(uid, uid, i * 10 + k * 2))
        chat = _group(msgs)
        with mock.patch.object(config, "GROUP_MATRIX_MEMBERS", 2):
            m = gs.calc_interaction_matrix(chat)
        self.assertEqual(len(m["members"]), 2)
        self.assertTrue(m["truncated"])
        self.assertEqual(m["dropped"], 2)
        self.assertEqual(m["matrix_limit"], 2)
        self.assertGreater(m["dropped_replies"], 0, "被截断的互动要记账，不能悄悄消失")
        self.assertEqual(len(m["directed"]), 2)
        self.assertEqual(len(m["directed"][0]), 2)

    def test_top_k_zero_means_no_truncation_not_the_default_cap(self):
        """top_k=0 必须真的是"不截断"。

        原来实现是 `limit = top_k or _matrix_top_k()`：0 是假值，于是调用方显式
        要求"不截断"反而拿回了默认上限。这不只是语义别扭——成员画像那条路
        （group_client._member_context）就是按"拿到每位成员的准确互动数字"来写注释
        并传 top_k=0 的，而它取的 `totals.get(uid, {})` 对榜外成员是空 dict，
        提示词于是写成"被精确回复 0 次、主动回复别人 0 次（事实）"，并要求模型
        据此判断这个人在群里的角色。select_ai_members 还会**专门**把不在前列的
        "我"选进来，所以最容易中招的正是用户本人。

        同时钉住默认值不变：榜单截断是给前端热力图用的，改了会让群聊页放大。
        """
        msgs = []
        seq = 0
        for i, uid in enumerate(["uA", "uB", "uC", "uD", "uE"]):
            for _ in range(5 - i):  # 条数严格递减 → 排名确定，uE 一定在榜尾
                msgs.append(_msg(uid, uid, seq))
                seq += 1
        chat = _group(msgs)

        with mock.patch.object(config, "GROUP_MATRIX_MEMBERS", 2):
            default = gs.calc_interaction_matrix(chat)
            untruncated = gs.calc_interaction_matrix(chat, top_k=0)
            capped = gs.calc_interaction_matrix(chat, top_k=4)

        self.assertEqual(len(default["members"]), 2, "默认仍按配置上限截断（前端行为不变）")
        self.assertEqual(len(untruncated["members"]), 5, "top_k=0 必须给到全部成员")
        self.assertEqual(untruncated["dropped"], 0)
        self.assertEqual(len(capped["members"]), 4, "显式 k 生效")

        default_uids = {t["uid"] for t in default["totals"]}
        self.assertNotIn("uE", default_uids, "榜外成员本就不在默认 totals 里")
        untr_map = {t["uid"]: t for t in untruncated["totals"]}
        self.assertIn("uE", untr_map)
        self.assertGreater(
            untr_map["uE"]["explicit_replies_to"] + untr_map["uE"]["replies_to"],
            0,
            "榜尾成员必须拿到真实计数，而不是被 .get(uid, {}) 静默变成 0",
        )

    def test_matrix_limit_reads_config_at_call_time(self):
        chat = _group([_msg("uA", "我", 0), _msg("uB", "小明", 1), _msg("uC", "小红", 2)])
        self.assertEqual(gs.calc_interaction_matrix(chat)["matrix_limit"], config.GROUP_MATRIX_MEMBERS)
        with mock.patch.object(config, "GROUP_MATRIX_MEMBERS", 3):
            self.assertEqual(gs.calc_interaction_matrix(chat)["matrix_limit"], 3)

    def test_sparse_member_summary_preserves_reply_mention_and_self_counts(self):
        first = _msg("uA", "我", 0)
        reply = _msg("uB", "小明", 1)
        reply.is_reply = True
        reply.reply_to_id = first.id
        reply.reply_to_uid = "uA"
        reply.mentions = ["uA"]
        reply.mentions_all = True
        self_reply = _msg("uA", "我", 2)
        self_reply.is_reply = True
        self_reply.reply_to_id = first.id
        self_reply.reply_to_uid = "uA"
        chat = _group([first, reply, self_reply])

        summary = gs.calc_member_interaction_summary(chat, "uA")

        self.assertEqual(summary["replies_to"], 1)
        self.assertEqual(summary["replied_by"], 1)
        self.assertEqual(summary["explicit_replies_to"], 1)
        self.assertEqual(summary["explicit_replied_by"], 2)
        self.assertEqual(summary["mentions_received"], 1)
        self.assertEqual(summary["top_explicit"][0]["uid"], "uB")
        self.assertIsNone(chat._matrix_cache, "画像摘要不能生成全员密集矩阵")

        matrix = gs.calc_interaction_matrix(chat, top_k=0)
        self.assertEqual(matrix["self_replies"], 1)
        self.assertEqual(matrix["mentions_all_count"], 1)

    def test_large_member_summary_keeps_only_sparse_edges(self):
        chat = _group([_msg(f"u{i}", f"成员{i}", i) for i in range(500)])

        summary = gs.calc_member_interaction_summary(chat, "u250")

        self.assertEqual(summary["replies_to"], 1)
        self.assertEqual(summary["replied_by"], 1)
        self.assertIsNone(chat._matrix_cache)
        self.assertEqual(len(chat._interaction_sparse_cache["directed"]), 499)
        self.assertEqual(sum(chat._interaction_sparse_cache["directed"].values()), 499)


class TestMemberHourlyAndOverview(unittest.TestCase):
    def test_member_hourly(self):
        chat = _group([_msg("uA", "我", 0), _msg("uA", "我", 5), _msg("uB", "小明", 61), _msg("", "", 62)])
        data = gs.calc_member_hourly(chat)
        self.assertEqual(data["hours"], list(range(24)))
        series = {s["uid"]: s["counts"] for s in data["series"]}
        self.assertEqual(series["uA"][20], 2)
        self.assertEqual(series["uB"][21], 1)
        self.assertEqual(data["unknown"][21], 1)
        self.assertEqual(sum(sum(s["counts"]) for s in data["series"]) + sum(data["unknown"]), 4)

    def test_group_overview_fields(self):
        chat = _group(
            [
                _msg("uA", "我", 0),
                _msg("uB", "小明", 1),
                _msg("uC", "小红", 2),
                _msg("", "", 3),
                _msg("uB", "小明", 100),
            ]
        )
        ov = gs.calc_group_overview(chat)
        self.assertTrue(ov["is_group"])
        self.assertEqual(ov["group_name"], "摸鱼群")
        self.assertEqual(ov["other_name"], "", "群聊没有单一「对方」，不能把群名塞进 other_name")
        self.assertEqual(ov["other_label"], "其他成员")
        self.assertEqual(ov["member_count"], 3)
        self.assertEqual(ov["most_active_member"]["uid"], "uB")
        self.assertEqual(ov["unknown_messages"], 1)
        self.assertIsNone(ov["exchange_rounds"], "群聊口径下轮次不成立，必须置空而不是给个误导值")
        self.assertEqual(ov["self_name"], "我")

    def test_peak_concurrent(self):
        """10 分钟窗口内的不同发言者峰值；窗口外的人不算同时在线"""
        chat = _group(
            [
                _msg("uA", "我", 0),
                _msg("uB", "小明", 1),
                _msg("uC", "小红", 2),
                _msg("uD", "阿强", 30),  # 已超出窗口
            ]
        )
        peak = gs.calc_group_overview(chat)["peak_concurrent"]
        self.assertEqual(peak["count"], 3)
        self.assertEqual(peak["window_minutes"], config.GROUP_PEAK_WINDOW_MINUTES)
        self.assertTrue(peak["at"])

    def test_group_milestones_extra_fields(self):
        # 三条消息都在同一天（基准 20:00 起 +200 分钟 = 23:20，跨过 240 分钟就换天了）
        chat = _group([_msg("uA", "我", 0), _msg("uB", "小明", 1), _msg("uC", "小红", 200)])
        ms = gs.calc_group_milestones(chat)
        self.assertEqual(ms["peak_day_members"], 3)
        self.assertEqual(ms["multi_member_days"], 1)
        self.assertIn("群聊口径", ms["mutual_nights_note"])


class TestComputeStatsBranch(unittest.TestCase):
    def test_group_branch_keys(self):
        chat = _group([_msg("uA", "我", 0), _msg("uB", "小明", 1)])
        stats = gs.compute_group_stats(chat)
        self.assertEqual(stats["mode"], "group")
        self.assertEqual(
            set(stats),
            {
                "mode",
                "overview",
                "member_activity",
                "interaction",
                "member_hourly",
                "daily_counts",
                "hourly_dist",
                "weekly_dist",
                "weekly_activity",
                "length_stats",
                "face_stats",
                "exchange_rounds",
                "milestones",
            },
        )
        self.assertNotIn("response_time", stats, "群聊不提供'对方→我'的回复速度（口径不成立）")

    def test_store_compute_stats_routes_by_mode(self):
        group_chat = _group([_msg("uA", "我", 0), _msg("uB", "小明", 1)])
        self.assertEqual(store.stats_mode_of(group_chat), store.STATS_MODE_GROUP)
        self.assertEqual(store.compute_stats(group_chat)["mode"], "group")

        private_chat = ChatData(
            chat_name="私聊",
            self_name="我",
            other_name="对方",
            self_uid="uA",
            other_uid="uB",
            messages=[_msg("uA", "我", 0), _msg("uB", "对方", 1)],
        )
        self.assertEqual(store.stats_mode_of(private_chat), store.STATS_MODE_PRIVATE)
        stats = store.compute_stats(private_chat)
        self.assertNotIn("mode", stats, "私聊统计的形状是对外契约：不得因为群聊而多出字段")
        self.assertIn("response_time", stats)


class TestStatsCacheModeIsolation(unittest.TestCase):
    """两套口径的统计缓存互不命中，且旧私聊缓存继续有效"""

    def _write_raw(self, chat_hash: str, payload: dict) -> None:
        os.makedirs(store.STATS_CACHE_DIR, exist_ok=True)
        with open(store._stats_path(chat_hash), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_legacy_private_cache_without_mode_still_loads(self):
        """升级前写下的缓存没有 mode 字段，必须继续命中（否则用户白等一次重算）"""
        self._write_raw("legacyhash", {"_v": store.STATS_SCHEMA_VERSION, "overview": {"total_messages": 1}})
        loaded = store._load_stats("legacyhash")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["overview"]["total_messages"], 1)

    def test_group_payload_only_loads_as_group(self):
        self._write_raw(
            "grouphash",
            {"_v": store.GROUP_STATS_SCHEMA_VERSION, "mode": store.STATS_MODE_GROUP, "overview": {}},
        )
        self.assertIsNotNone(store._load_stats("grouphash", expect_mode=store.STATS_MODE_GROUP))
        self.assertIsNone(store._load_stats("grouphash"), "群聊结果不得被私聊口径读走")
        self.assertIsNone(store._load_stats("grouphash", expect_mode=store.STATS_MODE_PRIVATE))

    def test_private_payload_only_loads_as_private(self):
        self._write_raw(
            "privhash",
            {"_v": store.STATS_SCHEMA_VERSION, "mode": store.STATS_MODE_PRIVATE, "overview": {}},
        )
        self.assertIsNotNone(store._load_stats("privhash", expect_mode=store.STATS_MODE_PRIVATE))
        self.assertIsNone(store._load_stats("privhash", expect_mode=store.STATS_MODE_GROUP))

    def test_save_then_load_round_trip_per_mode(self):
        store._save_stats("roundhash", {"overview": {}}, mode=store.STATS_MODE_GROUP)
        self.assertIsNone(store._load_stats("roundhash"))
        self.assertEqual(
            store._load_stats("roundhash", expect_mode=store.STATS_MODE_GROUP)["mode"], store.STATS_MODE_GROUP
        )
        store._save_stats("roundhash2", {"overview": {}})
        self.assertIsNotNone(store._load_stats("roundhash2"))
        self.assertEqual(store._load_stats("roundhash2")["mode"], store.STATS_MODE_PRIVATE)

    def test_group_version_mismatch_is_rejected(self):
        self._write_raw(
            "oldgrouphash",
            {"_v": store.GROUP_STATS_SCHEMA_VERSION + 1, "mode": store.STATS_MODE_GROUP, "overview": {}},
        )
        self.assertIsNone(store._load_stats("oldgrouphash", expect_mode=store.STATS_MODE_GROUP))


class TestFixtureStats(unittest.TestCase):
    """5 人群聊 fixture 跑完整套群聊统计（M2/M3 的输入就是它）"""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "group_5p.json"

    @classmethod
    def setUpClass(cls):
        import parser.qq_parser as qp
        from parser.qq_parser import load_chat

        with mock.patch.object(qp, "GROUP_TRACK_READY", True):
            cls.chat = load_chat(str(cls.FIXTURE))

    def test_member_activity_reconciles(self):
        activity = gs.calc_member_activity(self.chat)
        self.assertEqual(len(activity), 5)
        unknown = sum(1 for m in self.chat.statistical() if not m.sender_uid)
        self.assertEqual(sum(a["msg_count"] for a in activity) + unknown, len(self.chat.statistical()))

    def test_matrix_covers_all_five_members(self):
        m = gs.calc_interaction_matrix(self.chat)
        self.assertEqual(len(m["members"]), 5)
        self.assertFalse(m["truncated"])
        diagonal = [m["directed"][i][i] for i in range(5)]
        self.assertEqual(diagonal, [0] * 5, "对角线必须为 0（自己不接自己的话）")
        self.assertTrue(m["edges"], "5 人 fixture 里应该有实际互动")
        self.assertEqual(len(m["edges"]), sum(1 for row in m["undirected"] for v in row if v) // 2)

    def test_overview_and_milestones(self):
        stats = gs.compute_group_stats(self.chat)
        self.assertEqual(stats["overview"]["member_count"], 5)
        self.assertGreaterEqual(stats["overview"]["peak_concurrent"]["count"], 2)
        self.assertEqual(stats["milestones"]["peak_day_members"] >= 1, True)
        self.assertEqual(len(stats["member_hourly"]["series"]), 5)
        self.assertEqual(len(stats["daily_counts"]), self.chat.duration_days)


class TestGroupStatsPerformance(unittest.TestCase):
    """50 人 × 2.数万条：群聊统计必须在 2 秒内跑完（后台线程也不能慢到离谱）"""

    def test_fifty_members_under_two_seconds(self):
        msgs = []
        for i in range(50):
            uid = f"u{i:02d}"
            for k in range(500):
                msgs.append(_msg(uid, f"成员{i}", k * 50 + i, f"{uid} 的第 {k} 条消息"))
        chat = _group(msgs)
        t0 = time.time()
        stats = gs.compute_group_stats(chat)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0, f"群聊统计耗时 {elapsed:.2f}s，超出 2s 预算")
        self.assertEqual(stats["overview"]["member_count"], 50)
        self.assertEqual(len(stats["interaction"]["members"]), config.GROUP_MATRIX_MEMBERS)
        self.assertTrue(stats["interaction"]["truncated"])


_R7_BASE = 1704067200.0


def _r7_write_chat(payload) -> str:
    d = tempfile.mkdtemp(prefix="r7-parse-")
    path = os.path.join(d, "chat.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def _r7_msg(msg_id, uid, name, ts_s, el_type, el_data=None, text=""):
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


class TestInteractionMatrixIsMemoisedPerEffectiveLimit(unittest.TestCase):
    """Sparse interaction counts are primary; dense projections stay bounded."""

    def _chat(self, n_members=4):
        msgs = []
        seq = 0
        for i in range(n_members):
            uid = f"u{i}"
            for _ in range(n_members - i):
                msgs.append(_r7_msg(seq, uid, uid, _R7_BASE + seq * 2, "text", text="话"))
                seq += 1
        payload = {
            "chatInfo": {"name": "摸鱼群", "selfUid": "u0", "selfName": "u0", "type": "group"},
            "statistics": {"senders": [{"uid": f"u{i}", "name": f"u{i}"} for i in range(n_members)]},
            "messages": msgs,
        }
        path = _r7_write_chat(payload)
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

    def test_dense_projection_cache_is_bounded(self):
        chat = self._chat(8)
        for limit in (1, 2, 3, 4, 5):
            gs.calc_interaction_matrix(chat, top_k=limit)
        self.assertEqual(len(chat._matrix_cache), 3)
        self.assertEqual(set(chat._matrix_cache), {3, 4, 5})
        last = gs.calc_interaction_matrix(chat, top_k=5)
        self.assertIs(last, chat._matrix_cache[5])


_R6_BASE_MS = 1_700_000_000_000


class TestStatsConventions(unittest.TestCase):
    """断言 B3/B4/B5：媒体分家、中位数口径唯一、同名成员与自回复"""

    @staticmethod
    def _chat(items):
        """items: [(uid, name, text, kwargs...)] 直接构造 Message，绕开解析器"""
        from parser.qq_parser import ChatData, Message

        msgs = []
        for i, (uid, name, text, kw) in enumerate(items):
            m = Message(
                f"m{i}",
                _R6_BASE_MS + i * 60000,
                "2023-11-14 22:13:20",
                name,
                uid,
                text,
                text,
                "text",
                bool(kw.pop("has_image", False)),
                False,
                **kw,
            )
            msgs.append(m)
        return ChatData("c", "我", "对方", "u_self", "u_other", messages=msgs, total_count=len(msgs))

    def test_image_and_file_in_one_message_not_double_counted(self):
        """一条消息同时有图片与文件：字节必须分家，不能两边各记一份合计

        反向验证：把 calc_overview 里的 img_bytes/other_bytes 分家改回
        `image_bytes += m.media_bytes` / `other_media_bytes += m.media_bytes`，本条变红
        （仪表盘并排显示"图片 6144 · 文件/视频 6144"，而真实发送量只有 6144）。
        """
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [
                (
                    "u_self",
                    "我",
                    "看图说话",
                    {
                        "has_image": True,
                        "media_bytes": 2048 + 4096,
                        "media_kind": "file",
                        "image_bytes": 2048,
                        "image_count": 1,
                        "image_ids": ["IMG"],
                        "media_id": "IMG",
                    },
                ),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["image_bytes"], 2048, "图片体积只该算图片那一份")
        self.assertEqual(ov["other_media_bytes"], 4096, "非图片媒体只算剩下的那一份")
        self.assertEqual(ov["image_bytes"] + ov["other_media_bytes"], 6144, "合计不许重复计")

    def test_images_counted_per_picture_not_per_message(self):
        """一条消息发 3 张图 → "图片总数"该是 3（标签写的是张，此前按消息条数算）"""
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [
                (
                    "u_self",
                    "我",
                    "三连",
                    {
                        "has_image": True,
                        "media_bytes": 3000,
                        "image_bytes": 3000,
                        "image_count": 3,
                        "image_ids": ["A", "B", "C"],
                        "media_id": "A",
                    },
                ),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 3)
        self.assertEqual(ov["unique_images"], 3, "去重也按张，一条消息里的三张不该塌成一张")

    def test_legacy_message_without_image_split_still_counts(self):
        """手工构造的旧式 Message（没有分家字段）退回旧口径，不许把图片算成 0"""
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [("u_other", "对方", "图", {"has_image": True, "media_bytes": 100, "media_id": "x"})]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 1)
        self.assertEqual(ov["image_bytes"], 100)
        self.assertEqual(ov["other_media_bytes"], 0)

    def test_p50_survives_chronological_gaps(self):
        """中位数必须先排序：arr 是按对话时间序 append 的，"取中间那条"不是中位数

        反向验证：把 _stats 里的 _median(sorted(arr)) 改回 _median(arr)，本条变红——
        间隔按时间序 [10,100,20,30]，真中位数 25.0，未排序会报 (100+20)/2=60.0，
        等于把最长的一段对话当成"典型回复速度"，而 p50 恰恰是关系页的主展示指标。
        （为什么非要 n≥3 且乱序：n=2 时取中间两数平均与顺序无关；离群值若排在
        后半段，中间位置照样落进普通值——本轮两条 p50 用例都栽在这个盲区里。）
        """
        from analyzer.local_stats import calc_response_time
        from parser.qq_parser import ChatData, Message

        def m(i, uid, ts_s):
            return Message(
                f"m{i}",
                _R6_BASE_MS + ts_s * 1000,
                "2023-11-14 22:13:20",
                "我" if uid == "u_self" else "对方",
                uid,
                "话",
                "话",
                "text",
                False,
                False,
            )

        # 时间序上交替发言；self 侧回复间隔依次是 10、100、20、30 秒（乱序）
        timeline = [
            ("u_other", 0),
            ("u_self", 10),
            ("u_other", 15),
            ("u_self", 115),
            ("u_other", 120),
            ("u_self", 140),
            ("u_other", 145),
            ("u_self", 175),
        ]
        chat = ChatData(
            "c",
            "我",
            "对方",
            "u_self",
            "u_other",
            messages=[m(i, u, t) for i, (u, t) in enumerate(timeline)],
            total_count=len(timeline),
        )
        rt = calc_response_time(chat)
        self.assertEqual(rt["self"]["count"], 4, "self 侧应配对出 4 个间隔")
        self.assertEqual(rt["self"]["p50"], 25.0, f"[10,100,20,30] 的中位数是 25.0，实际 {rt['self']['p50']}")
        self.assertEqual(rt["self"]["avg"], 40.0)

    def test_median_of_even_sample_is_not_the_upper_middle(self):
        """句长中位数：偶数样本必须取中间两个的平均，而不是上中位

        反向验证：把 _median 改回 s[n//2]，本条变红 —— 两句 [1,9] 会被报成
        "中位句长 9"（真值 5.0），等于把最长的那句当成中位数。
        """
        from analyzer.local_stats import calc_message_length_stats

        chat = self._chat(
            [
                ("u_self", "我", "x", {}),
                ("u_self", "我", "y" * 9, {}),
                ("u_other", "对方", "a", {}),
                ("u_other", "对方", "b" * 100, {}),
            ]
        )
        st = calc_message_length_stats(chat)
        self.assertEqual(st["self"]["median"], 5.0, f"两句的中位数应是 5.0，实际 {st['self']['median']}")
        self.assertEqual(st["other"]["median"], 50.5)

    def test_p50_and_median_share_one_implementation(self):
        """同一个模块里"中位数"只许有一种算法（口径唯一是本地统计的立身之本）

        反向验证：让 p50 重新走 `_percentile(arr, 0.5)`（那是 int(round(0.5*(n-1)))，
        偶数样本取**下**中位），第二组断言立刻与第一组矛盾。
        """
        from analyzer.local_stats import _median

        self.assertEqual(_median([1, 9]), 5.0)
        self.assertEqual(_median([1, 2, 9]), 2.0)
        self.assertEqual(_median([]), 0.0)
        self.assertEqual(_median([7]), 7.0)

        # p50 走的就是它：造两个间隔 1s / 99s 的配对，仪表盘该报 50.0 而不是 1.0
        from analyzer.local_stats import calc_response_time

        msgs = [
            Message("a", _R6_BASE_MS, "t", "我", "u_self", "在", "在", "text", False, False),
            Message("b", _R6_BASE_MS + 1000, "t", "对方", "u_other", "嗯", "嗯", "text", False, False),
            Message("c", _R6_BASE_MS + 61000, "t", "我", "u_self", "在", "在", "text", False, False),
            Message("d", _R6_BASE_MS + 160000, "t", "对方", "u_other", "嗯", "嗯", "text", False, False),
        ]
        from parser.qq_parser import ChatData

        got = calc_response_time(
            ChatData("c", "我", "对方", "u_self", "u_other", messages=msgs, total_count=4)
        )
        self.assertEqual(got["other"]["p50"], 50.0, f"两次间隔的中位数应是 50s，实际 {got['other']['p50']}")

    def test_same_second_reply_is_not_reported_as_zero_speed(self):
        """秒级时间戳下同秒跨人配对不该算出"平均回复 0 秒"

        反向验证：把 calc_response_time 的 `gap <= 0` 判断去掉，本条变红
        （self: avg 0.0 / p50 0.0，界面显示的是一件物理上不可能的事）。
        """
        from analyzer.local_stats import calc_response_time

        chat = self._chat(
            [
                ("u_self", "我", "甲", {}),
                ("u_other", "对方", "乙", {}),
            ]
        )
        # 人为制造同毫秒的跨人配对
        chat.messages[0].timestamp = _R6_BASE_MS
        chat.messages[1].timestamp = _R6_BASE_MS
        got = calc_response_time(chat)
        self.assertEqual(got["other"]["count"], 0, "0 间隔不该被当成一次响应")
        self.assertEqual(got["other"]["avg"], 0.0)

    def test_duplicate_names_with_numeric_uids_are_unique(self):
        """真实 QQ 号前 4 位相同也必须消歧（此前 #uid4 会让两个"小明"合成一个人）

        反向验证：把 unique_display_names 的后缀改回 uid[:4]，本条变红。
        """
        from parser.group_identity import Participant, unique_display_names

        parts = [
            Participant(uid="1000000001", name="小明"),
            Participant(uid="1000000042", name="小明"),
        ]
        names = [p.name for p in unique_display_names(parts)]
        self.assertEqual(len(set(names)), 2, f"显示名冲突：{names}")
        self.assertTrue(all(n.startswith("小明#") for n in names), names)

    def test_anonymous_members_are_distinguishable(self):
        """两个匿名成员也不能撞名"""
        from parser.group_identity import Participant, unique_display_names

        parts = [Participant(uid="1000000001", name=""), Participant(uid="1000000042", name="")]
        names = [p.name for p in unique_display_names(parts)]
        self.assertEqual(len(set(names)), 2, f"匿名成员撞名：{names}")

    def test_self_reply_not_doubled_in_undirected_matrix(self):
        """自回复（引用自己发的消息）在无向矩阵对角上不该翻倍

        反向验证：把 _symmetrize 的对角分支改成与 off-diagonal 同样的相加，本条变红
        （3 次自回复报成 6 次，而矩阵与图例、总数从此对不上账）。
        """
        from analyzer.group_stats import calc_interaction_matrix
        from parser.qq_parser import load_chat

        msgs = []
        for i in range(4):
            msgs.append(
                {
                    "id": f"seed{i}",
                    "timestamp": _R6_BASE_MS + i * 60000,
                    "time": "2023-11-14 22:13:20",
                    "sender": {"uid": "uA", "name": "甲"},
                    "type": "text",
                    "content": {"text": f"甲原话{i}"},
                }
            )
        for i in range(3):
            msgs.append(
                {
                    "id": f"sr{i}",
                    "timestamp": _R6_BASE_MS + (400 + i) * 60000,
                    "time": "2023-11-14 23:00:00",
                    "sender": {"uid": "uA", "name": "甲"},
                    "type": "text",
                    "content": {
                        "text": f"自回复{i}",
                        "elements": [{"type": "reply", "data": {"referencedMessageId": f"seed{i}"}}],
                    },
                }
            )
        for i in range(3):
            msgs.append(
                {
                    "id": f"b{i}",
                    "timestamp": _R6_BASE_MS + (800 + i) * 60000,
                    "time": "2023-11-14 23:30:00",
                    "sender": {"uid": "uB", "name": "乙"},
                    "type": "text",
                    "content": {"text": f"乙{i}"},
                }
            )
        payload = {
            "chatInfo": {"name": "测试群", "selfUid": "uA", "selfName": "甲", "type": "group"},
            "statistics": {"totalMessages": len(msgs), "senders": [{"uid": "uA", "name": "甲"}]},
            "messages": msgs,
        }
        tmp = Path(tempfile.mkdtemp(prefix="r6-sr-")) / "selfreply.json"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            chat = load_chat(str(tmp))
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        inter = calc_interaction_matrix(chat)
        idx = [m["uid"] for m in inter["members"]].index("uA")
        self.assertEqual(inter["explicit_directed"][idx][idx], 3)
        self.assertEqual(inter["explicit_undirected"][idx][idx], 3, "无向对角不该翻倍")
        self.assertEqual(inter["self_replies"], 3, "自回复总数要能对账（关系图里没有自环）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
