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
"""健壮性/一致性回归测试（对应 2026-09-10 复查发现的问题）

覆盖：时间戳解析兜底、双方身份校验、取消真正生效、任务重叠拒绝、
缓存原子写与续期、登录限流、会话 Cookie、日志 handler 唯一性。
"""

import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 专门验证增量缓存的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 别名与项目模块的导入都必须排在环境准备之后：提前导入 webapp.* 会连带导入 config，
# 于是 QQCHAT_MONTH_CACHE / QQCHAT_DATA_DIR 被读成默认值（月份缓存意外开启）
from webapp import jobs as jobsmod  # noqa: E402
from webapp import security as securitymod  # noqa: E402
from webapp import store as storemod  # noqa: E402
from parser.qq_parser import Message, load_chat, split_by_month  # noqa: E402
from analyzer.local_stats import calc_overview  # noqa: E402


def _write_chat(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def _msg(uid: str, ts: int, text: str = "hello") -> Message:
    return Message(
        id="",
        timestamp=ts,
        time_str="2025-01-01 00:00:00",
        sender_name="x",
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


class TestParserTimestampRobustness(unittest.TestCase):
    """时间戳缺失/null/非数值：既不能崩溃，也不能把消息塞进 1970-01"""

    GOOD = {
        "id": "1",
        "timestamp": 1704067200000,
        "time": "2024-01-01 08:00:00",
        "sender": {"uid": "u_self", "name": "我"},
        "content": "正常",
    }

    @staticmethod
    def _load(msgs, info=None):
        data = {
            "chatInfo": info or {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]},
            "messages": msgs,
        }
        return load_chat(_write_chat(data))

    def test_null_timestamp_does_not_crash(self):
        chat = self._load([self.GOOD, {**self.GOOD, "id": "2", "timestamp": None}])
        self.assertEqual(len(chat.messages), 2)  # 用 time 字符串回退成功
        self.assertEqual(set(split_by_month(chat)), {"2024-01"})

    def test_string_timestamp_is_coerced(self):
        chat = self._load([{**self.GOOD, "timestamp": "1704067200000"}])
        self.assertEqual(chat.messages[0].timestamp, 1704067200000)

    def test_missing_timestamp_falls_back_to_time_string(self):
        msg = {k: v for k, v in self.GOOD.items() if k != "timestamp"}
        msg["time"] = "2024-02-02 08:00:00"
        chat = self._load([msg])
        self.assertEqual(list(split_by_month(chat)), ["2024-02"])

    def test_unusable_timestamp_is_dropped_not_1970(self):
        bad = {
            "id": "9",
            "timestamp": None,
            "time": "not-a-date",
            "sender": {"uid": "u_other", "name": "对方"},
            "content": "坏时间戳",
        }
        chat = self._load([self.GOOD, bad])
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(chat.dropped_messages, 1)
        self.assertNotIn("1970-01", split_by_month(chat))
        self.assertEqual(calc_overview(chat)["total_messages"], 1)

    def test_missing_self_identity_raises(self):
        """无法区分双方时必须报错，而不是把所有消息静默判给「对方」"""
        with self.assertRaises(ValueError) as ctx:
            self._load([self.GOOD], info={"name": "对方"})
        self.assertIn("selfUid", str(ctx.exception))

    def test_self_uid_recovered_from_self_name(self):
        chat = self._load([self.GOOD], info={"name": "会话", "selfName": "我"})
        self.assertEqual(chat.self_uid, "u_self")
        self.assertEqual(chat.other_name, "对方")

    def test_other_name_not_polluted_when_self_is_first_sender(self):
        """senders 里自己排第一时，不能把自己当成「对方」"""
        data = {
            "chatInfo": {"name": "会话", "selfName": "我"},  # 故意缺 selfUid
            "statistics": {"senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]},
            "messages": [self.GOOD],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.other_name, "对方")


class TestCancelActuallyStops(unittest.TestCase):
    """取消必须真的止住未开始的月份，否则"可随时取消"是假的、剩余月份照常计费"""

    @staticmethod
    def _months(n):
        return {
            f"2025-{m:02d}": [
                _msg("self", 1735689600000 + (m - 1) * 2678400000 + i * 60000) for i in range(3)
            ]
            for m in range(1, n + 1)
        }

    def test_cancel_after_first_month_stops_remaining(self):
        import analyzer.deepseek_client as dc

        calls = []
        flag = {"cancel": False}

        def fake_api(*a, **kw):
            calls.append(1)
            time.sleep(0.05)
            return {"self_emotion": "平静", "other_emotion": "平静"}

        def on_progress(done, total):
            if done >= 1:
                flag["cancel"] = True

        with (
            mock.patch.object(dc, "_call_api", side_effect=fake_api),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            res = dc._analyze_periods(
                self._months(8),
                "sys",
                lambda p, m: "prompt",
                max_tokens=1024,
                tag="emotion",
                on_progress=on_progress,
                should_cancel=lambda: flag["cancel"],
            )
        self.assertLess(len(calls), 8, "取消后不应继续调用剩余月份")
        self.assertLessEqual(len(calls), dc.CONCURRENCY, "只应跑完并发窗口内的任务")
        self.assertLess(len(res), 8)

    def test_cancel_before_start_makes_no_calls(self):
        import analyzer.deepseek_client as dc

        calls = []
        with (
            mock.patch.object(dc, "_call_api", side_effect=lambda *a, **kw: calls.append(1)),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            res = dc._analyze_periods(
                self._months(5),
                "sys",
                lambda p, m: "prompt",
                max_tokens=1024,
                tag="emotion",
                should_cancel=lambda: True,
            )
        self.assertEqual(calls, [])
        self.assertEqual(res, {})

    def test_without_cancel_all_months_run(self):
        import analyzer.deepseek_client as dc

        calls = []
        with (
            mock.patch.object(dc, "_call_api", side_effect=lambda *a, **kw: calls.append(1) or {"x": 1}),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            res = dc._analyze_periods(
                self._months(4), "sys", lambda p, m: "prompt", max_tokens=1024, tag="emotion"
            )
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(res), 4)


class TestJobOverlapGuard(unittest.TestCase):
    """全量任务与单维度任务重叠时拒绝，而不是把同一维度分析两遍（重复计费）"""

    def setUp(self):
        import app as appmod

        # 这些用例只关心任务编排，不能依赖本机 .env 是否配了 API Key（CI 没有 .env）
        self._patches = [
            mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True),
            mock.patch("webapp.api.is_api_configured", return_value=True),
        ]
        for p in self._patches:
            p.start()
        self.client = appmod.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as sess:
            self.token = sess["csrf_token"]
        payload = json.dumps(
            {
                "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
                "statistics": {
                    "senders": [{"uid": "u_self", "name": "我"}, {"uid": "u_other", "name": "对方"}]
                },
                "messages": [
                    {
                        "id": "1",
                        "timestamp": 1704067200000,
                        "time": "2024-01-01 08:00:00",
                        "sender": {"uid": "u_self", "name": "我"},
                        "content": "在吗",
                    },
                    {
                        "id": "2",
                        "timestamp": 1704067260000,
                        "time": "2024-01-01 08:01:00",
                        "sender": {"uid": "u_other", "name": "对方"},
                        "content": "在的",
                    },
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.client.post("/upload", data={"file": (io.BytesIO(payload), "c.json")}, headers=self._headers())

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with self.client.session_transaction() as sess:
            path, chash = sess.get("filepath"), sess.get("chat_hash")
        with jobsmod.JOBS_LOCK:
            jobsmod.JOBS.clear()
        if path and os.path.exists(path):
            os.remove(path)
        if chash:
            storemod._purge_chat_caches(chash)

    def _headers(self):
        return {"Origin": "http://localhost:5000", "X-CSRF-Token": self.token}

    def _fake_running_job(self, dim):
        with self.client.session_transaction() as sess:
            sid, chash = sess.sid, sess.get("chat_hash")
        with jobsmod.JOBS_LOCK:
            jobsmod.JOBS["testjob"] = {
                "status": "running",
                "dim": dim,
                "done": 0,
                "total": 0,
                "cancel": False,
                "chat_hash": chash,
                "sid": sid,
                "created": time.time(),
            }

    def test_single_dimension_rejected_while_all_running(self):
        self._fake_running_job("all")
        r = self.client.post("/api/analyze/emotion", headers=self._headers())
        self.assertEqual(r.status_code, 409)
        self.assertIn("全量", r.get_json()["error"])

    def test_analyze_all_rejected_while_dimension_running(self):
        self._fake_running_job("emotion")
        r = self.client.post("/api/analyze-all", headers=self._headers())
        self.assertEqual(r.status_code, 409)
        self.assertIn("情绪分析", r.get_json()["error"])

    def test_same_dimension_reuses_running_job(self):
        self._fake_running_job("emotion")
        r = self.client.post("/api/analyze/emotion", headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("reused"))

    def test_cache_is_readable_right_after_job_reports_done(self):
        """缓存必须先落盘再置 done，否则前端随后读缓存落空会重新付费分析"""
        with (
            mock.patch(
                "analyzer.deepseek_client._call_api",
                return_value={"self_emotion": "平静", "other_emotion": "平静"},
            ),
            mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True),
            mock.patch("webapp.api.is_api_configured", return_value=True),
        ):
            job = self.client.post("/api/analyze/emotion", headers=self._headers()).get_json()["job"]
            deadline = time.time() + 15
            status = None
            while time.time() < deadline:
                status = self.client.get(f"/api/analyze-job/{job}").get_json()["status"]
                if status in ("done", "error", "cancelled"):
                    break
                time.sleep(0.05)
            self.assertEqual(status, "done")
            cached = self.client.get("/api/analysis/emotion")
            self.assertEqual(cached.status_code, 200)
            self.assertTrue(cached.get_json().get("cached"))


class TestCacheAtomicity(unittest.TestCase):
    """缓存写入必须原子（临时文件 + replace），命中要续期 mtime"""

    def test_write_cache_leaves_no_tmp_and_is_readable(self):
        try:
            storemod._write_cache("emotion", "hashAtomic", {"a": 1})
            path = storemod._cache_path("emotion", "hashAtomic")
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(storemod._read_cache("emotion", "hashAtomic"), {"a": 1})
        finally:
            storemod._purge_chat_caches("hashAtomic")

    def test_cache_hit_refreshes_mtime(self):
        """天天用的缓存不该在 30 天后因 mtime 过期被清理掉"""
        try:
            storemod._write_cache("topics", "hashTtl", {"a": 1})
            path = storemod._cache_path("topics", "hashTtl")
            old = time.time() - 40 * 86400
            os.utime(path, (old, old))
            self.assertLess(os.path.getmtime(path), time.time() - 30 * 86400)
            self.assertIsNotNone(storemod._read_cache("topics", "hashTtl"))
            self.assertGreater(os.path.getmtime(path), time.time() - 60)
        finally:
            storemod._purge_chat_caches("hashTtl")


class TestLoginThrottle(unittest.TestCase):
    """绑定局域网时口令不能无限爆破"""

    def setUp(self):
        import app as appmod

        self.client = appmod.app.test_client()
        with securitymod._login_lock:
            securitymod._login_failures.clear()

    def tearDown(self):
        with securitymod._login_lock:
            securitymod._login_failures.clear()

    def test_repeated_failures_are_throttled(self):
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            for _ in range(securitymod.LOGIN_MAX_ATTEMPTS):
                r = self.client.post("/login", data={"password": "wrong"})
                self.assertEqual(r.status_code, 200)  # 正常渲染错误提示
            r = self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(r.status_code, 429)
            # 即使口令正确，窗口内也照样拒绝（避免爆破成功）
            r = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 429)

    def test_success_clears_failures(self):
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            self.client.post("/login", data={"password": "wrong"})
            r = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 302)
            with securitymod._login_lock:
                self.assertEqual(securitymod._login_failures, {})

    def test_retry_after_header_and_actionable_message(self):
        """被限流时要告诉客户端还能等多久（Retry-After），页面提示也用同一份秒数"""
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            for _ in range(securitymod.LOGIN_MAX_ATTEMPTS):
                self.client.post("/login", data={"password": "wrong"})
            r = self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(r.status_code, 429)
            retry = r.headers.get("Retry-After")
            self.assertIsNotNone(retry, "429 必须带 Retry-After")
            self.assertGreaterEqual(int(retry), 1)
            self.assertLessEqual(int(retry), securitymod.LOGIN_WINDOW_SECONDS)
            # 页面提示里的秒数与 Retry-After 一致，别让用户猜"稍后"是多久
            self.assertIn(f"请 {retry} 秒后再试", r.get_data(as_text=True))

    def test_window_expiry_lifts_throttle(self):
        """窗口滑过去之后必须自动解除，否则被锁的人只能重启服务"""
        from webapp import security as securitymod

        clock = [1_000_000.0]
        with (
            mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"),
            mock.patch.object(securitymod, "_now", lambda: clock[0]),
        ):
            for _ in range(securitymod.LOGIN_MAX_ATTEMPTS):
                self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(self.client.post("/login", data={"password": "s3cret"}).status_code, 429)

            # 还没到窗口边缘：仍然拒绝（边界内）
            clock[0] += securitymod.LOGIN_WINDOW_SECONDS - 1
            self.assertEqual(self.client.post("/login", data={"password": "s3cret"}).status_code, 429)

            # 越过窗口：限流解除，正确口令可以登录
            clock[0] += 2
            r = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 302)

    def test_throttle_is_per_ip(self):
        """限流按客户端地址计：一个人被锁不该把别人一起锁住"""
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            for _ in range(securitymod.LOGIN_MAX_ATTEMPTS):
                self.client.post(
                    "/login", data={"password": "wrong"}, environ_base={"REMOTE_ADDR": "10.0.0.9"}
                )
            blocked = self.client.post(
                "/login", data={"password": "s3cret"}, environ_base={"REMOTE_ADDR": "10.0.0.9"}
            )
            self.assertEqual(blocked.status_code, 429)
            # 同一时刻的另一个地址不受影响
            other = self.client.post(
                "/login", data={"password": "s3cret"}, environ_base={"REMOTE_ADDR": "10.0.0.10"}
            )
            self.assertEqual(other.status_code, 302)

    def test_limit_is_configurable(self):
        """上限/窗口来自 config，可被环境变量覆盖（反代/NAT 共享地址时要能调大）"""
        from webapp import security as securitymod
        from config import LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS

        self.assertGreaterEqual(LOGIN_MAX_ATTEMPTS, 1)
        self.assertGreaterEqual(LOGIN_WINDOW_SECONDS, 10)

        with (
            mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"),
            mock.patch.object(securitymod, "LOGIN_MAX_ATTEMPTS", 1),
        ):
            self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(self.client.post("/login", data={"password": "wrong"}).status_code, 429)

    def test_concurrent_attempts_cannot_exceed_the_limit(self):
        """判超限与记下尝试必须在**同一把锁内**完成。

        反向验证：把 _register_login_attempt 换回"先 _login_throttle_ok、
        再 _record_login_failure"的两步写法，本条立刻变红——那种写法下所有并发请求
        都会看到"还没到上限"，于是放行的次数等于并发数而不是上限。
        """
        from webapp import security as securitymod

        attempts = securitymod.LOGIN_MAX_ATTEMPTS + 8
        barrier = threading.Barrier(attempts)
        outcomes: list[int] = []
        collect_lock = threading.Lock()

        def one_attempt():
            barrier.wait()  # 让所有线程尽量同时进入
            allowed = 0 if securitymod._register_login_attempt("198.51.100.99") == 0 else 1
            with collect_lock:
                outcomes.append(allowed)

        threads = [threading.Thread(target=one_attempt) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            outcomes.count(0),
            securitymod.LOGIN_MAX_ATTEMPTS,
            "并发下放行的尝试次数必须正好等于上限",
        )

    def test_failure_table_is_bounded(self):
        """大量陌生地址扫描时，失败记录表必须有硬上限（否则内存随 IP 数无限涨）"""
        from webapp import security as securitymod

        clock = [1_000_000.0]
        with mock.patch.object(securitymod, "_now", lambda: clock[0]):
            for i in range(securitymod._LOGIN_FAILURES_MAX + 500):
                securitymod._record_login_failure(f"203.0.113.{i % 256}-{i}")
            with securitymod._login_lock:
                self.assertLessEqual(len(securitymod._login_failures), securitymod._LOGIN_FAILURES_MAX)
            # 淘汰的是最旧的条目：最近失败过的地址仍在表里，仍然被限流
            for _ in range(securitymod.LOGIN_MAX_ATTEMPTS):
                securitymod._record_login_failure("198.51.100.7")
            self.assertFalse(securitymod._login_throttle_ok("198.51.100.7"))

    def test_write_cache_survives_a_deleted_cache_dir(self):
        """README 教用户"删掉 ai_cache/ 即可彻底清除数据"，而服务可能还开着。

        缓存目录被删后写入方必须自愈：不补目录的后果不是"少一个文件"，而是此后
        **每一次**写入都静默失败——用户以为在命中缓存，实际每个月、每个维度都在
        重复付费，界面上完全看不出来。
        """
        import shutil

        storemod._write_cache("emotion", "hashNoDir", {"a": 1})
        shutil.rmtree(storemod.AI_CACHE_DIR, ignore_errors=True)
        self.assertFalse(os.path.isdir(storemod.AI_CACHE_DIR), "前提：目录确实没了")
        try:
            storemod._write_cache("emotion", "hashNoDir", {"a": 2})
            path = storemod._cache_path("emotion", "hashNoDir")
            self.assertTrue(os.path.exists(path), "缓存目录被删后必须自动重建")
            self.assertEqual(storemod._read_cache("emotion", "hashNoDir"), {"a": 2})
        finally:
            storemod._purge_chat_caches("hashNoDir")

    def test_month_cache_survives_a_deleted_cache_dir(self):
        """月份缓存与 manifest 同理：写不进去就等于增量分析整体失效。"""
        import shutil

        from analyzer import deepseek_client as dc

        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "ai_cache")
            dc.configure_month_cache(cache)
            try:
                dc._write_month_cache("nodir0000001", {"x": 1})
                dc._record_month_usage("hashNoDirChat", ["nodir0000001"])
                shutil.rmtree(cache, ignore_errors=True)

                dc._write_month_cache("nodir0000002", {"x": 2})
                dc._record_month_usage("hashNoDirChat", ["nodir0000002"])

                self.assertTrue(os.path.isdir(cache), "月份缓存目录必须自愈重建")
                self.assertEqual(dc._read_month_cache("nodir0000002"), {"x": 2})
                self.assertIn("nodir0000002", json.load(open(dc._manifest_path("hashNoDirChat")))["months"])
            finally:
                dc.configure_month_cache("")


class TestExpiredSessionResponses(unittest.TestCase):
    """会话过期时，API 与页面要走不同的响应形态——混用会让前端"看起来像服务坏了"。

    反向验证：把 security.require_login 里的 wants_json() 分支删掉，
    前两条用例立刻变红（它们会拿到 302 而不是 401）。
    """

    def setUp(self):
        import app as appmod

        self.client = appmod.app.test_client()

    def test_api_get_with_expired_session_returns_401_json(self):
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = self.client.get("/api/analyze-job/deadbeef")
        self.assertEqual(r.status_code, 401, "API 不能回 302：jQuery 会静默跟随拿到一页登录表单")
        self.assertNotEqual(r.status_code, 302)
        body = r.get_json()
        self.assertTrue(body.get("auth") is False)
        self.assertIn("刷新", body.get("error", ""))

    def test_api_post_with_expired_session_returns_401_json(self):
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = self.client.post("/api/analyze/emotion")
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.get_json())

    def test_ajax_upload_with_expired_session_returns_401_json(self):
        """上传页的表单提交是 fetch + X-Requested-With，同样不该收到 HTML 登录页"""
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = self.client.post("/upload", headers={"X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 401)

    def test_plain_page_request_still_redirects_to_login(self):
        """浏览器直接访问页面时仍应 302 到登录页——这条不能被上面的改动误伤"""
        from webapp import security as securitymod

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))


