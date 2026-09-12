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
"""第二轮审查修复项 —— 每一条都对应一个"承诺与实现不一致"的具体缺口

覆盖：
- 图片副本（uploads/media/<哈希>/）必须被 24 小时策略回收，并在删聊天时级联删除；
- 图片摘要固定非思考模式（512 预算下思维链会稳定截断摘要）；
- 登录 next= 的开放重定向（/\\evil.com 绕过）；
- 统一安全响应头；
- 表情图抓取的时长预算（同步请求不能挂住半小时）；
- 缓存落盘失败的出声与用量增量不丢；
- 上传体积上限可配；
- 对话段口径的唯一来源（is_session_start）；
- 前端轮询 404 时回读磁盘结果。
"""
import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# 测试隔离：数据目录指向临时目录，绝不碰真实 uploads/ai_cache/session。
# 只清理"自己创建的"目录——外部显式指定的 QQCHAT_DATA_DIR 一律不动。
import tempfile as _tempfile
import atexit as _atexit
import shutil as _shutil


def _drop_temp_data_dir():
    """跑完把临时数据目录删掉（先关日志，否则 logging 的 shutdown 又把它写回来）"""
    import logging
    logging.shutdown()
    _shutil.rmtree(os.environ["QQCHAT_DATA_DIR"], ignore_errors=True)


