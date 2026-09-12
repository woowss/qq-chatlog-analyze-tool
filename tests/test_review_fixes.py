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
"""复审修复项的回归测试 —— 针对"承诺与实现一致性"这一组问题

覆盖：群聊防线、日志按天保留与脱敏、清理随请求触发、任务表 TTL/上限、
指纹降级与手动 salt、用量合并写盘、统计后台化、保存即哈希、
前端资源本地化、webapp 拆分后的路由面兼容。
"""

import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# 测试隔离：数据目录指向临时目录（同其它测试文件）
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
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 本进程的临时目录统一挪到数据目录下，两个好处：
# 1) %TEMP% 只读受限的环境（沙箱、部分容器）里 tempfile.* 不再直接 PermissionError；
# 2) 用例产生的临时json/图片/表情包都落在数据目录内，随测试隔离目录一起回收，
#    不会在用户 %TEMP% 里留下上百个 qqchatlog-* 垃圾目录。
_TMP_ROOT = os.path.join(os.environ["QQCHAT_DATA_DIR"], "tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from webapp import store as storemod  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))


def _chat_payload(uids_to_msgs):
    msgs = []
    base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
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
    return _wrap(msgs, {k: "人" + k[-1] for k in uids_to_msgs})


def _wrap(msgs, senders, self_uid="uA"):
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


def _bulk(uid, name, n, start=0, text="在吗", **extra):
    """批量构造某人的 n 条普通消息"""
    base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
    out = []
    for i in range(n):
        m = {
            "id": str(start + i),
            "timestamp": base + (start + i) * 60000,
            "time": "2025-03-01 20:%02d:00" % ((start + i) % 60),
            "sender": {"uid": uid, "name": name},
            "content": text,
        }
        m.update(extra)
        out.append(m)
    return out


def _write_tmp(payload):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(payload)
        return f.name


class TestGroupChatGuard(unittest.TestCase):
    """多人（群聊）导出必须被拦下，而不是静默把所有人并进"对方"。

    另一面同样重要：QQChatExporter 会给系统类消息安排占位 sender
    （name="系统消息"），实测真实私聊导出里这类条目 system 标记并不齐全，
    早期版本因此把正常私聊误判成群聊直接拒收——这里用真实形态回归。
    """

    def test_three_participant_upload_is_rejected(self):
        msgs = _bulk("uA", "我", 20) + _bulk("uB", "对方", 20, start=20) + _bulk("uC", "第三人", 6, start=40)
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方", "uC": "第三人"}))
        try:
            from parser.qq_parser import load_chat

            with self.assertRaises(ValueError) as ctx:
                load_chat(path)
            self.assertIn("群聊", str(ctx.exception))
            self.assertIn("3 位有实质发言", str(ctx.exception))
        finally:
            os.remove(path)

    def test_system_placeholder_sender_is_not_group_chat(self):
        """真实形态：占位 sender（系统消息）里混了一条无 system 标记的 type_23"""
        msgs = _bulk("uA", "我", 30) + _bulk("uB", "对方", 28, start=30)
        placeholder = _bulk("未知uid未知", "系统消息", 4, start=100, system=True)
        placeholder.append(
            {  # 唯一没有 system 标记的占位消息
                "id": "999",
                "timestamp": int(datetime(2025, 3, 2, 9, 0, tzinfo=CST).timestamp() * 1000),
                "time": "2025-03-02 09:00:00",
                "sender": {"uid": "未知uid未知", "name": "系统消息"},
                "type": "type_23",
                "content": "商城表情",
            }
        )
        path = _write_tmp(_wrap(msgs + placeholder, {"uA": "我", "uB": "对方", "未知uid未知": "系统消息"}))
        try:
            from parser.qq_parser import load_chat

            chat = load_chat(path)  # 不得抛异常
            self.assertEqual(chat.self_uid, "uA")
            self.assertEqual(len(chat.messages), 63)
        finally:
            os.remove(path)

    def test_stray_few_messages_do_not_reject(self):
        """偶发第三方条目（未达"实质参与"门槛）不该把私聊判成群聊"""
        msgs = _bulk("uA", "我", 25) + _bulk("uB", "对方", 24, start=25)
        stray = _bulk("uX", "路人", 2, start=60)
        path = _write_tmp(_wrap(msgs + stray, {"uA": "我", "uB": "对方", "uX": "路人"}))
        try:
            from parser.qq_parser import load_chat

            self.assertEqual(len(load_chat(path).messages), 51)
        finally:
            os.remove(path)

    def test_env_override_allows_multi_party(self):
        msgs = _bulk("uA", "我", 20) + _bulk("uB", "对方", 20, start=20) + _bulk("uC", "第三人", 6, start=40)
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方", "uC": "第三人"}))
        try:
            from parser.qq_parser import load_chat

            with mock.patch.dict(os.environ, {"QQCHAT_ALLOW_MULTI_PARTY": "1"}):
                chat = load_chat(path)
            self.assertEqual(len(chat.messages), 46)
            # 放行时仍按"我 vs 其他人"两类归并（口径写死在错误文案里，用户知情）
            self.assertEqual(chat.self_uid, "uA")
        finally:
            os.remove(path)

    def test_two_participant_upload_still_fine(self):
        import app as appmod

        payload = _chat_payload({"uA": ["在吗"], "uB": ["在的"]})
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        r = client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "ok.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            p, h = sess.get("filepath"), sess.get("chat_hash")
        storemod.wait_for_stats(h)
        try:
            storemod._purge_chat_caches(h)
        finally:
            if p and os.path.exists(p):
                os.remove(p)


class TestLogPrivacy(unittest.TestCase):
    """日志：脱敏助手 + 保留天数来自配置"""

    def test_mask_name_keeps_first_char(self):
        import analyzer.logger as L

        with mock.patch.object(L, "LOG_REDACT_NAMES", True):
            self.assertEqual(L.mask_name("阿甜"), "阿*")
            self.assertEqual(L.mask_name("甜"), "*")  # 单字也不泄露全名
            self.assertEqual(L.mask_name(""), "*")
        with mock.patch.object(L, "LOG_REDACT_NAMES", False):
            self.assertEqual(L.mask_name("阿甜"), "阿甜")

    def test_timed_rotating_handler_with_retention(self):
        from logging.handlers import TimedRotatingFileHandler
        import analyzer.logger as L
        import config

        base = L.get_logger()
        handlers = [h for h in base.handlers if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(handlers), 1, "文件 handler 必须是按天轮转且只有一个")
        self.assertEqual(handlers[0].backupCount, config.LOG_RETENTION_DAYS)
        self.assertLessEqual(handlers[0].backupCount, 90, "日志保留必须有上界，5×5MB 式无限留存不得回潮")


class TestCleanupTriggeredWithoutUpload(unittest.TestCase):
    """清理不再只挂在上传路径：任何请求都会按小时去抖触发"""

    def test_get_request_triggers_maybe_cleanup(self):
        import app as appmod
        from webapp import cleanup as cleanupmod

        with (
            mock.patch.object(cleanupmod, "_last_cleanup", [0.0]),
            mock.patch.object(cleanupmod, "cleanup_old_files") as co,
        ):
            appmod.app.test_client().get("/")
            self.assertTrue(co.called, "GET / 也必须触发过期回收")

    def test_debounce_prevents_rescan(self):
        from webapp import cleanup as cleanupmod

        with (
            mock.patch.object(cleanupmod, "_last_cleanup", [time.time()]),
            mock.patch.object(cleanupmod, "cleanup_old_files") as co,
        ):
            cleanupmod.maybe_cleanup(3600)
            self.assertFalse(co.called, "一小时内重复请求不该反复扫描目录")


class TestFingerprintRobustness(unittest.TestCase):
    """源码不可读时指纹降级不崩；PROMPT_CACHE_SALT 可手动换键"""

    def test_fallback_when_source_unavailable(self):
        import analyzer.deepseek_client as dc

        with (
            mock.patch("inspect.getsource", side_effect=OSError("no source here")),
            mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": ""}),
        ):
            fp = dc._prompt_fingerprint()
            fp2 = dc._prompt_fingerprint()  # 必须在同一个降级上下文里比稳定性
        self.assertEqual(len(fp), 12)
        self.assertNotEqual(fp, dc.PROMPT_FINGERPRINT, "降级指纹应与正常指纹不同")
        self.assertEqual(fp, fp2, "降级指纹仍须稳定")

    def test_salt_changes_fingerprint(self):
        import analyzer.deepseek_client as dc

        with mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "2026-08-a"}):
            fp1 = dc._prompt_fingerprint()
            fp2 = dc._prompt_fingerprint()
        with mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "2026-08-b"}):
            fp3 = dc._prompt_fingerprint()
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertNotEqual(fp1, dc.PROMPT_FINGERPRINT)

    def test_grace_hours_env_invalid_does_not_crash(self):
        import analyzer.deepseek_client as dc

        with mock.patch.dict(os.environ, {"LLM_MONTH_CACHE_GRACE_HOURS": "abc"}):
            val = dc._env_number("LLM_MONTH_CACHE_GRACE_HOURS", 24, 0, 720)
        self.assertEqual(val, 24)  # 回退默认而不是 ValueError 崩在 import


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


