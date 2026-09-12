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
"""群聊 AI 层测试（M2）：全部离线

用 mock 替换 _call_api，验证的是"我们发给模型什么"与"拿回结果怎么处理"，
而不是模型答得好不好（那要靠真实数据反复打磨 prompt，不是单元测试的事）。

重点四件事：
1. **成员感知抽样**：大群里每位成员都要出现在样本里（低频成员被整段丢掉会直接
   污染"潜水比例"这类结论）；
2. **成员画像必须带群上下文**：只有他自己的发言，模型分不清"捧哏王"和"话题主导者"；
3. **指纹隔离**：群聊提示词只影响群聊缓存，私聊指纹与月份键一个字都不变；
4. **额度与取消**：与私聊同一套语义（配额耗尽中止、已完成的成员结果保留、取消不写缓存）。

**mock 目标的坑（踩过一次真实调用）**：三个群级维度走 `_analyze_periods`，而它内部的
`_call_api` 取自 `analyzer.deepseek_client` 的模块全局——只 patch `group_client._call_api`
对它**无效**（只对成员画像有效，因为那是本模块的直接调用）。所以：
- 测月度维度：patch `analyzer.deepseek_client._analyze_periods` 或 `analyzer.deepseek_client._call_api`；
- 测成员画像：patch `analyzer.group_client._call_api`；
- 想更保险：同时 patch `deepseek_client._get_client` 让它抛异常，任何漏网的真实调用都会立刻炸出来。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shutil as _shutil

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402
from _stats import ensure_stats  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser.qq_parser as qp  # noqa: E402
from parser.qq_parser import ChatData, Message  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer import group_prompts as gp  # noqa: E402

CST = timezone(timedelta(hours=8))
BASE = datetime(2025, 3, 1, 20, 0, tzinfo=CST)


def _msg(uid, name, minutes, text="内容", **extra):
    ts = int((BASE + timedelta(minutes=minutes)).timestamp() * 1000)
    msg = Message(
        id=f"{uid}-{minutes}",
        timestamp=ts,
        time_str=(BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="text",
        has_image=False,
        is_reply=False,
    )
    for key, value in extra.items():
        setattr(msg, key, value)
    if getattr(msg, "reply_to_id", "") or getattr(msg, "reply_to_uid", ""):
        msg.is_reply = True  # 解析器在有 reply 元素时同样会置 True
    return msg


def _group(messages, name="摸鱼群", self_uid="uA", self_name="我"):
    return ChatData(
        chat_name=name,
        self_name=self_name,
        other_name=name,
        self_uid=self_uid,
        other_uid="",
        messages=sorted(messages, key=lambda m: m.timestamp),
        is_group_chat=True,
        mode="group",
        chat_type="group",
    )


def _small_group(per_member=5):
    msgs = []
    for i, (uid, name) in enumerate([("uA", "我"), ("uB", "小明"), ("uC", "小红")]):
        for k in range(per_member):
            msgs.append(_msg(uid, name, i * 10 + k, f"{name}的第{k}句"))
    return msgs


class TestMemberAwareSampling(unittest.TestCase):
    """成员感知抽样：低频成员不能被整段丢掉"""

    def test_low_frequency_member_survives_sampling(self):
        msgs = []
        for i in range(200):  # 话痨
            msgs.append(_msg("uA", "我", i, f"话痨第{i}句，稍微长一点的正文内容"))
        for i in range(3):  # 只说了三句的人
            msgs.append(_msg("uZ", "潜水员", 300 + i, f"潜水员第{i}句"))
        chat = _group(msgs)
        with mock.patch.object(gc, "GROUP_MAX_DIALOG_CHARS", 1200):  # 强制触发抽样
            dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertIn("潜水员第0句", dialog, "低频成员被整段丢掉了——这会让模型把他读成缺席")
        self.assertIn("话痨第0句", dialog)
        self.assertIn("因篇幅限制展示其中", dialog)

    def test_no_sampling_when_within_budget(self):
        chat = _group(_small_group())
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertNotIn("因篇幅限制展示", dialog)
        for name in ("我", "小明", "小红"):
            self.assertIn(name, dialog)

    def test_dialog_header_carries_local_facts(self):
        chat = _group(_small_group())
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertIn("统计：", dialog)
        self.assertIn("群成员 3 位", dialog)
        self.assertIn("同时在聊高峰", dialog)
        self.assertIn("本月发言最多", dialog)
        self.assertIn("本月互动摘要", dialog)

    def test_interaction_digest_separates_fact_from_inference(self):
        msgs = [
            _msg("uA", "我", 0, "在吗"),
            _msg("uB", "小明", 1, "在", reply_to_id="uA-0", reply_to_uid="uA"),
            _msg("uC", "小红", 2, "@小明 看这个", mentions=["uB"]),
            _msg("uC", "小红", 3, "@全体成员 开会", mentions_all=True),
        ]
        chat = _group(msgs)
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertIn("精确回复（事实）", dialog)
        self.assertIn("@点名（事实）", dialog)
        self.assertIn("接话（推断", dialog)
        self.assertIn("含 @全体 1 条", dialog)


class TestMemberProfiles(unittest.TestCase):
    """成员画像：Top-K、自己必入选、每人带群上下文"""

    def test_select_ai_members_keeps_self_even_when_quiet(self):
        msgs = []
        for i in range(30):
            msgs.append(_msg("uB", "小明", i))
        msgs.append(_msg("uA", "我", 100, "我只说一句"))
        for i in range(5):
            msgs.append(_msg(f"u{i}", f"路人{i}", 200 + i))
        chat = _group(msgs)
        chosen = gc.select_ai_members(chat, limit=2)
        self.assertEqual(len(chosen), 2)
        self.assertTrue(any(p.is_self for p in chosen), "自己发言再少也必须入选（用户最关心自己）")

    def test_member_profiles_calls_once_per_member_with_context(self):
        chat = _group(_small_group(per_member=6))
        calls = []

        def fake_call(system_prompt, user_content, **kwargs):
            calls.append((kwargs.get("tag"), user_content))
            return {"name": "x", "verdict": "锐评"}

        with (
            mock.patch.object(gc, "_call_api", side_effect=fake_call),
            mock.patch.object(gc, "GROUP_AI_MAX_MEMBERS", 3),
        ):
            result = gc.analyze_member_profiles(chat)
        self.assertEqual(len(calls), 3, "每位入选成员一次调用")
        self.assertEqual(set(result), {"uA", "uB", "uC"})
        for tag, content in calls:
            self.assertEqual(tag, "member_profiles")
            self.assertIn("成员在群里的互动数字", content)
            self.assertIn("被精确回复", content)
            self.assertIn("主要互动对象", content)
        self.assertTrue(result["uA"]["is_self"], "自己那条结果要带 is_self，界面据此标注")

    def test_progress_and_cancel(self):
        chat = _group(_small_group(per_member=6))
        seen = []

        with (
            mock.patch.object(gc, "_call_api", return_value={"name": "x"}),
            mock.patch.object(gc, "GROUP_AI_MAX_MEMBERS", 3),
        ):
            gc.analyze_member_profiles(chat, on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])

        with (
            mock.patch.object(gc, "_call_api", return_value={"name": "x"}),
            mock.patch.object(gc, "GROUP_AI_MAX_MEMBERS", 3),
        ):
            result = gc.analyze_member_profiles(chat, should_cancel=lambda: True)
        self.assertEqual(result, {}, "取消后不应继续调用 API")

    def test_quota_exhausted_keeps_partial_results(self):
        chat = _group(_small_group(per_member=6))
        calls = {"n": 0}

        def fake_call(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"name": "first"}
            raise dc.QuotaExhaustedError("额度耗尽")

        with (
            mock.patch.object(gc, "_call_api", side_effect=fake_call),
            mock.patch.object(gc, "GROUP_AI_MAX_MEMBERS", 3),
        ):
            result = gc.analyze_member_profiles(chat)
        self.assertEqual(len(result), 1, "配额耗尽时要保留已完成的成员结果")

    def test_member_without_messages_is_skipped(self):
        chat = _group(_small_group(per_member=3))
        with mock.patch.object(gc, "_call_api", return_value={"name": "x"}):
            result = gc._analyze_member(
                chat,
                type("P", (), {"uid": "u_none", "name": "不存在", "is_self": False})(),
                gp.GROUP_SYSTEM_PROMPT_MEMBER_PROFILE,
                "{display_name}/{group_name}/{context}/{dialog}",
                max_tokens=1024,
                tag="member_profiles",
            )
        self.assertIsNone(result)


class TestMonthlyGroupDimensions(unittest.TestCase):
    """三个群级维度：逐月、写月份缓存、权重归一化、强度夹紧"""

    def test_dynamics_runs_per_month_and_fills_period(self):
        chat = _group(_small_group(per_member=3))
        prompts = []

        def fake_periods(months, system_prompt, make_prompt, **kwargs):
            for period, msgs in months.items():
                prompts.append((period, make_prompt(period, msgs)))
            return {p: {"period": p} for p in months}

        with mock.patch.object(gc, "_analyze_periods", side_effect=fake_periods):
            result = gc.analyze_group_dynamics(chat)
        self.assertEqual(list(result), ["2025-03"])
        self.assertIn("群聊数据", prompts[0][1])
        self.assertIn("摸鱼群", prompts[0][1])

    def test_group_dimensions_pass_group_fingerprint_to_month_cache(self):
        chat = _group(_small_group(per_member=3))
        seen = {}

        def fake_periods(months, system_prompt, make_prompt, **kwargs):
            seen["fingerprint"] = kwargs.get("fingerprint")
            return {}

        with mock.patch.object(gc, "_analyze_periods", side_effect=fake_periods):
            gc.analyze_group_topics(chat)
        self.assertEqual(seen["fingerprint"], gc.GROUP_PROMPT_FINGERPRINT)
        self.assertNotEqual(seen["fingerprint"], dc.PROMPT_FINGERPRINT)

    def test_topic_weights_are_normalized(self):
        chat = _group(_small_group(per_member=3))
        payload = {
            "2025-03": {
                "topics": [{"weight": 0.333}, {"weight": 0.333}, {"weight": 0.334}],
            }
        }
        with mock.patch.object(gc, "_analyze_periods", return_value=payload):
            result = gc.analyze_group_topics(chat)
        weights = [t["weight"] for t in result["2025-03"]["topics"]]
        self.assertAlmostEqual(sum(weights), 1.0, places=6)

    def test_emotion_clamps_intensity(self):
        chat = _group(_small_group(per_member=3))
        payload = {
            "2025-03": {
                "group_intensity": 99,
                "member_emotions": [{"name": "小明", "intensity": -5}],
            }
        }
        with mock.patch.object(gc, "_analyze_periods", return_value=payload):
            result = gc.analyze_group_emotion(chat)
        self.assertEqual(result["2025-03"]["group_intensity"], 10)
        self.assertEqual(result["2025-03"]["member_emotions"][0]["intensity"], 0)

    def test_registry_shape(self):
        self.assertEqual(
            list(gc.GROUP_DIMENSIONS),
            ["group_dynamics", "group_topics", "group_emotion", "member_profiles"],
            "顺序即一键全量的执行顺序：最贵的成员画像必须在最后",
        )
        for _dim, (label, func, unit) in gc.GROUP_DIMENSIONS.items():
            self.assertTrue(label and callable(func) and unit in ("月", "人"))


class TestFingerprintIsolation(unittest.TestCase):
    """群聊提示词只影响群聊缓存"""

    def test_group_fingerprint_changes_with_group_prompts(self):
        before = gc.group_prompt_fingerprint()
        with mock.patch.object(gp, "GROUP_SYSTEM_PROMPT_DYNAMICS", "完全不同的群聊提示词"):
            self.assertNotEqual(gc.group_prompt_fingerprint(), before)

    def test_private_fingerprint_ignores_group_module(self):
        """群聊提示词放独立模块，因此私聊指纹与月份键一个字都不变"""
        before = dc._prompt_fingerprint()
        gc.group_prompt_fingerprint()
        self.assertEqual(dc._prompt_fingerprint(), before)

    def test_fingerprint_for_dimension_routes_by_dim(self):
        self.assertEqual(dc.fingerprint_for_dimension("emotion"), dc.PROMPT_FINGERPRINT)
        self.assertEqual(dc.fingerprint_for_dimension("member_profiles"), gc.GROUP_PROMPT_FINGERPRINT)
        self.assertEqual(dc.fingerprint_for_dimension("nonexistent"), dc.PROMPT_FINGERPRINT)

    def test_group_fingerprint_tracks_scale_knobs(self):
        """GROUP_AI_MAX_MEMBERS 等规模开关参与群聊指纹：改了它们，群聊缓存换键重算

        这是有意的耦合——规模上限决定"分析哪些成员/矩阵保留多少人"，
        换了口径还沿用旧结果会让界面与结论对不上。代价只是重跑（私聊缓存不受影响）。
        """
        before = gc.group_prompt_fingerprint()
        with mock.patch.object(gc, "GROUP_AI_MAX_MEMBERS", 3):
            self.assertNotEqual(gc.group_prompt_fingerprint(), before)
        self.assertEqual(dc._prompt_fingerprint(), dc.PROMPT_FINGERPRINT, "私聊指纹不受影响")

    def test_group_max_tokens_configured(self):
        for dim in gc.GROUP_DIMENSIONS:
            self.assertIn(dim, dc.MAX_TOKENS_BY_DIM)
            self.assertGreaterEqual(
                dc.MAX_TOKENS_BY_DIM[dim], dc.THINKING_MIN_TOKENS, "开思考模式时预算不能低于思维链下限"
            )


class TestJobWiring(unittest.TestCase):
    """任务系统按模式取维度集（私聊那一侧不变）"""

    def test_dimensions_by_mode(self):
        from webapp import jobs

        self.assertEqual(
            jobs.dimensions_for_mode(False), ["emotion", "topics", "relationship", "habits", "profile"]
        )
        self.assertEqual(
            jobs.dimensions_for_mode(True),
            ["group_dynamics", "group_topics", "group_emotion", "member_profiles"],
        )
        self.assertFalse(jobs.is_group_dimension("emotion"))
        self.assertTrue(jobs.is_group_dimension("group_topics"))
        self.assertEqual(jobs.dimension_unit("member_profiles"), "人")
        self.assertEqual(jobs.dimension_unit("emotion"), "月")

    def test_analyze_func_lookup_covers_both_tracks(self):
        from webapp import jobs

        for dim in jobs.dimensions_for_mode(False) + jobs.dimensions_for_mode(True):
            self.assertIsNotNone(jobs.analyze_func_for(dim), dim)
        self.assertIsNone(jobs.analyze_func_for("not_a_dim"))

    def test_all_dimension_names_have_labels(self):
        from webapp import jobs

        for dim in jobs.dimensions_for_mode(True):
            self.assertTrue(jobs.ALL_DIMENSION_NAMES.get(dim), dim)


class TestGroupUploadRendersWithoutCrash(unittest.TestCase):
    """群聊文件走完整 HTTP 流程时，七个页面都不得 500（M3 之前它们仍用私聊模板）

    实测踩过：relationship.html / report.html 直接取 `response_time.self.p50`，而群聊统计
    刻意**不提供** response_time（"对方→我"的口径在群里不成立）→ 两个页面 500。
    模板已改为对缺失值显示占位符；这条用例从 HTTP 层把"不许再炸"钉住，同时也证明了
    "上传→判定→群聊统计→页面"这条链路是通的（M3 只要换模板即可）。
    """

    def test_seven_pages_render_for_group_chat(self):
        import io
        import json as _json

        import app as appmod
        from webapp import store

        fixture = Path(__file__).resolve().parent / "fixtures" / "group_5p.json"
        payload = _json.loads(fixture.read_text(encoding="utf-8"))
        blob = _json.dumps(payload, ensure_ascii=False).encode("utf-8")

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        with mock.patch.object(qp, "GROUP_TRACK_READY", True):
            resp = client.post("/upload", data={"file": (io.BytesIO(blob), "group.json")}, headers=headers)
        self.assertEqual(resp.status_code, 302)
        with client.session_transaction() as sess:
            chat_hash, mode = sess.get("chat_hash"), sess.get("chat_mode")
            filepath = sess.get("filepath")
        self.assertEqual(mode, "group")
        # 用 ensure_stats 而不是裸 wait_for_stats：共享 fixture 的缓存会被别的用例清掉，
        # 后台线程的结果可能因此被丢弃（生产上是刻意的 fail-safe），页面就会因为"没有统计
        # 数据"而 302。详见 tests/_stats.py。
        ensure_stats(filepath, chat_hash)
        self.assertEqual(store.stats_error(chat_hash), "", "群聊统计不应失败")
        self.assertIsNotNone(store._load_stats(chat_hash, expect_mode=store.STATS_MODE_GROUP))
        pages = ("/", "/dashboard", "/emotion", "/relationship", "/habits", "/topics", "/profile", "/report")
        for path in pages:
            with self.subTest(page=path):
                page = client.get(path)
                self.assertLess(page.status_code, 500, f"{path} 对群聊记录报错了")
        # 收尾：删掉这份缓存，避免影响其它用例
        store._purge_chat_caches(chat_hash)

    def test_private_dimension_rejected_for_group_session(self):
        """HTTP 层：群聊会话请求私聊维度 → 400 + 明确文案"""
        import io
        import json as _json

        import app as appmod
        from webapp import store

        fixture = Path(__file__).resolve().parent / "fixtures" / "group_5p.json"
        payload = _json.loads(fixture.read_text(encoding="utf-8"))
        blob = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        with mock.patch.object(qp, "GROUP_TRACK_READY", True):
            client.post("/upload", data={"file": (io.BytesIO(blob), "group.json")}, headers=headers)
        with client.session_transaction() as sess:
            chat_hash = sess.get("chat_hash")
        resp = client.post("/api/analyze/emotion", headers=headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不适用于当前记录", resp.get_json()["error"])
        store._purge_chat_caches(chat_hash)


class TestApiModeGuard(unittest.TestCase):
    """HTTP 层护栏：私聊会话不能跑群聊维度，反之亦然

    比"没意义"更严重的是：群聊维度会把"我 vs 对方"的数字当成群的数字讲给模型听，
    产出看着像结论、其实口径错位的东西，所以必须直接拒绝。
    """

    def _guard(self, dim, chat_mode):
        import app as appmod
        from webapp import api

        with appmod.app.test_request_context("/api/analyze/" + dim):
            from flask import session

            session["chat_mode"] = chat_mode
            resp = api._dimension_guard(dim)
            if resp is None:
                return None
            return resp[1]

    def test_private_session_rejects_group_dimension(self):
        self.assertEqual(self._guard("group_dynamics", "private"), 400)
        self.assertEqual(self._guard("member_profiles", "two_party"), 400)

    def test_group_session_rejects_private_dimension(self):
        self.assertEqual(self._guard("emotion", "group"), 400)
        self.assertEqual(self._guard("profile", "group"), 400)

    def test_matching_mode_passes(self):
        self.assertIsNone(self._guard("emotion", "private"))
        self.assertIsNone(self._guard("group_topics", "group"))

    def test_unknown_dimension_is_rejected(self):
        self.assertEqual(self._guard("not_a_dim", "group"), 400)


class TestRealFileShapedGroup(unittest.TestCase):
    """按真实导出的形状跑一遍（数十人、回复/@ 混合），确认不会因规模而失真"""

    def _big_group(self, with_self=True):
        msgs = []
        for i in range(40):  # 40 位成员，条数递减
            uid = "uA" if (i == 0 and with_self) else f"u{i:02d}"
            name = "我" if uid == "uA" else f"成员{i}"
            for k in range(40 - i):
                msgs.append(_msg(uid, name, i * 3 + k, f"{uid} 的第 {k} 句发言内容"))
        return _group(msgs)

    def test_dialog_and_selection_scale(self):
        chat = self._big_group()
        with mock.patch.object(gc, "GROUP_MAX_DIALOG_CHARS", 6000):
            dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertLessEqual(len(dialog), 6000 + 2000, "统计头之外的对话部分必须受预算约束")
        members = gc.select_ai_members(chat, limit=10)
        self.assertEqual(len(members), 10)
        self.assertTrue(any(p.is_self for p in members))

    def test_self_never_spoke_is_handled(self):
        """我全程没在群里说过话：不崩、照样给前 10 位成员（只是名单里没有我）"""
        chat = self._big_group(with_self=False)
        members = gc.select_ai_members(chat, limit=10)
        self.assertEqual(len(members), 10)
        self.assertFalse(any(p.is_self for p in members))
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertIn("统计：", dialog)

    def test_member_context_uses_exact_signals(self):
        msgs = [
            _msg("uA", "我", 0),
            _msg("uB", "小明", 1, "回复", reply_to_id="uA-0", reply_to_uid="uA"),
            _msg("uA", "我", 2, "@小明 在吗", mentions=["uB"]),
        ]
        chat = _group(msgs)
        me = next(p for p in chat.participants() if p.is_self)
        context = gc._member_context(chat, me)
        self.assertIn("被精确回复 1 次", context)
        self.assertIn("主动回复别人 0 次", context)
        self.assertIn("主动 @ 别人 1 次", context)
        self.assertIn("导出者本人", context)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLenientJsonParsing(unittest.TestCase):
    """模型输出里的裸控制字符不该让整月/整位成员的结果作废

    真实数据实测：一次完整群聊分析里 3 次失败，其中 2 次是"模型把多行文本写成裸换行"
    （`Invalid control character at line N`）。严格模式直接判非法 JSON，于是那个月/那位成员
    的结果就没了——用户看到的是"4 个月里只有 3 个月有结果"，却不知道为什么。
    strict=False 只放宽解析容忍度，不影响合法 JSON 的解析结果。
    """

    def _call_with_content(self, content: str):
        """把假响应的 message.content 塞进 _call_api，验证解析结果"""
        import types

        from analyzer import deepseek_client as dc

        fake_client = object()
        resp = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(content=content))
            ]
        )
        with (
            mock.patch.object(dc, "_get_client", return_value=fake_client),
            mock.patch.object(dc, "_request_with_retry", return_value=(resp, "stop")),
        ):
            return dc._call_api("sys", "user", max_tokens=1024, tag="group_dynamics", dim="group_dynamics")

    def test_raw_newline_inside_string_is_tolerated(self):
        payload = '{"month_title": "《测试》", "summary": "第一行\n第二行", "confidence": "high"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(payload)  # 严格模式：非法（这正是真实踩到的那类）
        result = self._call_with_content(payload)
        self.assertIsNotNone(result, "裸换行的 JSON 应该被容忍")
        self.assertIn("第一行", result["summary"])

    def test_valid_json_still_parses(self):
        payload = '{"group_vibe": "热闹", "confidence": "high"}'
        result = self._call_with_content(payload)
        self.assertEqual(result["group_vibe"], "热闹")

    def test_structurally_broken_json_still_fails(self):
        """结构性错误（少逗号/截断）不该被"宽容"掩盖——那种情况必须走失败路径"""
        result = self._call_with_content('{"group_vibe": "热闹" "confidence": "high"}')
        self.assertIsNone(result)


class TestMemberCache(unittest.TestCase):
    """成员画像按成员内容寻址缓存：补一位失败成员不该重付全部的钱

    真实数据实测：一次 10 人分析里有 2 位因模型输出非法 JSON 失败。没有按人缓存时，
    想补这 2 位只能 refresh 重跑 10 次；有了它，重跑时命中 8 次、只为缺失的 2 位付费。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qqchatlog-membercache-")
        dc.configure_month_cache(self._tmp)
        self.addCleanup(dc.configure_month_cache, "")
        self.addCleanup(_shutil.rmtree, self._tmp, True)

    def _run(self, chat, calls):
        def fake(*args, **kwargs):
            calls.append(1)
            return {"name": "x"}

        with mock.patch.object(gc, "_call_api", side_effect=fake):
            return gc.analyze_member_profiles(chat, chat_hash="hashM")

    def test_second_run_hits_cache_for_every_member(self):
        # 刻意**不** patch GROUP_AI_MAX_MEMBERS：它在群聊指纹里，改了会让缓存换键
        # （fixture 本来就只有 3 位成员，不需要限制人数）
        chat = _group(_small_group(per_member=6))
        calls: list = []
        first = self._run(chat, calls)
        self.assertEqual(len(calls), 3, "第一次应为每位成员各调用一次")
        second = self._run(chat, calls)
        self.assertEqual(len(calls), 3, "第二次应全部命中成员缓存，不再调用 API")
        self.assertEqual(set(first), set(second))

    def test_only_the_missing_member_is_repaid(self):
        """模拟"某位成员上次失败"：把缓存文件删掉一个，重跑只补他一个"""
        chat = _group(_small_group(per_member=6))
        calls: list = []
        self._run(chat, calls)
        files = [os.path.join(self._tmp, n) for n in os.listdir(self._tmp) if n.startswith("month_")]
        self.assertEqual(len(files), 3, "三位成员应各有一份缓存")
        os.remove(files[0])
        self._run(chat, calls)
        self.assertEqual(len(calls), 4, "只应为被删掉的那一位重新调用")


class TestSelfIsLabelledInGroupDialog(unittest.TestCase):
    """群聊对话头必须写明「我」是哪个昵称

    真实数据实测：不写这一行时，模型在 group_dynamics 里给出
    "样本未标注 self 发言，无法定位导出者本人的角色，数据不足"——一份本可避免的"数据不足"。
    群聊没有"对方"这种位置线索，模型只能靠这一行把导出者从几十个昵称里认出来。
    """

    def test_dialog_header_names_the_self_member(self):
        chat = _group(_small_group(per_member=4))
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertIn("「我」= 导出者本人", dialog)
        me = next(p for p in chat.participants() if p.is_self)
        self.assertIn(me.name, dialog.split("本月互动摘要")[0], "统计头里要出现我的显示名")

    def test_missing_self_member_does_not_break(self):
        """我全程没发言时没有 self 成员：不写这一行，也不能崩"""
        msgs = [_msg("uB", "小明", i) for i in range(5)]
        chat = _group(msgs)  # self_uid=uA 不在消息里
        dialog = gc.build_group_dialog(chat, chat.messages)
        self.assertNotIn("「我」= 导出者本人", dialog)
        self.assertIn("统计：", dialog)