if "QQCHAT_DATA_DIR" not in os.environ:
    os.environ["QQCHAT_DATA_DIR"] = _tempfile.mkdtemp(prefix="qqchatlog-test-")
    _atexit.register(_drop_temp_data_dir)
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 本进程的临时目录统一挪到数据目录下，两个好处：
# 1) %TEMP% 只读受限的环境（沙箱、部分容器）里 tempfile.* 不再直接 PermissionError；
# 2) 用例产生的临时json/图片/表情包都落在数据目录内，随测试隔离目录一起回收，
#    不会在用户 %TEMP% 里留下上百个 qqchatlog-* 垃圾目录。
_TMP_ROOT = os.path.join(os.environ["QQCHAT_DATA_DIR"], "tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 别名与项目模块的导入都必须排在环境准备之后（提前导入会把开关读成默认值）
import app as appmod  # noqa: E402
import config  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import face_images as fi  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
from analyzer import usage as usage_mod  # noqa: E402
from analyzer import vision  # noqa: E402
from webapp import cleanup as cleanupmod  # noqa: E402
from webapp import security as securitymod  # noqa: E402
from webapp import store as storemod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestMediaCopyLifecycle(unittest.TestCase):
    """图片副本是盘上最敏感的一批派生数据：既要有 24 小时回收，也要随聊天级联删除"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-media-lifecycle-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)

    def _patched_dirs(self):
        """把清理涉及的目录全部指向临时目录，避免碰到真实数据"""
        return [
            mock.patch.object(cleanupmod, "UPLOAD_FOLDER", self.tmp),
            mock.patch.object(cleanupmod, "SESSION_FILE_DIR",
                              os.path.join(self.tmp, "session")),
            mock.patch.object(cleanupmod, "AI_CACHE_DIR",
                              os.path.join(self.tmp, "ai")),
            mock.patch.object(cleanupmod, "STATS_CACHE_DIR",
                              os.path.join(self.tmp, "stats")),
            mock.patch.object(cleanupmod, "LOG_DIR", os.path.join(self.tmp, "logs")),
            mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp),
        ]

    def test_expired_media_copy_is_reclaimed_recursively(self):
        """uploads/media/<哈希>/ 里的图片副本必须被回收，空目录也要清掉。

        原先 _purge_dir 只看目录下的普通文件（os.path.isfile），整个 media 子树
        被跳过——图片副本永远留在盘上，与 README 的 24 小时承诺不符。
        """
        media_dir = os.path.join(self.tmp, "media", "abc123")
        os.makedirs(media_dir, exist_ok=True)
        old_file = os.path.join(media_dir, "old.jpg")
        new_file = os.path.join(media_dir, "new.jpg")
        for path in (old_file, new_file):
            with open(path, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"x" * 16)
        ancient = time.time() - 48 * 3600
        os.utime(old_file, (ancient, ancient))

        for patcher in self._patched_dirs():
            patcher.start()
            self.addCleanup(patcher.stop)
        removed = cleanupmod.cleanup_old_files()

        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.exists(old_file), "过期图片副本应被删除")
        self.assertTrue(os.path.exists(new_file), "未过期的副本不该被误删")

        # 再跑一次：这次全部过期 → 文件与空目录一起清掉，不留空壳目录
        os.utime(new_file, (ancient, ancient))
        cleanupmod.cleanup_old_files()
        self.assertFalse(os.path.isdir(media_dir), "回收干净后应删掉空的哈希目录")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "media")),
                         "空的 media 目录也不该留着")

    def test_purge_chat_caches_removes_media_copy(self):
        """删聊天（换文件/清理）时必须连带删掉图片副本目录"""
        chat_hash = "deadbeefcafe0001"
        media_dir = os.path.join(self.tmp, "media", chat_hash)
        os.makedirs(media_dir, exist_ok=True)
        for name in ("a.jpg", "b.png"):
            with open(os.path.join(media_dir, name), "wb") as f:
                f.write(b"x" * 8)

        with mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp):
            removed = storemod._purge_chat_caches(chat_hash)

        self.assertGreaterEqual(removed, 2, "回收计数应包含图片副本")
        self.assertFalse(os.path.exists(media_dir), "图片副本目录应被整目录删除")

    def test_purge_session_media_is_idempotent(self):
        """没有副本目录时不该报错（旧聊天、没开图片理解都要走这条路径）"""
        with mock.patch.object(vision, "UPLOAD_FOLDER", self.tmp):
            self.assertEqual(vision.purge_session_media("nonexistent"), 0)


class TestVisionCallIsNonThinking(unittest.TestCase):
    """摘要的 max_tokens 只有 512：开思考模式必然被思维链吃满并截断"""

    def test_call_vision_sends_thinking_disabled(self):
        captured = {}

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                msg = mock.Mock()
                msg.content = "- 一张图"
                choice = mock.Mock()
                choice.message = msg
                choice.finish_reason = "stop"
                resp = mock.Mock()
                resp.choices = [choice]
                resp.usage = None
                return resp

        fake_client = mock.Mock()
        fake_client.chat.completions = _FakeCompletions()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
            img_path = f.name
        self.addCleanup(os.remove, img_path)

        with mock.patch.object(dc, "_get_client", return_value=fake_client), \
             mock.patch.object(dc, "_SEND_THINKING_PARAM", True), \
             mock.patch.object(dc, "THINKING_DEFAULT", True), \
             mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0):
            text = dc._call_vision("sys", "看图", [
                {"path": img_path, "mime": "image/png", "key": "k1"}])

        self.assertEqual(text, "- 一张图")
        self.assertEqual(captured.get("extra_body"), {"thinking": {"type": "disabled"}},
                         "图片摘要必须显式关闭思考模式")
        self.assertEqual(captured.get("max_tokens"), 512)


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
        self.assertNotIn("evil.example.com", r.headers["Location"],
                         "浏览器把 /\\host 当协议相对地址，必须拦住")
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


class TestFaceFetchBudget(unittest.TestCase):
    """表情图抓取是同步请求：必须有单次时长预算，抓不完就下次继续"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-face-budget-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)

    def test_budget_stops_network_loop(self):
        faces = {
            "可怜": {"key": fi.key_for("可怜"), "market_url": None},
            "流泪": {"key": fi.key_for("流泪"), "market_url": None},
        }
        calls = []
        with mock.patch.object(fi, "FACE_IMAGES_ENABLED", True), \
             mock.patch.object(fi, "FACE_CACHE_DIR", self.tmp), \
             mock.patch.object(fi, "_download", side_effect=lambda url: calls.append(url)):
            # 极小预算：第一轮就该判定超时并收工
            fi.ensure(faces, allow_network=True, max_seconds=1e-9)
        self.assertEqual(calls, [], "预算用尽后不应再发起下载")

    def test_no_budget_means_no_limit(self):
        faces = {"可怜": {"key": fi.key_for("可怜"), "market_url": None}}
        calls = []
        with mock.patch.object(fi, "FACE_IMAGES_ENABLED", True), \
             mock.patch.object(fi, "FACE_CACHE_DIR", self.tmp), \
             mock.patch.object(fi, "_download", side_effect=lambda url: calls.append(url)):
            fi.ensure(faces, allow_network=True)      # 默认 0 = 不限时长
        self.assertEqual(len(calls), 1, "默认行为不变：没有预算就一直抓到上限/抓完")


class TestCacheWriteFailuresAreLoud(unittest.TestCase):
    """写失败必须留痕：静默失败会变成"每次都在重复付费"且用户毫不知情"""

    def test_month_cache_write_failure_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "not-a-dir")
            with open(missing, "w", encoding="utf-8") as f:
                f.write("占位：同名文件存在时，目录创建/写入必然失败")
            dc.configure_month_cache(missing)
            dc._last_write_warning[0] = 0.0
            try:
                with self.assertLogs("qqchatlog.deepseek", level="WARNING") as logs:
                    dc._write_month_cache("key1", {"a": 1})
            finally:
                dc.configure_month_cache("")
            self.assertTrue(any("写入失败" in line for line in logs.output),
                            "月份缓存写失败要打警告，而不是静默吞掉")

    def test_usage_flush_keeps_pending_on_failure(self):
        """用量落盘失败时增量要留在内存里下次再写，而不是被清空丢掉"""
        usage_mod._PENDING["days"].clear()
        usage_mod._PENDING["dims"].clear()
        for k in usage_mod._PENDING["total"]:
            usage_mod._PENDING["total"][k] = 0
        usage_mod.record_call("deepseek-flash", "emotion", 100, 50)
        with mock.patch.object(usage_mod, "_dump", side_effect=OSError("磁盘满")):
            usage_mod.flush()                      # 不应抛异常
        self.assertTrue(usage_mod._DIRTY, "失败后仍应标记为待落盘")
        self.assertEqual(usage_mod._PENDING["total"]["calls"], 1, "增量不能被清空")
        usage_mod._PENDING["days"].clear()
        usage_mod._PENDING["dims"].clear()
        for k in usage_mod._PENDING["total"]:
            usage_mod._PENDING["total"][k] = 0


