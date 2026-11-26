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

import importlib
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402
from result_fixtures import emotion as valid_emotion  # noqa: E402

bootstrap()
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 专门验证增量缓存的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 别名与项目模块的导入都必须排在环境准备之后：提前导入 webapp.* 会连带导入 config，
# 于是 QQCHAT_MONTH_CACHE / QQCHAT_DATA_DIR 被读成默认值（月份缓存意外开启）
import app as appmod  # noqa: E402
import config  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import shutdown as sd  # noqa: E402
from analyzer import usage as usage_mod  # noqa: E402
from analyzer.local_stats import calc_overview  # noqa: E402
from parser.qq_parser import CST, ChatData, Message, load_chat, split_by_month  # noqa: E402
from webapp import jobs as jobsmod  # noqa: E402
from webapp import security as securitymod  # noqa: E402
from webapp import store as storemod  # noqa: E402


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
            return valid_emotion()

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
            mock.patch.object(
                dc, "_call_api", side_effect=lambda *a, **kw: calls.append(1) or valid_emotion()
            ),
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
        from webapp.analysis_scope import scope_identity

        with self.client.session_transaction() as sess:
            sid, chash = sess.sid, sess.get("chat_hash")
            identity = scope_identity(sess.get("analysis_scope"))
        with jobsmod.JOBS_LOCK:
            jobsmod.JOBS["testjob"] = {
                "status": "running",
                "dim": dim,
                "done": 0,
                "total": 0,
                "cancel": False,
                "chat_hash": chash,
                "sid": sid,
                "identity": identity,
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
                return_value=valid_emotion(),
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

    def test_purge_matches_chat_hash_as_a_whole_segment(self):
        """隐私删除路径必须整段匹配，不能靠子串

        子串匹配两个方向都能错：漏删（命名格式一变、子串不再出现，含聊天内容摘要的
        缓存留在盘上，而用户以为已经清干净）与误删（顺手删掉另一个聊天的缓存）。
        """
        target = "0123456789abcdef"
        self.assertTrue(storemod._cache_belongs_to(f"emotion_{target}_deepseek-flash_abc123.json", target))
        self.assertTrue(storemod._cache_belongs_to(f"vision_{target}_deadbeef.json", target))
        self.assertFalse(
            storemod._cache_belongs_to(f"emotion_x{target}y_deepseek-flash_abc123.json", target),
            "哈希只是别人文件名的一部分时不该被删",
        )
        self.assertFalse(
            storemod._cache_belongs_to("emotion_fedcba9876543210_deepseek-flash_abc123.json", target),
            "另一个聊天的缓存不该被删",
        )

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
                manifest = json.load(io.open(dc._manifest_path("hashNoDirChat"), encoding="utf-8"))
                self.assertIn("nodir0000002", manifest["months"])
            finally:
                dc.configure_month_cache("")

    def test_stats_write_survives_the_cache_path_being_a_file(self):
        """`stats_cache/` 那个位置被一个同名文件占住时：记一条日志就算了，不许抛给请求层

        与上面两条同一个主题（README 教用户直接删这些目录），但失败形态不同：目录删掉
        可以自愈重建，位置被文件占住则**建不出来**。这条路径曾在 try 外面
        （makedirs 先于 try），FileExistsError 会一路穿到请求层，表现为"统计页 500"——
        与真实原因毫不相干的症状。

        反向验证：把 _save_stats 里的 os.makedirs 挪回 try 外面，本条立刻变红。
        """
        from webapp import store as storemod

        with tempfile.TemporaryDirectory() as d:
            blocked = os.path.join(d, "stats_cache")
            with open(blocked, "w", encoding="utf-8") as f:
                f.write("这个位置被一个文件占住了")
            with mock.patch.object(storemod, "STATS_CACHE_DIR", blocked):
                try:
                    storemod._save_stats("hashBlocked", {"overview": {"a": 1}})
                except OSError as e:  # pragma: no cover - 失败即说明修复被回退
                    self.fail(f"写统计缓存失败时不该把异常抛给调用方/请求层：{e}")
            self.assertTrue(os.path.isfile(blocked), "我们不该为了写入去删掉用户占住这个位置的文件")


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

    def test_every_json_endpoint_is_registered_under_the_api_prefix(self):
        """返回 JSON 的端点必须挂在 /api/ 下，否则会话过期时会退化成 302

        为什么值得一条守卫：上面那三条 401 用例靠的是 `wants_json()` 判"这个请求
        期望 JSON"，而它对陌生客户端只有一个依据——**路径前缀**（`Accept` 与
        `X-Requested-With` 都不能指望脚本带）。于是"新加的 JSON 端点写在 /api/ 之外"
        就是一颗静默的雷：功能全都正常，只有会话过期时才发作，而发作的样子是
        "轮询拿着 undefined 一直轮下去"（见 security.wants_json 的注释）。
        这里把这条不变量变成 CI 能发现的事，而不是等下一次排查。

        判据取"视图函数定义在 webapp/api.py 里"，而不是给每个端点打标记：标记会忘，
        定义文件不会。下面那条计数断言保证过滤条件真的匹配到了东西——
        万一将来 Flask 把视图包一层、`getsourcefile` 不再指向 api.py，这里会**响亮
        地红**，而不是安静地一条都不查。
        """
        import inspect

        import app as appmod
        from webapp import api as apimod

        api_file = os.path.abspath(inspect.getsourcefile(apimod))
        checked = []
        for rule in appmod.app.url_map.iter_rules():
            view = appmod.app.view_functions[rule.endpoint]
            source = inspect.getsourcefile(view) or ""
            if os.path.abspath(source) != api_file:
                continue
            checked.append(rule.rule)
            with self.subTest(endpoint=rule.endpoint, path=rule.rule):
                self.assertTrue(
                    rule.rule.startswith("/api/"),
                    f"webapp/api.py 的端点 {rule.endpoint} 注册在 {rule.rule}：JSON 端点必须在"
                    " /api/ 下，否则会话过期时它拿到的是 302（前端会静默无限轮询）而不是 401",
                )
        self.assertGreaterEqual(
            len(checked),
            8,
            f"只匹配到 {len(checked)} 个 api 模块的端点：守卫的过滤条件可能失效了"
            "（预期是 /api/ 下的全部 JSON 端点）",
        )


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

    def test_post_logout_invalidates_the_old_server_side_session(self):
        """登出必须让旧 sid **连服务端存储一起**作废，而不只是清空内容

        反向验证：把 security.logout 里的 _regenerate_session_id() 去掉，第一条断言
        立刻变红（旧 sid 的存储在盘上原样躺着，直到 24 小时回收——用户点了"退出登录"，
        却指望不了"这个会话从此不存在"）。
        """
        import app as appmod

        token = self._login()
        cookie_name = appmod.app.config.get("SESSION_COOKIE_NAME") or "session"
        iface = appmod.app.session_interface
        old_cookie = self.client.get_cookie(cookie_name)
        self.assertIsNotNone(old_cookie, "前提：登录后浏览器手上有会话 cookie")
        old_sid = self.client.get_cookie(cookie_name).value
        # 会话 cookie 没开签名（app.py 未设 SESSION_USE_SIGNER），cookie 值就是 sid
        old_store_id = iface._get_store_id(old_sid)
        self.assertIsNotNone(
            iface.cache.get(old_store_id), "前提：登录后服务端确实存着这份会话（否则下面验了个空）"
        )

        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            resp = self.client.post("/logout", data={"csrf_token": token})
            self.assertEqual(resp.status_code, 302)
            self.assertIsNone(
                iface.cache.get(old_store_id),
                "登出必须删掉旧 sid 的服务端存储：只清内容的话，那份文件还要在盘上躺到回收",
            )
            # 首页只看登录态（/dashboard 还会因"没有统计数据"而跳首页，不适合当探针）
            after = self.client.get("/")
        self.assertEqual(after.status_code, 302, "登出后受保护页面必须重新要求登录")
        self.assertIn("/login", after.headers.get("Location", ""))
        new_cookie = self.client.get_cookie(cookie_name)
        self.assertIsNotNone(new_cookie, "登出后应当拿到一个新的匿名会话 cookie")
        self.assertNotEqual(new_cookie.value, old_sid, "登出应当轮换 sid，而不是留着同一个")

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


def _r4_msg(uid: str, ts: int, text: str = "在吗", has_image: bool = False, **kw) -> Message:
    return Message(
        id=str(ts),
        timestamp=ts,
        time_str=datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=uid,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=has_image,
        is_reply=False,
        **kw,
    )


def _r4_chat(msgs) -> ChatData:
    return ChatData(chat_name="", self_name="A", other_name="B", self_uid="u1", other_uid="u2", messages=msgs)


def _r4_months_chat(n_months: int, pairs_per_month: int = 6) -> ChatData:
    """跨 n 个月的聊天（每月内容互不相同）"""
    msgs = []
    for m in range(n_months):
        year, month = 2025 + m // 12, (m % 12) + 1
        for i in range(pairs_per_month):
            t = datetime(year, month, 5, 20, 0, tzinfo=CST) + timedelta(minutes=17 * i)
            text = f"{year}-{month:02d} 第{i}条消息"
            msgs.append(_r4_msg("u1" if i % 2 else "u2", int(t.timestamp() * 1000), text=text))
    return _r4_chat(msgs)


class TestOpenRedirect(unittest.TestCase):
    """登录后的 next= 跳转不能把用户带出站外"""

    def setUp(self):
        self.client = appmod.app.test_client()
        self.client.get("/")

    def _login(self, nxt):
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            return self.client.post(f"/login?next={nxt}", data={"password": "s3cret"})

    def test_backslash_protocol_relative_is_rejected(self):
        r = self._login("/\\evil.example.com")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn(
            "evil.example.com", r.headers["Location"], "浏览器把 /\\host 当协议相对地址，必须拦住"
        )
        self.assertEqual(r.headers["Location"], "/")

    def test_double_slash_is_rejected(self):
        self.assertEqual(self._login("//evil.example.com").headers["Location"], "/")

    def test_absolute_url_is_rejected(self):
        self.assertEqual(self._login("http://evil.example.com/x").headers["Location"], "/")

    def test_local_path_is_kept(self):
        self.assertEqual(self._login("/dashboard").headers["Location"], "/dashboard")


class TestSecurityHeaders(unittest.TestCase):
    """统一安全响应头：即便将来某处漏了转义，注入也拿不到跨站资源"""

    def test_headers_present_on_pages(self):
        client = appmod.app.test_client()
        r = client.get("/")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(r.headers.get("Referrer-Policy"), "same-origin")
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'self'", csp)


class TestUploadLimitIsConfigurable(unittest.TestCase):
    """超长聊天的 JSON 会逼近 50MB：上限要能调，且默认值不变"""

    def test_default_is_50mb(self):
        self.assertEqual(config.MAX_CONTENT_LENGTH, 50 * 1024 * 1024)
        self.assertEqual(
            appmod.app.config["MAX_CONTENT_LENGTH"], config.MAX_CONTENT_LENGTH, "配置要真的接到 Flask 上"
        )

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"QQCHAT_MAX_UPLOAD_MB": "120"}):
            reloaded = importlib.reload(config)
            try:
                self.assertEqual(reloaded.MAX_CONTENT_LENGTH, 120 * 1024 * 1024)
            finally:
                os.environ.pop("QQCHAT_MAX_UPLOAD_MB", None)
                importlib.reload(config)
        self.assertEqual(config.MAX_CONTENT_LENGTH, 50 * 1024 * 1024)

    def test_413_returns_actionable_chinese_hint(self):
        """超过上限时不能只回 Werkzeug 的英文页：用户得知道限制是多少、怎么调"""
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": token}
        payload = b"x" * 4096
        # 把上限压到 1KB，避免测试真的上传 50MB
        original = appmod.app.config["MAX_CONTENT_LENGTH"]
        appmod.app.config["MAX_CONTENT_LENGTH"] = 1024
        try:
            # AJAX 上传（首页拖拽）：应拿到 JSON 形式的可读错误
            r = client.post(
                "/upload",
                data={"file": (io.BytesIO(payload), "big.json")},
                headers={**headers, "X-Requested-With": "fetch"},
            )
            self.assertEqual(r.status_code, 413)
            self.assertIn("QQCHAT_MAX_UPLOAD_MB", r.get_json().get("error", ""))

            # 普通表单提交：也应看到中文提示而不是英文模板
            r2 = client.post("/upload", data={"file": (io.BytesIO(payload), "big.json")}, headers=headers)
            self.assertEqual(r2.status_code, 413)
            body = r2.get_data(as_text=True)
            self.assertIn("上限", body)
            self.assertNotIn("Request Entity Too Large", body)
        finally:
            appmod.app.config["MAX_CONTENT_LENGTH"] = original