class TestLogout(unittest.TestCase):
    """登出：只接受 POST + CSRF，且真的同时清掉服务端会话与浏览器 cookie

    反向验证：把 security.logout 里的 session.clear() 删掉 → 第一条红；
    把路由的 methods 改成 ["GET", "POST"] → 第二条红。
    """

    def setUp(self):
        import app as appmod

        self.client = appmod.app.test_client()
        with securitymod._login_lock:
            securitymod._login_failures.clear()

    def tearDown(self):
        with securitymod._login_lock:
            securitymod._login_failures.clear()

    def _login(self) -> str:
        """登录并返回页面上真实注入的 CSRF token。

        刻意不用 client.session_transaction() 取 token：登录态下它读到的是空会话，
        退出上下文时还会把这份空会话**回写**，于是后续请求变成未登录——
        这正是本用例最初踩到的假失败。像浏览器一样从页面里取，不给会话加副作用。
        """
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            resp = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(resp.status_code, 302, "前提：先登录成功")
            page = self.client.get("/")
        self.assertEqual(page.status_code, 200, "前提：登录后能打开首页")
        matched = re.search(r"window\.CSRF_TOKEN = \"([0-9a-f]+)\"", page.get_data(as_text=True))
        self.assertIsNotNone(matched, "登录后的页面必须带上 CSRF token（base.html 注入）")
        return matched.group(1)

    def test_post_logout_clears_session_and_cookie(self):
        token = self._login()
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            resp = self.client.post("/logout", data={"csrf_token": token})
            self.assertEqual(resp.status_code, 302)
            set_cookie = resp.headers.get("Set-Cookie", "")
            self.assertIn("session=", set_cookie, "登出要显式让浏览器丢弃会话 cookie")
            self.assertIn("Max-Age=0", set_cookie)
            # 首页只看登录态（/dashboard 还会因"没有统计数据"而跳首页，不适合当探针）
            after = self.client.get("/")
        self.assertEqual(after.status_code, 302, "登出后受保护页面必须重新要求登录")
        self.assertIn("/login", after.headers.get("Location", ""))

    def test_get_logout_is_rejected(self):
        """登出是改状态的操作：GET 能被任意第三方页面的 <img src="/logout"> 触发"""
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", ""):
            resp = self.client.get("/logout")
        self.assertEqual(resp.status_code, 405)

    def test_logout_without_csrf_is_rejected(self):
        self._login()
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            resp = self.client.post("/logout")
            self.assertEqual(resp.status_code, 400)
            # 用首页判定登录态：/dashboard 在没有统计数据时会按设计跳回首页（302），
            # 拿它当"是否还登录着"的探针会得出错误结论。
            still_in = self.client.get("/")
        self.assertEqual(still_in.status_code, 200, "CSRF 失败时不该真的把登录态清掉")

    def test_logout_button_appears_only_when_password_is_set(self):
        plain = self.client.get("/")
        self.assertNotIn("退出登录", plain.get_data(as_text=True), "没设口令时导航栏不该出现退出按钮")

        self._login()
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            page = self.client.get("/")
        self.assertIn("退出登录", page.get_data(as_text=True))
        self.assertIn('action="/logout"', page.get_data(as_text=True))