class TestUsageBatchWrite(unittest.TestCase):
    """50 次调用合并成一次落盘；读接口永远能立刻看到最新值"""

    def test_batching_and_read_your_write(self):
        import analyzer.usage as usage

        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(tmp)
        orig_file, orig_dump = usage.TOKEN_USAGE_FILE, usage._dump
        writes = []

        def counting_dump(d):
            writes.append(1)
            orig_dump(d)

        usage.TOKEN_USAGE_FILE = tmp
        usage._dump = counting_dump
        try:
            usage.flush()  # 清掉别的测试可能留下的未决增量
            for _ in range(50):
                usage.record_call("deepseek-flash", "emotion", 100, 20)
            self.assertEqual(len(writes), 0, "增量不该逐次落盘")
            u = usage.get_usage()
            self.assertEqual(u["total"]["calls"], 50)
            self.assertEqual(u["dims"]["emotion|deepseek-flash"]["calls"], 50)
            self.assertEqual(len(writes), 1, "读前应恰好冲刷一次")
            usage.get_usage()
            self.assertEqual(len(writes), 1, "无增量的读不该再写盘")
        finally:
            usage._dump = orig_dump
            usage.TOKEN_USAGE_FILE = orig_file
            usage.flush()
            if os.path.exists(tmp):
                os.remove(tmp)


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

        payload = _chat_payload({"uA": ["在吗", "今晚吃啥"], "uB": ["在的", "火锅"]})
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


class TestSaveAndHash(unittest.TestCase):
    """保存即哈希：与落盘后重读的结果一致"""

    def test_hash_equals_reread(self):
        from webapp import store as storemod

        class FS:
            def __init__(self, data):
                self.stream = io.BytesIO(data)

        data = json.dumps({"hello": "世界" * 500}).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "f.json")
            fs = FS(data)
            size, h = storemod.save_and_hash(fs, dest)
            self.assertEqual(size, len(data))
            self.assertEqual(h, storemod._chat_hash(dest))

    def test_upload_records_hash_from_stream(self):
        import app as appmod

        payload = _chat_payload({"uA": ["在吗"], "uB": ["在的"]})
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        r = client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "h.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            path, h = sess["filepath"], sess["chat_hash"]
        try:
            self.assertEqual(h, storemod._chat_hash(path))
        finally:
            storemod._purge_chat_caches(h)
            if path and os.path.exists(path):
                os.remove(path)


