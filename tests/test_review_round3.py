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
"""第三轮审查修复项 —— 会话固定、Secure cookie、缓存生命周期、任务健壮性

覆盖（每条都对应一个"承诺与实现不一致"的具体缺口）：
- 登录成功后必须轮换**服务端 session id**（只 session.clear() 不换 sid 等于没防会话固定）；
- 会话 cookie 的 Secure 标志按绑定地址自动判定，并可用 QQCHAT_COOKIE_SECURE 覆盖；
- 删聊天时进程内已解析的 ChatData 也要丢（否则源文件同名同大小重建会读到旧内容）；
- 会话文件在"发起任务"与"后台开工"之间被回收时，要给出可执行的中文提示；
- 上传流不可回退且拷贝失败时，必须报错而不是交出"从中间开始"的半截 JSON；
- compute_stats 复用 overview 已算出的轮次，不再重复遍历全部消息；
- 对话行压缩的三种形态保持逐字兼容。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 本进程的临时目录统一挪到数据目录下（同 test_review_round2：避免在 %TEMP% 留垃圾）
_TMP_ROOT = os.path.join(os.environ["QQCHAT_DATA_DIR"], "tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 别名与项目模块的导入都必须排在环境准备之后（提前导入会把开关读成默认值）
import app as appmod  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from webapp import cleanup as cleanupmod  # noqa: E402
from webapp import jobs as jobsmod  # noqa: E402
from webapp import security as securitymod  # noqa: E402
from webapp import store as storemod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _msg(uid: str, ts: int, text: str) -> Message:
    return Message(
        id=f"{uid}-{ts}",
        timestamp=ts,
        time_str=datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=uid,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


class TestSessionIdRotation(unittest.TestCase):
    """会话固定：认证状态升级时必须换一个 sid，且旧 sid 立刻失效"""

    def setUp(self):
        # 测试客户端走 http，Secure cookie 不会被回传——固定关掉以保证用例确定性
        self._secure = appmod.app.config["SESSION_COOKIE_SECURE"]
        appmod.app.config["SESSION_COOKIE_SECURE"] = False
        self.addCleanup(appmod.app.config.__setitem__, "SESSION_COOKIE_SECURE", self._secure)
        patcher = mock.patch.object(securitymod, "ACCESS_PASSWORD", "pw-under-test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sid_changes_after_successful_login(self):
        client = appmod.app.test_client()
        client.get("/")
        before = client.get_cookie("session")
        self.assertIsNotNone(before, "匿名访问后应当已经下发 session cookie")

        resp = client.post("/login", data={"password": "pw-under-test"})
        self.assertEqual(resp.status_code, 302)

        after = client.get_cookie("session")
        self.assertIsNotNone(after)
        self.assertNotEqual(before.value, after.value, "登录成功后必须轮换 session id（否则可被会话固定）")

    def test_old_sid_loses_access_after_login(self):
        """旧 sid 上不该带着"已登录"状态：换个客户端拿旧 sid 访问应被弹回登录页"""
        client = appmod.app.test_client()
        client.get("/")
        old_sid = client.get_cookie("session").value
        client.post("/login", data={"password": "pw-under-test"})

        attacker = appmod.app.test_client()
        attacker.set_cookie("session", old_sid, domain="localhost")
        resp = attacker.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))

    def test_login_still_works_after_rotation(self):
        """轮换不能把新会话弄丢：登录后应能正常访问受保护页面"""
        client = appmod.app.test_client()
        client.get("/")
        client.post("/login", data={"password": "pw-under-test"})
        with client.session_transaction() as sess:
            self.assertTrue(sess.get("auth_ok"))
            self.assertTrue(sess.get("csrf_token"))


class TestSecureCookiePolicy(unittest.TestCase):
    """Secure cookie：回环不过网所以关掉，非回环默认要求 HTTPS，可显式覆盖"""

    def _enabled(self, host: str, mode: str) -> bool:
        with mock.patch.object(appmod, "FLASK_HOST", host), mock.patch.object(appmod, "COOKIE_SECURE", mode):
            return appmod._secure_cookie_enabled()

    def test_loopback_defaults_to_insecure(self):
        """回环绑定：恒 Secure 会让本机 http 访问直接登不上去，所以 auto 必须是 False"""
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertFalse(self._enabled(host, "auto"))

    def test_non_loopback_defaults_to_secure(self):
        """绑定到局域网/公网：明文 http 会让会话 id 裸奔，默认必须要求 HTTPS"""
        for host in ("0.0.0.0", "192.168.1.5", "10.0.0.7"):
            with self.subTest(host=host):
                self.assertTrue(self._enabled(host, "auto"))

    def test_explicit_override_wins(self):
        self.assertTrue(self._enabled("127.0.0.1", "true"))
        self.assertFalse(self._enabled("0.0.0.0", "false"))

    def test_app_config_matches_policy(self):
        """组装出来的 app 必须真的带上这个配置（不能只在 helper 里算）"""
        self.assertEqual(appmod.app.config["SESSION_COOKIE_SECURE"], appmod._secure_cookie_enabled())

    def test_invalid_mode_falls_back_to_auto(self):
        """拼错的值不能让"非回环要求 HTTPS"这条防线悄悄消失"""
        import config as configmod

        with mock.patch.dict(os.environ, {"QQCHAT_COOKIE_SECURE": "banana"}):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(
                    configmod.env_choice("QQCHAT_COOKIE_SECURE", "auto", ("auto", "true", "false")),
                    "auto",
                )
        self.assertIn("QQCHAT_COOKIE_SECURE", err.getvalue())
        with (
            mock.patch.object(appmod, "COOKIE_SECURE", "auto"),
            mock.patch.object(appmod, "FLASK_HOST", "0.0.0.0"),
        ):
            self.assertTrue(appmod._secure_cookie_enabled())


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
        missing = os.path.join(_TMP_ROOT, "definitely-not-here.json")
        jobsmod._run_job(job_id, "emotion", missing, "deadbeefcafe0000")

        job = jobsmod.JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("已被清理", job["error"])
        self.assertNotIn("No such file", job["error"])
        self.assertNotIn("Errno", job["error"])

    def test_run_analyze_all_reports_cleanup(self):
        job_id = "job-all-missing-file"
        self._register(job_id)
        missing = os.path.join(_TMP_ROOT, "definitely-not-here-either.json")
        jobsmod._run_analyze_all(job_id, missing, "deadbeefcafe0000", False)

        job = jobsmod.JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("已被清理", job["error"])


class TestSaveAndHashIntegrity(unittest.TestCase):
    """上传流落盘：要么给出完整文件的哈希，要么报错——绝不返回半截结果"""

    class _GoodFS:
        def __init__(self, data: bytes):
            self.stream = io.BytesIO(data)

    class _ExplodingStream:
        """先吐一部分数据再抛 OSError，且不可 seek（模拟不可回退的失败流）"""

        def __init__(self, data: bytes):
            self._data = data
            self._sent = False

        def seek(self, *args):
            raise OSError("not seekable")

        def read(self, n=-1):
            if not self._sent:
                self._sent = True
                return self._data[: max(1, n // 2)]
            raise OSError("source vanished")

    class _ExplodingFS:
        def __init__(self, data: bytes):
            self.stream = TestSaveAndHashIntegrity._ExplodingStream(data)
            self.save_called = False

        def save(self, path):
            self.save_called = True
            with open(path, "wb") as f:
                f.write(b"PARTIAL")

    def test_fast_path_hash_matches_full_reread(self):
        data = json.dumps({"hello": "世界" * 500}).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="qqchatlog-savehash-") as tmp:
            dest = os.path.join(tmp, "a.json")
            size, digest = storemod.save_and_hash(self._GoodFS(data), dest)
            self.assertEqual(size, len(data))
            self.assertEqual(digest, storemod._chat_hash(dest))

    def test_empty_stream_yields_empty_file_hash(self):
        with tempfile.TemporaryDirectory(prefix="qqchatlog-savehash-") as tmp:
            dest = os.path.join(tmp, "empty.json")
            size, digest = storemod.save_and_hash(self._GoodFS(b""), dest)
            self.assertEqual(size, 0)
            self.assertEqual(digest, storemod._chat_hash(dest))

    def test_unrewindable_failed_stream_raises_instead_of_truncating(self):
        """核心回归：拷贝中途失败且流不可回退时，必须报错。

        旧实现会在 seek 失败后照常调用 save()，而 save() 从"流当前的位置"拷贝——
        于是落下一份从中间开始的半截 JSON，还被当成上传成功返回。
        """
        data = json.dumps({"hello": "世界" * 500}).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="qqchatlog-savehash-") as tmp:
            dest = os.path.join(tmp, "b.json")
            fs = self._ExplodingFS(data)
            with self.assertRaises(OSError):
                storemod.save_and_hash(fs, dest)
            self.assertFalse(fs.save_called, "不可回退时不该再调 save() 写出半截文件")


class TestComputeStatsNoDoubleWork(unittest.TestCase):
    """compute_stats 不再把轮次算两遍（calc_overview 内部已经算过一次）"""

    def test_exchange_rounds_reused_from_overview(self):
        base = datetime(2024, 1, 1, 8, 0, tzinfo=CST)
        msgs = [
            _msg(uid, int((base + timedelta(minutes=5 * i)).timestamp() * 1000), f"m{i}")
            for i, uid in enumerate(["uA", "uB", "uA", "uB", "uA"])
        ]
        chat = ChatData(
            chat_name="c",
            self_name="A",
            other_name="B",
            self_uid="uA",
            other_uid="uB",
            messages=msgs,
        )
        stats = storemod.compute_stats(chat)
        self.assertEqual(stats["exchange_rounds"], stats["overview"]["exchange_rounds"])

    def test_exchange_rounds_computed_only_once(self):
        """用调用计数钉住"只算一次"，避免以后又被加回去。

        调用点是 local_stats.calc_exchange_rounds（由 calc_overview 内部调用），
        所以盯的是 local_stats 命名空间里的那个引用。
        """
        from analyzer import local_stats as ls

        base = datetime(2024, 1, 1, 8, 0, tzinfo=CST)
        msgs = [
            _msg(uid, int((base + timedelta(minutes=5 * i)).timestamp() * 1000), f"m{i}")
            for i, uid in enumerate(["uA", "uB", "uA"])
        ]
        chat = ChatData(
            chat_name="c",
            self_name="A",
            other_name="B",
            self_uid="uA",
            other_uid="uB",
            messages=msgs,
        )
        real = ls.calc_exchange_rounds
        calls = []

        def counting(c):
            calls.append(1)
            return real(c)

        with mock.patch.object(ls, "calc_exchange_rounds", counting):
            storemod.compute_stats(chat)
        self.assertEqual(len(calls), 1, f"轮次只应算一次，实际 {len(calls)} 次")


class TestMessageLineFormatCompatibility(unittest.TestCase):
    """对话行压缩的三种形态：重构后必须逐字保持原样（缓存键含这两个函数的源码）"""

    def setUp(self):
        self.t0 = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        self.m = {
            i: _msg(uid, self.t0 + off, f"消息{i}")
            for i, (uid, off) in enumerate(
                [("u2", 0), ("u1", 3 * 60000), ("u2", 6 * 60000), ("u2", 7 * 60000), ("u1", 207 * 60000)]
            )
        }

    def test_first_line_has_absolute_time_and_name(self):
        self.assertEqual(dc._message_line(self.m[0], "B"), "[01-01 08:00] B: 消息0")

    def test_same_speaker_continuation_drops_everything(self):
        line = dc._message_line(self.m[3], "B", prev_uid="u2", prev_ts=self.m[2].timestamp)
        self.assertEqual(line, "消息3")

    def test_speaker_change_keeps_name_and_relative_mark(self):
        line = dc._message_line(self.m[1], "A", prev_uid="u2", prev_ts=self.m[0].timestamp)
        self.assertEqual(line, "A(+3m): 消息1")

    def test_large_gap_prints_absolute_time(self):
        line = dc._message_line(self.m[4], "A", prev_uid="u2", prev_ts=self.m[3].timestamp)
        self.assertTrue(line.startswith("["), line)
        self.assertIn("A:", line)

    def test_same_speaker_within_section_keeps_gap_mark(self):
        """段内同人连发但间隔够大（≥2 分钟且 <30 分钟）：只留 (+Nm)，不重复昵称"""
        gap_msg = _msg("u2", self.m[0].timestamp + 10 * 60000, "消息X")
        line = dc._message_line(gap_msg, "B", prev_uid="u2", prev_ts=self.m[0].timestamp)
        self.assertEqual(line, "(+10m): 消息X")

    def test_gap_at_threshold_switches_to_absolute_time(self):
        """恰好 30 分钟就翻到"新的一段"：打印绝对时间（口径边界不能漂）"""
        gap_msg = _msg("u2", self.m[0].timestamp + 30 * 60000, "消息X")
        line = dc._message_line(gap_msg, "B", prev_uid="u2", prev_ts=self.m[0].timestamp)
        self.assertEqual(line, "[01-01 08:30] B: 消息X")


class TestCleanupClosureBinding(unittest.TestCase):
    """清理判定函数把 now 显式冻进闭包，不依赖外层变量的后续取值"""

    def test_older_than_expired(self):
        mtime = os.path.getmtime(__file__)
        self.assertTrue(cleanupmod._older_than(mtime + 1000, 100)(__file__))

    def test_older_than_fresh(self):
        mtime = os.path.getmtime(__file__)
        self.assertFalse(cleanupmod._older_than(mtime + 10, 100)(__file__))

    def test_cleanup_still_purges_expired_upload(self):
        """端到端：过期的 uploads 文件仍会被回收（别人的 now 改动不该漏掉它）"""
        import time

        with tempfile.TemporaryDirectory(prefix="qqchatlog-cleanup-") as tmp:
            uploads = os.path.join(tmp, "uploads")
            os.makedirs(uploads, exist_ok=True)
            stale = os.path.join(uploads, "stale.json")
            with open(stale, "w", encoding="utf-8") as f:
                f.write("{}")
            old = time.time() - 3 * 86400
            os.utime(stale, (old, old))

            with (
                mock.patch.object(cleanupmod, "UPLOAD_FOLDER", uploads),
                mock.patch.object(cleanupmod, "SESSION_FILE_DIR", os.path.join(tmp, "session")),
                mock.patch.object(cleanupmod, "AI_CACHE_DIR", os.path.join(tmp, "ai")),
                mock.patch.object(cleanupmod, "STATS_CACHE_DIR", os.path.join(tmp, "stats")),
                mock.patch.object(cleanupmod, "LOG_DIR", os.path.join(tmp, "logs")),
                mock.patch.object(cleanupmod, "sweep_orphan_month_cache", lambda: 0),
            ):
                cleanupmod.cleanup_old_files(max_age_seconds=86400)
            self.assertFalse(os.path.exists(stale), "过期的上传文件应被清理")


if __name__ == "__main__":
    unittest.main()
