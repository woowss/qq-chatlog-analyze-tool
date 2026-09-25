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
"""群聊地基的护栏测试（M0：只加不改，默认行为与升级前逐字节一致）

这一层不测"群聊分析好不好用"（那是 M1/M2/M3 的事），只钉死四件事：
1. 判定矩阵：默认仍是拒收；off 仍是拒收；two_party/旧变量仍是归并；
   占位 sender 与零散第三方仍判私聊（三条既有回归不得被新代码破坏）。
2. 数据层兼容：ChatData 的构造签名、相等性与 repr 语义不变；
   参与者名单按口径收集、显示名唯一化、结果带缓存。
3. 提示词指纹隔离：私聊指纹与月份键**取值不变**，且不再受新增
   SYSTEM_PROMPT_* 常量影响（否则全部私聊缓存会被一次性作废、用户重新付费）。
4. 私聊冻结：compute_stats 的键集合与页面渲染不得出现群聊字段/标记。

命名前缀 test_group_*：M1 起还有 test_group_stats.py / test_group_ai.py / test_group_web.py。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402
from _stats import ensure_stats  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser import qq_parser  # noqa: E402
from parser.qq_parser import ChatData, Message, load_chat  # noqa: E402

CST = timezone(timedelta(hours=8))
_BASE_MS = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)


def _bulk(uid: str, name: str, count: int, start: int = 0, **extra) -> list[dict]:
    """造 count 条来自同一个人的消息（extra 用于叠加 system/type 等特殊字段）"""
    out = []
    for i in range(count):
        step = start + i
        out.append(
            {
                "id": f"{uid}-{step}",
                "timestamp": _BASE_MS + step * 60_000,
                "time": "2025-03-01 20:00:00",
                "sender": {"uid": uid, "name": name},
                "content": f"{name}的第 {step} 条",
                **extra,
            }
        )
    return out


def _wrap(msgs, senders, self_uid="uA", chat_name="对方"):
    return json.dumps(
        {
            "chatInfo": {"name": chat_name, "selfUid": self_uid, "selfName": senders.get(self_uid, "我")},
            "statistics": {
                "senders": [{"uid": uid, "name": name} for uid, name in senders.items()],
                "totalMessages": len(msgs),
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    )


def _write_tmp(payload: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
        f.write(payload)
        return f.name


def _three_party_payload(chat_name="对方"):
    """3 位有实质发言者：私聊判定的"群聊样本"（与既有回归同一形状）"""
    msgs = _bulk("uA", "我", 20) + _bulk("uB", "对方", 20, start=20) + _bulk("uC", "第三人", 6, start=40)
    return _wrap(msgs, {"uA": "我", "uB": "对方", "uC": "第三人"}, chat_name=chat_name)


def _msg(uid, name, ts_offset=0, text="x", **kw):
    """直接构造 Message（数据层用例不必绕 JSON）"""
    return Message(
        id=f"{uid}-{ts_offset}",
        timestamp=_BASE_MS + ts_offset * 60_000,
        time_str="2025-03-01 20:00:00",
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type=kw.get("msg_type", "type_1"),
        has_image=False,
        is_reply=False,
        recalled=kw.get("recalled", False),
        system=kw.get("system", False),
    )


def _chat(messages, self_uid="uA", self_name="我", other_name="对方", other_uid="uB"):
    return ChatData(
        chat_name="测试",
        self_name=self_name,
        other_name=other_name,
        self_uid=self_uid,
        other_uid=other_uid,
        messages=messages,
    )


class TestMultiPartyAction(unittest.TestCase):
    """判定矩阵：默认与升级前一致，只有显式配置才改变处置方式"""

    def setUp(self):
        # 每个用例都从"干净的环境"开始：不清掉旧变量会让用例互相污染
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("QQCHAT_GROUP_CHAT", None)
        os.environ.pop("QQCHAT_ALLOW_MULTI_PARTY", None)

    def test_default_mode_detects_group(self):
        """默认（auto + 群聊轨就绪，2026-09-12 起）→ 3 位有实质发言者按群聊处理"""
        self.assertEqual(qq_parser.group_chat_mode(), "auto")
        self.assertTrue(qq_parser.GROUP_TRACK_READY, "M3 之后群聊轨应处于就绪状态")
        path = _write_tmp(_three_party_payload())
        try:
            chat = load_chat(path)
            self.assertTrue(chat.is_group_chat)
            self.assertEqual(chat.mode, "group")
        finally:
            os.remove(path)

    def test_off_mode_rejects(self):
        path = _write_tmp(_three_party_payload())
        try:
            with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "off"}):
                self.assertEqual(qq_parser.group_chat_mode(), "off")
                with self.assertRaises(ValueError):
                    load_chat(path)
        finally:
            os.remove(path)

    def test_two_party_mode_merges(self):
        """显式 two_party：仍按"我 vs 其他人"归并，且如实记下口径"""
        path = _write_tmp(_three_party_payload())
        try:
            with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "two_party"}):
                chat = load_chat(path)
            self.assertFalse(chat.is_group_chat)
            self.assertEqual(chat.mode, "two_party")
            self.assertEqual(len(chat.messages), 46)
            self.assertEqual(chat.self_uid, "uA")
            self.assertTrue(chat.other_uid)  # 仍有单一"对方"
        finally:
            os.remove(path)

    def test_legacy_env_alias_is_two_party(self):
        """旧变量 QQCHAT_ALLOW_MULTI_PARTY=1 继续有效（README 与旧配置依赖它）"""
        path = _write_tmp(_three_party_payload())
        try:
            with mock.patch.dict(os.environ, {"QQCHAT_ALLOW_MULTI_PARTY": "1"}):
                self.assertEqual(qq_parser.group_chat_mode(), "two_party")
                chat = load_chat(path)
            self.assertEqual(chat.mode, "two_party")
        finally:
            os.remove(path)

    def test_new_variable_wins_over_legacy(self):
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "off", "QQCHAT_ALLOW_MULTI_PARTY": "1"}):
            self.assertEqual(qq_parser.group_chat_mode(), "off")

    def test_invalid_mode_falls_back_to_auto(self):
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "groupchat"}):
            self.assertEqual(qq_parser.group_chat_mode(), "auto")

    def test_group_when_track_ready(self):
        """群聊轨就绪后（M3 会翻这个开关）：识别为群聊，且没有单一"对方" """
        path = _write_tmp(_three_party_payload(chat_name="摸鱼群"))
        try:
            with mock.patch.object(qq_parser, "GROUP_TRACK_READY", True):
                chat = load_chat(path)
            self.assertTrue(chat.is_group_chat)
            self.assertEqual(chat.mode, "group")
            self.assertEqual(chat.other_uid, "")
            self.assertEqual(chat.other_name, "摸鱼群")
            names = [p.name for p in chat.participants()]
            self.assertEqual(names[0], "我")
            self.assertEqual(len(names), 3)
            self.assertTrue(chat.participants()[0].is_self)
        finally:
            os.remove(path)

    def test_placeholder_sender_still_private(self):
        """占位 sender（系统消息 + 一条无 system 标记的 type_23）不得判成群聊"""
        msgs = _bulk("uA", "我", 30) + _bulk("uB", "对方", 28, start=30)
        placeholder = _bulk("未知uid未知", "系统消息", 4, start=100, system=True)
        placeholder.append(
            {
                "id": "999",
                "timestamp": _BASE_MS + 200 * 60_000,
                "time": "2025-03-01 23:20:00",
                "sender": {"uid": "未知uid未知", "name": "系统消息"},
                "type": "type_23",
                "content": "商城表情",
            }
        )
        path = _write_tmp(_wrap(msgs + placeholder, {"uA": "我", "uB": "对方", "未知uid未知": "系统消息"}))
        try:
            for env in ({}, {"QQCHAT_GROUP_CHAT": "off"}, {"QQCHAT_GROUP_CHAT": "two_party"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    chat = load_chat(path)
                    self.assertFalse(chat.is_group_chat)
                    self.assertEqual(chat.other_name, "对方")
        finally:
            os.remove(path)

    def test_stray_third_party_still_private(self):
        """零散第三方（2 条，未达门槛）仍判私聊：残留失真保持现状，不改变行为"""
        msgs = _bulk("uA", "我", 25) + _bulk("uB", "对方", 24, start=25) + _bulk("uX", "路人", 2, start=60)
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方", "uX": "路人"}))
        try:
            with mock.patch.object(qq_parser, "GROUP_TRACK_READY", True):
                chat = load_chat(path)  # 门槛未达 → 即使群聊轨就绪也仍是私聊
            self.assertFalse(chat.is_group_chat)
            self.assertEqual(chat.other_name, "对方")
        finally:
            os.remove(path)


class TestParticipantIdentity(unittest.TestCase):
    """参与者名单：口径、排序、唯一显示名与缓存"""

    def test_collect_sorted_by_count_and_marks_self(self):
        msgs = (
            _bulk_chat_messages("uB", "对方", 5)
            + _bulk_chat_messages("uA", "我", 3)
            + _bulk_chat_messages("uC", "第三人", 1)
        )
        people = _chat(msgs).participants()
        self.assertEqual([p.uid for p in people], ["uB", "uA", "uC"])
        self.assertEqual([p.is_self for p in people], [False, True, False])

    def test_empty_sender_uid_is_excluded(self):
        """没有 sender_uid 的消息不归属任何成员（统计层另有"未知"桶）"""
        msgs = _bulk_chat_messages("uA", "我", 2) + _bulk_chat_messages("", "", 3)
        people = _chat(msgs).participants()
        self.assertEqual([p.uid for p in people], ["uA"])

    def test_duplicate_names_all_get_suffix(self):
        """同名成员必须都被加上后缀：只改一个会让读者分不清谁是谁"""
        msgs = _bulk_chat_messages("uA", "小明", 2) + _bulk_chat_messages("uB", "小明", 1)
        people = _chat(msgs, self_name="小明").participants()
        names = sorted(p.name for p in people)
        self.assertEqual(names, ["小明#uA", "小明#uB"])
        self.assertTrue(all(p.raw_name == "小明" for p in people))

    def test_unique_names_keep_original_when_no_clash(self):
        msgs = _bulk_chat_messages("uA", "小明", 1) + _bulk_chat_messages("uB", "小红", 1)
        self.assertEqual(sorted(p.name for p in _chat(msgs).participants()), ["小明", "小红"])

    def test_empty_name_falls_back_to_uid(self):
        msgs = _bulk_chat_messages("uA", "", 2)
        people = _chat(msgs).participants()
        self.assertEqual(people[0].name, "uA")

    def test_most_frequent_name_wins(self):
        """同一人改过昵称：显示名取出现最多的那个，结果确定"""
        msgs = (
            _bulk_chat_messages("uB", "旧昵称", 1)
            + _bulk_chat_messages("uB", "新昵称", 3)
            + _bulk_chat_messages("uB", "旧昵称", 1)
        )
        self.assertEqual(_chat(msgs).participants()[0].name, "新昵称")

    def test_result_is_cached_on_the_instance(self):
        chat = _chat(_bulk_chat_messages("uA", "我", 1))
        first = chat.participants()
        self.assertIs(first, chat.participants(), "第二次调用必须复用同一列表（同 months() 的约定）")

    def test_system_and_recalled_messages_do_not_create_members(self):
        msgs = _bulk_chat_messages("uA", "我", 2) + [
            _msg("uZ", "系统消息", 99, system=True),
            _msg("uY", "撤回的人", 100, recalled=True),
        ]
        self.assertEqual([p.uid for p in _chat(msgs).participants()], ["uA"])


def _bulk_chat_messages(uid: str, name: str, count: int) -> list:
    """直接造 Message 列表（与 _bulk 的 JSON 版本区分开，避免混淆两种夹具）

    时间戳按 UID 的字符和错开且**不依赖 hash()**：hash 随机化会让每次跑测试的
    消息顺序都不同，一旦将来有用例依赖顺序就会变成"偶发失败"。
    """
    base = sum(ord(c) for c in (uid or "empty")) % 500
    return [_msg(uid, name, ts_offset=base + i, text=str(i)) for i in range(count)]


class TestChatDataCompatibility(unittest.TestCase):
    """数据层只加不改：构造签名、相等性、repr 全部保持既有语义"""

    def test_construct_with_legacy_kwargs_only(self):
        chat = _chat([_msg("uA", "我", 0)])
        self.assertFalse(chat.is_group_chat)
        self.assertEqual(chat.mode, "private")
        self.assertIsNone(chat._participants_cache)

    def test_new_fields_do_not_affect_equality_or_repr(self):
        a = _chat([_msg("uA", "我", 0)])
        b = _chat([_msg("uA", "我", 0)])
        a.participants()  # 触发派生缓存
        self.assertEqual(a, b, "派生缓存不得参与相等比较")
        self.assertNotIn("_participants_cache", repr(a))

    def test_participants_is_a_declared_field(self):
        """同 _stats_cache：声明成字段而不是 setattr 动态挂载，读代码时看得见"""
        from dataclasses import fields as dc_fields

        self.assertIn("_participants_cache", {f.name for f in dc_fields(ChatData)})

    def test_statistical_and_months_unchanged(self):
        msgs = [_msg("uA", "我", 0), _msg("uB", "对方", 1), _msg("uC", "系统", 2, system=True)]
        chat = _chat(msgs)
        self.assertEqual(len(chat.statistical()), 2)
        self.assertEqual(list(chat.months()), ["2025-03"])


#: 私聊提示词指纹的**绝对值**（默认配置下实测值）。
#:
#: 为什么要钉死一个字面量：这个值同时进维度缓存文件名与月份缓存键，它一变，
#: 所有既有用户的私聊分析缓存（含月份级增量缓存）就全部不再命中——用户下次分析
#: 要**重新为同样的对话付费**。所以"指纹变了"必须是代码评审里**看得见**的一件事，
#: 而不是 CI 静默放过。
#:
#: 改这个值的前提：① 确实改了提示词/对话格式/进哈希的常量或预算；
#: ② CHANGELOG 里写明"会让既有缓存在宽限期后被回收，用户需重新分析"。
#: 只更新数字而不写 CHANGELOG，等于把一笔用户成本藏进测试改动里。
#: 历史：M0（2026-09-12）实测为 b6c5074dc226；此后提示词与输出预算改过，现值如右。
PINNED_PRIVATE_FINGERPRINT = "26bf952fe772"

#: 会让上面那个绝对值必然对不上的环境变量（它们都通过常量进哈希）。
#: 本机配了其中任何一个，说明这位开发者正在用非默认预算/视觉参数跑测试——
#: 那时跳过而不是误报。CI 是干净环境，那里永远严格执行。
_FINGERPRINT_ENV_KEYS = (
    "PROMPT_CACHE_SALT",
    "LLM_MAX_DIALOG_CHARS",
    "LLM_VISION_DETAIL",
    "LLM_VISION_MAX_PER_MONTH",
    "LLM_VISION_MIN_SIDE",
)


class TestPrivateFingerprintIsolation(unittest.TestCase):
    """私聊提示词指纹：取值不变，且不受新增 SYSTEM_PROMPT_* 常量影响"""

    def test_private_fingerprint_absolute_value_is_pinned(self):
        """绝对值断言：指纹一变就红，逼着改动者面对"用户要为缓存重新付费"这件事"""
        from analyzer import deepseek_client as dc

        overrides = sorted(
            k for k in os.environ if k in _FINGERPRINT_ENV_KEYS or k.startswith("LLM_MAX_TOKENS_")
        )
        if overrides:
            self.skipTest(
                "本机设置了影响指纹的环境变量 %s：绝对值必然与默认配置不同，"
                "跳过以免误报（CI 无这些变量，会严格执行）" % overrides
            )
        self.assertEqual(
            dc.PROMPT_FINGERPRINT,
            PINNED_PRIVATE_FINGERPRINT,
            "私聊提示词指纹变了。它进维度缓存文件名与月份缓存键，一变就等于让所有既有用户"
            "在下次分析时重新付费（月份缓存会在宽限期后被孤儿回收）。若确属有意改动，"
            "请同步更新本用例的 PINNED_PRIVATE_FINGERPRINT，并在 CHANGELOG 写明这一点。",
        )

    def test_every_listed_env_input_really_changes_the_fingerprint(self):
        """名单的**下界**要有依据：里面每个变量都必须真的参与哈希。

        名单一旦多出一个不相干的变量，绝对值用例就会为它白白放宽适用面（开发者只是
        设了个无关参数，指纹断言却静默跳过）；少一个则会让那位开发者拿到一条假红。
        这里逐个把它们改掉，验证指纹确实跟着变。
        """
        import analyzer.vision as vision
        from analyzer import deepseek_client as dc

        base = dc._prompt_fingerprint()
        cases = [
            ("LLM_MAX_DIALOG_CHARS", mock.patch.object(dc, "MAX_DIALOG_CHARS", dc.MAX_DIALOG_CHARS + 1)),
            ("LLM_VISION_DETAIL", mock.patch.object(vision, "VISION_DETAIL", "low")),
            ("LLM_VISION_MAX_PER_MONTH", mock.patch.object(vision, "VISION_MAX_PER_MONTH", 7)),
            ("LLM_VISION_MIN_SIDE", mock.patch.object(vision, "VISION_MIN_SIDE", 123)),
            ("PROMPT_CACHE_SALT", mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "probe"})),
        ]
        for name, patcher in cases:
            with patcher:
                self.assertNotEqual(dc._prompt_fingerprint(), base, "%s 应当参与提示词指纹" % name)
        with mock.patch.dict(dc.MAX_TOKENS_BY_DIM, {"profile": dc.MAX_TOKENS_BY_DIM["profile"] + 1}):
            self.assertNotEqual(dc._prompt_fingerprint(), base, "LLM_MAX_TOKENS_PROFILE 应当参与提示词指纹")
        with mock.patch.object(vision, "VISION_SYSTEM", vision.VISION_SYSTEM + "（探测）"):
            self.assertNotEqual(dc._prompt_fingerprint(), base, "视觉 system prompt 应当参与提示词指纹")

    def test_source_moves_do_not_change_the_fingerprint(self):
        """搬迁不改指纹：进哈希的是**函数自身源码**，不是它在哪个文件里。

        这条是重构的安全绳。它同时也是警告：改名、改注释、改 docstring、被
        ruff format 重排——这些都在 getsource 的返回范围内，会让指纹变化。
        """
        import inspect

        from analyzer import deepseek_client as dc

        funcs = (dc._build_dialog, dc._message_line, dc._fit_lines, dc._conversation_stats, dc._short_time)
        for f in funcs:
            src = inspect.getsource(f)
            module_path = inspect.getsourcefile(f)
            self.assertNotIn(
                module_path,
                src,
                "%s 的源码里不含模块路径，因此把它搬到别的文件不会换键" % f.__name__,
            )
            self.assertTrue(
                src.lstrip().startswith("def "), "%s 的源码以 def 行起始（含名字与 docstring）" % f.__name__
            )

    def test_explicit_list_matches_legacy_dir_order(self):
        """显式名单必须与旧实现 sorted(dir(prompts)) 的项与顺序逐一致

        这是"零值变更"的证明：指纹函数其余部分未动，输入清单又完全相同，
        所以新增群聊提示词不会让私聊指纹发生变化（当前绝对值见
        test_private_fingerprint_absolute_value_is_pinned，那里是唯一记录它的地方），
        既有私聊维度缓存与月份缓存全部继续命中。
        """
        import analyzer.prompts as prompts
        from analyzer import deepseek_client as dc

        legacy = [n for n in sorted(dir(prompts)) if n.startswith("SYSTEM_PROMPT_")]
        self.assertEqual(legacy, list(dc._PRIVATE_PROMPT_NAMES))

    def test_group_prompt_constant_does_not_change_fingerprint(self):
        """往 analyzer/prompts.py 里加群聊提示词不得改变私聊指纹（否则缓存全废）"""
        import analyzer.prompts as prompts
        from analyzer import deepseek_client as dc

        before = dc._prompt_fingerprint()
        prompts.SYSTEM_PROMPT_GROUP_DYNAMICS = "群聊提示词占位"
        try:
            self.assertEqual(dc._prompt_fingerprint(), before)
        finally:
            del prompts.SYSTEM_PROMPT_GROUP_DYNAMICS

    def test_private_prompt_change_does_change_fingerprint(self):
        """反面证明：真改了私聊提示词，指纹必须变（否则新风格会顶着旧缓存返回）"""
        import analyzer.prompts as prompts
        from analyzer import deepseek_client as dc

        before = dc._prompt_fingerprint()
        with mock.patch.object(prompts, "SYSTEM_PROMPT_EMOTION", "完全不同的提示词"):
            self.assertNotEqual(dc._prompt_fingerprint(), before)

    def test_salt_still_changes_fingerprint(self):
        from analyzer import deepseek_client as dc

        self.assertNotEqual(dc._prompt_fingerprint("s1"), dc._prompt_fingerprint("s2"))

    def test_month_key_default_is_unchanged_path(self):
        """月份键不给指纹参数时，与"显式传私聊指纹"必须同值（私聊键不变）"""
        from analyzer import deepseek_client as dc

        self.assertEqual(dc._month_key("SYS", "USER"), dc._month_key("SYS", "USER", dc.PROMPT_FINGERPRINT))


class TestPrivateStatsShapeFrozen(unittest.TestCase):
    """私聊统计形状冻结：M1 加的群聊字段绝不能出现在私聊结果里"""

    def test_compute_stats_keys_are_frozen(self):
        from analyzer.local_stats import calc_overview
        from webapp.store import compute_stats

        chat = _chat(_bulk_chat_messages("uA", "我", 3) + _bulk_chat_messages("uB", "对方", 2))
        stats = compute_stats(chat)
        self.assertEqual(
            set(stats),
            {
                "overview",
                "daily_counts",
                "hourly_dist",
                "weekly_dist",
                "length_stats",
                "face_stats",
                "response_time",
                "exchange_rounds",
                "weekly_activity",
                "milestones",
            },
            "私聊统计的键集合是对外契约（模板逐字段引用），只能整体新增一个群聊分支，不能改动这里",
        )
        for group_only in ("member_activity", "interaction_matrix", "roles", "member_count"):
            self.assertNotIn(group_only, stats)
            self.assertNotIn(group_only, calc_overview(chat))


class TestUploadPathUnchanged(unittest.TestCase):
    """HTTP 级护栏：默认配置下群聊导出仍被拒收、私聊 7 页仍照常渲染

    只在 load_chat 层面验证是不够的——上传路由才是用户真正碰到的那一层
    （捕获异常 → 400 文案、会话不写入、旧数据不受影响）。
    """

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.appmod = appmod
        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.token = sess["csrf_token"]
        cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": cls.token}

        # 一份正常私聊：上传应成功，7 个页面全部 200
        msgs = []
        for i in range(40):
            uid = "u_self" if i % 2 else "u_other"
            msgs.append(
                {
                    "id": str(i),
                    "timestamp": _BASE_MS + i * 3600_000,
                    "time": "2025-03-01 20:00:00",
                    "sender": {"uid": uid, "name": "我" if uid == "u_self" else "对方"},
                    "content": ["在吗", "在的", "今天好累", "早点睡", "晚安"][i % 5],
                }
            )
        payload = _wrap(msgs, {"u_self": "我", "u_other": "对方"}, self_uid="u_self")
        r = cls.client.post(
            "/upload",
            data={"file": (io.BytesIO(payload.encode("utf-8")), "chat.json")},
            headers=cls.headers,
        )
        assert r.status_code == 302, f"私聊上传失败: {r.status_code}"
        with cls.client.session_transaction() as sess:
            cls.filepath = sess.get("filepath")
            cls.chat_hash = sess.get("chat_hash")

    @classmethod
    def tearDownClass(cls):
        from webapp import store as storemod

        if cls.filepath and os.path.exists(cls.filepath):
            os.remove(cls.filepath)
        storemod._purge_chat_caches(cls.chat_hash)

    def test_private_pages_have_no_group_markers(self):
        """私聊页面不得出现任何群聊痕迹（M3 加群模板后这条会立刻报警）"""
        pages = ("/", "/dashboard", "/emotion", "/relationship", "/habits", "/topics", "/profile", "/report")
        for path in pages:
            with self.subTest(page=path):
                resp = self.client.get(path)
                body = resp.get_data(as_text=True)
                self.assertEqual(resp.status_code, 200)
                if path == "/":
                    # 上传页是唯一例外：它**刻意**在"清理会话缓存"的脚本里列出群聊维度名
                    # （只清私聊 5 维的话，换文件后会显示上一份群聊的 AI 结果）。
                    self.assertIn("group_dynamics", body)
                    continue
                for marker in ("群聊", "群成员", "interaction_matrix", "group_dashboard"):
                    self.assertNotIn(marker, body, f"{path} 出现了群聊标记 {marker}")

    def test_group_export_is_rejected_over_http_when_switched_off(self):
        """QQCHAT_GROUP_CHAT=off：多人导出在上传层被拒（400 + 群聊提示），且不写入会话

        用**独立的客户端**发这份上传：被拒的上传不能污染本类共享会话（这里也顺便验证了
        这一点——被拒之后共享会话仍指向原来那份私聊）。
        """
        import app as appmod

        msgs = _bulk("uA", "我", 20) + _bulk("uB", "对方", 20, start=20) + _bulk("uC", "第三人", 6, start=40)
        payload = _wrap(msgs, {"uA": "我", "uB": "对方", "uC": "第三人"})
        fresh = appmod.app.test_client()
        fresh.get("/")
        with fresh.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "off"}):
            r = fresh.post(
                "/upload",
                data={"file": (io.BytesIO(payload.encode("utf-8")), "group.json")},
                headers=headers,
            )
        self.assertEqual(r.status_code, 400)
        self.assertIn("群聊", r.get_data(as_text=True))
        # 共享会话不受影响（那份上传用的是独立客户端）
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("chat_hash"), self.chat_hash)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_group_export_is_accepted_by_default(self):
        """默认配置：多人导出被接受并按群聊渲染（用独立客户端，避免污染本类会话）"""
        import app as appmod
        from webapp import store as storemod

        msgs = _bulk("uA", "我", 20) + _bulk("uB", "对方", 20, start=20) + _bulk("uC", "第三人", 6, start=40)
        payload = _wrap(msgs, {"uA": "我", "uB": "对方", "uC": "第三人"})
        fresh = appmod.app.test_client()
        fresh.get("/")
        with fresh.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        r = fresh.post(
            "/upload",
            data={"file": (io.BytesIO(payload.encode("utf-8")), "group.json")},
            headers=headers,
        )
        self.assertEqual(r.status_code, 302)
        with fresh.session_transaction() as sess:
            chat_hash, mode = sess.get("chat_hash"), sess.get("chat_mode")
            filepath = sess.get("filepath")
        try:
            self.assertEqual(mode, "group")
            # 共享 fixture 的统计缓存会被别的用例清掉，裸 wait_for_stats 在没有线程在跑时
            # 会直接返回，于是 /dashboard 因为"没有统计数据"跳回首页。ensure_stats 把
            # "统计可用"变成同步保证。详见 tests/_stats.py。
            ensure_stats(filepath, chat_hash)
            body = fresh.get("/dashboard").get_data(as_text=True)
            self.assertIn("群仪表盘", body)
            self.assertIn("同时在线高峰", body)
        finally:
            storemod._purge_chat_caches(chat_hash)


class TestGroupFixture(unittest.TestCase):
    """5 人群聊 fixture 的形态自检（M1/M2 的统计与 AI 用例都建立在它之上）

    边角覆盖：同名成员、低频成员、空 sender_uid、系统占位 sender、无 system 标记的
    type_23、图片/大表情/回复/文件/转发/撤回，以及跨 3 个月（逐月分析要用）。
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "group_5p.json"

    def _load_as_group(self):
        with mock.patch.object(qq_parser, "GROUP_TRACK_READY", True):
            return load_chat(str(self.FIXTURE))

    def test_fixture_is_parsed_as_group_with_unique_member_names(self):
        chat = self._load_as_group()
        self.assertTrue(chat.is_group_chat)
        self.assertEqual(chat.mode, "group")
        self.assertEqual(chat.other_uid, "")
        self.assertEqual(chat.other_name, "摸鱼群")
        self.assertEqual(
            [p.name for p in chat.participants()],
            ["我", "小明#u_2", "小明#u_3", "阿强", "小美"],
            "同名成员必须被唯一化（#uid4），否则模型与界面会把两个人当成一个",
        )
        self.assertTrue(chat.participants()[0].is_self)
        self.assertEqual(list(chat.months()), ["2025-01", "2025-02", "2025-03"])

    def test_fixture_covers_edge_cases(self):
        chat = self._load_as_group()
        self.assertTrue(any(m.has_image for m in chat.messages), "缺图片样本")
        self.assertTrue(any(m.face_names for m in chat.messages), "缺大表情样本")
        self.assertTrue(any(m.is_reply for m in chat.messages), "缺回复样本")
        self.assertTrue(any(m.media_kind == "file" for m in chat.messages), "缺文件样本")
        self.assertTrue(any(m.media_kind == "forward" for m in chat.messages), "缺转发样本")
        self.assertTrue(any(m.recalled for m in chat.messages), "缺撤回样本")
        self.assertTrue(any(m.system for m in chat.messages), "缺系统消息样本")
        self.assertEqual(
            sum(1 for m in chat.messages if not m.sender_uid), 3, "缺空 sender_uid 样本（未知桶要用）"
        )

    def test_fixture_is_detected_as_group_by_default(self):
        """默认配置（群聊轨已就绪）：这份群聊 fixture 直接按群聊解析"""
        chat = load_chat(str(self.FIXTURE))
        self.assertTrue(chat.is_group_chat)
        self.assertEqual(chat.mode, "group")


if __name__ == "__main__":
    unittest.main(verbosity=2)
