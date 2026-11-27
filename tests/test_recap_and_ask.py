# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Regression tests for recap dimension, ask Q&A, trends, and job history."""

import io
import json
import logging
import unittest
from datetime import datetime
from unittest import mock

import os
import tempfile
import time
from _bootstrap import api_configured_patcher, bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from parser.qq_parser import CST  # noqa: E402
from webapp import store  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
from parser.qq_parser import load_chat  # noqa: E402
from result_fixtures import ask as valid_ask  # noqa: E402


_R7_BASE = 1704067200.0


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
            _r7_msg(1, "uA", "我", _R7_BASE, "text", text="我们什么时候去看海"),
            _r7_msg(2, "uB", "对方", _R7_BASE + 60, "text", text="下个月吧"),
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


class TestTrends(unittest.TestCase):
    """calc_trends：逐月互发/句长、≥3 天冷场的重启与重启方、称呼变迁、首条消息。"""

    def _two_party_msgs(self):
        day = 86400
        return [
            _f7_text(1, "uA", "我", _F7_BASE, "在吗"),
            _f7_text(2, "uB", "对方", _F7_BASE + 60, "在的呀宝贝"),
            # 冷场 5 天后"我"先开口 → 一次重启（重启方=我）
            _f7_text(3, "uA", "我", _F7_BASE + (5 * day), "最近好吗"),
            _f7_text(4, "uB", "新备注", _F7_BASE + (5 * day) + 60, "挺好的"),
        ]

    def test_months_and_avg_len(self):
        chat = _f7_load(self._two_party_msgs())
        months = ls.calc_trends(chat)["months"]
        self.assertEqual(len(months), 1, "都在同一个月")
        m0 = months[0]
        self.assertEqual((m0["self"], m0["other"]), (2, 2))
        # 句长只数非空正文：对方 = (5+3)/2，我 = (2+4)/2
        self.assertEqual(m0["avg_len_other"], 4.0)
        self.assertEqual(m0["avg_len_self"], 3.0)

    def test_restart_detection_and_who(self):
        chat = _f7_load(self._two_party_msgs())
        r = ls.calc_trends(chat)["restarts"]
        self.assertEqual(r["threshold_days"], 3)
        self.assertEqual(r["count"], 1)
        self.assertEqual((r["self"], r["other"]), (1, 0), "冷场后是我先开口")
        # 5 天冷场但第二条消息晚 60 秒，间隔向下取整为 4 天（口径：完整天数才算）
        self.assertEqual(r["events"][0]["gap_days"], 4)

    def test_no_restart_below_threshold(self):
        day = 86400
        chat = _f7_load(
            [_f7_text(1, "uA", "我", _F7_BASE, "早"), _f7_text(2, "uB", "对方", _F7_BASE + 2 * day, "早")]
        )
        self.assertEqual(ls.calc_trends(chat)["restarts"]["count"], 0, "隔 2 天不算冷场重启")

    def test_name_history_tracks_variants(self):
        chat = _f7_load(self._two_party_msgs())
        nh = ls.calc_trends(chat)["name_history"]
        other_names = [n["name"] for n in nh["other"]]
        self.assertIn("对方", other_names)
        self.assertIn("新备注", other_names, "显示名变化必须留下轨迹（称呼变迁是关系信号）")
        changed = next(n for n in nh["other"] if n["name"] == "新备注")
        self.assertEqual(changed["count"], 1)

    def test_first_message_has_content(self):
        chat = _f7_load(self._two_party_msgs())
        fm = ls.calc_trends(chat)["first_message"]
        self.assertEqual(fm["text"], "在吗")
        self.assertEqual(fm["who"], "self")

    def test_empty_chat_degrades(self):
        chat = _f7_load([_f7_text(1, "uA", "我", _F7_BASE, "x")])
        chat.messages = []
        tr = ls.calc_trends(chat)
        self.assertEqual(tr["months"], [])
        self.assertIsNone(tr["first_message"])

    def test_compute_stats_carries_trends(self):
        from webapp import store

        chat = _f7_load(self._two_party_msgs())
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
            _f7_text(1, "uA", "我", base, "今晚吃什么"),
            _f7_text(2, "uB", "对方", base + 60, "火锅"),
            _f7_text(3, "uA", "我", base + 2 * day, "好想念火锅"),
            _f7_text(4, "uB", "对方", base + 60 * day, "下个月见面"),
            _f7_el(5, "uB", "对方", base + 61 * day, "image", {"url": "x.jpg", "md5": "m1", "size": "1"}),
            {**_f7_text(6, "uA", "我", base + 62 * day, "说错话了"), "recalled": True},
            {**_f7_text(7, "uB", "系统消息", base + 63 * day, "对方撤回了一条消息"), "system": True},
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
                        _f7_text(
                            len(msgs) + 1,
                            "uA" if j % 2 == 0 else "uB",
                            "我" if j % 2 == 0 else "对方",
                            _F7_BASE + month_idx * 31 * day + j * 60,
                            w,
                        )
                    )
                msgs.append(
                    _f7_text(
                        len(msgs) + 1,
                        "uA",
                        "我",
                        _F7_BASE + (month_idx + 1) * 31 * day + 5 * day,
                        "重启一下话题",
                    )
                )
            cls.CHAT = _f7_load(msgs)
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
        # 缓存文件名里嵌的是 recap 指纹（族隔离在文件名层面可见）
        path = store._cache_path("recap", "hashR")
        self.assertIn(rc.RECAP_PROMPT_FINGERPRINT, path)
        self.assertNotIn(dc.PROMPT_FINGERPRINT, path)

    def test_recap_and_ask_fingerprint_cover_their_own_input(self):
        """recap/ask 的版本号、提示词、抽样预算、输出预算或 salt 变更必须换键。"""
        from unittest import mock

        from analyzer import deepseek_client as dc
        from analyzer import recap_client as rc
        from analyzer import recap_prompts as rp

        recap_base = rc.recap_prompt_fingerprint()
        ask_base = rc.ask_fingerprint()

        # ① 抽样预算：改变进模型的对话样本量
        with mock.patch.object(rc, "RECAP_SAMPLE_LINES", 200):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "改抽样条数必须换 recap 指纹")
            self.assertNotEqual(ask_base, rc.ask_fingerprint(), "改抽样条数必须换 ask 指纹")
        with mock.patch.object(rc, "RECAP_SAMPLE_CHARS", 1000):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint(), "改抽样字符预算必须换指纹")
            self.assertNotEqual(ask_base, rc.ask_fingerprint(), "改抽样字符预算必须换 ask 指纹")

        # ② 提示词 / 版本号 / 输出预算 / salt 变化必须换键
        with mock.patch.object(rp, "SYSTEM_PROMPT_RECAP", "新的总括提示词"):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint())
        with mock.patch.object(rp, "SYSTEM_PROMPT_ASK", "新的提问提示词"):
            self.assertNotEqual(ask_base, rc.ask_fingerprint())
        with mock.patch.object(rc, "RECAP_CACHE_VERSION", "v999"):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint())
        with mock.patch.object(rc, "ASK_CACHE_VERSION", "v999"):
            self.assertNotEqual(ask_base, rc.ask_fingerprint())
        with mock.patch.dict(dc.MAX_TOKENS_BY_DIM, {"recap": 4096, "ask": 4096}):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint())
            self.assertNotEqual(ask_base, rc.ask_fingerprint())
        with mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "recap-ask-salt"}):
            self.assertNotEqual(recap_base, rc.recap_prompt_fingerprint())
            self.assertNotEqual(ask_base, rc.ask_fingerprint())

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
            _f7_text(1, "uA", "我", _F7_BASE, "我们什么时候去看海"),
            _f7_text(2, "uB", "对方", _F7_BASE + 60, "下个月吧"),
            _f7_text(3, "uA", "我", _F7_BASE + 40 * 86400, "海还去看吗"),
            _f7_text(4, "uB", "对方", _F7_BASE + 41 * 86400, "太忙了"),
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

    def test_ask_normalizes_confidence_variants_end_to_end(self):
        """/api/ask 与 answer_question 都要先归一化枚举再校验，不因大小写或中文置信度误报 502。"""
        guard = api_configured_patcher()
        guard.start()
        try:
            with mock.patch(
                "analyzer.deepseek_client._call_api",
                return_value={"answer": "原定周六出发。", "confidence": "高", "evidence": ["周五晚"]},
            ):
                r1 = self._ask("什么时候出发", refresh="1")
            self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True)[:200])
            self.assertEqual(r1.get_json()["result"]["confidence"], "high")

            with mock.patch(
                "analyzer.recap_client.answer_question",
                return_value={"answer": "大致在三月。", "confidence": "中等", "evidence": []},
            ):
                r2 = self._ask("大概是哪个月", refresh="1")
            self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True)[:200])
            self.assertEqual(r2.get_json()["result"]["confidence"], "medium")
        finally:
            guard.stop()
            for n in os.listdir(store.AI_CACHE_DIR):
                if n.startswith("ask_" + self.chat_hash + "_"):
                    os.remove(os.path.join(store.AI_CACHE_DIR, n))


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
