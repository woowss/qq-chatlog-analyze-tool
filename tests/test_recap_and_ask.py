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

from _bootstrap import api_configured_patcher, bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from parser.qq_parser import CST  # noqa: E402
from webapp import store  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
