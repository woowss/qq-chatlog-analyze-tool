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
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 让测试可以从项目根目录导入包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试隔离：数据目录指向临时目录，避免测试读写真实的 uploads/ai_cache/session
import tempfile as _tempfile
os.environ.setdefault("QQCHAT_DATA_DIR", _tempfile.mkdtemp(prefix="qqchatlog-test-"))
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 专门验证增量缓存的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

from parser.qq_parser import Message, load_chat, split_by_month  # noqa: E402
from analyzer.local_stats import calc_overview  # noqa: E402


def _write_chat(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def _msg(uid: str, ts: int, text: str = "hello") -> Message:
    return Message(id="", timestamp=ts, time_str="2025-01-01 00:00:00",
                   sender_name="x", sender_uid=uid, text=text, raw_text=text,
                   msg_type="type_1", has_image=False, is_reply=False)


class TestParserTimestampRobustness(unittest.TestCase):
    """时间戳缺失/null/非数值：既不能崩溃，也不能把消息塞进 1970-01"""

    GOOD = {"id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
            "sender": {"uid": "u_self", "name": "我"}, "content": "正常"}

    @staticmethod
    def _load(msgs, info=None):
        data = {
            "chatInfo": info or {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": msgs,
        }
        return load_chat(_write_chat(data))

    def test_null_timestamp_does_not_crash(self):
        chat = self._load([self.GOOD, {**self.GOOD, "id": "2", "timestamp": None}])
        self.assertEqual(len(chat.messages), 2)      # 用 time 字符串回退成功
        self.assertEqual(set(split_by_month(chat)), {"2025-09"})

    def test_string_timestamp_is_coerced(self):
        chat = self._load([{**self.GOOD, "timestamp": "1758031009000"}])
        self.assertEqual(chat.messages[0].timestamp, 1758031009000)

    def test_missing_timestamp_falls_back_to_time_string(self):
        msg = {k: v for k, v in self.GOOD.items() if k != "timestamp"}
        msg["time"] = "2025-10-02 08:00:00"
        chat = self._load([msg])
        self.assertEqual(list(split_by_month(chat)), ["2025-10"])

    def test_unusable_timestamp_is_dropped_not_1970(self):
        bad = {"id": "9", "timestamp": None, "time": "not-a-date",
               "sender": {"uid": "u_other", "name": "对方"}, "content": "坏时间戳"}
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
            "chatInfo": {"name": "会话", "selfName": "我"},   # 故意缺 selfUid
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [self.GOOD],
        }
        chat = load_chat(_write_chat(data))
        self.assertEqual(chat.other_name, "对方")


class TestCancelActuallyStops(unittest.TestCase):
    """取消必须真的止住未开始的月份，否则"可随时取消"是假的、剩余月份照常计费"""

    @staticmethod
    def _months(n):
        return {f"2025-{m:02d}": [_msg("self", 1735689600000 + (m - 1) * 2678400000 + i * 60000)
                                  for i in range(3)]
                for m in range(1, n + 1)}

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

        with mock.patch.object(dc, "_call_api", side_effect=fake_api), \
             mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0):
            res = dc._analyze_periods(self._months(8), "sys", lambda p, m: "prompt",
                                      max_tokens=1024, tag="emotion",
                                      on_progress=on_progress,
                                      should_cancel=lambda: flag["cancel"])
        self.assertLess(len(calls), 8, "取消后不应继续调用剩余月份")
        self.assertLessEqual(len(calls), dc.CONCURRENCY, "只应跑完并发窗口内的任务")
        self.assertLess(len(res), 8)

    def test_cancel_before_start_makes_no_calls(self):
        import analyzer.deepseek_client as dc
        calls = []
        with mock.patch.object(dc, "_call_api",
                               side_effect=lambda *a, **kw: calls.append(1)), \
             mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0):
            res = dc._analyze_periods(self._months(5), "sys", lambda p, m: "prompt",
                                      max_tokens=1024, tag="emotion",
                                      should_cancel=lambda: True)
        self.assertEqual(calls, [])
        self.assertEqual(res, {})

    def test_without_cancel_all_months_run(self):
        import analyzer.deepseek_client as dc
        calls = []
        with mock.patch.object(dc, "_call_api",
                               side_effect=lambda *a, **kw: calls.append(1) or {"x": 1}), \
             mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0):
            res = dc._analyze_periods(self._months(4), "sys", lambda p, m: "prompt",
                                      max_tokens=1024, tag="emotion")
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(res), 4)