class TestShutdownSignal(unittest.TestCase):
    """中断信号：第一次留 grace 让进行中的月份收尾，第二次立即退出

    反向验证：把 app._on_shutdown_signal 里 `if not request_shutdown()` 那个分支删掉，
    第二条用例立刻变红（它会再睡满一个 grace，用户看起来像"按了没反应"）。
    """

    def setUp(self):
        from analyzer import shutdown as shutdownmod

        shutdownmod.reset_shutdown()

    def tearDown(self):
        from analyzer import shutdown as shutdownmod

        shutdownmod.reset_shutdown()

    def test_first_signal_sets_flag_waits_then_interrupts(self):
        import app as appmod
        from analyzer import shutdown as shutdownmod

        with (
            mock.patch.object(appmod, "SHUTDOWN_GRACE_SECONDS", 5.0),
            mock.patch.object(appmod.time, "sleep") as slept,
            mock.patch.object(appmod, "flush_usage") as flushed,
        ):
            with self.assertRaises(KeyboardInterrupt):
                appmod._on_shutdown_signal(signal.SIGINT, None)
            slept.assert_called_once_with(5.0)
            flushed.assert_called_once()
        self.assertTrue(shutdownmod.shutdown_requested(), "第一次信号必须置位标志（停止派发新调用）")

    def test_second_signal_exits_without_waiting(self):
        import app as appmod
        from analyzer import shutdown as shutdownmod

        shutdownmod.request_shutdown()  # 模拟"用户已经按过一次"
        with (
            mock.patch.object(appmod, "SHUTDOWN_GRACE_SECONDS", 5.0),
            mock.patch.object(appmod.time, "sleep") as slept,
            mock.patch.object(appmod, "flush_usage") as flushed,
        ):
            with self.assertRaises(KeyboardInterrupt):
                appmod._on_shutdown_signal(signal.SIGINT, None)
            slept.assert_not_called()
            flushed.assert_called_once()  # 用量仍要落盘：这次是真的要走了

    def test_zero_grace_skips_waiting_entirely(self):
        """QQCHAT_SHUTDOWN_GRACE_SECONDS=0 是既有的"按下就退出"退路，不能被改坏"""
        import app as appmod

        with (
            mock.patch.object(appmod, "SHUTDOWN_GRACE_SECONDS", 0.0),
            mock.patch.object(appmod.time, "sleep") as slept,
        ):
            with self.assertRaises(KeyboardInterrupt):
                appmod._on_shutdown_signal(signal.SIGTERM, None)
            slept.assert_not_called()