class TestInsecureBaseUrlDetection(unittest.TestCase):
    """明文 http 的非本机端点会让 Key 与聊天内容裸奔，启动时就该提醒"""

    def test_detection(self):
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "http://api.example.com/v1"):
            self.assertTrue(dc.is_insecure_base_url())
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "http://127.0.0.1:11434/v1"):
            self.assertFalse(dc.is_insecure_base_url(), "本机明文端点不算裸奔")
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "https://api.example.com/v1"):
            self.assertFalse(dc.is_insecure_base_url())


class TestChatCachePurge(unittest.TestCase):
    """删聊天时进程内的 ChatData 也要丢：它带着上一条聊天的全部消息内容"""

    def test_purge_drops_in_memory_chat(self):
        payload = {
            "chatInfo": {"name": "对方", "selfName": "我", "selfUid": "uA"},
            "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}]},
            "messages": [
                {
                    "id": "1",
                    "timestamp": 1704067191000,
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "在吗",
                        "elements": [{"type": "text", "data": {"text": "在吗"}}],
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="qqchatlog-chatcache-") as tmp:
            src = os.path.join(tmp, "c.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with storemod._CHAT_CACHE_LOCK:
                storemod._CHAT_CACHE.clear()
            self.addCleanup(storemod._CHAT_CACHE.clear)

            storemod._load_chat_cached(src)
            self.assertEqual(len(storemod._CHAT_CACHE), 1)
            storemod._purge_chat_caches("some-other-hash")
            self.assertEqual(len(storemod._CHAT_CACHE), 0, "清理后进程内不该还留着已解析的聊天内容")


class TestJobSurvivesFileCleanup(unittest.TestCase):
    """后台任务开工时会话文件可能已被 24 小时清理收走：要给可读提示，不能是 Errno 2"""

    def _register(self, job_id: str) -> None:
        jobsmod.JOBS[job_id] = {
            "status": "running",
            "dim": "emotion",
            "done": 0,
            "total": 0,
            "cancel": False,
            "chat_hash": "deadbeefcafe0000",
            "sid": "sess",
            "created": 0.0,
        }
        self.addCleanup(jobsmod.JOBS.pop, job_id, None)

    def test_run_job_reports_cleanup_not_filenotfound(self):
        job_id = "job-missing-file"
        self._register(job_id)
        missing = os.path.join(tempfile.gettempdir(), "definitely-not-here.json")
        jobsmod._run_job(job_id, "emotion", missing, "deadbeefcafe0000")

        job = jobsmod.JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("已被清理", job["error"])
        self.assertNotIn("No such file", job["error"])
        self.assertNotIn("Errno", job["error"])

    def test_run_selected_plan_reports_cleanup(self):
        job_id = "job-all-missing-file"
        self._register(job_id)
        missing = os.path.join(tempfile.gettempdir(), "definitely-not-here-either.json")
        plan = {
            "dimensions": ["emotion"],
            "units": [{"key": "emotion:month:2024-01", "dimension": "emotion", "status": "待调用"}],
        }
        jobsmod._run_selected_plan(
            job_id, missing, "deadbeefcafe0000", False, plan, ["emotion:month:2024-01"]
        )

        job = jobsmod.JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("已被清理", job["error"])

    def test_run_boundary_is_released_on_every_exit_path(self):
        """begin_run/end_run 必须成对：漏掉的后果是 LLM_MAX_CALLS_PER_RUN 变成
        「进程启动以来的累计」，之后每次 begin_run 都不再清零，正常分析也会被掐断。

        反向验证：删掉 _run_job（或 _run_selected_plan）finally 里的 end_run()，
        本条立刻变红。这里刻意走「文件已被清理」这条**提前 return** 的出口——
        它是 finally 最容易被漏掉的那一类路径。

        断言用"相对进入前"的口径而不是绝对值：这个计数器是进程级的，别的用例可能
        已经把它留在某个状态上，绝对断言会变成顺序相关的假失败。
        """
        from analyzer import deepseek_client as dc

        depth_before = dc._run_depth
        active_before = dc._run_active
        missing = os.path.join(tempfile.gettempdir(), "definitely-not-here-boundary.json")

        job_id = "job-boundary-single"
        self._register(job_id)
        jobsmod._run_job(job_id, "emotion", missing, "deadbeefcafe0001")
        self.assertEqual(dc._run_depth, depth_before, "_run_job 退出时必须释放自己占用的运行边界")
        self.assertEqual(dc._run_active, active_before, "_run_job 不该把「运行中」粘住")

        job_id = "job-boundary-all"
        self._register(job_id)
        plan = {
            "dimensions": ["emotion"],
            "units": [{"key": "emotion:month:2024-01", "dimension": "emotion", "status": "待调用"}],
        }
        jobsmod._run_selected_plan(
            job_id, missing, "deadbeefcafe0002", False, plan, ["emotion:month:2024-01"]
        )
        self.assertEqual(dc._run_depth, depth_before, "_run_selected_plan 退出时必须释放运行边界")
        self.assertEqual(dc._run_active, active_before, "_run_selected_plan 不该把「运行中」粘住")


class TestSharedRetrySkeleton(unittest.TestCase):
    """_call_api 与 _call_vision 共用 _request_with_retry，错误分类必须一致"""

    def setUp(self):
        self._orig_interval = dc.CALL_MIN_INTERVAL
        dc.CALL_MIN_INTERVAL = 0.0
        self.addCleanup(lambda: setattr(dc, "CALL_MIN_INTERVAL", self._orig_interval))

    @staticmethod
    def _ok():
        resp = mock.Mock()
        resp.choices = [mock.Mock(finish_reason="stop", message=mock.Mock(content='{"a": 1}'))]
        resp.usage = None
        return resp

    @staticmethod
    def _err(status: int, message: str) -> Exception:
        err = Exception(message)
        err.status_code = status
        return err

    def test_both_entry_points_use_the_shared_skeleton(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = self._ok()
        with (
            mock.patch.object(dc, "_get_client", return_value=client),
            mock.patch.object(dc, "_request_with_retry", wraps=dc._request_with_retry) as shared,
        ):
            self.assertEqual(dc._call_api("s", "u"), {"a": 1})
            self.assertEqual(shared.call_count, 1)

        vision_client = mock.Mock()
        vision_client.chat.completions.create.return_value = self._ok()
        with (
            mock.patch.object(dc, "_get_client", return_value=vision_client),
            mock.patch.object(dc, "_request_with_retry", wraps=dc._request_with_retry) as shared,
            mock.patch("analyzer.vision.load_image_b64", return_value="data:image/png;base64,xx"),
        ):
            dc._call_vision("s", "看图", [{"path": "p.png", "mime": "image/png"}])
            self.assertEqual(shared.call_count, 1, "看图调用同样要走公共骨架")

    def test_vision_degrades_where_text_analysis_raises(self):
        """同一类失败：文本分析抛错中止，看图返回空串继续——这是两者的既定分工"""
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = self._err(429, "Allocated quota exceeded")
        with (
            mock.patch.object(dc, "_get_client", return_value=fake),
            mock.patch.object(dc, "TPM_WAIT_SECONDS", 0.01),
            mock.patch("analyzer.vision.load_image_b64", return_value="data:image/png;base64,xx"),
        ):
            with self.assertRaises(dc.QuotaExhaustedError):
                dc._call_api("s", "u", tpm_wait=0.01)
            self.assertEqual(
                dc._call_vision("s", "看图", [{"path": "p.png", "mime": "image/png"}]),
                "",
                "看图失败不该拖垮文本分析",
            )

    def test_generic_errors_keep_the_old_retry_count(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = OSError("connection reset")
        with (
            mock.patch.object(dc, "_get_client", return_value=fake),
            mock.patch.object(dc.time, "sleep"),
        ):
            with self.assertRaises(OSError):
                dc._call_api("s", "u", retry=2)
        self.assertEqual(fake.chat.completions.create.call_count, 3, "retry=2 → 共 3 次尝试")


class TestGracefulShutdown(unittest.TestCase):
    """Ctrl+C 之后不该再发起新的付费调用，也不该把残缺结果写进维度缓存"""

    def setUp(self):
        sd.reset_shutdown()
        self.addCleanup(sd.reset_shutdown)

    def test_no_new_months_after_shutdown(self):
        chat = _r4_months_chat(3)
        calls = []

        def fake_api(*a, **kw):
            calls.append(1)
            return valid_emotion()

        def on_progress(done, total):
            sd.request_shutdown()  # 第一个月一完成就"按下 Ctrl+C"

        with (
            mock.patch.object(dc, "_call_api", side_effect=fake_api),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
            mock.patch.object(dc, "CONCURRENCY", 1),
        ):
            out = dc.analyze_emotion(chat, on_progress=on_progress, chat_hash="")
        self.assertEqual(len(calls), 1, "关闭后不得再启动新的月份")
        self.assertEqual(len(out), 1, "已完成的那一个月仍要返回")

    def test_shutdown_flag_is_one_way_until_reset(self):
        self.assertFalse(sd.shutdown_requested())
        self.assertTrue(sd.request_shutdown(), "第一次请求返回 True（调用方据此打日志）")
        self.assertTrue(sd.shutdown_requested())
        self.assertFalse(sd.request_shutdown(), "重复请求返回 False")
        sd.reset_shutdown()
        self.assertFalse(sd.shutdown_requested())

    def test_partial_dimension_result_is_not_cached_on_shutdown(self):
        filepath = os.path.join(tempfile.gettempdir(), "shutdown-chat.json")
        Path(filepath).write_text("{}", encoding="utf-8")
        self.addCleanup(lambda: os.path.exists(filepath) and os.remove(filepath))
        jobsmod.JOBS["jobSD"] = {"status": "running", "cancel": False}
        self.addCleanup(lambda: jobsmod.JOBS.pop("jobSD", None))
        written = []

        def fake_dim(chat, on_progress=None, should_cancel=None, chat_hash=""):
            sd.request_shutdown()  # 维度跑完一个月就遇到关闭
            return {"2024-01": {"self_emotion": "平静"}}

        with (
            mock.patch.dict(jobsmod.ANALYZE_FUNCS, {"emotion": fake_dim}),
            mock.patch.object(jobsmod.store, "_load_chat_cached", return_value=_r4_months_chat(1)),
            mock.patch.object(jobsmod.store, "_write_cache", side_effect=lambda *a: written.append(a)),
        ):
            jobsmod._run_job("jobSD", "emotion", filepath, "hashSD")

        self.assertEqual(written, [], "残缺的维度结果一旦落盘，重跑会命中它、缺的月份再也补不回来")
        self.assertEqual(jobsmod.JOBS["jobSD"]["status"], "cancelled")

    def test_usage_flush_is_wired_into_the_signal_handler(self):
        """信号处理器必须落盘 token 用量（它按天累计在内存里，硬杀进程就丢了）"""
        import signal

        import app as appmod

        self.assertTrue(hasattr(appmod, "_install_shutdown_handler"), "app.py 应提供优雅关闭的安装函数")
        grace_before = appmod.SHUTDOWN_GRACE_SECONDS
        self.addCleanup(setattr, appmod, "SHUTDOWN_GRACE_SECONDS", grace_before)
        self.addCleanup(signal.signal, signal.SIGINT, signal.default_int_handler)

        with mock.patch.object(appmod, "flush_usage") as flush:
            appmod.SHUTDOWN_GRACE_SECONDS = 0
            appmod._install_shutdown_handler()
            handler = signal.getsignal(signal.SIGINT)
            self.assertTrue(callable(handler))
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
            flush.assert_called_once()
            self.assertTrue(sd.shutdown_requested())

    def test_usage_module_exposes_flush(self):
        self.assertTrue(callable(usage_mod.flush))


class TestHealthEndpoint(unittest.TestCase):
    """存活探针：不登录、不建会话、不写日志"""

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.appmod = appmod
        cls.client = appmod.app.test_client()

    def test_returns_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(as_text=True), "ok")

    def test_bypasses_login(self):
        from webapp import security

        with mock.patch.object(security, "ACCESS_PASSWORD", "s3cret"):
            r = self.client.get("/health")
        self.assertEqual(r.status_code, 200, "设了口令后探针不能被 302 到登录页")

    def test_does_not_create_a_session(self):
        fresh = self.appmod.app.test_client()
        r = fresh.get("/health")
        self.assertIsNone(r.headers.get("Set-Cookie"), "探针不该建会话（高频调用会堆满磁盘）")

    def test_is_not_logged(self):
        """探针可能每秒一次，逐条记日志会把真正有用的日志刷爆"""
        from webapp import views

        with mock.patch.object(views.logger, "info") as info:
            self.client.get("/health")
        info.assert_not_called()

    def test_endpoint_is_in_bypass_list(self):
        from webapp.security import BYPASS_ENDPOINTS

        self.assertIn("health", BYPASS_ENDPOINTS)


class TestWaitForStatsDoesNotJoinAnUnstartedThread(unittest.TestCase):
    """`start_stats_job` 是"锁内登记、锁外 start"，窗口内 join 未启动线程会抛 RuntimeError。

    症状是仪表盘 500（`cannot join thread before it is started`），而它的成因恰好是
    本轮想消灭的"平白弹回首页"这条链上的另一环。修法是 start 移进锁内 + join 前查
    `is_alive()`，这里钉住后者（前者无法从外部稳定复现那个纳秒级窗口）。
    """

    HASH = "statsrace000001"

    def setUp(self):
        self.addCleanup(storemod._STATS_THREADS.pop, self.HASH, None)

    def test_wait_does_not_raise_on_registered_but_unstarted_thread(self):
        t = threading.Thread(target=lambda: None, name="stats-not-started")
        with storemod._STATS_LOCK:
            storemod._STATS_THREADS[self.HASH] = t
        try:
            storemod.wait_for_stats(self.HASH, timeout=0.01)  # 不许抛
        except RuntimeError as e:  # pragma: no cover - 失败路径
            self.fail(f"join 了未启动的线程：{e}")
        self.assertFalse(t.is_alive())

    def test_reverse_verification_joining_directly_does_raise(self):
        """反向验证：对未启动的线程直接 join 确实抛错，所以上面那道 `is_alive()` 是必需的。"""
        t = threading.Thread(target=lambda: None)
        with self.assertRaises(RuntimeError):
            t.join(0.01)


class TestConnectionPing(unittest.TestCase):
    def test_unconfigured_says_so(self):
        from analyzer import deepseek_client as dc

        with mock.patch.object(dc, "DEEPSEEK_API_KEY", ""):
            ok, detail = dc.test_connection()
        self.assertFalse(ok)
        self.assertIn("API Key 未配置", detail)

    def test_error_detail_is_honest_and_bounded(self):
        from analyzer import deepseek_client as dc

        class _Completions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("Error code: 401 - " + "x" * 500)

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        with (
            mock.patch.object(dc, "DEEPSEEK_API_KEY", "sk-secret-not-logged"),
            mock.patch.object(dc, "_get_client", return_value=_Client()),
        ):
            ok, detail = dc.test_connection()
        self.assertFalse(ok)
        self.assertIn("RuntimeError", detail, "错误类型与文本就是排障线索，必须原样带回")
        self.assertLessEqual(len(detail), 260, "错误截断在预算内")
        self.assertNotIn("sk-secret", detail, "Key 绝不能出现在回显里")

    def test_success_reports_model_and_latency(self):
        from analyzer import deepseek_client as dc

        class _Completions:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] == 1, "自检必须是最小请求，不能顺手烧一次真实分析的钱"
                return object()

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        with (
            mock.patch.object(dc, "DEEPSEEK_API_KEY", "sk-test"),
            mock.patch.object(dc, "_get_client", return_value=_Client()),
        ):
            ok, detail = dc.test_connection()
        self.assertTrue(ok)
        self.assertIn("延迟", detail)


_FIX_CST = timezone(timedelta(hours=8))


def _fix_chat_payload(uids_to_msgs):
    msgs = []
    base = int(datetime(2025, 3, 1, 20, 0, tzinfo=_FIX_CST).timestamp() * 1000)
    i = 0
    for uid, texts in uids_to_msgs.items():
        for t in texts:
            msgs.append(
                {
                    "id": str(i),
                    "timestamp": base + i * 60000,
                    "time": "2025-03-01 20:%02d:00" % (i % 60),
                    "sender": {"uid": uid, "name": "人" + uid[-1]},
                    "content": t,
                }
            )
            i += 1
    return _fix_wrap(msgs, {k: "人" + k[-1] for k in uids_to_msgs})


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


class TestJobsHygiene(unittest.TestCase):
    """任务表：TTL 收紧、轮询即修剪、条数上限、running 永不淘汰"""

    def _mk(self, n, status, finished_at):
        from webapp import jobs as jobsmod

        with jobsmod.JOBS_LOCK:
            for i in range(n):
                jobsmod.JOBS[f"j{i}-{status}-{finished_at}"] = {
                    "status": status,
                    "dim": "emotion",
                    "done": 0,
                    "total": 0,
                    "cancel": False,
                    "chat_hash": "h",
                    "sid": "s",
                    "created": finished_at,
                    "finished_at": finished_at,
                }

    def test_default_ttl_is_short(self):
        import config

        self.assertLessEqual(config.JOB_TTL_SECONDS, 3600)

    def test_expired_and_overflow_pruned_running_kept(self):
        from webapp import jobs as jobsmod

        jobsmod.JOBS.clear()
        old = time.time() - 99999
        self._mk(3, "done", old)  # 超 TTL
        self._mk(2, "running", time.time())  # 进行中
        self._mk(jobsmod.MAX_JOBS_KEPT + 10, "done", time.time())
        jobsmod._prune_jobs()
        statuses = [v["status"] for v in jobsmod.JOBS.values()]
        self.assertNotIn("expired", statuses)
        self.assertEqual(statuses.count("running"), 2, "running 条目绝不能被数量上限淘汰")
        self.assertLessEqual(len(jobsmod.JOBS), jobsmod.MAX_JOBS_KEPT)
        jobsmod.JOBS.clear()

    def test_running_job_survives_ttl_prune(self):
        """长任务不能在自己的轮询里把自己删掉。

        _prune_jobs 就在 api_analyze_job 入口调用，而一键全量分析（5 维度 × 多月 ×
        思考模式）跑过默认 15 分钟是常态：曾经 running 也按 created 走 TTL，于是
        任务把自己删掉，之后每次轮询都 404，用户看到"任务不存在（服务可能已重启）"。
        注意 created 取的是**过去**的时间——旧用例把 running 的 created 设成 now，
        恰好绕开了这个场景。
        """
        from webapp import jobs as jobsmod

        jobsmod.JOBS.clear()
        started_long_ago = time.time() - (jobsmod.JOB_TTL_SECONDS + 60)
        self._mk(2, "running", started_long_ago)
        jobsmod._prune_jobs()
        statuses = [v["status"] for v in jobsmod.JOBS.values()]
        self.assertEqual(statuses.count("running"), 2, "running 条目不能按 TTL 淘汰")
        jobsmod.JOBS.clear()

    def test_stale_running_entry_has_hard_cap(self):
        """线程异常死亡没来得及写 finished_at 时，running 条目靠硬上限兜底，不会永久滞留"""
        from webapp import jobs as jobsmod

        jobsmod.JOBS.clear()
        self.assertGreater(
            jobsmod.JOB_RUNNING_MAX_SECONDS,
            jobsmod.JOB_TTL_SECONDS,
            "running 的硬上限必须显著长于 TTL，否则等于没修",
        )
        self._mk(2, "running", time.time() - (jobsmod.JOB_RUNNING_MAX_SECONDS + 60))
        jobsmod._prune_jobs()
        self.assertEqual(jobsmod.JOBS, {}, "超过硬上限的滞留 running 条目必须被回收")
        jobsmod.JOBS.clear()

    def test_poll_prunes_even_without_new_jobs(self):
        import app as appmod
        from webapp import jobs as jobsmod

        jobsmod.JOBS.clear()
        old = time.time() - 99999
        jobsmod.JOBS["stale"] = {
            "status": "done",
            "dim": "emotion",
            "done": 0,
            "total": 0,
            "cancel": False,
            "chat_hash": "h",
            "sid": "other",
            "created": old,
            "finished_at": old,
        }
        client = appmod.app.test_client()
        r = client.get("/api/analyze-job/nonexistent")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("stale", jobsmod.JOBS, "轮询入口必须顺手修剪过期条目")
        jobsmod.JOBS.clear()


class TestStatsInBackground(unittest.TestCase):
    """上传响应不再背着统计计算；首个页面请求自动等它收口"""

    def _upload(self, client, token, payload):
        return client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )

    def test_dashboard_waits_for_background_stats(self):
        import app as appmod
        from webapp import store as storemod

        payload = _fix_chat_payload({"uA": ["在吗", "今晚吃啥"], "uB": ["在的", "火锅"]})
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        r = self._upload(client, token, payload)
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            chat_hash, path = sess["chat_hash"], sess["filepath"]
        try:
            # 不手动 wait：dashboard 的 _current_stats 必须自己等到数据就绪
            page = client.get("/dashboard")
            self.assertEqual(page.status_code, 200)
            self.assertIn("总消息数", page.get_data(as_text=True))
            stats = storemod._load_stats(chat_hash)
            self.assertIsNotNone(stats)
            self.assertIn("milestones", stats)
            self.assertNotIn("word_freq", stats, "词频仍应是懒算，不进后台统计")
        finally:
            storemod.wait_for_stats(chat_hash, timeout=30)
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)

    def test_purge_after_upload_does_not_resurrect_stats(self):
        """级联清理发生时统计线程仍在跑：它收尾时不得把刚删的缓存复活；
        而用户之后重新上传同一内容（新会话）必须能正常重算——标记要撤销"""
        import threading
        from webapp import store as storemod

        stats = {"overview": {"x": 1}}
        started, release = threading.Event(), threading.Event()

        def blocking_compute(chat):
            started.set()
            self.assertTrue(release.wait(10), "测试超时未放行")
            return stats

        with mock.patch.object(storemod, "compute_stats", side_effect=blocking_compute):
            storemod.start_stats_job(object(), "hashG")
            started.wait(10)  # 线程已进入计算
            storemod._purge_chat_caches("hashG")  # 用户在它落盘前清掉了这个聊天
            release.set()
            storemod.wait_for_stats("hashG", timeout=10)
            self.assertIsNone(storemod._load_stats("hashG"), "被清理的哈希不能由晚到的线程复活成孤儿缓存")

            # 同一内容重新上传：新任务的正当写入不能被旧标记误拦
            release.set()
            started.clear()
            storemod.start_stats_job(object(), "hashG")
            storemod.wait_for_stats("hashG", timeout=10)
            self.assertIsNotNone(storemod._load_stats("hashG"))
            storemod._purge_chat_caches("hashG")


class TestFailureSurfacedToUser(unittest.TestCase):
    """异步化之后，失败必须有人告诉用户——静默跳回首页是最糟的失败方式"""

    def _upload(self, client, token, payload):
        return client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )

    def test_stats_failure_is_shown_on_home(self):
        import app as appmod
        from webapp import store as storemod

        payload = _fix_chat_payload({"uA": ["在吗"], "uB": ["在的"]})
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        with mock.patch.object(storemod, "compute_stats", side_effect=RuntimeError("磁盘炸了")):
            r = self._upload(client, token, payload)
            self.assertEqual(r.status_code, 302, "上传本身应成功（解析没问题）")
            with client.session_transaction() as s:
                chat_hash = s["chat_hash"]
            storemod.wait_for_stats(chat_hash, timeout=20)
            home = client.get("/").get_data(as_text=True)
        self.assertIn("本地统计计算失败", home)
        self.assertIn("磁盘炸了", home)
        # 仪表盘没有数据可渲染，回首页——但首页必须解释原因
        self.assertEqual(client.get("/dashboard").status_code, 302)
        storemod._STATS_ERRORS.pop(chat_hash, None)
        storemod._purge_chat_caches(chat_hash)

    def test_csrf_failure_message_is_actionable(self):
        import app as appmod

        client = appmod.app.test_client()
        r = client.post("/upload", data={}, headers={"Origin": "http://localhost:5000"})
        self.assertEqual(r.status_code, 400)
        body = r.get_data(as_text=True)
        self.assertIn("CSRF", body)  # 保留原有语义（安全测试依赖）
        self.assertIn("刷新", body)  # 并且告诉用户怎么办


if __name__ == "__main__":
    unittest.main(verbosity=2)