class TestJobOverlapGuard(unittest.TestCase):
    """全量任务与单维度任务重叠时拒绝，而不是把同一维度分析两遍（重复计费）"""

    def setUp(self):
        import app as appmod
        self.appmod = appmod
        # 这些用例只关心任务编排，不能依赖本机 .env 是否配了 API Key（CI 没有 .env）
        self._patches = [
            mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True),
            mock.patch("app.is_api_configured", return_value=True),
        ]
        for p in self._patches:
            p.start()
        self.client = appmod.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as sess:
            self.token = sess["csrf_token"]
        payload = json.dumps({
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"senders": [{"uid": "u_self", "name": "我"},
                                       {"uid": "u_other", "name": "对方"}]},
            "messages": [
                {"id": "1", "timestamp": 1758031009000, "time": "2025-09-16 21:56:49",
                 "sender": {"uid": "u_self", "name": "我"}, "content": "在吗"},
                {"id": "2", "timestamp": 1758031069000, "time": "2025-09-16 21:57:49",
                 "sender": {"uid": "u_other", "name": "对方"}, "content": "在的"},
            ],
        }, ensure_ascii=False).encode("utf-8")
        self.client.post("/upload", data={"file": (io.BytesIO(payload), "c.json")},
                         headers=self._headers())

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with self.client.session_transaction() as sess:
            path, chash = sess.get("filepath"), sess.get("chat_hash")
        with self.appmod.JOBS_LOCK:
            self.appmod.JOBS.clear()
        if path and os.path.exists(path):
            os.remove(path)
        if chash:
            self.appmod._purge_chat_caches(chash)

    def _headers(self):
        return {"Origin": "http://localhost:5000", "X-CSRF-Token": self.token}

    def _fake_running_job(self, dim):
        with self.client.session_transaction() as sess:
            sid, chash = sess.sid, sess.get("chat_hash")
        with self.appmod.JOBS_LOCK:
            self.appmod.JOBS["testjob"] = {
                "status": "running", "dim": dim, "done": 0, "total": 0, "cancel": False,
                "chat_hash": chash, "sid": sid, "created": time.time(),
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
        with mock.patch("analyzer.deepseek_client._call_api",
                        return_value={"self_emotion": "平静", "other_emotion": "平静"}), \
             mock.patch("analyzer.deepseek_client.is_api_configured", return_value=True), \
             mock.patch("app.is_api_configured", return_value=True):
            job = self.client.post("/api/analyze/emotion",
                                   headers=self._headers()).get_json()["job"]
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
        import app as appmod
        try:
            appmod._write_cache("emotion", "hashAtomic", {"a": 1})
            path = appmod._cache_path("emotion", "hashAtomic")
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".tmp"))
            self.assertEqual(appmod._read_cache("emotion", "hashAtomic"), {"a": 1})
        finally:
            appmod._purge_chat_caches("hashAtomic")

    def test_cache_hit_refreshes_mtime(self):
        """天天用的缓存不该在 30 天后因 mtime 过期被清理掉"""
        import app as appmod
        try:
            appmod._write_cache("topics", "hashTtl", {"a": 1})
            path = appmod._cache_path("topics", "hashTtl")
            old = time.time() - 40 * 86400
            os.utime(path, (old, old))
            self.assertLess(os.path.getmtime(path), time.time() - 30 * 86400)
            self.assertIsNotNone(appmod._read_cache("topics", "hashTtl"))
            self.assertGreater(os.path.getmtime(path), time.time() - 60)
        finally:
            appmod._purge_chat_caches("hashTtl")


class TestLoginThrottle(unittest.TestCase):
    """绑定局域网时口令不能无限爆破"""

    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self.client = appmod.app.test_client()
        with appmod._login_lock:
            appmod._login_failures.clear()

    def tearDown(self):
        with self.appmod._login_lock:
            self.appmod._login_failures.clear()

    def test_repeated_failures_are_throttled(self):
        import app as appmod
        with mock.patch.object(appmod, "ACCESS_PASSWORD", "s3cret"):
            for _ in range(appmod.LOGIN_MAX_ATTEMPTS):
                r = self.client.post("/login", data={"password": "wrong"})
                self.assertEqual(r.status_code, 200)      # 正常渲染错误提示
            r = self.client.post("/login", data={"password": "wrong"})
            self.assertEqual(r.status_code, 429)
            # 即使口令正确，窗口内也照样拒绝（避免爆破成功）
            r = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 429)

    def test_success_clears_failures(self):
        import app as appmod
        with mock.patch.object(appmod, "ACCESS_PASSWORD", "s3cret"):
            self.client.post("/login", data={"password": "wrong"})
            r = self.client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 302)
            with appmod._login_lock:
                self.assertEqual(appmod._login_failures, {})


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
        file_handlers = [h for h in base.handlers if hasattr(h, "baseFilename")]
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
