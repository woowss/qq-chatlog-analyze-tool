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
import sys
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
from parser.qq_parser import ChatData, Message  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
