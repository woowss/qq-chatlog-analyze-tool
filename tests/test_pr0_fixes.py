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
"""PR-0 缺陷修复与边界加固回归测试（全合成数据，零外部依赖）"""

import hashlib
import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()
ROOT = Path(__file__).resolve().parent.parent

import app as appmod  # noqa: E402
import config as cfg  # noqa: E402
from analyzer import (  # noqa: E402
    deepseek_client as dc,
    face_images as fi,
    group_client as gc,
    llm_transport,
    recap_client as rc,
    shutdown as shutdownmod,
    vision,
)
from analyzer.atomic_write import write_bytes_atomic  # noqa: E402
from parser.qq_parser import ChatData, Message, load_chat  # noqa: E402
from webapp import (  # noqa: E402
    analysis_plan,
    cache_bundle,
    evidence,
    jobs,
    security,
    store,
)


def _msg(
    uid: str,
    name: str,
    text: str,
    ts: int = 1704067200000,
    time_str: str = "2024-01-01 08:00:00",
    msg_id: str = "1",
    reply_to_id: str = "",
    reply_to_uid: str = "",
) -> Message:
    return Message(
        id=msg_id,
        timestamp=ts,
        time_str=time_str,
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=bool(reply_to_id),
        reply_to_id=reply_to_id,
        reply_to_uid=reply_to_uid,
    )


def _private_chat(messages: list[Message] | None = None) -> ChatData:
    if messages is None:
        messages = [
            _msg("uA", "我", "早上好", 1704067200000, "2024-01-01 08:00:00", "m1"),
            _msg("uB", "对方", "早安", 1704067260000, "2024-01-01 08:01:00", "m2"),
        ]
    return ChatData(
        chat_name="对方",
        self_name="我",
        other_name="对方",
        self_uid="uA",
        other_uid="uB",
        messages=messages,
        total_count=len(messages),
    )


class TestModelCacheFilenameSanitization(unittest.TestCase):
    """模型名带 `/`、`\\`、`:` 时，缓存路径必须留在 AI_CACHE_DIR 内并兼容 bundle 校验"""

    def test_standard_model_keeps_exact_legacy_filename(self):
        with (
            mock.patch.object(store, "DEEPSEEK_MODEL", "deepseek-chat"),
            mock.patch.object(store, "thinking_enabled", return_value=False),
        ):
            path = store._cache_path("emotion", "hashlegacy01", fingerprint="fp1234567890")
            ask_path = store._ask_path("hashlegacy01", "hello")
        self.assertEqual(os.path.basename(path), "emotion_hashlegacy01_deepseek-chat_fp1234567890.json")
        self.assertIn("ask_hashlegacy01_deepseek-chat_", os.path.basename(ask_path))

    def test_unsafe_model_name_stays_single_segment_and_roundtrips(self):
        with (
            tempfile.TemporaryDirectory() as ai_dir,
            tempfile.TemporaryDirectory() as stats_dir,
            mock.patch.object(store, "AI_CACHE_DIR", ai_dir),
            mock.patch.object(store, "STATS_CACHE_DIR", stats_dir),
            mock.patch.object(store, "purge_month_cache", return_value=0),
            mock.patch.object(store, "purge_job_history", return_value=0),
            mock.patch.object(vision, "purge_memo", return_value=0),
            mock.patch.object(vision, "purge_session_media", return_value=0),
        ):
            seen_names = set()
            for idx, model in enumerate(("deepseek/deepseek-chat-v3", "qwen2.5:7b", "../evil/model:latest")):
                chat_hash = f"hashmodeltest{idx:03d}"
                with self.subTest(model=model), mock.patch.object(store, "DEEPSEEK_MODEL", model):
                    path = store._cache_path("emotion", chat_hash, fingerprint="fp0001")
                    ask_path = store._ask_path(chat_hash, "测试问题")
                    self.assertEqual(os.path.dirname(path), ai_dir)
                    self.assertEqual(os.path.dirname(ask_path), ai_dir)
                    base = os.path.basename(path)
                    ask_base = os.path.basename(ask_path)
                    self.assertRegex(base, cache_bundle.BUNDLE_NAME_RE)
                    self.assertRegex(ask_base, cache_bundle.BUNDLE_NAME_RE)
                    self.assertTrue(store._cache_belongs_to(base, chat_hash))
                    self.assertTrue(store._cache_belongs_to(ask_base, chat_hash))
                    self.assertNotIn(base, seen_names)
                    seen_names.add(base)

                    store._write_cache("emotion", chat_hash, {"2024-01": {"ok": 1}}, fingerprint="fp0001")
                    got = store._read_cache("emotion", chat_hash, fingerprint="fp0001")
                    self.assertEqual(got, {"2024-01": {"ok": 1}})
                    store._purge_chat_caches(chat_hash)
                    self.assertFalse(os.path.exists(path))


