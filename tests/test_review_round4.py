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
"""第四轮审查的优化项回归测试

覆盖：月份分组缓存、总览单次遍历、月份 manifest 批量写入与引用键缓存、
调用重试骨架合并、优雅关闭（不再派发新的付费调用 / 不写残缺缓存）、
/health 探针的中间件豁免、api_ok 统一注入、静态资源破缓存。
"""

import json
import os
import shutil as _shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 需要它的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

_TMP_ROOT = os.path.join(os.environ["QQCHAT_DATA_DIR"], "tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
tempfile.tempdir = _TMP_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.qq_parser import CST, ChatData, Message, split_by_month  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
from analyzer import shutdown as sd  # noqa: E402
from analyzer import usage as usage_mod  # noqa: E402
from webapp import jobs as jobsmod  # noqa: E402


def _msg(uid: str, ts: int, text: str = "在吗", has_image: bool = False, **kw) -> Message:
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


def _chat(msgs) -> ChatData:
    return ChatData(chat_name="", self_name="A", other_name="B", self_uid="u1", other_uid="u2", messages=msgs)


def _months_chat(n_months: int, pairs_per_month: int = 6) -> ChatData:
    """跨 n 个月的聊天（每月内容互不相同）"""
    msgs = []
    for m in range(n_months):
        year, month = 2025 + m // 12, (m % 12) + 1
        for i in range(pairs_per_month):
            t = datetime(year, month, 5, 20, 0, tzinfo=CST) + timedelta(minutes=17 * i)
            text = f"{year}-{month:02d} 第{i}条消息"
            msgs.append(_msg("u1" if i % 2 else "u2", int(t.timestamp() * 1000), text=text))
    return _chat(msgs)


class TestMonthGroupingCache(unittest.TestCase):
    """split_by_month 在一次分析里会被多个维度各调一遍，不能每次都重算"""

    def test_second_call_reuses_the_same_object(self):
        chat = _months_chat(3)
        first = split_by_month(chat)
        second = split_by_month(chat)
        self.assertIs(first, second, "应直接返回缓存的字典")
        self.assertEqual(list(first), ["2025-01", "2025-02", "2025-03"])

    def test_cache_lives_on_the_instance_and_is_not_part_of_equality(self):
        chat = _months_chat(1)
        self.assertIsNone(chat._months_cache, "初始为 None，不该在构造时就计算")
        chat.months()
        self.assertIsNotNone(chat._months_cache)
        # 缓存字段不参与相等比较，也不该出现在 repr 里（它可能很大）
        other = _months_chat(1)
        self.assertEqual(chat, other)
        self.assertNotIn("_months_cache", repr(chat))

    def test_stats_cache_is_a_declared_field(self):
        """_stats_cache 曾经是 setattr 动态挂载；现在是声明字段，读代码时看得见"""
        chat = _months_chat(1)
        self.assertIsNone(chat._stats_cache)
        ls.calc_overview(chat)
        self.assertIsNotNone(chat._stats_cache)


class TestOverviewSinglePass(unittest.TestCase):
    """calc_overview 合并成单次遍历后，字段口径必须与原实现逐项一致"""

    def _reference(self, chat):
        ms = chat.statistical()
        self_count = sum(1 for m in ms if m.sender_uid == chat.self_uid)
        image_msgs = [m for m in ms if m.has_image]
        media = {}
        for m in ms:
            if m.media_kind:
                media[m.media_kind] = media.get(m.media_kind, 0) + 1
        return {
            "self_count": self_count,
            "other_count": len(ms) - self_count,
            "self_chars": sum(len(m.text) for m in ms if m.sender_uid == chat.self_uid),
            "other_chars": sum(len(m.text) for m in ms if m.sender_uid != chat.self_uid),
            "total_images": len(image_msgs),
            "total_faces": sum((len(m.face_names) or len(m.face_ids)) for m in ms),
            "image_bytes": sum(m.media_bytes for m in image_msgs),
            "other_media_bytes": sum(m.media_bytes for m in ms if m.media_kind),
            "unique_images": len({m.media_id for m in image_msgs if m.media_id}) or None,
            "media": media,
        }

    def test_fields_match_reference_implementation(self):
        base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        msgs = [
            _msg("u1", base, text="图片消息", has_image=True, media_bytes=100, media_id="m1"),
            _msg("u2", base + 1000, text="同一张图", has_image=True, media_bytes=100, media_id="m1"),
            _msg("u1", base + 2000, text="文件", media_kind="file", media_bytes=5000, media_id="f1"),
            _msg("u2", base + 3000, text="视频", media_kind="video", media_bytes=9000),
            _msg("u1", base + 4000, text="表情", face_ids=[3], face_names=[]),
            _msg("u2", base + 5000, text="大表情", face_ids=[], face_names=["微笑"]),
            _msg("u1", base + 6000, text="没有媒体的普通消息"),
        ]
        chat = _chat(msgs)
        ov = ls.calc_overview(chat)
        ref = self._reference(chat)
        for key in (
            "self_count",
            "other_count",
            "self_chars",
            "other_chars",
            "total_images",
            "total_faces",
            "image_bytes",
            "other_media_bytes",
            "unique_images",
        ):
            self.assertEqual(ov[key], ref[key], f"{key} 与参考实现不一致")
        self.assertEqual(ov["total_files"], ref["media"].get("file", 0))
        self.assertEqual(ov["total_videos"], ref["media"].get("video", 0))
        self.assertEqual(ov["unique_images"], 1, "两张同 md5 的图应算一张")

    def test_faces_prefer_names_over_ids(self):
        """商城大表情没有数字 id：face_names 与 face_ids 取两者中有的那个，不能相加"""
        base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        chat = _chat([_msg("u1", base, face_ids=[1, 2], face_names=["a", "b", "c"])])
        self.assertEqual(ls.calc_overview(chat)["total_faces"], 3)


class TestMonthManifestBatching(unittest.TestCase):
    """manifest 从"每月读一次写一次"改成"每个维度读一次写一次" """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-manifest-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_dir = dc._MONTH_CACHE_DIR
        dc.configure_month_cache(self.tmp)
        self.addCleanup(lambda: dc.configure_month_cache(self._orig_dir))

    def _run(self, chat, chat_hash):
        def fake_api(*a, **kw):
            return {"self_emotion": "平静", "other_emotion": "平静"}

        with (
            mock.patch.object(dc, "_call_api", side_effect=fake_api),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            return dc.analyze_emotion(chat, chat_hash=chat_hash)

    def test_manifest_written_once_per_dimension(self):
        """3 个月的维度：_record_month_usage 只应被调用 1 次（而不是 3 次）"""
        real = dc._record_month_usage
        calls = []

        def spy(chat_hash, keys):
            calls.append(set(keys))
            return real(chat_hash, keys)

        with mock.patch.object(dc, "_record_month_usage", side_effect=spy):
            result = self._run(_months_chat(3), "hashBatch")
        self.assertEqual(len(result), 3)
        self.assertEqual(len(calls), 1, "应当只写一次 manifest")
        self.assertEqual(len(calls[0]), 3, "三个月份的键都要记进 manifest")

        with open(os.path.join(self.tmp, "manifest_hashBatch.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["months"]), 3)

    def test_manifest_accumulates_across_runs(self):
        self._run(_months_chat(2), "hashGrow")
        self._run(_months_chat(3), "hashGrow")
        with open(os.path.join(self.tmp, "manifest_hashGrow.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["months"]), 3, "第二次运行应把新月份并进同一个 manifest")

    def test_referenced_keys_uses_mtime_cache(self):
        """引用键集合带进程级缓存：manifest 没被改过就不该再读一遍文件"""
        dc._record_month_usage("hashCache", {"k1", "k2"})
        with (
            mock.patch.object(dc, "_MONTH_CACHE_LOCK", dc._MONTH_CACHE_LOCK),
            mock.patch.object(dc.json, "load", side_effect=AssertionError("不该重新解析 manifest")),
        ):
            keys = dc._referenced_keys_locked()
        self.assertEqual(keys, {"k1", "k2"})

    def test_mtime_change_invalidates_cache(self):
        dc._record_month_usage("hashMtime", {"k1"})
        path = os.path.join(self.tmp, "manifest_hashMtime.json")
        # 绕过写路径直接改文件（模拟外部修改）：mtime 变了就必须重读
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"months": ["k9"]}, f)
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(dc._referenced_keys_locked(), {"k9"})

    def test_purge_ignores_other_manifests(self):
        """purge 走的仍是"别人引用的不能删"这套语义（缓存不能把它改坏）"""
        self._run(_months_chat(2), "hashA")
        self._run(_months_chat(3), "hashB")
        before = len([n for n in os.listdir(self.tmp) if n.startswith("month_")])
        self.assertEqual(dc.purge_month_cache("hashA"), 0, "共享的月份文件不能被删")
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), before)


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
        chat = _months_chat(3)
        calls = []

        def fake_api(*a, **kw):
            calls.append(1)
            return {"self_emotion": "平静", "other_emotion": "平静"}

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
        filepath = os.path.join(_TMP_ROOT, "shutdown-chat.json")
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
            mock.patch.object(jobsmod.store, "_load_chat_cached", return_value=object()),
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


class TestAssetCacheBusting(unittest.TestCase):
    """自有 JS/CSS 必须带版本参数，否则用户会长期跑到旧脚本"""

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.client = appmod.app.test_client()

    def test_own_assets_carry_version(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertRegex(body, r"css/style\.css\?v=\d+")
        self.assertRegex(body, r"js/charts\.js\?v=\d+")
        self.assertRegex(body, r"js/analyze\.js\?v=\d+")

    def test_vendor_assets_keep_their_own_filenames(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("vendor/jquery.min.js", body)
        self.assertNotIn("vendor/jquery.min.js?v=", body, "vendor 文件名自带版本，不必再挂参数")

    def test_version_is_numeric(self):
        from web import ASSET_VERSION

        self.assertRegex(ASSET_VERSION, r"^\d+$")


class TestApiOkInjectedGlobally(unittest.TestCase):
    """api_ok 由 context processor 统一注入；视图不再各传一份"""

    def test_pages_get_api_ok_from_context_processor(self):
        import app as appmod
        from flask import template_rendered

        captured = {}

        def record(_sender, **extra):
            # blinker 1.9 起接收者只能收 sender + 关键字参数，位置参数会直接 TypeError
            captured.update(extra.get("context") or {})

        template_rendered.connect(record, appmod.app)
        self.addCleanup(template_rendered.disconnect, record, appmod.app)

        with mock.patch.object(appmod.views, "is_api_configured", return_value=False):
            body = appmod.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("api_ok", captured, "模板上下文里必须有 api_ok")
        self.assertFalse(captured["api_ok"])
        self.assertIn("未配置 API Key", body, "页面要如实显示未配置状态")

    def test_views_no_longer_pass_it_manually(self):
        src = Path("webapp/views.py").read_text(encoding="utf-8")
        self.assertEqual(
            src.count("api_ok=is_api_configured()"),
            0,
            "视图里不该再逐个传 api_ok（由 context processor 注入）",
        )


class TestNetworkInterruptionRecovery(unittest.TestCase):
    """服务重启/断网时前端要回读缓存，而不是报"分析失败"诱导用户重跑"""

    def test_analyze_js_handles_status_zero(self):
        src = Path("web/static/js/analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 0", src, "网络层失败要有单独分支")
        branch = src.split("xhr.status === 0")[1].split("opts.onError((xhr.responseJSON")[0]
        self.assertIn("loadAnalysis", branch, "网络中断时要尝试回读磁盘缓存")
        self.assertIn("不会重复付费", branch, "提示要说清「结果已保存」")

    def test_analyze_js_still_handles_404(self):
        src = Path("web/static/js/analyze.js").read_text(encoding="utf-8")
        self.assertIn("xhr.status === 404", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