class TestHardeningMisc(unittest.TestCase):
    def test_session_cookie_flags(self):
        import app as appmod

        self.assertTrue(appmod.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(appmod.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_logger_handlers_configured_once(self):
        """handler 只挂包级 logger：多个 logger 抢同一文件会让轮转在 Windows 上失败"""
        import analyzer.logger as L

        base = L.get_logger()
        app_logger = L.get_logger("app")
        ds_logger = L.get_logger("deepseek")
        self.assertEqual(app_logger.handlers, [])
        self.assertEqual(ds_logger.handlers, [])
        self.assertTrue(app_logger.name.startswith(base.name))
        from logging.handlers import TimedRotatingFileHandler

        file_handlers = [h for h in base.handlers if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)


class TestTemplatesCompile(unittest.TestCase):
    """所有模板必须能编译：改版时漏掉/多出 {% block %} 会让整页 500"""

    def test_all_templates_compile(self):
        import app as appmod

        tpl_dir = Path(appmod.app.template_folder)
        if not tpl_dir.is_absolute():
            tpl_dir = Path(appmod.app.root_path) / tpl_dir
        names = sorted(p.name for p in tpl_dir.glob("*.html"))
        self.assertTrue(names, "未找到模板文件")
        for name in names:
            with self.subTest(template=name):
                appmod.app.jinja_env.get_template(name)


class TestFrontendEscaping(unittest.TestCase):
    """聊天内容与模型输出进入 DOM 前必须转义

    覆盖过一次真实缺陷：情绪折线图的 ECharts tooltip 直接把模型返回的
    self_emotion/other_emotion 拼进 HTML（tooltip 默认按 HTML 渲染），
    而这两个字段受聊天内容影响，等于给"聊天记录 → 页面 XSS"留了通道。
    """

    CHARTS = Path(__file__).resolve().parent.parent / "web" / "static" / "js" / "charts.js"

    def test_emotion_tooltip_escapes_model_output(self):
        src = self.CHARTS.read_text(encoding="utf-8")
        self.assertIn("esc(selfEmotions[idx])", src)
        self.assertIn("esc(otherEmotions[idx])", src)
        self.assertNotIn("+ selfEmotions[idx] +", src)
        self.assertNotIn("+ otherEmotions[idx]", src)

    def test_esc_covers_html_metacharacters(self):
        src = self.CHARTS.read_text(encoding="utf-8")
        body = src.split("function esc(s)")[1].split("\n}")[0]
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(entity, body, f"esc() 缺少 {entity} 转义")


if __name__ == "__main__":
    unittest.main(verbosity=2)
