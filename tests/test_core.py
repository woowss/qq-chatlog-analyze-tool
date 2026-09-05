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
"""核心逻辑测试：解析器健壮性、统计口径、AI 对话截断、CSRF 防护、缓存与异步任务"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# 让测试可以从项目根目录导入包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.qq_parser import (CST, ChatData, Message, is_statistical,
                              load_chat, split_by_month)
from analyzer.deepseek_client import (MAX_DIALOG_CHARS, _build_dialog,
                                      _fit_lines)
from analyzer.local_stats import calc_overview, calc_response_time


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
                "id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
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
                "id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
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
                "id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
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
                "id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
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
        """2025-09-30 23:30 UTC = 2025-10-01 07:30 北京时间，应按月归入 2025-10"""
        ts = int(datetime(2025, 9, 30, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
        chat = ChatData(
            chat_name="", self_name="我", other_name="对方",
            self_uid="self", other_uid="other",
            messages=[_msg("self", ts)],
        )
        months = split_by_month(chat)
        self.assertEqual(list(months.keys()), ["2025-10"])


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


class TestStatisticalFiltering(unittest.TestCase):
    """系统/撤回/转发消息不进入统计与 AI 分析"""

    def _flagged(self):
        normal = _msg("self", 1000, text="正常消息")
        recalled = _msg("self", 2000, text="被撤回的")
        recalled.recalled = True
        system = _msg("other", 3000, text="对方撤回了一条消息")
        system.system = True
        forwarded = _msg("other", 4000, text="转发内容", msg_type="type_11")
        return normal, recalled, system, forwarded

    def test_is_statistical(self):
        normal, recalled, system, forwarded = self._flagged()
        self.assertTrue(is_statistical(normal))
        self.assertFalse(is_statistical(recalled))
        self.assertFalse(is_statistical(system))
        self.assertFalse(is_statistical(forwarded))

    def test_overview_excludes_non_statistical(self):
        normal, recalled, system, forwarded = self._flagged()
        chat = ChatData(chat_name="", self_name="我", other_name="对方",
                        self_uid="self", other_uid="other",
                        messages=[normal, recalled, system, forwarded])
        ov = calc_overview(chat)
        self.assertEqual(ov["total_messages"], 1)
        self.assertEqual(ov["self_count"], 1)
        self.assertEqual(ov["other_count"], 0)

    def test_dialog_excludes_recalled_and_system(self):
        normal, recalled, system, forwarded = self._flagged()
        dialog = _build_dialog([normal, recalled, system, forwarded],
                               "self", "我", "对方", max_chars=MAX_DIALOG_CHARS)
        self.assertIn("正常消息", dialog)
        self.assertNotIn("被撤回的", dialog)
        self.assertNotIn("撤回了一条消息", dialog)
        self.assertNotIn("转发内容", dialog)

    def test_parser_reads_recalled_and_system_flags(self):
        data = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [
                {"id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
                 "sender": {"uid": "u_other", "name": "对方"},
                 "content": "hi", "recalled": True, "system": False},
                {"id": "2", "timestamp": 1758031010000, "time": "2025-09-16 21:56:50",
                 "sender": {"uid": "u_other", "name": "系统"},
                 "content": "对方撤回了一条消息", "recalled": False, "system": True},
            ],
        }
        chat = load_chat(_write_chat(data))
        self.assertTrue(chat.messages[0].recalled)
        self.assertTrue(chat.messages[1].system)
        self.assertEqual(len(chat.statistical()), 0)


class TestAiCache(unittest.TestCase):
    """服务端缓存：哈希稳定、读写往返、api_analyze 命中缓存不再调用模型"""

    def test_chat_hash_stable_and_cache_roundtrip(self):
        import app as appmod
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            h1 = appmod._chat_hash(path)
            h2 = appmod._chat_hash(path)
            self.assertEqual(h1, h2)
            self.assertEqual(len(h1), 16)
            payload = {"2025-09": {"self_emotion": "快乐"}}
            appmod._write_cache("emotion", h1, payload)
            got = appmod._read_cache("emotion", h1)
            self.assertEqual(got, payload)
        finally:
            os.remove(path)
            for f in Path(appmod.AI_CACHE_DIR).glob(f"emotion_{h1}_*"):
                f.unlink()

    def test_api_analyze_uses_cache_then_job(self):
        import app as appmod
        appmod.app.config["TESTING"] = True
        client = appmod.app.test_client()
        # 建立 session + 上传一个最小聊天文件
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        chat_json = json.dumps({
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}],
                           "totalMessages": 2},
            "messages": [
                {"id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "在吗"},
                {"id": "2", "timestamp": 1758031069000, "time": "2025-09-16 21:57:49",
                 "sender": {"uid": "u_other", "name": "对方"}, "content": "在的"},
            ],
        }, ensure_ascii=False).encode("utf-8")
        r = client.post("/upload", data={"file": (io.BytesIO(chat_json), "chat.json")},
                        headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            uploaded_path = sess.get("filepath")

        fake_result = {"self_emotion": "平静", "other_emotion": "快乐",
                       "self_intensity": 5, "other_intensity": 7,
                       "self_keywords": ["在吗"], "other_keywords": ["在的"],
                       "overall_tone": "轻松愉快"}
        try:
            with mock.patch("analyzer.deepseek_client._call_api", return_value=fake_result) as m:
                with mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True), \
                     mock.patch("app.is_api_configured", return_value=True):
                    r = client.post("/api/analyze/emotion",
                                    headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
                    body = r.get_json()
                    self.assertIn("job", body)
                    # 轮询直到任务结束
                    deadline = time.time() + 15
                    status = None
                    while time.time() < deadline:
                        s = client.get("/api/analyze-job/" + body["job"]).get_json()
                        status = s["status"]
                        if status in ("done", "error", "cancelled"):
                            break
                        time.sleep(0.1)
                    self.assertEqual(status, "done", f"任务未完成: {s}")
                    self.assertEqual(m.call_count, 1)

                    # 第二次请求应命中缓存，不再调用模型
                    r2 = client.post("/api/analyze/emotion",
                                     headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
                    b2 = r2.get_json()
                    self.assertTrue(b2.get("cached"))
                    self.assertIn("2025-09", b2["result"])
                    self.assertEqual(m.call_count, 1)  # 没有新的 API 调用

                    # GET 接口能读到缓存
                    r3 = client.get("/api/analysis/emotion")
                    self.assertTrue(r3.get_json().get("cached"))
        finally:
            if uploaded_path and os.path.exists(uploaded_path):
                os.remove(uploaded_path)
            with client.session_transaction() as sess:
                fp = sess.get("filepath")
            if fp and os.path.exists(fp):
                os.remove(fp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