class TestUploadLimitIsConfigurable(unittest.TestCase):
    """超长聊天的 JSON 会逼近 50MB：上限要能调，且默认值不变"""

    def test_default_is_50mb(self):
        self.assertEqual(config.MAX_CONTENT_LENGTH, 50 * 1024 * 1024)
        self.assertEqual(appmod.app.config["MAX_CONTENT_LENGTH"],
                         config.MAX_CONTENT_LENGTH, "配置要真的接到 Flask 上")

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
            r = client.post("/upload", data={"file": (io.BytesIO(payload), "big.json")},
                            headers={**headers, "X-Requested-With": "fetch"})
            self.assertEqual(r.status_code, 413)
            self.assertIn("QQCHAT_MAX_UPLOAD_MB", r.get_json().get("error", ""))

            # 普通表单提交：也应看到中文提示而不是英文模板
            r2 = client.post("/upload", data={"file": (io.BytesIO(payload), "big.json")},
                             headers=headers)
            self.assertEqual(r2.status_code, 413)
            body = r2.get_data(as_text=True)
            self.assertIn("上限", body)
            self.assertNotIn("Request Entity Too Large", body)
        finally:
            appmod.app.config["MAX_CONTENT_LENGTH"] = original


class TestSessionBoundarySingleSource(unittest.TestCase):
    """对话段口径只有一份实现：本地统计与 prompt 统计头必须完全一致"""

    def _chat(self, gaps_minutes):
        from parser.qq_parser import ChatData, Message
        base = 1758031009000
        ts = base
        msgs = []
        for i, gap in enumerate(gaps_minutes):
            ts += gap * 60000
            msgs.append(Message(id=str(i), timestamp=ts, time_str="",
                                sender_name="我" if i % 2 else "对方",
                                sender_uid="u_self" if i % 2 else "u_other",
                                text="在吗", raw_text="在吗", msg_type="type_1",
                                has_image=False, is_reply=False))
        chat = ChatData(chat_name="c", self_name="我", other_name="对方",
                        self_uid="u_self", other_uid="u_other", messages=msgs)
        return chat

    def test_predicate_boundary(self):
        self.assertTrue(ls.is_session_start(None, 1000), "首条必然是新段")
        self.assertFalse(ls.is_session_start(1000, 1000 + ls.SESSION_GAP_MS),
                         "刚好等于阈值不算新段（与原口径一致）")
        self.assertTrue(ls.is_session_start(1000, 1000 + ls.SESSION_GAP_MS + 1))

    def test_local_and_prompt_agree_on_session_count(self):
        chat = self._chat([0, 1, 31, 1, 40, 1])
        local_sessions = len(ls.calc_conversation_sessions(chat))
        stats = dc._conversation_stats(chat.messages, chat.self_uid)
        self.assertEqual(stats["sessions"], local_sessions,
                         "prompt 统计头与本地统计的段数必须一致（同一份口径）")
        self.assertEqual(stats["sessions"], 3)


