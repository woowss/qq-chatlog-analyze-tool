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
"""新版导出器真实格式的回归测试（对着真实群聊导出逐条核对过）

真实文件（数千条 / 数十位发言者 / 跨数月）暴露了四条与解析器假设不同的东西，
这个文件把它们钉死，避免以后改回去：

1. **消息 type 是语义化名字**（text/reply/system/file/forward/json/video/type_17/type_31），
   不是旧格式的 type_N —— `SKIP_MSG_TYPES` 里的 type_11/type_23 在真实文件里一次都不匹配。
2. **chatInfo.type = "group"**：导出器自己就写了这是群聊，比"数发言者"可靠得多。
3. **reply 元素带被回复消息的 id**（真实文件中绝大多数可回查到发言人），
   这是精确的"谁回复了谁"，不必靠相邻消息推断。
4. **at 元素带 uid**（真实文件中全部带），atType=1 是 @全体成员（uid="all"），
   不能当成某个人。

本文件只测数据层与统计口径（不联网、不渲染页面）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser.qq_parser as qp  # noqa: E402
from parser.qq_parser import CST, load_chat  # noqa: E402
from parser.group_identity import is_placeholder_sender  # noqa: E402
from analyzer import group_stats as gs  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
from analyzer.dialog import _message_line  # noqa: E402

BASE_MS = 1740900000000  # 2025-03-02 12:00:00 CST 附近，用例不关心具体日期


def _text_el(text):
    return {"type": "text", "data": {"text": text}}


def _msg(uid, name, i, text="内容", elements=None, msg_type="text", **extra):
    msg = {
        "id": f"m{i}",
        "timestamp": BASE_MS + i * 60_000,
        "time": "2025-03-02 12:00:00",
        "sender": {"uid": uid, "name": name},
        "type": msg_type,
        "content": {"text": text, "elements": [_text_el(text)] if elements is None else elements},
    }
    msg.update(extra)
    return msg


def _wrap(msgs, senders, self_uid="uA", chat_type="group", name="摸鱼群"):
    info = {"name": name, "selfUid": self_uid, "selfName": senders.get(self_uid, "我")}
    if chat_type:
        info["type"] = chat_type
    return json.dumps(
        {
            "chatInfo": info,
            "exportOptions": {"version": "5.x"},  # 新版导出器会多带这一层，解析器应忽略
            "statistics": {
                "senders": [
                    {"uid": u, "name": n, "messageCount": 0, "percentage": 0} for u, n in senders.items()
                ],
                "totalMessages": len(msgs),
                "timeRange": {"start": "2025-03-02T04:00:00.000Z", "end": "2025-03-02T05:00:00.000Z"},
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    )


def _write(payload: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
        f.write(payload)
        return f.name


class BaseCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("QQCHAT_GROUP_CHAT", None)
        os.environ.pop("QQCHAT_ALLOW_MULTI_PARTY", None)

    def load(self, payload: str, ready: bool = True):
        path = _write(payload)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch.object(qp, "GROUP_TRACK_READY", ready):
            return load_chat(path)


class TestExporterGroupTypeHint(BaseCase):
    """chatInfo.type == group 是比「数发言者」可靠的群聊信号"""

    def _small_group(self):
        # 三位成员，但第三位只说了 1 句 → 按发言门槛不会判成群聊
        msgs = (
            [_msg("uA", "我", i) for i in range(10)]
            + [_msg("uB", "小明", 10 + i) for i in range(10)]
            + [_msg("uC", "小红", 25, "我只说一句")]
        )
        return _wrap(msgs, {"uA": "我", "uB": "小明", "uC": "小红"}, name="三人小组")

    def test_group_label_is_honoured_when_track_ready(self):
        chat = self.load(self._small_group(), ready=True)
        self.assertTrue(chat.is_group_chat)
        self.assertEqual(chat.mode, "group")
        self.assertEqual(len(chat.participants()), 3)
        self.assertEqual(chat.other_uid, "")
        self.assertEqual(chat.other_name, "三人小组")

    def test_group_label_changes_nothing_before_track_is_ready(self):
        """群聊轨未就绪：这份文件的行为必须与升级前完全一样（当私聊、不拒收）"""
        chat = self.load(self._small_group(), ready=False)
        self.assertFalse(chat.is_group_chat)
        self.assertEqual(chat.mode, "private")
        self.assertTrue(chat.other_uid)

    def test_group_label_does_not_trigger_rejection_in_off_mode(self):
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "off"}):
            chat = self.load(self._small_group(), ready=True)
        self.assertFalse(chat.is_group_chat)
        self.assertEqual(chat.mode, "private")

    def test_group_label_with_two_party_below_threshold_is_unchanged(self):
        """two_party 模式 + 未达发言门槛的多人文件：语义就是「我 vs 其他人」（private），
        与升级前完全一致。这里刻意**不**因为导出器自报 type=group 就改标签——那会让
        仪表盘多出一条提示，属于用户可见的行为变化，不在本次改动范围内。"""
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "two_party"}):
            chat = self.load(self._small_group(), ready=True)
        self.assertEqual(chat.mode, "private")
        self.assertFalse(chat.is_group_chat)
        self.assertTrue(chat.other_uid)

    def test_private_export_without_type_is_unchanged(self):
        msgs = [_msg("uA", "我", i) for i in range(6)] + [_msg("uB", "对方", 6 + i) for i in range(6)]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "对方"}, chat_type="", name="对方"), ready=True)
        self.assertFalse(chat.is_group_chat)
        self.assertEqual(chat.chat_type, "")
        self.assertEqual(chat.other_uid, "uB")

    def test_two_member_group_is_still_a_group(self):
        """两人群（或长期只有两人说话）也应当按群处理，而不是硬套「我 vs 对方」"""
        msgs = [_msg("uA", "我", i) for i in range(5)] + [_msg("uB", "小明", 5 + i) for i in range(5)]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明"}, name="两人小群"))
        self.assertTrue(chat.is_group_chat)
        self.assertEqual(len(chat.participants()), 2)
        stats = gs.compute_group_stats(chat)
        self.assertEqual(stats["overview"]["member_count"], 2)
        self.assertEqual(len(stats["interaction"]["directed"]), 2)


class TestPlaceholderSender(BaseCase):
    """占位 sender 不得变成「幽灵成员」（哪怕漏了 system 标记）"""

    def test_predicate(self):
        self.assertTrue(is_placeholder_sender("未知uid未知", "系统消息"))
        self.assertTrue(is_placeholder_sender("未知uid未知", ""))
        self.assertTrue(is_placeholder_sender("u_1", "系统消息"))
        self.assertTrue(is_placeholder_sender("unknown_user", "某甲"))
        self.assertFalse(is_placeholder_sender("u_1", "小明"))
        self.assertFalse(is_placeholder_sender("u_1", ""))

    def test_unflagged_placeholder_goes_to_unknown_bucket(self):
        """真实踩过的坑：占位消息只有 1 条漏了 system 标记，照样不能当成员"""
        msgs = [_msg("uA", "我", i) for i in range(6)] + [_msg("uB", "小明", 6 + i) for i in range(6)]
        msgs.append(_msg("未知uid未知", "系统消息", 30, "（无标记的占位消息）"))  # type=text、无 system
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明", "未知uid未知": "系统消息"}))
        self.assertEqual([p.uid for p in chat.participants()], ["uA", "uB"])
        unknown = [m for m in chat.statistical() if gs.is_unknown_message(m)]
        self.assertEqual(len(unknown), 1)
        activity = gs.calc_member_activity(chat)
        self.assertEqual(sum(a["msg_count"] for a in activity) + len(unknown), len(chat.statistical()))

    def test_system_type_messages_are_skipped_even_without_flag(self):
        """新版导出器的 type=system 也要拦住（标记不齐全是实测踩过的坑）"""
        msgs = [_msg("uA", "我", i) for i in range(4)]
        msgs.append(_msg("uB", "小明", 10, "谁撤回了一条消息", msg_type="system"))
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明"}))
        self.assertEqual(len(chat.statistical()), 4)
        self.assertEqual([p.uid for p in chat.participants()], ["uA"])


class TestReplyTargets(BaseCase):
    """reply 元素 = 精确的「谁回复了谁」"""

    def _chat(self):
        msgs = [
            _msg("uA", "我", 0, "第一条"),
            _msg(
                "uB",
                "小明",
                1,
                "回复第一条",
                elements=[{"type": "reply", "data": {"referencedMessageId": "m0"}}, _text_el("回复第一条")],
            ),
            _msg(
                "uA",
                "我",
                3,
                "回复小明的回复",
                elements=[{"type": "reply", "data": {"referencedMessageId": "m1"}}, _text_el("回复你")],
            ),
            _msg(
                "uB",
                "小明",
                5,
                "引用一条不在导出里的",
                elements=[
                    {"type": "reply", "data": {"referencedMessageId": "missing"}},
                    _text_el("引用不到"),
                ],
            ),
        ]
        return self.load(_wrap(msgs, {"uA": "我", "uB": "小明"}))

    def test_reply_target_is_resolved(self):
        chat = self._chat()
        by_id = {m.id: m for m in chat.messages}
        self.assertEqual(by_id["m1"].reply_to_id, "m0")
        self.assertEqual(by_id["m1"].reply_to_uid, "uA")
        self.assertEqual(by_id["m3"].reply_to_uid, "uB")
        self.assertTrue(all(m.is_reply for m in chat.messages if m.reply_to_id))

    def test_unresolved_reply_is_counted_not_guessed(self):
        chat = self._chat()
        # _msg 用的是显式 id（m0/m1/m3/m5），引用不到的那条是 m5
        self.assertEqual([m.id for m in chat.messages if m.reply_to_id and not m.reply_to_uid], ["m5"])
        mi = gs.calc_interaction_matrix(chat)
        self.assertEqual(mi["reply_total"], 3, "三条回复标记（is_reply）")
        self.assertEqual(mi["reply_located"], 3, "三条都带引用 id")
        self.assertEqual(mi["reply_resolved"], 2)
        self.assertEqual(mi["reply_unresolved"], 1)
        # 账目必须闭合：总数 = 可定位 + 无可定位引用
        self.assertEqual(mi["reply_total"], mi["reply_located"] + mi["reply_no_target"])
        self.assertEqual(mi["reply_located"], mi["reply_resolved"] + mi["reply_unresolved"])

    def test_numeric_message_id_still_resolves_the_reply_target(self):
        """数字型 id 不许让"谁回复了谁"整体归零。

        回填靠 `uid_by_id = {m.id: ...}` 再按 reply_to_id 查，而 referencedMessageId
        是 str() 归一过的：只要 m.id 保留原生的 int，两边就永远对不上，
        每条回复都解析不到人。症状完全是静默的——文件解析成功、页面正常、
        只是精确互动矩阵全空、全部计入 reply_unresolved、成员画像拿到"0 次"。
        本仓库已承认数字 uid/id 在真实导出里存在，所以这不是理论输入。
        """
        msgs = [
            _msg("uA", "我", 0, "第一条", id=9001),
            _msg(
                "uB",
                "小明",
                1,
                "回复第一条",
                id=9002,
                elements=[
                    {"type": "reply", "data": {"referencedMessageId": 9001}},
                    _text_el("回复第一条"),
                ],
            ),
        ]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明"}))
        by_id = {m.id: m for m in chat.messages}
        self.assertIn("9001", by_id, "id 必须与 reply_to_id 同一类型口径（str）")
        self.assertEqual(by_id["9002"].reply_to_uid, "uA", "数字 id 的回复也必须回查到发言人")
        mi = gs.calc_interaction_matrix(chat)
        self.assertEqual(mi["reply_resolved"], 1, "精确回复信号不能因为 id 类型而丢失")
        self.assertEqual(mi["reply_unresolved"], 0)

    def test_explicit_matrix_uses_same_convention_as_inferred(self):
        """约定：X[i][j] = 「j 对 i 的动作」；列和=我对别人动作，行和=别人对我动作"""
        chat = self._chat()
        mi = gs.calc_interaction_matrix(chat)
        idx = {x["uid"]: i for i, x in enumerate(mi["members"])}
        self.assertEqual(mi["explicit_directed"][idx["uA"]][idx["uB"]], 1)  # B 回复了 A
        self.assertEqual(mi["explicit_directed"][idx["uB"]][idx["uA"]], 1)  # A 回复了 B
        totals = {t["uid"]: t for t in mi["totals"]}
        self.assertEqual(totals["uB"]["explicit_replies_to"], 1)  # 我回复别人 1 次
        self.assertEqual(totals["uB"]["explicit_replied_by"], 1)  # 别人回复我 1 次
        self.assertEqual(len(mi["explicit_edges"]), 1)  # 一条无向边，权重 2
        self.assertEqual(mi["explicit_edges"][0]["value"], 2)

    def test_reply_without_target_id_is_accounted(self):
        """真实文件里有 6 条 referencedMessageId 为 null（原消息已删除）：
        它们有回复标记却没有引用 id，必须计入 reply_no_target，不能悄悄消失"""
        msgs = [
            _msg("uA", "我", 0),
            _msg(
                "uB",
                "小明",
                1,
                "回复了但引用已删除",
                elements=[
                    {"type": "reply", "data": {"referencedMessageId": None, "senderName": "某人"}},
                    _text_el("回复"),
                ],
            ),
        ]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明"}))
        self.assertTrue(chat.messages[1].is_reply)
        self.assertEqual(chat.messages[1].reply_to_id, "")
        mi = gs.calc_interaction_matrix(chat)
        self.assertEqual(mi["reply_total"], 1)
        self.assertEqual(mi["reply_no_target"], 1)
        self.assertEqual(mi["reply_located"], 0)
        self.assertEqual(mi["reply_resolved"], 0)

    def test_mention_accounting_is_closed(self):
        """@ 点名也要对账：总数 = 进矩阵的 + 指向占位/无归属的 + 在矩阵之外的"""
        msgs = [
            _msg("uA", "我", 0),
            _msg(
                "uB",
                "小明",
                1,
                "@小红 @未知用户",
                elements=[
                    {"type": "at", "data": {"uid": "uC", "name": "小红", "atType": 2}},
                    {"type": "at", "data": {"uid": "未知uid未知", "name": "系统消息", "atType": 2}},
                    _text_el("@小红 @未知用户"),
                ],
            ),
        ]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明", "uC": "小红", "未知uid未知": "系统消息"}))
        mi = gs.calc_interaction_matrix(chat)
        attributed = sum(sum(r) for r in mi["mention_directed"])
        self.assertEqual(mi["mention_total"], 2)
        self.assertEqual(mi["mention_unknown"], 1)
        self.assertEqual(attributed + mi["mention_unknown"] + mi["mention_outside"], mi["mention_total"])

    def test_explicit_and_inferred_are_kept_apart(self):
        """精确回复与相邻推断必须分开：把它们相加会得出「互动次数」这种假精确数字"""
        chat = self._chat()
        mi = gs.calc_interaction_matrix(chat)
        self.assertNotEqual(mi["directed"], mi["explicit_directed"])
        self.assertEqual(sum(sum(r) for r in mi["explicit_directed"]), 2)
        self.assertGreaterEqual(sum(sum(r) for r in mi["directed"]), 2)

    def test_reply_to_placeholder_is_not_attributed(self):
        msgs = [
            _msg("uA", "我", 0),
            _msg(
                "uB",
                "小明",
                1,
                "引用占位消息",
                elements=[{"type": "reply", "data": {"referencedMessageId": "m9"}}, _text_el("引用占位")],
            ),
            _msg("未知uid未知", "系统消息", 9, "占位", msg_type="text"),
        ]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明", "未知uid未知": "系统消息"}))
        mi = gs.calc_interaction_matrix(chat)
        self.assertEqual(mi["reply_unknown"], 1)
        self.assertEqual(sum(sum(r) for r in mi["explicit_directed"]), 0)


class TestTimestampMagnitude(BaseCase):
    """时间戳量级：秒/微秒/纳秒导出不许崩溃，也不许凭空造出一个付费月份"""

    def _one_msg(self, ts, time_str="2023-11-15 08:00:00"):
        msgs = [
            {
                "id": "x1",
                "timestamp": ts,
                "time": time_str,
                "sender": {"uid": "uA", "name": "我"},
                "type": "text",
                "content": {"text": "在吗", "elements": [_text_el("在吗")]},
            }
        ]
        return self.load(_wrap(msgs, {"uA": "我"}))

    def test_seconds_and_microseconds_normalise_to_the_same_moment(self):
        """同一个时刻的四种写法，归一后必须落在同一个月份。

        量级错了的后果两边都不可接受：微秒 → /1000 后是荒谬的年 →
        datetime.fromtimestamp 抛 `OSError: [Errno 22]` 让**整份文件传不上去**
        （用户看到的症状与"时间戳格式"毫不相干）；秒 → 解释成 1970-01，
        凭空多出一个"月份"，而 _analyze_periods 会把它当真实月份发一次付费调用。
        """
        wanted = self._one_msg(1700000000000).messages[0].time_str[:7]
        self.assertEqual(wanted, "2023-11")
        for ts in (1700000000, 1700000000000, 1700000000000000, 1700000000000000000):
            with self.subTest(ts=ts):
                chat = self._one_msg(ts)
                self.assertEqual(len(chat.messages), 1, "不许把救得回来的量级整条丢弃")
                self.assertEqual(chat.messages[0].time_str[:7], wanted, f"量级 {ts} 归一错了")
                self.assertNotIn("1970-01", chat.messages[0].time_str, "不许造出 1970 的幽灵月份")

    def test_unrescuable_positive_value_falls_back_to_the_time_string(self):
        """数值救不回来时回退 time 字符串，而不是把荒谬值喂给下游。"""
        chat = self._one_msg(5, time_str="2023-11-15 08:00:00")
        self.assertEqual(len(chat.messages), 1)
        self.assertTrue(chat.messages[0].time_str.startswith("2023-11-15"))

    def test_stats_survive_a_mis_scaled_export(self):
        """回归钉子：微秒导出过去会在 calc_overview 里直接把整份文件打崩。"""
        from analyzer.local_stats import calc_overview

        chat = self._one_msg(1700000000000000)
        ov = calc_overview(chat)  # 修复前这里 OSError: [Errno 22] Invalid argument
        self.assertEqual(ov["total_messages"], 1)

    def test_missing_timestamp_is_dropped_not_invented(self):
        """非正数且无可用 time 字符串：丢弃并计数，绝不落到 1970-01。"""
        msgs = [
            {
                "id": "y1",
                "timestamp": 0,
                "sender": {"uid": "uA", "name": "我"},
                "type": "text",
                "content": {"text": "坏时间戳", "elements": [_text_el("坏时间戳")]},
            }
        ]
        chat = self.load(_wrap(msgs, {"uA": "我"}))
        self.assertEqual(chat.messages, [], "ts<=0 且无 time 字符串必须丢弃")
        self.assertEqual(chat.dropped_messages, 1)


class TestAtMentions(BaseCase):
    """at 元素 = 明确的点名；@全体成员不是人"""

    def _chat(self):
        msgs = [
            _msg("uA", "我", 0),
            _msg(
                "uB",
                "小明",
                1,
                "@小红 看这个",
                elements=[
                    {"type": "at", "data": {"uid": "uC", "name": "小红", "atType": 2}},
                    _text_el("@小红 看这个"),
                ],
            ),
            _msg(
                "uC",
                "小红",
                2,
                "@小明 收到",
                elements=[
                    {"type": "at", "data": {"uid": "uB", "name": "小明", "atType": 2}},
                    _text_el("@小明 收到"),
                ],
            ),
            _msg(
                "uA",
                "我",
                3,
                "@全体成员 开会",
                elements=[
                    {"type": "at", "data": {"uid": "all", "uin": "0", "name": "全体成员", "atType": 1}},
                    _text_el("@全体成员 开会"),
                ],
            ),
        ]
        return self.load(_wrap(msgs, {"uA": "我", "uB": "小明", "uC": "小红"}))

    def test_mentions_are_attributed(self):
        chat = self._chat()
        self.assertEqual(chat.messages[1].mentions, ["uC"])
        self.assertEqual(chat.messages[2].mentions, ["uB"])
        mi = gs.calc_interaction_matrix(chat)
        idx = {x["uid"]: i for i, x in enumerate(mi["members"])}
        self.assertEqual(mi["mention_directed"][idx["uC"]][idx["uB"]], 1)  # B 点名 C
        self.assertEqual(mi["mention_directed"][idx["uB"]][idx["uC"]], 1)  # C 点名 B
        totals = {t["uid"]: t for t in mi["totals"]}
        self.assertEqual(totals["uC"]["mentions_received"], 1)
        self.assertEqual(totals["uB"]["mentions_sent"], 1)

    def test_at_all_is_not_a_member(self):
        chat = self._chat()
        self.assertTrue(chat.messages[3].mentions_all)
        self.assertEqual(chat.messages[3].mentions, [])
        mi = gs.calc_interaction_matrix(chat)
        self.assertEqual(mi["mentions_all_count"], 1)
        self.assertEqual(sum(sum(r) for r in mi["mention_directed"]), 2, "@全体成员不得摊到任何成员头上")
        self.assertNotIn("all", [x["uid"] for x in mi["members"]])

    def test_duplicate_mention_in_one_message_counted_once(self):
        msgs = [
            _msg("uA", "我", 0),
            _msg(
                "uB",
                "小明",
                1,
                "@小红 @小红",
                elements=[
                    {"type": "at", "data": {"uid": "uC", "name": "小红", "atType": 2}},
                    {"type": "at", "data": {"uid": "uC", "name": "小红", "atType": 2}},
                    _text_el("@小红 @小红"),
                ],
            ),
        ]
        chat = self.load(_wrap(msgs, {"uA": "我", "uB": "小明", "uC": "小红"}))
        self.assertEqual(chat.messages[1].mentions, ["uC"])


class TestMatrixSchemaStability(BaseCase):
    """矩阵的字段名是对外契约（M3 的图表按名取用），改名必须是有意识的"""

    def test_interaction_keys(self):
        msgs = [_msg("uA", "我", i) for i in range(3)] + [_msg("uB", "小明", 3 + i) for i in range(3)]
        mi = gs.calc_interaction_matrix(self.load(_wrap(msgs, {"uA": "我", "uB": "小明"})))
        self.assertEqual(
            set(mi),
            {
                "members",
                "directed",
                "undirected",
                "edges",
                "explicit_directed",
                "explicit_undirected",
                "explicit_edges",
                # 自回复总数：关系图的边只取 i<j（力导向画不出自环），而自回复在矩阵
                # 对角上又看得到，所以必须有个字段能对账，不能让它无声消失。
                # 第六轮审查加的字段（见 analyzer/group_stats.py 的 _symmetrize）。
                "self_replies",
                "mention_directed",
                "mentions_all_count",
                "totals",
                "truncated",
                "dropped",
                "dropped_replies",
                "matrix_limit",
                "unknown_replies",
                "reply_total",
                "reply_located",
                "reply_no_target",
                "reply_resolved",
                "reply_unresolved",
                "reply_unknown",
                "reply_outside",
                "mention_total",
                "mention_unknown",
                "mention_outside",
            },
        )
        self.assertEqual(
            set(mi["totals"][0]),
            {
                "uid",
                "name",
                "is_self",
                "replies_to",
                "replied_by",
                "explicit_replies_to",
                "explicit_replied_by",
                "mentions_sent",
                "mentions_received",
            },
        )


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


class TestNameHistoryStripsDisplayNames(unittest.TestCase):
    """带尾空格的昵称不许被拆成两个桶——那会显示一次从未发生的改名。"""

    def _chat(self):
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
            "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "小明"}]},
            "messages": [
                _r7_msg(1, "uA", "我", _R7_BASE, "text", text="早"),
                _r7_msg(2, "uB", "小明", _R7_BASE + 60, "text", text="早"),
                _r7_msg(3, "uB", "小明 ", _R7_BASE + 120, "text", text="在"),
                _r7_msg(4, "uB", "   ", _R7_BASE + 180, "text", text="忙"),
            ],
        }
        path = _r7_write_chat(payload)
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


_F7_BASE = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp())


def _f7_el(msg_id, uid, name, ts_s, el_type, data, text=""):
    """构造一条"带结构化元素"的消息（新版导出器形态）"""
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": el_type,
        "content": {"text": text, "elements": [{"type": el_type, "data": data}]},
    }


def _f7_text(msg_id, uid, name, ts_s, text):
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": "text",
        "content": {"text": text, "elements": [{"type": "text", "data": {"text": text}}]},
    }


def _f7_load(msgs, self_uid="uA"):
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


class TestVoiceMedia(unittest.TestCase):
    """语音消息本体此前不在任何分派分支：以零正文混进计数、模型完全看不见。"""

    def test_voice_gets_kind_and_label(self):
        chat = _f7_load([_f7_el(1, "uB", "对方", _F7_BASE, "voice", {"duration": 12})])
        m = chat.messages[0]
        self.assertEqual(m.media_kind, "voice", "语音必须进媒体口径（kind 非空才不被当空正文）")
        self.assertIn("12", m.media_label)
        self.assertEqual(m.text, "", "占位符不进正文，避免污染词频与句长")

    def test_voice_dialog_line_has_mark(self):
        chat = _f7_load([_f7_el(1, "uB", "对方", _F7_BASE, "voice", {"duration": 5})])
        m = chat.messages[0]
        line = _message_line(m, "对方")
        self.assertIn("语音", line, "喂模型的对话行要标注语音，否则模型看不见它发生过")

    def test_voice_without_duration_still_labeled(self):
        chat = _f7_load([_f7_el(1, "uB", "对方", _F7_BASE, "voice", {"summary": ""})])
        self.assertEqual(chat.messages[0].media_kind, "voice")


class TestUnknownElements(unittest.TestCase):
    """未知元素类型不再被静默吞掉：计数、上报，且绝不静默把正文判死。"""

    def test_unknown_type_is_counted(self):
        chat = _f7_load(
            [
                _f7_text(1, "uA", "我", _F7_BASE, "早"),
                _f7_el(2, "uB", "对方", _F7_BASE + 60, "brand_new_thing", {"x": 1}),
            ]
        )
        self.assertEqual(chat.unknown_element_types.get("brand_new_thing"), 1)
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["unknown_element_types"].get("brand_new_thing"), 1, "overview 要能拿到漂移信号")

    def test_all_unknown_message_falls_back_to_raw_text(self):
        # 导出器把 "text" 改名成新类型：结构化侧给不出正文，但原始文本在——不能丢。
        m = {
            "id": "1",
            "timestamp": _F7_BASE * 1000,
            "time": "2025-03-01 20:00:00",
            "sender": {"uid": "uB", "name": "对方"},
            "type": "renamed_text",
            "content": {"text": "记得吃饭", "elements": [{"type": "renamed_text", "data": {}}]},
        }
        chat = _f7_load([m])
        self.assertEqual(chat.messages[0].text, "记得吃饭", "全未知元素时回落原始文本，正文不丢")

    def test_image_only_message_does_not_fall_back(self):
        # 有已识别元素（image）的消息不回落，以免把 "[图片]" 当成正文（旧口径钉住）。
        chat = _f7_load([_f7_el(1, "uB", "对方", _F7_BASE, "image", {"url": "x.jpg"}, text="[图片]")])
        self.assertEqual(chat.messages[0].text, "")


class TestRecallCounts(unittest.TestCase):
    """撤回标记已解析却零消费——补上按人计数（recalled 内容仍不进正文统计，那是正确的）。"""

    def test_recall_counted_per_party(self):
        msgs = [
            _f7_text(1, "uA", "我", _F7_BASE, "在吗"),
            _f7_text(2, "uB", "对方", _F7_BASE + 60, "在的"),
        ]
        msgs[1]["recalled"] = True
        msgs.append({**_f7_text(3, "uA", "我", _F7_BASE + 120, "算了"), "recalled": True})
        ov = ls.calc_overview(_f7_load(msgs))
        self.assertEqual(ov["total_recalls"], 2)
        self.assertEqual(ov["self_recalls"], 1)
        self.assertEqual(ov["other_recalls"], 1)

    def test_recalled_not_in_body_stats(self):
        # 反向锚点：撤回的那条不该进 total_messages（正文口径），只进撤回计数。
        msgs = [
            _f7_text(1, "uA", "我", _F7_BASE, "你好"),
            {**_f7_text(2, "uB", "对方", _F7_BASE + 60, "撤回我"), "recalled": True},
        ]
        ov = ls.calc_overview(_f7_load(msgs))
        self.assertEqual(ov["total_messages"], 1)
        self.assertEqual(ov["total_recalls"], 1)


class TestOtherMediaFix(unittest.TestCase):
    """total_other_media 此前写死枚举三类，卡片/通话被解析被计数却漏进汇总（界面恒显示 0）。"""

    def test_card_and_call_now_included(self):
        chat = _f7_load(
            [
                _f7_el(1, "uB", "对方", _F7_BASE, "json", {"summary": "分享了一个链接"}),
                _f7_el(2, "uB", "对方", _F7_BASE + 60, "av_record", {"summary": "通话 - 未接听"}),
            ]
        )
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_other_media"], 2, "卡片+通话都要进「其他」，旧枚举口径是 0")

    def test_no_double_count_with_voice(self):
        chat = _f7_load(
            [
                _f7_el(1, "uB", "对方", _F7_BASE, "voice", {"duration": 3}),
                _f7_el(2, "uB", "对方", _F7_BASE + 60, "wallet", {"summary": "恭喜发财"}),
            ]
        )
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_voices"], 1)
        # 语音单独成项，不重复计入 other；other 只剩红包
        self.assertEqual(ov["total_other_media"], 1)


def _fix_wrap(msgs, senders, self_uid="uA"):
    """按导出格式包装（msgs 为完整消息字典，便于构造 system/type_23 等特殊条目）"""
    return json.dumps(
        {
            "chatInfo": {
                "name": senders.get("uB", "对方"),
                "selfUid": self_uid,
                "selfName": senders.get(self_uid, "我"),
            },
            "statistics": {
                "senders": [{"uid": uid, "name": name} for uid, name in senders.items()],
                "totalMessages": len(msgs),
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _fix_write_tmp(payload):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(payload)
        return f.name


class TestMediaParticipation(unittest.TestCase):
    """文件/视频/转发/卡片/通话/商城表情都要参与统计与分析（真实导出里的形态）"""

    @staticmethod
    def _msg_with(el_type, data, msg_type=None, text="", **kw):
        m = {
            "id": "1",
            "timestamp": 1704067200000,
            "time": "2024-01-01T00:00:00.000Z",
            "sender": {"uid": "uA", "name": "我"},
            "type": msg_type or el_type,
            "content": {"text": text, "elements": [{"type": el_type, "data": data}]},
        }
        m.update(kw)
        return m

    def _load(self, msgs):
        path = _fix_write_tmp(_fix_wrap(msgs, {"uA": "我", "uB": "对方"}))
        try:
            from parser.qq_parser import load_chat

            return load_chat(path)
        finally:
            os.remove(path)

    def test_file_and_video_carry_labels_but_not_body_text(self):
        chat = self._load(
            [
                self._msg_with(
                    "file", {"filename": "example.zip", "size": "12345"}, text="[文件:example.zip]"
                ),
                self._msg_with("video", {"filename": "clip.mp4", "size": "999"}, text="[视频:clip.mp4]"),
            ]
        )
        f, v = chat.messages
        self.assertEqual((f.media_kind, f.media_label), ("file", "example.zip"))
        self.assertEqual((v.media_kind, v.media_label), ("video", "clip.mp4"))
        # 正文保持干净：文件名不能灌进词频/句长口径
        self.assertEqual(f.text, "")
        self.assertEqual(v.text, "")

    def test_forward_card_gets_title_and_count(self):
        chat = self._load(
            [
                self._msg_with(
                    "forward",
                    {"title": "小明和小红的聊天记录", "messageCount": "25"},
                    msg_type="json",
                    text="[转发消息: 25条]",
                )
            ]
        )
        m = chat.messages[0]
        self.assertEqual(m.media_kind, "forward")
        self.assertIn("25条", m.media_label)
        self.assertIn("聊天记录", m.media_label)

    def test_call_record_and_json_card_and_wallet(self):
        chat = self._load(
            [
                self._msg_with("av_record", {}, msg_type="type_19", text="通话 - 未接听，点击回拨"),
                self._msg_with("json", {}, msg_type="json", text="[JSON消息]"),
                self._msg_with(
                    "wallet", {"summary": "红包/钱包消息"}, msg_type="type_10", text="红包/钱包消息"
                ),
            ]
        )
        kinds = [m.media_kind for m in chat.messages]
        self.assertEqual(kinds, ["av_record", "json", "wallet"])
        self.assertIn("未接听", chat.messages[0].media_label)

    def test_market_face_counts_as_face_and_is_not_skipped(self):
        """type_17（商城大表情）此前被整个跳过，是 277 条白白丢掉的信号"""
        chat = self._load(
            [self._msg_with("market_face", {"name": "[[叉腰]]"}, msg_type="type_17", text="[[叉腰]]")]
        )
        m = chat.messages[0]
        self.assertEqual(m.face_names, ["叉腰"])
        from parser.qq_parser import is_statistical

        self.assertTrue(is_statistical(m), "商城表情不该被当成不可分析的卡片")
        from analyzer.local_stats import calc_face_stats, calc_overview

        self.assertEqual(calc_overview(chat)["total_faces"], 1)
        self.assertEqual(calc_face_stats(chat)["self"], {"叉腰": 1})

    def test_media_messages_enter_ai_dialog_with_markers(self):
        import analyzer.deepseek_client as dc

        chat = self._load(
            [
                self._msg_with("file", {"filename": "示例表.xlsx"}, text="[文件:示例表.xlsx]"),
                self._msg_with(
                    "forward",
                    {"title": "小明和小红的聊天记录", "messageCount": "4"},
                    msg_type="json",
                    text="[转发消息: 4条]",
                ),
                self._msg_with("av_record", {}, msg_type="type_19", text="通话 - 未接听"),
            ]
        )
        for m in chat.messages:
            self.assertTrue(dc._has_content(m), "媒体消息必须能进 AI 对话")
        lines = [dc._message_line(m, "我") for m in chat.messages]
        self.assertIn("[文件:示例表.xlsx]", lines[0])
        self.assertIn("转发:", lines[1])
        self.assertIn("通话:", lines[2])

    def test_media_does_not_pollute_length_or_word_stats(self):
        from analyzer.local_stats import calc_message_length_stats

        long_name = "a" * 80 + ".zip"
        chat = self._load([self._msg_with("file", {"filename": long_name}, text=f"[文件:{long_name}]")])
        stats = calc_message_length_stats(chat)
        self.assertEqual(stats["self"]["max"], 0, "文件名不该计入发言长度")

    def test_overview_counts_media_by_kind(self):
        from analyzer.local_stats import calc_overview

        chat = self._load(
            [
                self._msg_with("file", {"filename": "a.zip"}),
                self._msg_with("file", {"filename": "b.zip"}),
                self._msg_with("video", {"filename": "c.mp4"}),
                self._msg_with("forward", {"title": "t", "messageCount": "3"}, msg_type="json"),
                self._msg_with("wallet", {"summary": "红包"}),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_files"], 2)
        self.assertEqual(ov["total_videos"], 1)
        self.assertEqual(ov["total_forwards"], 1)
        self.assertEqual(ov["total_other_media"], 1)

    def test_media_volume_and_dedup(self):
        """导出器给到的 size/md5 用于体积与去重统计（同一张图反复发只算一张）"""
        from analyzer.local_stats import calc_overview

        img = {"filename": "a.jpg", "size": "2048", "md5": "AAA"}
        chat = self._load(
            [
                self._msg_with("image", img),
                self._msg_with("image", img),  # 同一张图重复发送
                self._msg_with("image", {"filename": "b.jpg", "size": "1024", "md5": "BBB"}),
                self._msg_with("file", {"filename": "c.zip", "size": "4096", "md5": "CCC"}),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 3)
        self.assertEqual(ov["image_bytes"], 2048 * 2 + 1024)
        self.assertEqual(ov["unique_images"], 2)
        self.assertEqual(ov["other_media_bytes"], 4096, "文件体积计入非图片媒体，不计入图片体积")

    def test_media_size_dirty_values_do_not_crash(self):
        from analyzer.local_stats import calc_overview

        chat = self._load([self._msg_with("image", {"filename": "x.jpg", "size": "未知", "md5": None})])
        ov = calc_overview(chat)
        self.assertEqual(ov["image_bytes"], 0)
        self.assertIsNone(ov["unique_images"], "没有 md5 时应给 None 而不是假装 0")


class TestTimeNormalization(unittest.TestCase):
    """时间字符串必须与统计口径一致（新版导出器写的是 UTC ISO 时间）"""

    def test_iso_utc_time_is_converted_to_cst(self):
        from parser.qq_parser import load_chat

        msgs = [
            {
                "id": "1",
                "timestamp": 1704067200000,
                "time": "2024-01-01T00:00:00.000Z",
                "sender": {"uid": "uA", "name": "我"},
                "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
            }
        ]
        path = _fix_write_tmp(_fix_wrap(msgs, {"uA": "我", "uB": "对方"}))
        try:
            chat = load_chat(path)
        finally:
            os.remove(path)
        self.assertEqual(chat.messages[0].time_str, "2024-01-01 08:00:00")

    def test_iso_fallback_when_timestamp_missing(self):
        from parser.qq_parser import _parse_timestamp

        ts = _parse_timestamp(None, "2024-01-01T00:00:00.000Z")
        self.assertIsNotNone(ts, "缺 timestamp 时应能从 ISO 字符串回退解析")
        self.assertEqual(ts, 1704067200000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