class TestRetryBudgetSeparation(unittest.TestCase):
    """通用重试与 TPM 限流各自独立计数，互不挤占；极端数值 env 安全回退"""

    def setUp(self):
        self._orig_interval = dc.CALL_MIN_INTERVAL
        dc.CALL_MIN_INTERVAL = 0.0
        self.addCleanup(lambda: setattr(dc, "CALL_MIN_INTERVAL", self._orig_interval))

    @staticmethod
    def _tpm_err():
        err = Exception("Allocated quota exceeded")
        err.status_code = 429
        return err

    def test_generic_failures_do_not_exhaust_tpm_attempts(self):
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = [
            OSError("reset 1"),
            OSError("reset 2"),
            self._tpm_err(),
            self._tpm_err(),
            self._tpm_err(),
            self._tpm_err(),
        ]
        with (
            mock.patch.object(dc, "_get_client", return_value=fake),
            mock.patch.object(dc.time, "sleep") as sleep_mock,
        ):
            with self.assertRaises(dc.QuotaExhaustedError):
                dc._call_api("sys", "user", retry=2, tpm_wait=0.01)
        self.assertEqual(fake.chat.completions.create.call_count, 2 + dc.TPM_MAX_ATTEMPTS)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list if c.args[0] >= 1], [1, 2])

    def test_tpm_hits_before_generic_error_keep_exponential_backoff(self):
        resp = mock.Mock()
        resp.choices = [mock.Mock(finish_reason="stop", message=mock.Mock(content='{"ok": true}'))]
        resp.usage = None
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = [
            self._tpm_err(),
            self._tpm_err(),
            OSError("transient 1"),
            OSError("transient 2"),
            resp,
        ]
        with (
            mock.patch.object(dc, "_get_client", return_value=fake),
            mock.patch.object(dc.time, "sleep") as sleep_mock,
        ):
            out = dc._call_api("sys", "user", retry=2, tpm_wait=0.01)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(fake.chat.completions.create.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep_mock.call_args_list if c.args[0] >= 1], [1, 2])

    def test_request_with_retry_returns_last_failure_kind_when_max_attempts_reached(self):
        client = mock.Mock()
        client.chat.completions.create.side_effect = self._tpm_err()
        res, reason = llm_transport.request_with_retry(
            client=client,
            build_params=lambda: {},
            model="m",
            tag="emotion",
            max_attempts=2,
            generic_retries=2,
            tpm_max_attempts=5,
            tpm_wait=0.01,
            fatal_message="fatal",
            logger=dc.logger,
            count_call=lambda: None,
            pace=lambda: None,
            record_call=lambda *a: None,
            set_cooldown=lambda *a: None,
            scrub_secrets=lambda e: str(e),
            is_plan_exhausted=lambda e: False,
            is_tpm_throttle=lambda e: True,
            quota_exhausted_error=dc.QuotaExhaustedError,
            sleep=lambda s: None,
        )
        self.assertIsNone(res)
        self.assertEqual(reason, "tpm")

    def test_numeric_env_outliers_fallback_safely(self):
        for bad in ("inf", "-inf", "nan", "abc"):
            with mock.patch.dict(os.environ, {"LLM_MAX_TOKENS_EMOTION": bad}):
                self.assertEqual(dc._max_tokens("emotion", 8192), 8192)
            with mock.patch.dict(os.environ, {"QQCHAT_SHUTDOWN_GRACE_SECONDS": bad}):
                self.assertEqual(cfg.env_number("QQCHAT_SHUTDOWN_GRACE_SECONDS", 5.0, 0.0, 300.0), 5.0)
        with mock.patch.dict(os.environ, {"QQCHAT_SHUTDOWN_GRACE_SECONDS": "0"}):
            self.assertEqual(cfg.env_number("QQCHAT_SHUTDOWN_GRACE_SECONDS", 5.0, 0.0, 300.0), 0.0)