class TestFrontendRecoversAfterRestart(unittest.TestCase):
    """任务记录只在内存（TTL/重启会丢），但已完成的维度结果已落盘：

    轮询到 404 时应回读磁盘缓存，而不是把已经付过费的分析显示成失败。
    """

    def test_poll_falls_back_to_disk_cache_on_404(self):
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 404", src)
        self.assertIn("loadAnalysis(opts.dim", src, "404 时应回读磁盘结果")
        self.assertIn("opts.dim = dim", src, "startAnalyze 要把维度名传下去")

    def test_polling_backs_off_and_pauses_when_hidden(self):
        """固定 1.5s 的 setInterval 会让一次全量分析发出几百次请求"""
        src = (ROOT / "web" / "static" / "js" / "analyze.js").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(", src, "轮询不该再用固定间隔")
        self.assertIn("POLL_MAX_DELAY_MS", src, "应有退避上限")
        self.assertIn("visibilitychange", src, "后台标签页应降频、回到前台立刻补查")

    def test_charts_share_one_resize_listener(self):
        """每个图表各挂一个 resize 监听且从不移除：同一页面重绘 N 次就留下 N 个监听"""
        src = (ROOT / "web" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
        self.assertEqual(src.count("addEventListener('resize'"), 1,
                         "resize 监听只应保留全局那一个")
        self.assertNotIn("window.addEventListener('resize', function() { chart.resize(); });", src)
        self.assertIn("function mountChart(", src)
        self.assertEqual(src.count("echarts.init("), 1,
                         "只允许 mountChart 内部建实例（重绘要先 dispose 旧的）")
        self.assertIn("dispose()", src)


class TestUsageRetention(unittest.TestCase):
    """用量按天聚合，但历史不该无限累积（日志只留 7 天，这里口径应一致）"""

    def test_old_days_are_pruned_on_flush(self):
        path = os.path.join(os.environ["QQCHAT_DATA_DIR"], "usage_prune.json")
        ancient = (datetime.now(tz=usage_mod.CST)
                   - timedelta(days=config.LOG_RETENTION_DAYS + 10)).strftime("%Y-%m-%d")
        recent = datetime.now(tz=usage_mod.CST).strftime("%Y-%m-%d")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"days": {ancient: {"calls": 9, "prompt": 1, "completion": 1},
                                recent: {"calls": 1, "prompt": 1, "completion": 1}},
                       "dims": {}, "total": {"calls": 10, "prompt": 2, "completion": 2}}, f)
        try:
            with mock.patch.object(usage_mod, "TOKEN_USAGE_FILE", path):
                usage_mod.record_call("deepseek-flash", "emotion", 10, 5)
                usage_mod.flush()
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn(ancient, saved["days"], "超过保留期的日期应被清掉")
            self.assertIn(recent, saved["days"], "保留期内的记录必须还在")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestVisionCacheHasCreatedStamp(unittest.TestCase):
    """含图片描述的缓存必须有创建时间，否则绝对 90 天上限对它永远不生效"""

    def test_write_includes_created_and_reads_legacy(self):
        path = os.path.join(os.environ["QQCHAT_DATA_DIR"], "vision_probe.json")
        try:
            vision._write_cache(path, "- 一张截图")
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.assertIsInstance(payload.get("_created"), float, "要写 _created 供硬上限回收")
            self.assertEqual(payload.get("digest"), "- 一张截图")
            self.assertEqual(vision._read_cache(path), "- 一张截图")
            self.assertGreater(cleanupmod._cache_created_at(path), time.time() - 60,
                               "清理任务能读到刚写入的创建时间")

            # 旧格式（没有 _created）仍要能读，避免升级后缓存集体失效、重复调用视觉模型
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"digest": "- 旧格式"}, f)
            self.assertEqual(vision._read_cache(path), "- 旧格式")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestMaxTokensConfigurable(unittest.TestCase):
    """输出预算要能通过 .env 调整，而不是让用户去改源码"""

    def test_defaults_match_legacy_values(self):
        self.assertEqual(dc.MAX_TOKENS_BY_DIM["emotion"], 32768)
        self.assertEqual(dc.MAX_TOKENS_BY_DIM["profile"], 49152)

    def test_env_override_and_invalid_value(self):
        with mock.patch.dict(os.environ, {"LLM_MAX_TOKENS_PROFILE": "65536"}):
            self.assertEqual(dc._max_tokens("profile", 49152), 65536)
        with mock.patch.dict(os.environ, {"LLM_MAX_TOKENS_PROFILE": "abc"}):
            self.assertEqual(dc._max_tokens("profile", 49152), 49152, "非法值应回退默认")
        with mock.patch.dict(os.environ, {"LLM_MAX_TOKENS_PROFILE": "10"}):
            self.assertEqual(dc._max_tokens("profile", 49152), 49152, "越界值应回退默认")


class TestInsecureBaseUrlDetection(unittest.TestCase):
    """明文 http 的非本机端点会让 Key 与聊天内容裸奔，启动时就该提醒"""

    def test_detection(self):
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "http://api.example.com/v1"):
            self.assertTrue(dc.is_insecure_base_url())
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "http://127.0.0.1:11434/v1"):
            self.assertFalse(dc.is_insecure_base_url(), "本机明文端点不算裸奔")
        with mock.patch.object(dc, "DEEPSEEK_BASE_URL", "https://api.example.com/v1"):
            self.assertFalse(dc.is_insecure_base_url())


class TestLoginRotatesSession(unittest.TestCase):
    """登录成功后轮换 CSRF token：认证态与匿名态不共用同一个 token"""

    def test_csrf_token_changes_after_login(self):
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            before = sess.get("csrf_token")
        self.assertTrue(before)
        with mock.patch.object(securitymod, "ACCESS_PASSWORD", "s3cret"):
            r = client.post("/login", data={"password": "s3cret"})
            self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            after = sess.get("csrf_token")
            self.assertTrue(sess.get("auth_ok"))
        self.assertNotEqual(before, after, "登录后 CSRF token 应轮换")


if __name__ == "__main__":
    unittest.main(verbosity=2)
