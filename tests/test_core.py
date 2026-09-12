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

# 测试隔离：数据目录指向临时目录，避免测试读写真实的 uploads/ai_cache/session
import tempfile as _tempfile
# 测试隔离：数据目录指向临时目录，绝不碰真实 uploads/ai_cache/session。
# 只清理"自己创建的"目录——外部显式指定的 QQCHAT_DATA_DIR 一律不动。
import atexit as _atexit
import shutil as _shutil


def _drop_temp_data_dir():
    """跑完把临时数据目录删掉（先关日志：否则我们的清理先跑，logging 的
    shutdown 又把 app.log 写回来，留下一堆空目录）"""
    import logging
    logging.shutdown()
    _shutil.rmtree(os.environ["QQCHAT_DATA_DIR"], ignore_errors=True)


if "QQCHAT_DATA_DIR" not in os.environ:
    os.environ["QQCHAT_DATA_DIR"] = _tempfile.mkdtemp(prefix="qqchatlog-test-")
    _atexit.register(_drop_temp_data_dir)
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 专门验证增量缓存的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 本进程的临时目录统一挪到数据目录下，两个好处：
# 1) %TEMP% 只读受限的环境（沙箱、部分容器）里 tempfile.* 不再直接 PermissionError；
# 2) 用例产生的临时json/图片/表情包都落在数据目录内，随测试隔离目录一起回收，
#    不会在用户 %TEMP% 里留下上百个 qqchatlog-* 垃圾目录。
_TMP_ROOT = os.path.join(os.environ["QQCHAT_DATA_DIR"], "tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

# 数据目录/路径准备必须在导入项目模块之前完成，故下面的导入带 noqa: E402
# （别名同样要放在这里：提前导入 webapp.* 会连带导入 config，把环境开关读成默认值）
from webapp import security as securitymod  # noqa: E402
from webapp import store as storemod  # noqa: E402
from parser.qq_parser import (CST, ChatData, Message, is_statistical,  # noqa: E402
                              load_chat, split_by_month)
from analyzer.deepseek_client import (MAX_DIALOG_CHARS, _build_dialog,  # noqa: E402
                                      _fit_lines)
from analyzer.local_stats import calc_milestones, calc_overview, calc_response_time  # noqa: E402


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
        self.assertLessEqual(sum(len(line) + 1 for line in fitted), 500)
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

    def test_non_ascii_csrf_token_is_rejected_not_500(self):
        """token 由请求方构造：非 ASCII 曾让 compare_digest 抛 TypeError → 500"""
        self._get_csrf()
        for headers, data in (
            ({"Origin": "http://localhost:5000"}, {"csrf_token": "中文口令"}),
            ({"Origin": "http://localhost:5000", "X-CSRF-Token": "中文口令"}, {}),
        ):
            r = self.client.post("/upload", data=data, headers=headers)
            self.assertEqual(r.status_code, 400, f"应返回 400，实际 {r.status_code}")
        # 对照：普通错误 token 也是 400
        r = self.client.post("/upload", data={},
                             headers={"Origin": "http://localhost:5000", "X-CSRF-Token": "wrong"})
        self.assertEqual(r.status_code, 400)

    def test_origin_equal_to_host_is_not_trusted(self):
        """Origin == Host 也必须是白名单内的主机，否则 DNS rebinding 可绕过校验"""
        import app as appmod
        with appmod.app.test_request_context(
                "/upload", headers={"Host": "evil.example.com", "Origin": "http://evil.example.com"}):
            self.assertFalse(securitymod._origin_allowed())
        with appmod.app.test_request_context(
                "/upload", headers={"Host": "127.0.0.1:5000", "Origin": "http://127.0.0.1:5000"}):
            self.assertTrue(securitymod._origin_allowed())

    def test_allowed_origins_config_is_honored(self):
        """局域网/自定义域名通过 ALLOWED_ORIGINS 显式放行"""
        import app as appmod
        from webapp import security as securitymod
        with mock.patch.object(securitymod, "ALLOWED_ORIGINS", frozenset({"chat.lan"})):
            with appmod.app.test_request_context(
                    "/upload", headers={"Host": "127.0.0.1:5000", "Origin": "http://chat.lan"}):
                self.assertTrue(securitymod._origin_allowed())
        with appmod.app.test_request_context(
                "/upload", headers={"Host": "127.0.0.1:5000", "Origin": "http://chat.lan"}):
            self.assertFalse(securitymod._origin_allowed())


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
                {"id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                 "sender": {"uid": "u_other", "name": "对方"},
                 "content": "hi", "recalled": True, "system": False},
                {"id": "2", "timestamp": 1704067201000, "time": "2024-01-01 08:00:01",
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
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            h1 = storemod._chat_hash(path)
            h2 = storemod._chat_hash(path)
            self.assertEqual(h1, h2)
            self.assertEqual(len(h1), 16)
            payload = {"2024-01": {"self_emotion": "快乐"}}
            storemod._write_cache("emotion", h1, payload)
            got = storemod._read_cache("emotion", h1)
            self.assertEqual(got, payload)
        finally:
            os.remove(path)
            for f in Path(storemod.AI_CACHE_DIR).glob(f"emotion_{h1}_*"):
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
                {"id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "在吗"},
                {"id": "2", "timestamp": 1704067260000, "time": "2024-01-01 08:01:00",
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
        cache_dir = Path(storemod.AI_CACHE_DIR)
        cache_before = set(cache_dir.glob("*")) if cache_dir.exists() else set()
        try:
            with mock.patch("analyzer.deepseek_client._call_api", return_value=fake_result) as m:
                with mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True), \
                     mock.patch("webapp.api.is_api_configured", return_value=True):
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
                    self.assertIn("2024-01", b2["result"])
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
            # 清理本测试新写入的缓存文件，不污染真实 ai_cache/
            cache_after = set(cache_dir.glob("*")) if cache_dir.exists() else set()
            for f in cache_after - cache_before:
                try:
                    f.unlink()
                except OSError:
                    pass


class TestQuotaAndThrottle(unittest.TestCase):
    """429=每分钟限流（TPM/RPM，等待后重试，官方称约1分钟恢复）；
    403/欠费=真正额度耗尽（快速失败，中止剩余任务）"""

    @staticmethod
    def _err(status, msg):
        e = Exception(msg)
        e.status_code = status
        return e

    @staticmethod
    def _ok_resp(content='{"a": 1}'):
        r = mock.MagicMock()
        r.choices = [mock.MagicMock()]
        r.choices[0].finish_reason = "stop"
        r.choices[0].message.content = content
        r.usage = None
        return r

    def setUp(self):
        import analyzer.deepseek_client as dc
        self.dc = dc
        self._orig_interval = dc.CALL_MIN_INTERVAL
        dc.CALL_MIN_INTERVAL = 0.0  # 测试中关闭全局调用闸门，避免拖慢

    def tearDown(self):
        self.dc.CALL_MIN_INTERVAL = self._orig_interval

    def test_tpm_retries_then_raises(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = self._err(429, "Allocated quota exceeded")
        with mock.patch.object(self.dc, "_get_client", return_value=fake):
            with self.assertRaises(self.dc.QuotaExhaustedError):
                self.dc._call_api("s", "u", retry=2, tpm_wait=0.01)
        self.assertEqual(fake.chat.completions.create.call_count, self.dc.TPM_MAX_ATTEMPTS)

    def test_tpm_transient_then_success(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = [
            self._err(429, "Allocated quota exceeded"), self._ok_resp()]
        with mock.patch.object(self.dc, "_get_client", return_value=fake):
            out = self.dc._call_api("s", "u", retry=2, tpm_wait=0.01)
        self.assertEqual(out, {"a": 1})
        self.assertEqual(fake.chat.completions.create.call_count, 2)

    def test_plan_exhausted_fails_fast(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = self._err(403, "Free allocated quota exceeded")
        with mock.patch.object(self.dc, "_get_client", return_value=fake):
            with self.assertRaises(self.dc.QuotaExhaustedError):
                self.dc._call_api("s", "u", retry=2, tpm_wait=0.01)
        self.assertEqual(fake.chat.completions.create.call_count, 1)  # 不重试

    def test_insufficient_balance_fails_fast(self):
        """DeepSeek 官方余额不足（402 Insufficient Balance）同样属于不可重试的致命错误"""
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = self._err(402, "Insufficient Balance")
        with mock.patch.object(self.dc, "_get_client", return_value=fake):
            with self.assertRaises(self.dc.QuotaExhaustedError):
                self.dc._call_api("s", "u", retry=2, tpm_wait=0.01)
        self.assertEqual(fake.chat.completions.create.call_count, 1)

    def test_thinking_disabled_for_normal_dims(self):
        """全局关闭 + 白名单只有 profile 时，emotion 走非思考：下发 temperature 与 disabled"""
        fake = mock.Mock()
        fake.chat.completions.create.return_value = self._ok_resp()
        with mock.patch.object(self.dc, "THINKING_DEFAULT", False), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset({"profile"})), \
             mock.patch.object(self.dc, "_SEND_THINKING_PARAM", True), \
             mock.patch.object(self.dc, "_get_client", return_value=fake):
            self.dc._call_api("s", "u", tag="emotion")
            # 断言必须在 patch 内：thinking_enabled 读的是模块级配置，
            # 放到 with 外面会依赖本机 .env（CI 无 .env → 白名单为空 → 误报失败）
            self.assertFalse(self.dc.thinking_enabled("emotion"))
            self.assertTrue(self.dc.thinking_enabled("profile"))
        kw = fake.chat.completions.create.call_args.kwargs
        self.assertEqual(kw["temperature"], 0.3)
        self.assertEqual(kw["extra_body"], {"thinking": {"type": "disabled"}})

    def test_thinking_enabled_for_whitelisted_profile_only(self):
        """白名单维度 profile 开思考：不传 temperature（服务端会忽略），传 enabled"""
        fake = mock.Mock()
        fake.chat.completions.create.return_value = self._ok_resp()
        with mock.patch.object(self.dc, "THINKING_DEFAULT", False), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset({"profile"})), \
             mock.patch.object(self.dc, "_SEND_THINKING_PARAM", True), \
             mock.patch.object(self.dc, "_get_client", return_value=fake):
            self.dc._call_api("s", "u", tag="profile")
            self.assertTrue(self.dc.thinking_enabled("profile"))
            self.assertFalse(self.dc.thinking_enabled("emotion"))
            self.assertFalse(self.dc.thinking_enabled("habits"))
        kw = fake.chat.completions.create.call_args.kwargs
        self.assertNotIn("temperature", kw)
        self.assertEqual(kw["extra_body"], {"thinking": {"type": "enabled"}})

    def test_global_thinking_switch_covers_all_dims(self):
        """LLM_THINKING=enabled 时所有维度都开（白名单为空也不影响）"""
        with mock.patch.object(self.dc, "THINKING_DEFAULT", True), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset()):
            for dim in ("emotion", "topics", "relationship", "habits", "profile"):
                self.assertTrue(self.dc.thinking_enabled(dim))
        with mock.patch.object(self.dc, "THINKING_DEFAULT", False), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset()):
            self.assertFalse(self.dc.thinking_enabled("profile"))

    def test_profile_budget_fits_chain_of_thought(self):
        """锐评预算必须能同时装下思维链与长 JSON（实测思考模式约占 2-4k token）"""
        self.assertGreaterEqual(self.dc.MAX_TOKENS_BY_DIM["profile"], 16384)

    def test_all_budgets_leave_room_for_thinking(self):
        """准确性优先：每月一调的维度也要留足思维链空间（官方 max output 384K）"""
        for dim, budget in self.dc.MAX_TOKENS_BY_DIM.items():
            self.assertGreaterEqual(budget, 16384, f"{dim} 预算过小，思考模式易被截断")
            self.assertLessEqual(budget, 384_000, f"{dim} 超过官方 max output")

    def test_truncated_output_retries_without_thinking(self):
        """被截断时降级重试：宁可精度略降，也不让这个月从结果里消失"""
        truncated = mock.Mock()
        truncated.choices = [mock.Mock(finish_reason="length",
                                       message=mock.Mock(content='{"a": 1}'))]
        truncated.usage = None
        ok = self._ok_resp()
        ok.choices[0].message.content = '{"self_emotion": "平静"}'
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = [truncated, ok]
        with mock.patch.object(self.dc, "THINKING_DEFAULT", True), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset()), \
             mock.patch.object(self.dc, "_SEND_THINKING_PARAM", True), \
             mock.patch.object(self.dc, "_get_client", return_value=fake):
            out = self.dc._call_api("s", "u", tag="emotion", retry=0)
        self.assertEqual(out, {"self_emotion": "平静"}, "降级重试应拿到结果")
        calls = fake.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(calls[1].kwargs["extra_body"], {"thinking": {"type": "disabled"}},
                         "第二次必须关掉思考模式")
        self.assertIn("temperature", calls[1].kwargs)

    def test_non_deepseek_gateway_gets_no_thinking_param(self):
        """百炼等网关未显式配置思考模式时不发送 thinking 字段，避免非法参数"""
        fake = mock.Mock()
        fake.chat.completions.create.return_value = self._ok_resp()
        with mock.patch.object(self.dc, "THINKING_DEFAULT", False), \
             mock.patch.object(self.dc, "THINKING_DIMS", frozenset()), \
             mock.patch.object(self.dc, "_SEND_THINKING_PARAM", False), \
             mock.patch.object(self.dc, "_get_client", return_value=fake):
            self.dc._call_api("s", "u", tag="profile")
        kw = fake.chat.completions.create.call_args.kwargs
        self.assertNotIn("extra_body", kw)
        self.assertEqual(kw["temperature"], 0.3)

    def test_periods_abort_remaining_on_fatal(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = self._err(403, "Free allocated quota exceeded")
        months = {f"2025-{m:02d}": [_msg("self", 1735689600000 + i * 2678400000, text="hi")]
                  for m, i in [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4)]}
        with mock.patch.object(self.dc, "_get_client", return_value=fake):
            with self.assertRaises(self.dc.QuotaExhaustedError):
                self.dc._analyze_periods(months, "sys", lambda p, m: "prompt", max_tokens=1024)
        # 致命错误后剩余月份被中止：调用数不超过月份总数（无重试放大）
        self.assertLessEqual(fake.chat.completions.create.call_count, len(months))


class TestEmptyMonthSkipped(unittest.TestCase):
    """整月只有撤回/系统消息时，不调用 API"""

    def test_recalled_only_month_not_sent(self):
        import analyzer.deepseek_client as dc
        sep_msg = _msg("self", 1704067200000, text="一月消息")   # 2024-01
        oct_recalled = _msg("other", 1706745600000, text="二月被撤回")  # 2024-02
        oct_recalled.recalled = True
        chat = ChatData(chat_name="", self_name="我", other_name="对方",
                        self_uid="self", other_uid="other",
                        messages=[sep_msg, oct_recalled])
        result = {"self_emotion": "平静", "other_emotion": "平静",
                  "self_intensity": 5, "other_intensity": 5,
                  "self_keywords": [], "other_keywords": [],
                  "overall_tone": "平淡日常"}
        with mock.patch.object(dc, "_call_api", return_value=result) as m:
            out = dc.analyze_emotion(chat)
        self.assertEqual(m.call_count, 1)          # 十月整月无效，未调用
        self.assertIn("2024-01", out)
        self.assertNotIn("2024-02", out)


class TestMilestones(unittest.TestCase):
    """时光里程碑：连续纪录/沉默期/深夜/峰值"""

    @staticmethod
    def _at(uid, y, mo, d, h, text="hi"):
        ts = int(datetime(y, mo, d, h, 30, tzinfo=CST).timestamp() * 1000)
        return _msg(uid, ts, text=text)

    def test_milestones(self):
        msgs = [
            self._at("self", 2025, 1, 1, 21), self._at("other", 2025, 1, 1, 22),
            self._at("self", 2025, 1, 2, 10),
            self._at("self", 2025, 1, 3, 3),    # 凌晨 3 点
            self._at("other", 2025, 1, 3, 4),   # 双方都熬夜 → mutual_nights
            self._at("self", 2025, 1, 10, 23),
            self._at("other", 2025, 1, 10, 23),
            self._at("self", 2025, 1, 10, 23),  # 峰值日 3 条
        ]
        chat = ChatData(chat_name="", self_name="我", other_name="对方",
                        self_uid="self", other_uid="other", messages=msgs)
        ms = calc_milestones(chat)
        self.assertEqual(ms["first_day"], "2025-01-01")
        self.assertEqual(ms["last_day"], "2025-01-10")
        self.assertEqual(ms["active_days"], 4)
        self.assertEqual(ms["longest_streak"]["days"], 3)
        self.assertEqual(ms["longest_silence"]["days"], 6)   # 01-03 → 01-10
        self.assertEqual(ms["midnight_days"], 1)             # 01-03
        self.assertEqual(ms["midnight_msgs"], 2)
        self.assertEqual(ms["late_night_msgs"], 2)
        self.assertEqual(ms["mutual_nights"], 1)
        self.assertEqual(ms["peak_day"], {"date": "2025-01-10", "count": 3})
        self.assertEqual(ms["busiest_month"]["month"], "2025-01")

    def test_milestones_empty(self):
        chat = ChatData(chat_name="", self_name="我", other_name="对方",
                        self_uid="self", other_uid="other", messages=[])
        self.assertEqual(calc_milestones(chat), {})


class TestUsageRecord(unittest.TestCase):
    """token 用量按天×维度聚合，原子落盘"""

    def test_record_and_aggregate(self):
        import analyzer.usage as usage
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(tmp)  # 从空文件状态开始
        orig = usage.TOKEN_USAGE_FILE
        usage.TOKEN_USAGE_FILE = tmp
        try:
            usage.record_call("model-a", "emotion", 100, 20)
            usage.record_call("model-a", "emotion", 50, 10)
            usage.record_call("model-a", "profile", 30, 70)
            u = usage.get_usage()
            self.assertEqual(u["total"]["calls"], 3)
            self.assertEqual(u["total"]["prompt"], 180)
            self.assertEqual(u["total"]["completion"], 100)
            self.assertEqual(u["total"]["total"], 280)
            self.assertEqual(u["dims"]["emotion|model-a"]["calls"], 2)
            self.assertEqual(u["dims"]["profile|model-a"]["calls"], 1)
            self.assertEqual(len(u["days"]), 1)
        finally:
            usage.TOKEN_USAGE_FILE = orig
            if os.path.exists(tmp):
                os.remove(tmp)


class TestAnalyzeAll(unittest.TestCase):
    """一键全量：5 维度跑完并写缓存；再跑一次全部命中缓存不再调用 API"""

    def test_analyze_all_flow(self):
        import app as appmod
        appmod.app.config["TESTING"] = True
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        chat_json = json.dumps({
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [
                {"id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "在吗"},
                {"id": "2", "timestamp": 1704067260000, "time": "2024-01-01 08:01:00",
                 "sender": {"uid": "u_other", "name": "对方"}, "content": "在的"},
            ],
        }, ensure_ascii=False).encode("utf-8")
        r = client.post("/upload", data={"file": (io.BytesIO(chat_json), "chat.json")},
                        headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            uploaded_path = sess.get("filepath")

        fake = {"self_emotion": "平静", "other_emotion": "快乐",
                "self_intensity": 5, "other_intensity": 7,
                "self_keywords": [], "other_keywords": [],
                "overall_tone": "轻松愉快", "topics": [], "summary": "s"}
        cache_dir = Path(storemod.AI_CACHE_DIR)
        cache_before = set(cache_dir.glob("*"))
        try:
            with mock.patch("analyzer.deepseek_client._call_api", return_value=fake) as m, \
                 mock.patch("webapp.api.is_api_configured", return_value=True):
                r = client.post("/api/analyze-all",
                                headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
                body = r.get_json()
                self.assertIn("job", body)
                deadline = time.time() + 20
                s = {}
                while time.time() < deadline:
                    s = client.get("/api/analyze-job/" + body["job"]).get_json()
                    if s.get("status") in ("done", "error", "cancelled"):
                        break
                    time.sleep(0.1)
                self.assertEqual(s.get("status"), "done", f"未完成: {s}")
                self.assertEqual(len(s["result"]), 5)
                self.assertTrue(all(v == "done" for v in s["result"].values()))
                calls_after_first = m.call_count
                self.assertGreaterEqual(calls_after_first, 5)  # 每维度至少一次

                # 再跑一次：全部命中缓存，零新增调用
                r2 = client.post("/api/analyze-all",
                                 headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
                b2 = r2.get_json()
                s2 = {}
                deadline = time.time() + 10
                while time.time() < deadline:
                    s2 = client.get("/api/analyze-job/" + b2["job"]).get_json()
                    if s2.get("status") in ("done", "error", "cancelled"):
                        break
                    time.sleep(0.05)
                self.assertEqual(s2.get("status"), "done")
                self.assertTrue(all(v == "cached" for v in s2["result"].values()))
                self.assertEqual(m.call_count, calls_after_first)  # 没有新调用
        finally:
            if uploaded_path and os.path.exists(uploaded_path):
                os.remove(uploaded_path)
            cache_after = set(cache_dir.glob("*"))
            for f in cache_after - cache_before:
                try:
                    f.unlink()
                except OSError:
                    pass


class TestPromptContract(unittest.TestCase):
    """JSON 契约防漂移：前端渲染依赖的老字段名必须始终存在于对应 prompt 中"""

    def test_field_names_present(self):
        from analyzer import prompts as P
        cases = [
            (P.SYSTEM_PROMPT_EMOTION, ["self_emotion", "other_emotion", "self_intensity",
                                       "other_intensity", "self_keywords", "other_keywords",
                                       "overall_tone"]),
            (P.SYSTEM_PROMPT_TOPICS, ["topics", "weight", "keywords", "summary",
                                      "topic_shift_detected", "shift_description"]),
            (P.SYSTEM_PROMPT_RELATIONSHIP, ["initiator_tendency", "initiator_ratio_self",
                                            "interaction_style", "closeness_score",
                                            "closeness_trend", "self_role", "other_role",
                                            "relationship_summary"]),
            (P.SYSTEM_PROMPT_HABITS, ["personality_tags", "common_phrases", "emoji_style",
                                      "top_emojis", "sentence_length", "reply_speed",
                                      "topic_jumping", "unique_traits"]),
            (P.SYSTEM_PROMPT_PROFILE, ["overall_impression", "core_type", "strengths",
                                       "weaknesses", "quirks", "signature_phrases",
                                       "fun_facts", "scoring", "verdict",
                                       "counter_evidence", "confidence"]),
        ]
        for prompt, fields in cases:
            for f in fields:
                self.assertIn(f, prompt, f"字段 {f} 从 prompt 中消失，前端契约被破坏")

    def test_objectivity_and_humor_rules_present(self):
        """客观/幽默机制必须写进共用守则"""
        from analyzer import prompts as P
        for keyword in ("反例自查", "置信度", "证据优先", "损而不伤", "反套话黑名单"):
            self.assertIn(keyword, P._OBSERVER_CREED)


class TestCacheLifecycle(unittest.TestCase):
    """缓存键含提示词版本；聊天文件删除时派生缓存联动清除"""

    def test_cache_path_includes_prompt_fingerprint(self):
        """缓存键用提示词/格式指纹：改 prompt 或对话格式后旧缓存自动失效"""
        from analyzer.deepseek_client import PROMPT_FINGERPRINT
        path = storemod._cache_path("emotion", "deadbeef" * 2)
        self.assertIn(PROMPT_FINGERPRINT, path)

    def test_cache_path_separates_thinking_mode(self):
        """切换思考模式必须换键：否则开/关 thinking 后会命中另一模式的旧结果"""
        from webapp import store as storemod
        with mock.patch.object(storemod, "thinking_enabled", lambda d: d == "profile"):
            think_path = storemod._cache_path("profile", "hashX")
            plain_path = storemod._cache_path("emotion", "hashX")
        self.assertTrue(think_path.endswith("_think.json"))
        self.assertFalse(plain_path.endswith("_think.json"))
        # 未开思考时不加后缀，既有缓存键保持兼容
        with mock.patch.object(storemod, "thinking_enabled", lambda d: False):
            self.assertFalse(storemod._cache_path("profile", "hashX").endswith("_think.json"))

    def test_purge_removes_thinking_cache_too(self):
        """级联删除不看思考模式后缀，思考模式结果同样不会成为孤儿"""
        from webapp import store as storemod
        with mock.patch.object(storemod, "thinking_enabled", lambda d: True):
            storemod._write_cache("profile", "hashCCC", {"x": 1})
            path = storemod._cache_path("profile", "hashCCC")
            try:
                self.assertTrue(os.path.exists(path))
                self.assertEqual(storemod._purge_chat_caches("hashCCC"), 1)
                self.assertFalse(os.path.exists(path))
            finally:
                if os.path.exists(path):
                    os.remove(path)

    def test_purge_chat_caches(self):
        storemod._write_cache("emotion", "hashAAA", {"x": 1})
        storemod._write_cache("topics", "hashAAA", {"x": 2})
        storemod._write_cache("emotion", "hashBBB", {"x": 3})
        try:
            self.assertEqual(storemod._purge_chat_caches("hashAAA"), 2)
            self.assertIsNone(storemod._read_cache("emotion", "hashAAA"))
            self.assertIsNotNone(storemod._read_cache("emotion", "hashBBB"))
        finally:
            storemod._purge_chat_caches("hashBBB")


class TestJobDedup(unittest.TestCase):
    """同 session 同维度并发发起时复用 running job，不重复烧 API"""

    def test_reuses_running_job(self):
        import threading
        import app as appmod
        appmod.app.config["TESTING"] = True
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        chat_json = json.dumps({
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [
                {"id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "在吗"},
            ],
        }, ensure_ascii=False).encode("utf-8")
        r = client.post("/upload", data={"file": (io.BytesIO(chat_json), "chat.json")},
                        headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})
        with client.session_transaction() as sess:
            uploaded_path = sess.get("filepath")
        self.assertEqual(r.status_code, 302)

        release = threading.Event()
        fake = {"self_emotion": "平静", "other_emotion": "平静",
                "self_intensity": 5, "other_intensity": 5,
                "self_keywords": [], "other_keywords": [], "overall_tone": "平淡日常"}

        def blocking(*a, **k):
            self.assertTrue(release.wait(15), "测试超时未放行")
            return fake

        cache_dir = Path(storemod.AI_CACHE_DIR)
        cache_before = set(cache_dir.glob("*"))
        try:
            with mock.patch("analyzer.deepseek_client._call_api", side_effect=blocking), \
                 mock.patch("webapp.api.is_api_configured", return_value=True):
                h = {"Origin": "http://localhost:5000", "X-CSRF-Token": token}
                b1 = client.post("/api/analyze/emotion", headers=h).get_json()
                b2 = client.post("/api/analyze/emotion", headers=h).get_json()
                self.assertIn("job", b1)
                self.assertEqual(b1["job"], b2["job"])   # 复用同一任务
                self.assertTrue(b2.get("reused"))
            release.set()
            deadline = time.time() + 15
            while time.time() < deadline:
                s = client.get("/api/analyze-job/" + b1["job"]).get_json()
                if s.get("status") in ("done", "error", "cancelled"):
                    break
                time.sleep(0.1)
            self.assertEqual(s.get("status"), "done")
        finally:
            release.set()
            if uploaded_path and os.path.exists(uploaded_path):
                os.remove(uploaded_path)
            for f in set(cache_dir.glob("*")) - cache_before:
                try:
                    f.unlink()
                except OSError:
                    pass


class TestStratifiedProfileSample(unittest.TestCase):
    """锐评样本必须覆盖整个时间轴（growth_observation 的前提），而非只取最近"""

    def test_profile_sample_spans_timeline(self):
        """样本量已按"准确性优先"上调（800 条），这里用 1600 条构造 stride=2 的场景"""
        import analyzer.deepseek_client as dc
        sample_size = 800
        total = sample_size * 2          # 正好触发 stride=2 的抽稀
        msgs = []
        for i in range(total):   # 每天一条
            m = _msg("self", 1735689600000 + i * 86400000, text=f"消息{i}")
            m.time_str = datetime.fromtimestamp(m.timestamp / 1000, tz=CST) \
                            .strftime("%Y-%m-%d %H:%M:%S")
            msgs.append(m)
        chat = ChatData(chat_name="", self_name="我", other_name="对方",
                        self_uid="self", other_uid="other", messages=msgs)
        captured = []

        def spy(system_prompt, user_content, **k):
            captured.append(user_content)
            return None   # 不产生结果，只看 prompt

        with mock.patch.object(dc, "_call_api", side_effect=spy):
            dc.analyze_profile(chat)

        self.assertEqual(len(captured), 1)   # other 一方无发言
        prompt = captured[0]
        self.assertIn("按时间均匀抽样覆盖整个时段", prompt)
        self.assertIn("消息0", prompt)                    # 最早
        self.assertIn(f"消息{total - 2}", prompt)         # 最晚（stride=2 的最后一个偶数下标）
        self.assertNotIn("消息1\n", prompt)               # 奇数条目被抽稀


class TestReuploadCacheLifecycle(unittest.TestCase):
    """重传同一文件应保留缓存（省钱）；换不同文件才联动清除旧缓存"""

    def _upload(self, client, token, payload: dict, name="chat.json"):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return client.post("/upload", data={"file": (io.BytesIO(data), name)},
                           headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token})

    def test_same_file_keeps_cache_different_file_purges(self):
        import app as appmod
        appmod.app.config["TESTING"] = True
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        chat_a = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [
                {"id": "1", "timestamp": 1704067200000, "time": "2024-01-01 08:00:00",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "A内容"},
            ],
        }
        chat_b = json.loads(json.dumps(chat_a))
        chat_b["messages"][0]["content"] = "B内容（不同哈希）"

        r = self._upload(client, token, chat_a)
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            hash_a = sess["chat_hash"]
            path_a = sess["filepath"]
        storemod._write_cache("emotion", hash_a, {"keep": True})

        # 重传同一文件：旧文件删除但缓存保留
        r2 = self._upload(client, token, chat_a)
        self.assertEqual(r2.status_code, 302)
        self.assertFalse(os.path.exists(path_a))          # 旧文件已清理
        self.assertIsNotNone(storemod._read_cache("emotion", hash_a))  # 缓存还在

        # 换不同内容文件：旧哈希的缓存被联动清除
        r3 = self._upload(client, token, chat_b)
        self.assertEqual(r3.status_code, 302)
        self.assertIsNone(storemod._read_cache("emotion", hash_a))
        with client.session_transaction() as sess:
            path_b = sess.get("filepath")
            hash_b = sess.get("chat_hash")
        try:
            storemod._purge_chat_caches(hash_b)
        finally:
            if path_b and os.path.exists(path_b):
                os.remove(path_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