class TestMixedMediaAndMultiImageVision(unittest.TestCase):
    """混合媒体不覆盖图片字段、多图逐张选图、重复 id 引用回填不猜错"""

    def test_mixed_file_and_image_preserves_image_fields_for_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            img_dir = exports / "resources" / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            img_file = img_dir / "shot.png"
            img_bytes = b"\x89PNG\r\n\x1a\n" + b"a" * 64
            img_file.write_bytes(img_bytes)
            img_md5 = hashlib.md5(img_bytes, usedforsecurity=False).hexdigest()
            file_md5 = hashlib.md5(b"attachment", usedforsecurity=False).hexdigest()

            payload = {
                "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
                "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}]},
                "messages": [
                    {
                        "id": "m1",
                        "timestamp": 1704067200000,
                        "sender": {"uid": "uA", "name": "我"},
                        "content": {
                            "elements": [
                                {
                                    "type": "file",
                                    "data": {
                                        "fileName": "report.pdf",
                                        "size": 4096,
                                        "url": "resources/files/report.pdf",
                                        "md5": file_md5,
                                    },
                                },
                                {
                                    "type": "image",
                                    "data": {
                                        "size": 2048,
                                        "width": 1200,
                                        "height": 1600,
                                        "url": "resources/images/shot.png",
                                        "md5": img_md5,
                                    },
                                },
                            ]
                        },
                    }
                ],
            }
            json_path = Path(tmp) / "chat.json"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            chat = load_chat(str(json_path))
            msg = chat.messages[0]
            self.assertEqual(msg.media_kind, "file")
            self.assertEqual(msg.media_path, "resources/images/shot.png")
            self.assertEqual(msg.media_id, img_md5)
            self.assertEqual(len(msg.image_items), 1)
            with (
                mock.patch.object(vision, "MEDIA_ROOT", str(exports)),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
            ):
                picked = vision.pick_images(chat.messages, limit=5)
            self.assertEqual(len(picked), 1)
            self.assertTrue(picked[0]["path"].endswith("shot.png"))
            self.assertTrue(picked[0]["trusted_md5"])

    def test_multi_image_message_skips_small_sticker_and_picks_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp) / "exports"
            img_dir = exports / "resources" / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            (img_dir / "sticker.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"s" * 32)
            shot = img_dir / "screen.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 128)

            payload = {
                "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
                "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}]},
                "messages": [
                    {
                        "id": "m1",
                        "timestamp": 1704067200000,
                        "sender": {"uid": "uB", "name": "对方"},
                        "content": {
                            "elements": [
                                {
                                    "type": "image",
                                    "data": {
                                        "size": 500,
                                        "width": 100,
                                        "height": 100,
                                        "url": "resources/images/sticker.png",
                                    },
                                },
                                {
                                    "type": "image",
                                    "data": {
                                        "size": 5000,
                                        "width": 1920,
                                        "height": 1080,
                                        "url": "resources/images/screen.png",
                                    },
                                },
                            ]
                        },
                    }
                ],
            }
            json_path = Path(tmp) / "multi.json"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            chat = load_chat(str(json_path))
            self.assertEqual(chat.messages[0].image_count, 2)
            with (
                mock.patch.object(vision, "MEDIA_ROOT", str(exports)),
                mock.patch.object(vision, "VISION_MIN_SIDE", 200),
            ):
                wanted = vision.plan_wanted({"2024-01": chat.messages}, limit_per_month=5)
                picked = vision.pick_images(chat.messages, limit=5)
            self.assertEqual(wanted, ["resources/images/screen.png"])
            self.assertEqual(len(picked), 1)
            self.assertEqual(picked[0]["path"], str(shot))

    def test_duplicate_message_id_reply_backfill_avoids_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "chatInfo": {"name": "对方", "selfUid": "uA", "selfName": "我"},
                "statistics": {"senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}]},
                "messages": [
                    {
                        "id": "dup",
                        "timestamp": 1704067200000,
                        "sender": {"uid": "uA", "name": "我"},
                        "content": {"text": "A1"},
                    },
                    {
                        "id": "r1",
                        "timestamp": 1704067210000,
                        "sender": {"uid": "uB", "name": "对方"},
                        "content": {
                            "elements": [
                                {"type": "reply", "data": {"referencedMessageId": "dup"}},
                                {"type": "text", "data": {"text": "早于碰撞点的回复"}},
                            ]
                        },
                    },
                    {
                        "id": "dup",
                        "timestamp": 1704067220000,
                        "sender": {"uid": "uB", "name": "对方"},
                        "content": {"text": "B1 冲突 id"},
                    },
                    {
                        "id": "r2",
                        "timestamp": 1704067230000,
                        "sender": {"uid": "uA", "name": "我"},
                        "content": {
                            "elements": [
                                {"type": "reply", "data": {"referencedMessageId": "dup"}},
                                {"type": "text", "data": {"text": "碰撞后的回复不能猜"}},
                            ]
                        },
                    },
                ],
            }
            json_path = Path(tmp) / "dup.json"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            chat = load_chat(str(json_path))
            by_id = {m.id: m for m in chat.messages if m.id in ("r1", "r2")}
            self.assertEqual(by_id["r1"].reply_to_uid, "uA")
            self.assertEqual(by_id["r2"].reply_to_uid, "")


class TestRecapAndAnalyzeAllFixes(unittest.TestCase):
    """空范围 recap 不出网且计划标为不可分析；_run_analyze_all 如实上报局部失败"""

    def test_recap_empty_scope_short_circuits_and_marks_plan_empty(self):
        sys_msg = _msg("uA", "我", "系统提示", msg_id="s1")
        sys_msg.system = True
        empty_chat = _private_chat([sys_msg])
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("空范围不应出网")):
            self.assertIsNone(rc.analyze_recap(empty_chat))
        plan = analysis_plan.build_analysis_plan(empty_chat, "hash-recap-empty", ["recap"])
        self.assertEqual(plan["units"][0]["status"], "无可分析消息")
        self.assertEqual(plan["summary"]["input_tokens_max"], 0)

    def test_analyze_all_marks_error_and_keeps_partial_results_on_partial_failure(self):
        chat = _private_chat()
        chat_hash = "hash-all-partial-01"
        jid = "job-all-partial-01"
        jobs.JOBS[jid] = {"status": "running", "chat_hash": chat_hash, "cancel": False}
        self.addCleanup(lambda: jobs.JOBS.pop(jid, None))

        def ok_emotion(*_a, **_kw):
            return {"2024-01": {"self_emotion": "平静"}}

        def fail_topics(*_a, **_kw):
            raise RuntimeError("synthetic dimension failure")

        funcs = {"emotion": ok_emotion, "topics": fail_topics}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "AI_CACHE_DIR", tmpdir),
            mock.patch.object(store, "append_job_history"),
            mock.patch.object(jobs, "dimensions_for_mode", return_value=["emotion", "topics"]),
            mock.patch.object(jobs, "analyze_func_for", side_effect=funcs.__getitem__),
            mock.patch.object(jobs, "validate_dimension_result"),
        ):
            jobs._run_analyze_all(jid, "synthetic.json", chat_hash, refresh=True)
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["result"], {"emotion": "done", "topics": "error"})
        self.assertEqual(job["partial_results"], {"emotion": {"2024-01": {"self_emotion": "平静"}}})
        self.assertTrue(job["partial"])

    def test_analyze_all_quota_abort_preserves_partial_results(self):
        chat = _private_chat()
        chat_hash = "hash-all-quota-01"
        jid = "job-all-quota-01"
        jobs.JOBS[jid] = {"status": "running", "chat_hash": chat_hash, "cancel": False}
        self.addCleanup(lambda: jobs.JOBS.pop(jid, None))

        def ok_emotion(*_a, **_kw):
            return {"2024-01": {"self_emotion": "平静"}}

        def abort_topics(*_a, **_kw):
            raise dc.QuotaExhaustedError("配额耗尽")

        funcs = {"emotion": ok_emotion, "topics": abort_topics}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "AI_CACHE_DIR", tmpdir),
            mock.patch.object(store, "append_job_history"),
            mock.patch.object(jobs, "dimensions_for_mode", return_value=["emotion", "topics"]),
            mock.patch.object(jobs, "analyze_func_for", side_effect=funcs.__getitem__),
            mock.patch.object(jobs, "validate_dimension_result"),
        ):
            jobs._run_analyze_all(jid, "synthetic.json", chat_hash, refresh=True)
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["result"], {"emotion": "done", "topics": "aborted"})
        self.assertEqual(job["partial_results"], {"emotion": {"2024-01": {"self_emotion": "平静"}}})


