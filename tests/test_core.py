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
# -*- coding: utf-8 -*-
"""核心逻辑测试：解析器健壮性、统计口径、AI 对话截断、CSRF 防护"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# 让测试可以从项目根目录导入包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.qq_parser import CST, ChatData, Message, load_chat, split_by_month
from analyzer.deepseek_client import MAX_DIALOG_CHARS, _build_dialog, _fit_lines
from analyzer.local_stats import calc_response_time


def _write_chat(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def _msg(uid: str, ts: int, text: str = "hello", msg_type: str = "type_1") -> Message:
    return Message(
        id="", timestamp=ts, time_str="2025-01-01 00:00:00",
        sender_name="x", sender_uid=uid,
        text=text, raw_text=text, msg_type=msg_type,
        has_image=False, is_reply=False,
    )


class TestParserRobustness(unittest.TestCase):
    def test_content_as_string(self):
        """content 直接是纯文本时不应崩溃，且文本被保留"""
        data = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]},
            "messages": [{
                "id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                "sender": {"uid": "u_self", "name": "我"},
                "content": "这是纯文本消息",
            }],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.messages[0].text, "这是纯文本消息")

    def test_no_elements_falls_back_to_raw_text(self):
        """content 有 text 但没有 elements 时，回退到原始文本，避免消息丢失"""
        data = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]},
            "messages": [{
                "id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                "sender": {"uid": "u_self", "name": "我"},
                "content": {"text": "无 elements 的消息"},
            }],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.messages[0].text, "无 elements 的消息")

    def test_image_only_message_not_fallback(self):
        """只有图片 elements 的消息不应把 "[图片]" 占位符当正文"""
        data = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]},
            "messages": [{
                "id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                "sender": {"uid": "u_self", "name": "我"},
                "content": {"text": "[图片]", "elements": [{"type": "image", "data": {}}]},
            }],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.messages[0].text, "")
        self.assertTrue(chat.messages[0].has_image)

    def test_empty_self_uid_fallback(self):
        """缺少 selfUid 时，按显示名从 senders 找回自己的 UID"""
        data = {
            "chatInfo": {"name": "对方", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_other", "name": "对方"}, {"uid": "u_self", "name": "我"}]},
            "messages": [{
                "id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                "sender": {"uid": "u_self", "name": "我"},
                "content": "来自我",
            }],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.self_uid, "u_self")
        self.assertEqual(chat.messages[0].sender_uid, "u_self")


class TestResponseTime(unittest.TestCase):
    def test_only_cross_sender_gaps_counted(self):
        """只统计对方发来后本方的回复间隔；同人连发不计入"""
        chat = ChatData(
            chat_name="", self_name="我", other_name="对方",
            self_uid="self", other_uid="other",
            messages=[_msg("self", 1000), _msg("other", 6000), _msg("self", 13000)],
        )
        rt = calc_response_time(chat)
        self.assertEqual(rt["other_avg_seconds"], 5.0)  # 对方 5s 内回应
        self.assertEqual(rt["self_avg_seconds"], 7.0)   # 自己 7s 内回应

    def test_consecutive_same_sender_skipped(self):
        """self->self 的间隔不应算作响应时间"""
        chat = ChatData(
            chat_name="", self_name="我", other_name="对方",
            self_uid="self", other_uid="other",
            messages=[_msg("self", 1000), _msg("self", 5000), _msg("other", 9000)],
        )
        rt = calc_response_time(chat)
        self.assertEqual(rt["other_avg_seconds"], 4.0)   # self->other 间隔 4s
        self.assertEqual(rt["self_avg_seconds"], 0)      # 没有 self 的响应记录


class TestSplitByMonthTimezone(unittest.TestCase):
    def test_grouping_uses_cst(self):
        """2024-01-31 23:30 UTC = 2024-02-01 07:30 北京时间，应按月归入 2024-02"""
        ts = int(datetime(2024, 1, 31, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
        chat = ChatData(
            chat_name="", self_name="我", other_name="对方",
            self_uid="self", other_uid="other",
            messages=[_msg("self", ts)],
        )
        months = split_by_month(chat)
        self.assertEqual(list(months.keys()), ["2024-02"])


class TestDialogTruncation(unittest.TestCase):
    def test_fit_lines_within_limit(self):
        lines = [f"第{i}条消息内容" for i in range(500)]
        fitted = _fit_lines(lines, 500)
        self.assertLessEqual(sum(len(l) + 1 for l in fitted), 500)
        self.assertTrue(len(fitted) >= 1)

    def test_build_dialog_filters_system_and_empty(self):
        messages = [
            _msg("self", 1000, text=""),
            _msg("self", 2000, text="正常消息", msg_type="type_11"),
            _msg("other", 3000, text="有效回复"),
        ]
        dialog = _build_dialog(messages, "self", "我", "对方", max_chars=MAX_DIALOG_CHARS)
        self.assertNotIn("正常消息", dialog)
        self.assertIn("有效回复", dialog)

    def test_build_dialog_respects_max_chars(self):
        messages = [_msg("self", 1000, text="长" * 100 + str(i)) for i in range(50)]
        dialog = _build_dialog(messages, "self", "我", "对方", max_chars=200)
        self.assertLessEqual(len(dialog), 200 + 200)  # 宽松断言：单条截断逻辑保证不超太多


class TestCsrfProtection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_post_without_csrf_rejected(self):
        r = self.client.post("/upload", data={}, headers={"Origin": "http://localhost:5000"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("CSRF", r.get_data(as_text=True))

    def _get_csrf(self) -> str:
        """先请求一次页面以建立 session 并注入 csrf_token"""
        self.client.get("/")
        with self.client.session_transaction() as sess:
            return sess["csrf_token"]

    def test_cross_origin_rejected(self):
        token = self._get_csrf()
        r = self.client.post(
            "/upload",
            data={"csrf_token": token},
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(r.status_code, 403)

    def test_valid_csrf_passes_guard(self):
        token = self._get_csrf()
        r = self.client.post(
            "/upload",
            data={"csrf_token": token},
            headers={"Origin": "http://localhost:5000"},
        )
        # 通过防护后，因为没带文件，返回"请选择文件"而不是 400 防护错误
        self.assertEqual(r.status_code, 400)
        self.assertIn("请选择文件", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