class TestLocalizedAssets(unittest.TestCase):
    """前端资源本地化：页面不再伸手向 CDN；导出仍换回 CDN"""

    def test_vendor_files_present_and_nonempty(self):
        vendor = ROOT / "web" / "static" / "vendor"
        for name, min_size in (
            ("bootstrap.min.css", 100_000),
            ("bootstrap.bundle.min.js", 50_000),
            ("jquery.min.js", 50_000),
            ("echarts.min.js", 500_000),
            ("echarts-wordcloud.min.js", 10_000),
        ):
            p = vendor / name
            self.assertTrue(p.exists(), f"缺少本地化资源 {name}")
            self.assertGreater(p.stat().st_size, min_size, f"{name} 体积异常，疑似下载失败")

    def test_page_templates_have_no_runtime_cdn(self):
        for name in ("base.html", "login.html"):
            src = (ROOT / "web" / "templates" / name).read_text(encoding="utf-8")
            self.assertNotIn("cdn.jsdelivr", src, f"{name} 仍在运行时拉 CDN")

    def test_exported_report_rewrites_vendor_to_cdn(self):
        src = (ROOT / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        self.assertIn("VENDOR_CDN", src, "导出必须把 vendor 路径换回 CDN，否则分享出去的报告没了样式")
        for name in ("bootstrap.min.css", "jquery.min.js", "echarts.min.js"):
            self.assertIn(name, src)

    def test_theme_toggle_has_view_transition(self):
        css = (ROOT / "web" / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn("@view-transition", css, "主题切换 reload 应带跨文档过渡，缓解整页闪跳")

    def test_report_cdn_map_covers_every_vendor_file(self):
        """base.html 引用的本地 vendor 资源必须都在导出映射表里。

        漏一个的后果很隐蔽：应用内一切正常，只有"下载 HTML 发给别人"时
        那份报告掉了样式/图表，而导出是纯前端拼接，没人会立刻发现。
        """
        import re

        base = (ROOT / "web" / "templates" / "base.html").read_text(encoding="utf-8")
        report = (ROOT / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        refs = set(re.findall(r"filename='vendor/([\w.-]+)'", base))
        self.assertTrue(refs, "base.html 应引用本地化的 vendor 资源")
        for name in sorted(refs):
            self.assertIn(
                "'%s'" % name, report, f"{name} 不在 report.html 的 VENDOR_CDN 映射里，导出的报告会 404"
            )


class TestMediaParticipation(unittest.TestCase):
    """文件/视频/转发/卡片/通话/商城表情都要参与统计与分析（真实导出里的形态）"""

    @staticmethod
    def _msg_with(el_type, data, msg_type=None, text="", **kw):
        m = {
            "id": "1",
            "timestamp": 1758031009000,
            "time": "2025-09-16T13:56:49.000Z",
            "sender": {"uid": "uA", "name": "我"},
            "type": msg_type or el_type,
            "content": {"text": text, "elements": [{"type": el_type, "data": data}]},
        }
        m.update(kw)
        return m

    def _load(self, msgs):
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方"}))
        try:
            from parser.qq_parser import load_chat

            return load_chat(path)
        finally:
            os.remove(path)

    def test_file_and_video_carry_labels_but_not_body_text(self):
        chat = self._load(
            [
                self._msg_with(
                    "file", {"filename": "example.zip", "size": "12345"}, text="[文件:example.zip]"
                ),
                self._msg_with("video", {"filename": "clip.mp4", "size": "999"}, text="[视频:clip.mp4]"),
            ]
        )
        f, v = chat.messages
        self.assertEqual((f.media_kind, f.media_label), ("file", "example.zip"))
        self.assertEqual((v.media_kind, v.media_label), ("video", "clip.mp4"))
        # 正文保持干净：文件名不能灌进词频/句长口径
        self.assertEqual(f.text, "")
        self.assertEqual(v.text, "")

    def test_forward_card_gets_title_and_count(self):
        chat = self._load(
            [
                self._msg_with(
                    "forward",
                    {"title": "小明和小红的聊天记录", "messageCount": "25"},
                    msg_type="json",
                    text="[转发消息: 25条]",
                )
            ]
        )
        m = chat.messages[0]
        self.assertEqual(m.media_kind, "forward")
        self.assertIn("25条", m.media_label)
        self.assertIn("聊天记录", m.media_label)

    def test_call_record_and_json_card_and_wallet(self):
        chat = self._load(
            [
                self._msg_with("av_record", {}, msg_type="type_19", text="通话 - 未接听，点击回拨"),
                self._msg_with("json", {}, msg_type="json", text="[JSON消息]"),
                self._msg_with(
                    "wallet", {"summary": "红包/钱包消息"}, msg_type="type_10", text="红包/钱包消息"
                ),
            ]
        )
        kinds = [m.media_kind for m in chat.messages]
        self.assertEqual(kinds, ["av_record", "json", "wallet"])
        self.assertIn("未接听", chat.messages[0].media_label)

    def test_market_face_counts_as_face_and_is_not_skipped(self):
        """type_17（商城大表情）此前被整个跳过，是 277 条白白丢掉的信号"""
        chat = self._load(
            [self._msg_with("market_face", {"name": "[[叉腰]]"}, msg_type="type_17", text="[[叉腰]]")]
        )
        m = chat.messages[0]
        self.assertEqual(m.face_names, ["叉腰"])
        from parser.qq_parser import is_statistical

        self.assertTrue(is_statistical(m), "商城表情不该被当成不可分析的卡片")
        from analyzer.local_stats import calc_face_stats, calc_overview

        self.assertEqual(calc_overview(chat)["total_faces"], 1)
        self.assertEqual(calc_face_stats(chat)["self"], {"叉腰": 1})

    def test_media_messages_enter_ai_dialog_with_markers(self):
        import analyzer.deepseek_client as dc

        chat = self._load(
            [
                self._msg_with("file", {"filename": "示例表.xlsx"}, text="[文件:示例表.xlsx]"),
                self._msg_with(
                    "forward",
                    {"title": "小明和小红的聊天记录", "messageCount": "4"},
                    msg_type="json",
                    text="[转发消息: 4条]",
                ),
                self._msg_with("av_record", {}, msg_type="type_19", text="通话 - 未接听"),
            ]
        )
        for m in chat.messages:
            self.assertTrue(dc._has_content(m), "媒体消息必须能进 AI 对话")
        lines = [dc._message_line(m, "我") for m in chat.messages]
        self.assertIn("[文件:示例表.xlsx]", lines[0])
        self.assertIn("转发:", lines[1])
        self.assertIn("通话:", lines[2])

    def test_media_does_not_pollute_length_or_word_stats(self):
        from analyzer.local_stats import calc_message_length_stats

        long_name = "a" * 80 + ".zip"
        chat = self._load([self._msg_with("file", {"filename": long_name}, text=f"[文件:{long_name}]")])
        stats = calc_message_length_stats(chat)
        self.assertEqual(stats["self"]["max"], 0, "文件名不该计入发言长度")

    def test_overview_counts_media_by_kind(self):
        from analyzer.local_stats import calc_overview

        chat = self._load(
            [
                self._msg_with("file", {"filename": "a.zip"}),
                self._msg_with("file", {"filename": "b.zip"}),
                self._msg_with("video", {"filename": "c.mp4"}),
                self._msg_with("forward", {"title": "t", "messageCount": "3"}, msg_type="json"),
                self._msg_with("wallet", {"summary": "红包"}),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_files"], 2)
        self.assertEqual(ov["total_videos"], 1)
        self.assertEqual(ov["total_forwards"], 1)
        self.assertEqual(ov["total_other_media"], 1)

    def test_media_volume_and_dedup(self):
        """导出器给到的 size/md5 用于体积与去重统计（同一张图反复发只算一张）"""
        from analyzer.local_stats import calc_overview

        img = {"filename": "a.jpg", "size": "2048", "md5": "AAA"}
        chat = self._load(
            [
                self._msg_with("image", img),
                self._msg_with("image", img),  # 同一张图重复发送
                self._msg_with("image", {"filename": "b.jpg", "size": "1024", "md5": "BBB"}),
                self._msg_with("file", {"filename": "c.zip", "size": "4096", "md5": "CCC"}),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 3)
        self.assertEqual(ov["image_bytes"], 2048 * 2 + 1024)
        self.assertEqual(ov["unique_images"], 2)
        self.assertEqual(ov["other_media_bytes"], 4096, "文件体积计入非图片媒体，不计入图片体积")

    def test_media_size_dirty_values_do_not_crash(self):
        from analyzer.local_stats import calc_overview

        chat = self._load([self._msg_with("image", {"filename": "x.jpg", "size": "未知", "md5": None})])
        ov = calc_overview(chat)
        self.assertEqual(ov["image_bytes"], 0)
        self.assertIsNone(ov["unique_images"], "没有 md5 时应给 None 而不是假装 0")


class TestTimeNormalization(unittest.TestCase):
    """时间字符串必须与统计口径一致（新版导出器写的是 UTC ISO 时间）"""

    def test_iso_utc_time_is_converted_to_cst(self):
        from parser.qq_parser import load_chat

        msgs = [
            {
                "id": "1",
                "timestamp": 1758031009000,
                "time": "2025-09-16T13:56:49.000Z",
                "sender": {"uid": "uA", "name": "我"},
                "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
            }
        ]
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方"}))
        try:
            chat = load_chat(path)
        finally:
            os.remove(path)
        self.assertEqual(chat.messages[0].time_str, "2025-09-16 21:56:49")

    def test_iso_fallback_when_timestamp_missing(self):
        from parser.qq_parser import _parse_timestamp

        ts = _parse_timestamp(None, "2025-09-16T13:56:49.000Z")
        self.assertIsNotNone(ts, "缺 timestamp 时应能从 ISO 字符串回退解析")
        self.assertEqual(ts, 1758031009000)


class TestVisionDigest(unittest.TestCase):
    """图片理解：挑图规则、摘要缓存（同一批图只花一次视觉调用）、注入 prompt"""

    @staticmethod
    def _png(w, h, rgb):
        import struct
        import zlib

        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

        def chunk(tag, data):
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    def _media_dir(self, tmp, specs):
        """specs: [(相对路径, 宽, 高, md5)] → 建立假图片文件并返回消息列表"""
        from parser.qq_parser import Message

        msgs = []
        for i, (rel, w, h, md5) in enumerate(specs):
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(self._png(max(w, 1), max(h, 1), (10, 20, 30)))
            msgs.append(
                Message(
                    id=str(i),
                    timestamp=1758031009000 + i * 1000,
                    time_str="2025-09-16 21:56:49",
                    sender_name="我",
                    sender_uid="uA",
                    text="",
                    raw_text="",
                    msg_type="type_3",
                    has_image=True,
                    is_reply=False,
                    media_path=rel,
                    media_w=w,
                    media_h=h,
                    media_id=md5 or "",
                )
            )
        return msgs

    def test_disabled_without_media_root(self):
        from analyzer import vision

        with mock.patch.object(vision, "MEDIA_ROOT", ""):
            self.assertFalse(vision.available())
            with mock.patch("analyzer.deepseek_client._call_vision") as call:
                self.assertEqual(vision.digest([], chat_hash="h"), "")
                self.assertFalse(call.called, "没配媒体目录时不该发起任何视觉调用")

    def test_pick_images_dedupes_small_and_missing(self):
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(
                tmp,
                [
                    ("resources/images/a.png", 1080, 2400, "AAA"),
                    ("resources/images/a2.png", 1080, 2400, "AAA"),  # 同一张图（md5 相同）
                    ("resources/images/tiny.png", 64, 64, "TINY"),  # 表情包尺寸，跳过
                    ("resources/images/b.png", 1200, 900, "BBB"),
                ],
            )
            msgs.append(
                msgs[0].__class__(
                    id="9",
                    timestamp=1,
                    time_str="",
                    sender_name="我",
                    sender_uid="uA",
                    text="",
                    raw_text="",
                    msg_type="type_3",
                    has_image=True,
                    is_reply=False,
                    media_path="resources/images/missing.png",
                    media_w=1000,
                    media_h=1000,
                    media_id="MISSING",
                )
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "VISION_MAX_BYTES", 10**7),
            ):
                picked = vision.pick_images(msgs, limit=8)
            self.assertEqual([p["key"] for p in picked], ["AAA", "BBB"])

    def test_digest_cached_across_dimensions_and_runs(self):
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(
                tmp,
                [
                    ("resources/images/a.png", 1080, 2400, "AAA"),
                    ("resources/images/b.png", 1200, 900, "BBB"),
                ],
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "_MEMO", {}),
                mock.patch(
                    "analyzer.deepseek_client._call_vision",
                    return_value="- 截图：在讨论选课\n- 照片：路边的小猫",
                ) as call,
            ):
                first = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                second = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                self.assertIn("小猫", first)
                self.assertEqual(first, second)
                self.assertEqual(call.call_count, 1, "同一批图只该花一次视觉调用")
                # 模拟进程重启：内存缓存清空，磁盘缓存仍应命中
                vision._MEMO.clear()
                third = vision.digest(msgs, chat_hash="hashV", label="2026-08 月")
                self.assertEqual(third, first)
                self.assertEqual(call.call_count, 1, "磁盘缓存应跨进程复用")

    def test_digest_injected_into_dialog(self):
        import analyzer.deepseek_client as dc
        from analyzer import vision

        with tempfile.TemporaryDirectory() as tmp:
            msgs = self._media_dir(tmp, [("resources/images/a.png", 1080, 2400, "AAA")])
            text_msg = msgs[0].__class__(
                id="x",
                timestamp=1758031009000,
                time_str="2025-09-16 21:56:49",
                sender_name="我",
                sender_uid="uA",
                text="在吗",
                raw_text="在吗",
                msg_type="type_1",
                has_image=False,
                is_reply=False,
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", tmp),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
                mock.patch.object(vision, "_MEMO", {}),
                mock.patch("analyzer.deepseek_client._call_vision", return_value="- 截图：课程表"),
            ):
                dialog = dc._build_dialog(
                    [text_msg, msgs[0]], "uA", "我", "对方", chat_hash="h", vision_label="2026-08 月"
                )
        self.assertIn("图片内容摘要", dialog)
        self.assertIn("课程表", dialog)

    def test_vision_failure_does_not_break_text_analysis(self):
        """图片理解出任何非致命问题，都必须退回纯文本分析而不是让分析失败"""
        import analyzer.deepseek_client as dc
        from analyzer import vision

        with mock.patch.object(vision, "digest", side_effect=RuntimeError("视觉挂了")):
            self.assertEqual(dc._vision_digest([], "h", "标签"), "")
        with mock.patch.object(vision, "digest", side_effect=dc.QuotaExhaustedError("额度")):
            with self.assertRaises(dc.QuotaExhaustedError):
                dc._vision_digest([], "h", "标签")


class TestWebUIMediaUpload(unittest.TestCase):
    """WebUI 两阶段上传：先传 JSON → 服务端告知要哪些图 → 浏览器只传这些图"""

    def setUp(self):
        import app as appmod
        from analyzer import vision

        self.appmod = appmod
        self.vision = vision
        self.client = appmod.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as s:
            self.token = s["csrf_token"]
        self.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": self.token}
        self._dir = tempfile.mkdtemp(prefix="qqchatlog-media-")
        # 用真实的图片元素构造导出：两条图片消息 + 一条文本
        self.payload = _wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1758031009000,
                    "time": "2025-09-16T13:56:49.000Z",
                    "sender": {"uid": "uA", "name": "我"},
                    "type": "type_3",
                    "content": {
                        "text": "",
                        "elements": [
                            {
                                "type": "image",
                                "data": {
                                    "filename": "pic.jpg",
                                    "md5": "AAA",
                                    "size": "2048",
                                    "width": "1080",
                                    "height": "2400",
                                    "url": "resources/images/aaa_pic.jpg",
                                },
                            }
                        ],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1758031069000,
                    "time": "2025-09-16T13:57:49.000Z",
                    "sender": {"uid": "uB", "name": "对方"},
                    "type": "type_1",
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )

    def tearDown(self):
        import shutil

        with self.client.session_transaction() as s:
            chat_hash, path = s.get("chat_hash"), s.get("filepath")
        if chat_hash:
            storemod._purge_chat_caches(chat_hash)
            shutil.rmtree(self.vision.session_media_dir(chat_hash), ignore_errors=True)
        if path and os.path.exists(path):
            os.remove(path)
        shutil.rmtree(self._dir, ignore_errors=True)

    def _upload_json(self):
        r = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(self.payload), "c.json")},
            headers={**self.headers, "X-Requested-With": "fetch"},
        )
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def _png(self, w=1080, h=2400):
        return TestVisionDigest._png(w, h, (200, 100, 50))

    def test_ajax_upload_returns_wanted_media(self):
        body = self._upload_json()
        self.assertTrue(body["ok"])
        self.assertIn(
            "aaa_pic.jpg", [os.path.basename(p) for p in body["wanted_media"]], "应告知前端需要哪张图"
        )
        self.assertEqual(body["next"], "/dashboard")

    def test_plain_form_upload_still_redirects(self):
        """没有 JS/没选目录时，普通表单提交必须照旧 302 跳转"""
        r = self.client.post(
            "/upload", data={"file": (io.BytesIO(self.payload), "c.json")}, headers=self.headers
        )
        self.assertEqual(r.status_code, 302)

    def test_media_endpoint_stores_only_images_and_blocks_traversal(self):
        self._upload_json()
        data = {
            "files": [
                (io.BytesIO(self._png()), "aaa_pic.jpg"),
                (io.BytesIO(b"not an image"), "evil.exe"),  # 扩展名白名单拦下
                (io.BytesIO(self._png()), "../../../evil.jpg"),  # 路径穿越：只取 basename
            ],
        }
        r = self.client.post(
            "/api/media", data=data, headers=self.headers, content_type="multipart/form-data"
        )
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["saved"], 2)
        self.assertEqual(body["skipped"], 1)
        with self.client.session_transaction() as s:
            chat_hash = s["chat_hash"]
        media_dir = self.vision.session_media_dir(chat_hash)
        self.assertEqual(sorted(os.listdir(media_dir)), ["aaa_pic.jpg", "evil.jpg"])
        # 越界文件绝不能写到 uploads/ 之外
        self.assertFalse(
            os.path.exists(os.path.abspath(os.path.join(media_dir, "..", "..", "..", "evil.jpg")))
        )

    def test_digest_uses_uploaded_copy_without_media_root(self):
        """没配 QQCHAT_MEDIA_DIR 也应该能用（图片来自 WebUI 上传的副本）"""
        self._upload_json()
        self.client.post(
            "/api/media",
            data={"files": [(io.BytesIO(self._png()), "aaa_pic.jpg")]},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        from parser.qq_parser import load_chat

        with self.client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        chat = load_chat(path)
        with (
            mock.patch.object(self.vision, "MEDIA_ROOT", ""),
            mock.patch.object(self.vision, "VISION_MIN_SIDE", 100),
            mock.patch.object(self.vision, "_MEMO", {}),
            mock.patch("analyzer.deepseek_client._call_vision", return_value="- 截图：宠物医院候诊") as call,
        ):
            text = self.vision.digest(chat.messages, chat_hash=chat_hash, label="2025-09 月")
        self.assertIn("宠物医院", text)
        self.assertEqual(call.call_count, 1)

    def test_media_endpoint_rejects_without_session(self):
        fresh = self.appmod.app.test_client()
        fresh.get("/")
        with fresh.session_transaction() as s:
            tok = s["csrf_token"]
        r = fresh.post(
            "/api/media",
            data={"files": [(io.BytesIO(self._png()), "x.jpg")]},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": tok},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 400)


class TestFaceEmoji(unittest.TestCase):
    """表情排行用 emoji 渲染：有对应就画表情，没有就回退名字（不猜）"""

    def test_known_faces_map_to_emoji(self):
        from analyzer.face_emoji import emoji_for

        self.assertEqual(emoji_for("/可怜"), "🥺")
        self.assertEqual(emoji_for("/流泪"), "😢")
        self.assertEqual(emoji_for("/doge"), "🐶")
        self.assertEqual(emoji_for("微笑"), "🙂")  # 不带斜杠也能认

    def test_qq_only_faces_fall_back_to_empty(self):
        """QQ 专属超级表情/商城表情没有 Unicode 对应，必须返回空串而不是硬凑"""
        from analyzer.face_emoji import emoji_for

        for name in ("/吃糖", "/大怨种", "/菜汪", "/宕机", "/偷感", "[[叉腰]]", "[13]"):
            self.assertEqual(emoji_for(name), "", f"{name} 不该被硬映射成某个 emoji")

    def test_emoji_map_only_keeps_mapped(self):
        from analyzer.face_emoji import emoji_map

        m = emoji_map(["/可怜", "/吃糖", "/流泪"])
        self.assertEqual(m, {"/可怜": "🥺", "/流泪": "😢"})

    def test_habits_page_embeds_emoji_map(self):
        """页面里真的把 emoji 传给了前端渲染"""
        import app as appmod

        payload = _wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1758031009000,
                    "time": "2025-09-16 21:56:49",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "111", "name": "/可怜"}}],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1758031019000,
                    "time": "2025-09-16 21:56:59",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            body = client.get("/habits").get_data(as_text=True)
            # tojson 会把 emoji 转成 \ud83e\udd7a 转义，这里还原后断言真实字符。
            # 现在是 renderFaceBarChart(dom, 排行数组, 名字, emoji 映射, 表情图映射)
            m = re.search(
                r"renderFaceBarChart\('selfFaceChart',\s*\[.*?\],\s*\"[^\"]*\",\s*"
                r"(\{.*?\}),\s*\{.*?\}\);",
                body,
            )
            self.assertIsNotNone(m, "习惯页应调用 renderFaceBarChart 并传入 emoji 映射")
            mapping = json.loads(m.group(1))
            self.assertEqual(mapping.get("/可怜"), "🥺")
            report = client.get("/report").get_data(as_text=True)
            self.assertIn("🥺", report, "报告页也应显示 emoji")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)

    def test_face_ranking_keeps_count_order(self):
        """表情排行必须按次数排：Flask 的 tojson 默认 sort_keys=True，
        直接传 dict 会被按汉字码点重排，"排行"名不副实（真实踩到过）。"""
        import app as appmod

        payload = _wrap(
            [
                {
                    "id": str(i),
                    "timestamp": 1758031009000 + i * 1000,
                    "time": "2025-09-16 21:56:49",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "5", "name": face}}],
                    },
                }
                # 次数：/流泪 3 次 > /可怜 2 次 > /微笑 1 次，
                # 但按编码排序会变成 可怜 < 微笑 < 流泪，正好能验出问题
                for i, face in enumerate(["/流泪", "/流泪", "/流泪", "/可怜", "/可怜", "/微笑"])
            ]
            + [
                {
                    "id": "x",
                    "timestamp": 1758031069000,
                    "time": "2025-09-16 21:57:49",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": token},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            body = client.get("/habits").get_data(as_text=True)
            m = re.search(r"renderFaceBarChart\('selfFaceChart',\s*(\[\[.*?\]\]),", body)
            self.assertIsNotNone(m, "表情排行应按二维数组传参（保序）")
            ranking = json.loads(m.group(1))
            self.assertEqual([r[0] for r in ranking][:3], ["/流泪", "/可怜", "/微笑"])
            self.assertEqual([r[1] for r in ranking][:3], [3, 2, 1], "必须按次数降序")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)


class TestFaceImagesOptional(unittest.TestCase):
    """原始表情图是可选功能：默认关、联网失败必须优雅降级"""

    def setUp(self):
        from analyzer import face_images

        self.fi = face_images
        self._tmp = tempfile.mkdtemp(prefix="qqchatlog-faces-")
        self._patches = [
            mock.patch.object(face_images, "FACE_CACHE_DIR", self._tmp),
            mock.patch.object(face_images, "FACE_IMAGES_ENABLED", True),
            mock.patch.object(face_images, "_COLLECT_MEMO", {}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        import shutil

        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_disabled_by_default(self):
        import config

        self.assertFalse(config.FACE_IMAGES_ENABLED, "默认必须是关的（可选项）")
        with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
            self.assertFalse(self.fi.enabled())
            self.assertEqual(self.fi.ensure({"/流泪": {"key": "c5"}}), {})

    def test_url_resolved_by_name_not_by_export_id(self):
        """按名字取图：同一张表情在不同导出里的 id 可能不同，名字才与显示一致"""
        self.assertEqual(self.fi.url_for("/可怜"), "https://qzonestyle.gtimg.cn/qzone/em/e153.gif")
        self.assertEqual(self.fi.url_for("流泪"), "https://qzonestyle.gtimg.cn/qzone/em/e105.gif")
        # 超级表情：既不在经典表里、也没有商城地址 → 绝不猜地址
        self.assertIsNone(self.fi.url_for("/吃糖"))
        self.assertIsNone(self.fi.url_for("/大怨种"))

    def test_market_face_uses_its_own_url(self):
        url = "https://gxh.vip.qq.com/club/item/parcel/item/3b/abc/raw300.gif"
        self.assertEqual(self.fi.url_for("[13]", url), url)
        self.assertNotEqual(self.fi.key_for("[13]", url), self.fi.key_for("[13]"))

    def test_foreign_host_is_refused(self):
        """只有已知表情 CDN 允许访问，避免这段代码被当成任意下载器"""
        self.assertIsNone(self.fi.url_for("x", "https://evil.example.com/a.gif"))
        with (
            mock.patch.object(self.fi, "urlopen", create=True) as _u,
            mock.patch("urllib.request.urlopen") as real,
        ):
            self.assertIsNone(self.fi._download("https://evil.example.com/a.gif"))
            self.assertFalse(real.called)

    def test_offline_failure_degrades_gracefully(self):
        import urllib.error

        with mock.patch.object(self.fi, "_download", side_effect=urllib.error.URLError("no network")):
            out = self.fi.ensure({"/流泪": {}}, allow_network=True)
        self.assertEqual(out, {}, "离线时应静默返回空，交由 emoji/文字回退")

    def test_cached_file_is_reused_without_network(self):
        with open(os.path.join(self._tmp, "c5.gif"), "wb") as f:
            f.write(b"GIF89a" + b"y" * 10)
        with mock.patch.object(self.fi, "_download") as dl:
            out = self.fi.ensure({"/流泪": {}}, allow_network=True)
        self.assertFalse(dl.called, "已有缓存时不该联网")
        self.assertIn("/流泪", out)
        self.assertTrue(out["/流泪"]["path"].endswith("c5.gif"))

    def test_local_pack_wins_over_network(self):
        """本地表情包目录优先于联网（超级表情只能靠它拿到原图）"""
        pack = tempfile.mkdtemp(prefix="qqchatlog-pack-")
        try:
            with open(os.path.join(pack, "吃糖.gif"), "wb") as f:
                f.write(b"GIF89a" + b"z" * 10)
            with (
                mock.patch.dict(os.environ, {"QQCHAT_FACE_DIR": pack}),
                mock.patch.object(self.fi, "_download") as dl,
            ):
                out = self.fi.ensure({"/吃糖": {}}, allow_network=True)
            self.assertFalse(dl.called, "本地已有图就不该联网")
            self.assertIn("/吃糖", out)
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)

    def test_serve_path_rejects_traversal(self):
        self.assertIsNone(self.fi.serve_path("../../etc/passwd"))
        self.assertIsNone(self.fi.serve_path("c5/../../x"))
        with open(os.path.join(self._tmp, "c5.gif"), "wb") as f:
            f.write(b"GIF89a")
        self.assertTrue(self.fi.serve_path("c5"))

    def test_fetch_endpoint_requires_switch(self):
        import app as appmod

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            tok = s["csrf_token"]
        headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": tok}
        with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
            r = client.post("/api/faces/fetch", headers=headers)
            self.assertEqual(r.status_code, 403, "开关关闭时接口必须拒绝")
        r2 = client.post("/api/faces/fetch", headers=headers)
        self.assertEqual(r2.status_code, 400, "没有聊天记录时提示重新上传")

    def test_habits_page_shows_button_when_enabled(self):
        import app as appmod

        payload = _wrap(
            [
                {
                    "id": "1",
                    "timestamp": 1758031009000,
                    "time": "2025-09-16 21:56:49",
                    "sender": {"uid": "uA", "name": "我"},
                    "content": {
                        "text": "",
                        "elements": [{"type": "face", "data": {"id": "5", "name": "/流泪"}}],
                    },
                },
                {
                    "id": "2",
                    "timestamp": 1758031069000,
                    "time": "2025-09-16 21:57:49",
                    "sender": {"uid": "uB", "name": "对方"},
                    "content": {"text": "在吗", "elements": [{"type": "text", "data": {"text": "在吗"}}]},
                },
            ],
            {"uA": "我", "uB": "对方"},
        )
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            tok = s["csrf_token"]
        client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "c.json")},
            headers={"Origin": "http://localhost:5000", "X-CSRF-Token": tok},
        )
        with client.session_transaction() as s:
            chat_hash, path = s["chat_hash"], s["filepath"]
        try:
            storemod.wait_for_stats(chat_hash, timeout=20)
            on = client.get("/habits").get_data(as_text=True)
            self.assertIn('id="fetchFacesBtn"', on, "开关打开时应给出联网抓取入口")
            with mock.patch.object(self.fi, "FACE_IMAGES_ENABLED", False):
                off = client.get("/habits").get_data(as_text=True)
            self.assertNotIn('id="fetchFacesBtn"', off, "开关关闭时不该出现联网入口")
        finally:
            storemod._purge_chat_caches(chat_hash)
            if path and os.path.exists(path):
                os.remove(path)

    def test_report_export_does_not_swallow_the_body(self):
        """导出报告在 DOM 克隆体上删节点，不再对 HTML 字符串做正则手术。

        曾经的写法 `<script(?![^>]*src=)[^>]*>[\\s\\S]*?window\\.CSRF_TOKEN[\\s\\S]*?</script>`
        会从 <head> 的主题脚本一路匹配到页面底部的 CSRF 脚本，把整个 body 删掉
        （实测导出的 HTML 只剩 205 字节）。这里守住"按节点处理"的写法：
        跨脚本的贪婪正则不许再出现，CSRF 脚本仍要按内容精确剔除。
        """
        src = (ROOT / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        # 注释里会引用旧写法作为反面教材，所以只检查真正的代码行
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))
        self.assertNotIn("[\\s\\S]*?window\\.CSRF_TOKEN", code, "不许再用跨脚本的贪婪正则删 CSRF 脚本")
        self.assertNotIn(
            "document.documentElement.outerHTML", code, "导出快照必须走 cloneNode，不能拿整页 HTML 串做替换"
        )
        self.assertIn("cloneNode(true)", code, "应当在 DOM 克隆体上删改")
        self.assertIn("window\\.CSRF_TOKEN", code, "仍要剥掉带 token 的那个脚本")
        self.assertIn("querySelectorAll('script')", code, "按节点遍历脚本，逐块判断")

    def test_report_export_inlines_face_images(self):
        """导出的报告要自带表情图（内联成 data URL），否则分享出去就是裂图"""
        src = (ROOT / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        self.assertIn("inlineFaceImages", src)
        self.assertIn("readAsDataURL", src)


class TestPathContainment(unittest.TestCase):
    """导出文件是可以被构造的：其中的路径字段不能把读取范围带出允许的目录"""

    def test_vision_refuses_path_outside_media_root(self):
        from analyzer import vision
        from parser.qq_parser import Message

        base = tempfile.mkdtemp(prefix="qqchatlog-traversal-")
        media_root = os.path.join(base, "exports")
        os.makedirs(media_root, exist_ok=True)
        outside = os.path.join(base, "private.png")  # 媒体根目录之外的图片
        with open(outside, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        inside = os.path.join(media_root, "ok.png")
        with open(inside, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"y" * 32)
        try:
            with mock.patch.object(vision, "MEDIA_ROOT", media_root):
                self.assertIsNone(vision.resolve("../private.png"), "不得解析到媒体根目录之外的文件")
                self.assertIsNone(vision.resolve("..\\private.png"))
                self.assertIsNone(vision.resolve("resources/../../private.png"))
                self.assertEqual(vision.resolve("ok.png"), inside)
            # 也不该被挑进"要送给模型的图片"
            msg = Message(
                id="1",
                timestamp=1758031009000,
                time_str="",
                sender_name="我",
                sender_uid="uA",
                text="",
                raw_text="",
                msg_type="type_3",
                has_image=True,
                is_reply=False,
                media_path="../private.png",
                media_w=1080,
                media_h=2400,
                media_id="X",
            )
            with (
                mock.patch.object(vision, "MEDIA_ROOT", media_root),
                mock.patch.object(vision, "VISION_MIN_SIDE", 100),
            ):
                self.assertEqual(vision.pick_images([msg], limit=5), [])
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)

    def test_face_pack_refuses_path_outside_pack_dir(self):
        from analyzer import face_images as fi

        pack = tempfile.mkdtemp(prefix="qqchatlog-pack2-")
        base = os.path.dirname(pack)
        evil = os.path.join(base, "evil.gif")
        with open(evil, "wb") as f:
            f.write(b"GIF89a" + b"z" * 8)
        try:
            self.assertIsNone(
                fi.local_pack_path("n1", "../../evil", local_dir=pack), "表情名里的 ../ 不能被带出表情包目录"
            )
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)
            if os.path.exists(evil):
                os.remove(evil)

    def test_origin_header_is_sanitized_in_logs(self):
        """Origin 是请求方给的：写日志前要清掉换行/控制字符，避免伪造日志行。

        真实 HTTP 头里发不出裸换行（Werkzeug 也会拒绝），但 U+2028 这类分隔符
        能进来并且在很多查看器里渲染成换行，所以清洗放在服务端做。
        """
        import app as appmod
        from webapp import security

        self.assertEqual(security._safe_for_log("http://a\n[ERROR] 伪造"), "http://a[ERROR] 伪造")
        self.assertEqual(security._safe_for_log("http://a\u2028伪造"), "http://a伪造")
        self.assertEqual(security._safe_for_log("http://a\t伪造"), "http://a伪造")
        self.assertEqual(len(security._safe_for_log("x" * 500)), 120, "日志长度要有上限")

        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as s:
            token = s["csrf_token"]
        r = client.post("/upload", data={"csrf_token": token}, headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403, "非白名单来源仍必须被拦下")