class TestEvidenceBundleAndSecurityHardening(unittest.TestCase):
    """群聊证据行一致、ZIP 真实解压总量拦截、登录 Origin 守卫、控制字符跳转、密钥权限与健康探针"""

    def test_evidence_group_input_lines_match_build_group_dialog(self):
        m1 = _msg("uA", "甲", "第一句", 1704067200000, "2024-01-01 08:00:00", "m1")
        m2 = _msg("uA", "甲", "紧接着第二句", 1704067230000, "2024-01-01 08:00:30", "m2")
        m3 = _msg("uB", "乙", "第三句", 1704067260000, "2024-01-01 08:01:00", "m3")
        m4 = _msg("uC", "丙", "第四句", 1704067290000, "2024-01-01 08:01:30", "m4")
        chat = ChatData(
            chat_name="群",
            self_name="甲",
            other_name="",
            self_uid="uA",
            other_uid="",
            messages=[m1, m2, m3, m4],
            total_count=4,
            is_group_chat=True,
            mode="group",
        )
        line_map = evidence._input_lines(chat, "group_topics", "2024-01")
        dialog = gc.build_group_dialog(chat, chat.messages, max_chars=20000)
        for msg in (m1, m2, m3, m4):
            self.assertIn(line_map[id(msg)], dialog)

    def test_cache_bundle_enforces_actual_decompressed_bytes_cap_and_meta_cap(self):
        chat_hash = "deadbeef" * 2
        with (
            tempfile.TemporaryDirectory() as tmp_ai,
            tempfile.TemporaryDirectory() as tmp_stats,
        ):
            payload_a = json.dumps({"_created": 1, "result": {"k": "x" * 200}}).encode("utf-8")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("meta.json", json.dumps({"format": 1, "chat_hash": chat_hash}))
                info1 = zipfile.ZipInfo(f"ai_cache/emotion_{chat_hash}_m_fp1.json")
                info1.file_size = 10
                zf.writestr(info1, payload_a)
                info2 = zipfile.ZipInfo(f"ai_cache/topics_{chat_hash}_m_fp2.json")
                info2.file_size = 10
                zf.writestr(info2, payload_a)
            res = cache_bundle.read_chat_bundle(
                io.BytesIO(buf.getvalue()),
                ai_cache_dir=tmp_ai,
                stats_cache_dir=tmp_stats,
                name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,120}\.json$"),
                hash_re=re.compile(r"^[0-9a-f]{16,64}$"),
                max_entries=50,
                max_total_bytes=len(payload_a) + 10,
                meta_name="meta.json",
                leaf_ok=lambda *_a: True,
                payload_ok=lambda *_a: True,
                write_bytes_atomic=write_bytes_atomic,
                clear_chat_cache=lambda: None,
                logger=store.logger,
            )
            self.assertIn("超限", res.get("error", ""))
            self.assertEqual(os.listdir(tmp_ai), [], "超限前预检暂存，不得落盘半截条目")

            oversized_meta_buf = io.BytesIO()
            with zipfile.ZipFile(oversized_meta_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("meta.json", json.dumps({"chat_hash": chat_hash, "pad": "x" * (70 * 1024)}))
            meta_res = cache_bundle.read_chat_bundle(
                io.BytesIO(oversized_meta_buf.getvalue()),
                ai_cache_dir=tmp_ai,
                stats_cache_dir=tmp_stats,
                name_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,120}\.json$"),
                hash_re=re.compile(r"^[0-9a-f]{16,64}$"),
                max_entries=50,
                max_total_bytes=1024 * 1024,
                meta_name="meta.json",
                leaf_ok=lambda *_a: True,
                payload_ok=lambda *_a: True,
                write_bytes_atomic=write_bytes_atomic,
                clear_chat_cache=lambda: None,
                logger=store.logger,
            )
            self.assertIn("元信息过大", meta_res.get("error", ""))

    def test_login_post_origin_and_control_char_redirect_and_health_shutdown(self):
        client = appmod.app.test_client()
        with mock.patch.object(security, "ACCESS_PASSWORD", "s3cret"):
            bad = client.post(
                "/login",
                data={"password": "wrong"},
                headers={"Origin": "http://evil.example.com"},
            )
            self.assertEqual(bad.status_code, 403)
            ok = client.post(
                "/login?next=/dashboard%0d%0aInjected:1",
                data={"password": "s3cret"},
                headers={"Origin": "http://localhost:5000"},
            )
            self.assertEqual(ok.status_code, 302)
            self.assertEqual(ok.headers["Location"], "/")

        shutdownmod.reset_shutdown()
        self.addCleanup(shutdownmod.reset_shutdown)
        self.assertEqual(client.get("/health").status_code, 200)
        shutdownmod.request_shutdown()
        self.assertEqual(client.get("/health").status_code, 503)

    def test_secret_key_created_with_owner_only_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX file modes not applicable on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with (
                mock.patch.object(cfg, "DATA_DIR", tmp_path / "data"),
                mock.patch.object(cfg, "BASE_DIR", tmp_path / "base"),
                mock.patch.dict(os.environ, {"SECRET_KEY": ""}),
            ):
                secret = cfg._load_or_create_secret()
                key_file = tmp_path / "data" / ".secret_key"
                self.assertEqual(key_file.read_text(encoding="utf-8"), secret)
                self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)


class TestFaceMemoAndFrontendGuards(unittest.TestCase):
    """表情索引缓存按 limit 分开并在聊天清理时回收；前端修复静默回归护栏"""

    def test_collect_cached_scopes_by_limit_and_purges_on_chat_purge(self):
        chat = _private_chat()
        chat.messages[0].face_names = ["微笑", "流泪"]
        with (
            mock.patch.object(fi, "_COLLECT_MEMO", {}),
            mock.patch.object(fi, "collect", side_effect=lambda _c, limit=400: {"limit": limit}) as mocked,
            mock.patch.object(store, "purge_month_cache", return_value=0),
            mock.patch.object(store, "purge_job_history", return_value=0),
            mock.patch.object(vision, "purge_memo", return_value=0),
            mock.patch.object(vision, "purge_session_media", return_value=0),
        ):
            self.assertEqual(fi.collect_cached(chat, "chat-face-1", limit=10), {"limit": 10})
            self.assertEqual(fi.collect_cached(chat, "chat-face-1", limit=20), {"limit": 20})
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(fi.collect_cached(chat, "chat-face-2", limit=10), {"limit": 10})
            removed = store._purge_chat_caches("chat-face-1")
            self.assertEqual(removed, 0)
            self.assertIn(("chat-face-2", 10), fi._COLLECT_MEMO)
            self.assertNotIn(("chat-face-1", 10), fi._COLLECT_MEMO)
            self.assertNotIn(("chat-face-1", 20), fi._COLLECT_MEMO)

    def test_frontend_pr0_guards(self):
        group_emotion = (ROOT / "web" / "templates" / "group_emotion.html").read_text(encoding="utf-8")
        self.assertLess(
            group_emotion.index("classList.remove('d-none')"),
            group_emotion.index("mountChart('emotionTrend')"),
        )

        group_charts = (ROOT / "web" / "static" / "js" / "group_charts.js").read_text(encoding="utf-8")
        render_trend = group_charts.split("function renderGroupEmotionTrend")[1].split("\nfunction ")[0]
        self.assertLess(
            render_trend.index("classList.remove('d-none')"),
            render_trend.index("mountChart('emotionTrend')"),
        )
        self.assertIn("meta.partialResults", group_charts)

        profile = (ROOT / "web" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn('<div id="profileContent" class="row">', profile)

        charts = (ROOT / "web" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
        render_emotion_charts = charts.split("function renderEmotionCharts")[1].split("\nfunction ")[0]
        self.assertNotIn("</div></div></div>';", render_emotion_charts)

        messages = (ROOT / "web" / "templates" / "messages.html").read_text(encoding="utf-8")
        self.assertIn("state.pages = res.pages || 0;", messages)

        index_html = (ROOT / "web" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("$('#importFile').on('change'", index_html)

        dashboard = (ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("我的称呼变迁", dashboard)
        for key in ("ask:", "plan:", "group_activity:", "member_profiles:"):
            self.assertIn(key, dashboard)

        assets = (ROOT / "web" / "templates" / "_report_assets.html").read_text(encoding="utf-8")
        self.assertTrue(assets.rstrip().endswith("</script>"))
        self.assertIn("setTimeout(function () { URL.revokeObjectURL(url); }, 1000);", assets)
        for tpl_name in ("report.html", "group_report.html"):
            tpl = (ROOT / "web" / "templates" / tpl_name).read_text(encoding="utf-8")
            self.assertIn('{% include "_report_assets.html" %}\n<script>', tpl)