class TestFrontendRegressionGuards(unittest.TestCase):
    """前端踩过的坑：用静态断言守住，避免改回去"""

    JS = ROOT / "web" / "static" / "js" / "charts.js"
    REPORT = ROOT / "web" / "templates" / "report.html"
    INDEX = ROOT / "web" / "templates" / "index.html"

    def test_heatmap_uses_theme_tokens_not_hardcoded_colors(self):
        """热力图曾写死浅色（#6b7280/#e4e7eb/#ffffff）：深色主题下格子描边发白"""
        src = self.JS.read_text(encoding="utf-8")
        heatmap = src.split("function renderHeatmapChart")[1].split("function render")[0]
        # 注释里会提到旧写法作为说明，只检查真正的代码行
        code = "\n".join(ln for ln in heatmap.splitlines() if not ln.strip().startswith("//"))
        self.assertNotIn("#ffffff", code)
        self.assertNotIn("#6b7280", code)
        self.assertNotIn("#e4e7eb", code)
        self.assertIn("T.axis", code)
        self.assertIn("T.surface", code)

    def test_chart_entry_points_guard_missing_dom(self):
        """图表入口都要能在节点缺失时安全返回，否则一处 TypeError 会打断整块渲染"""
        src = self.JS.read_text(encoding="utf-8")
        for fn in (
            "renderEmotionCharts",
            "renderTopicsCharts",
            "renderRelationshipInsight",
            "renderHabitsInsight",
        ):
            body = src.split("function %s(" % fn)[1].split("\nfunction ")[0]
            self.assertIn("if (!data) return;", body, f"{fn} 缺少 data 判空")

    def test_report_keeps_style_link_when_inlining_failed(self):
        """样式没取到时不能把 <link> 也删掉，否则导出的报告只剩 Bootstrap"""
        src = self.REPORT.read_text(encoding="utf-8")
        self.assertIn("if (cssText) {", src)
        # 删 <link> 必须发生在这个守卫之内：取不到样式就保留原链接，别让报告裸奔
        self.assertLess(
            src.index("if (cssText) {"),
            src.index('link[href^="/static/css/"]'),
            "删样式表链接的代码要在 cssText 守卫之内",
        )
        self.assertIn("asList", src, "模型返回值先兜底成数组，避免一处 TypeError 打断报告")

    def test_report_replaces_unavailable_face_images(self):
        src = self.REPORT.read_text(encoding="utf-8")
        self.assertIn("replaceChild", src, "内联失败的表情图要降级成文字，不能留裂图")
        self.assertIn("getAttribute('alt')", src, "降级文字取自 alt（表情名）")

    def test_index_reports_partial_upload_failure(self):
        """分批上传失败时不能静默跳转，要如实说明还剩多少没传"""
        src = self.INDEX.read_text(encoding="utf-8")
        self.assertIn("中途失败", src)
        self.assertIn("服务端跳过", src)


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

        payload = _chat_payload({"uA": ["在吗"], "uB": ["在的"]})
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


class TestOtherUidDerivation(unittest.TestCase):
    """对方的 UID 必须取"发言最多的非自己一方"，不能被占位 sender 抢走"""

    def test_placeholder_sender_does_not_take_other_uid(self):
        from parser.qq_parser import load_chat

        # 占位 sender 排在文件最前面，且有一条非 system 消息（真实导出就是这样）
        msgs = (
            [
                {
                    "id": "0",
                    "timestamp": 1758031009000,
                    "time": "2025-09-16 21:56:49",
                    "sender": {"uid": "未知uid未知", "name": "系统消息"},
                    "content": "验证消息",
                }
            ]
            + _bulk("uA", "我", 20, start=1)
            + _bulk("uB", "对方", 18, start=21)
        )
        path = _write_tmp(_wrap(msgs, {"uA": "我", "uB": "对方", "未知uid未知": "系统消息"}))
        try:
            chat = load_chat(path)
            self.assertEqual(chat.other_uid, "uB")
            self.assertEqual(chat.other_name, "对方")
        finally:
            os.remove(path)


class TestRouteSurfaceUnchanged(unittest.TestCase):
    """webapp 拆分是搬家不是改建：路由与端点名必须原样"""

    def test_all_endpoints_registered(self):
        import app as appmod

        endpoints = {r.endpoint for r in appmod.app.url_map.iter_rules()}
        for e in (
            "index",
            "upload",
            "dashboard",
            "emotion",
            "relationship",
            "habits",
            "topics",
            "profile",
            "report",
            "login",
            "static",
            "api_analyze",
            "api_analyze_job",
            "api_analyze_cancel",
            "api_analysis_result",
            "api_analyze_all",
            "api_usage",
            "api_status",
        ):
            self.assertIn(e, endpoints, f"端点 {e} 在拆分后消失了")

    def test_url_rules_point_where_expected(self):
        import app as appmod

        rules = {r.rule: r.endpoint for r in appmod.app.url_map.iter_rules()}
        self.assertEqual(rules["/api/analyze-all"], "api_analyze_all")
        self.assertEqual(rules["/upload"], "upload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
