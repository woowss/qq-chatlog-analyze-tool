# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""实时 AI 输出流与 UI 重载恢复的单元与接口回归测试。"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _bootstrap import bootstrap  # noqa: E402

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import llm_stream  # noqa: E402
from analyzer import llm_transport  # noqa: E402
from analyzer import purge_marks  # noqa: E402
from parser.qq_parser import Message  # noqa: E402
from result_fixtures import for_dimension  # noqa: E402
from webapp import job_stream, jobs, store  # noqa: E402


def _make_chunk(*, reasoning="", content="", finish_reason=None, usage=None):
    delta = SimpleNamespace(reasoning_content=reasoning or None, content=content or None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice] if (reasoning or content or finish_reason) else [], usage=usage)


class TestLlmStreamAndTransport(unittest.TestCase):
    def test_streaming_coalesces_thinking_and_content_and_records_usage(self):
        events = []
        captured_kwargs = []

        def fake_create(**kwargs):
            captured_kwargs.append(kwargs)
            return iter(
                [
                    _make_chunk(reasoning="合成思考前半"),
                    _make_chunk(reasoning="合成思考后半"),
                    _make_chunk(content='{"month_vibe":'),
                    _make_chunk(content='"轻松平稳"}', finish_reason="stop"),
                    _make_chunk(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45)),
                ]
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        record_calls = []

        with llm_stream.stream_context(
            sink=events.append,
            unit="emotion:2025-01",
            label="2025-01 月",
            fresh_state=True,
        ):
            resp, err = llm_transport.request_with_retry(
                client,
                lambda: {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 512,
                },
                tag="emotion",
                max_attempts=3,
                generic_retries=1,
                tpm_wait=0.0,
                fatal_message="quota",
                count_call=lambda: None,
                pace=lambda: None,
                is_plan_exhausted=lambda _e: False,
                is_tpm_throttle=lambda _e: False,
                set_cooldown=lambda _s: None,
                tpm_max_attempts=2,
                logger=dc.logger,
                record_call=lambda *args: record_calls.append(args),
                model="test-model",
                scrub_secrets=str,
                quota_exhausted_error=RuntimeError,
                sleep=lambda _s: None,
            )

        self.assertIsNone(err)
        self.assertTrue(captured_kwargs[0]["stream"])
        self.assertEqual(captured_kwargs[0]["stream_options"], {"include_usage": True})
        self.assertEqual(resp.choices[0].message.content, '{"month_vibe":"轻松平稳"}')
        self.assertEqual(resp.choices[0].message.reasoning_content, "合成思考前半合成思考后半")
        self.assertEqual(record_calls, [("test-model", "emotion", 120, 45)])

        thinking = "".join(e["text"] for e in events if e["kind"] == "thinking")
        content = "".join(e["text"] for e in events if e["kind"] == "content")
        usages = [e for e in events if e["kind"] == "usage"]
        self.assertEqual(thinking, "合成思考前半合成思考后半")
        self.assertEqual(content, '{"month_vibe":"轻松平稳"}')
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["data"]["prompt_tokens"], 120)
        self.assertEqual(usages[0]["data"]["completion_tokens"], 45)
        self.assertTrue(all(e["unit"] == "emotion:2025-01" and e["label"] == "2025-01 月" for e in events))

    def test_stream_options_and_stream_400_fallback(self):
        seen_params = []

        class FakeBadRequest(RuntimeError):
            def __init__(self, msg):
                super().__init__(msg)
                self.status_code = 400

        def reject_stream_options(**kwargs):
            seen_params.append(dict(kwargs))
            if "stream_options" in kwargs:
                raise FakeBadRequest("Unknown parameter: stream_options")
            if kwargs.get("stream"):
                raise FakeBadRequest("Unsupported parameter: stream")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(reasoning_content="整块思维", content='{"ok": true}'),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
            )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=reject_stream_options))
        )
        events = []
        with llm_stream.stream_context(sink=events.append, unit="recap", label="整体总括", fresh_state=True):
            resp, err = llm_transport.request_with_retry(
                client,
                lambda: {"model": "m", "messages": []},
                tag="recap",
                max_attempts=2,
                generic_retries=1,
                tpm_wait=0.0,
                fatal_message="quota",
                count_call=lambda: None,
                pace=lambda: None,
                is_plan_exhausted=lambda _e: False,
                is_tpm_throttle=lambda _e: False,
                set_cooldown=lambda _s: None,
                tpm_max_attempts=2,
                logger=dc.logger,
                record_call=lambda *args: None,
                model="m",
                scrub_secrets=str,
                quota_exhausted_error=RuntimeError,
                sleep=lambda _s: None,
            )

        self.assertIsNone(err)
        self.assertEqual(len(seen_params), 3)
        self.assertEqual(resp.choices[0].message.content, '{"ok": true}')
        self.assertEqual("".join(e["text"] for e in events if e["kind"] == "thinking"), "整块思维")
        self.assertEqual("".join(e["text"] for e in events if e["kind"] == "content"), '{"ok": true}')

    def test_no_active_stream_preserves_original_kwargs(self):
        seen = []

        def fake_create(**kwargs):
            seen.append(dict(kwargs))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'), finish_reason="stop")],
                usage=None,
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        llm_transport.request_with_retry(
            client,
            lambda: {"model": "m", "messages": [], "max_tokens": 64},
            tag="emotion",
            max_attempts=1,
            generic_retries=0,
            tpm_wait=0.0,
            fatal_message="quota",
            count_call=lambda: None,
            pace=lambda: None,
            is_plan_exhausted=lambda _e: False,
            is_tpm_throttle=lambda _e: False,
            set_cooldown=lambda _s: None,
            tpm_max_attempts=1,
            logger=dc.logger,
            record_call=lambda *args: None,
            model="m",
            scrub_secrets=str,
            quota_exhausted_error=RuntimeError,
            sleep=lambda _s: None,
        )
        self.assertEqual(seen, [{"model": "m", "messages": [], "max_tokens": 64}])

    def test_mid_stream_cancellation_closes_stream_without_retrying(self):
        closed = []
        calls = [0]
        cancelled = [False]

        class FakeStream:
            def __iter__(self):
                yield _make_chunk(reasoning="第一段推理")
                cancelled[0] = True
                yield _make_chunk(content="后续不应输出")

            def close(self):
                closed.append(True)

        def fake_create(**_kwargs):
            calls[0] += 1
            return FakeStream()

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        events = []
        with llm_stream.stream_context(
            sink=events.append,
            unit="emotion:2025-01",
            label="2025-01 月",
            cancel_check=lambda: cancelled[0],
            fresh_state=True,
        ):
            with self.assertRaises(llm_stream.StreamCancelledError):
                llm_transport.request_with_retry(
                    client,
                    lambda: {"model": "m", "messages": []},
                    tag="emotion",
                    max_attempts=3,
                    generic_retries=2,
                    tpm_wait=0.0,
                    fatal_message="quota",
                    count_call=lambda: None,
                    pace=lambda: None,
                    is_plan_exhausted=lambda _e: False,
                    is_tpm_throttle=lambda _e: False,
                    set_cooldown=lambda _s: None,
                    tpm_max_attempts=2,
                    logger=dc.logger,
                    record_call=lambda *args: None,
                    model="m",
                    scrub_secrets=str,
                    quota_exhausted_error=RuntimeError,
                    sleep=lambda _s: None,
                )

        self.assertEqual(calls[0], 1)
        self.assertEqual(closed, [True])

    def test_tpm_and_error_retry_emit_warn_and_mocked_call_api_emits_result(self):
        events = []
        attempts = [0]

        def flaky_create(**_kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError("429 rate limit")
            raise RuntimeError("transient connection error")

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=flaky_create)))
        with llm_stream.stream_context(
            sink=events.append,
            unit="emotion:2025-01",
            label="2025-01 月",
            fresh_state=True,
        ):
            with self.assertRaises(RuntimeError):
                llm_transport.request_with_retry(
                    client,
                    lambda: {"model": "m", "messages": []},
                    tag="emotion",
                    max_attempts=3,
                    generic_retries=0,
                    tpm_wait=1.0,
                    fatal_message="quota",
                    count_call=lambda: None,
                    pace=lambda: None,
                    is_plan_exhausted=lambda _e: False,
                    is_tpm_throttle=lambda e: "429" in str(e),
                    set_cooldown=lambda _s: None,
                    tpm_max_attempts=2,
                    logger=dc.logger,
                    record_call=lambda *args: None,
                    model="m",
                    scrub_secrets=str,
                    quota_exhausted_error=RuntimeError,
                    sleep=lambda _s: None,
                )
        warns = [e["text"] for e in events if e["kind"] == "warn"]
        self.assertTrue(any("限流" in w for w in warns))

        # Mocked dc._call_api directly inside _analyze_periods should still emit result JSON fallback
        fallback_events = []
        msg = Message(
            id="m1",
            timestamp=1735783200000,
            time_str="2025-01-02 10:00:00",
            sender_name="甲",
            sender_uid="u1",
            text="测试问候在吗",
            raw_text="测试问候在吗",
            msg_type="type_1",
            has_image=False,
            is_reply=False,
        )
        valid_emotion = for_dimension("emotion")
        with (
            mock.patch.object(dc, "_call_api", return_value=dict(valid_emotion)),
            llm_stream.stream_context(sink=fallback_events.append, fresh_state=True),
        ):
            out = dc._analyze_periods(
                {"2025-01": [msg]},
                "sys",
                lambda _p, _m: "prompt",
                max_tokens=1024,
                tag="emotion",
                chat_hash="",
            )
        self.assertIn("2025-01", out)
        self.assertTrue(any(e["kind"] == "result" and "轻松愉快" in e["text"] for e in fallback_events))


class TestJobStreamPersistenceAndApi(unittest.TestCase):
    def setUp(self):
        with jobs.JOBS_LOCK:
            jobs.JOBS.clear()
        with job_stream._STREAM_LOCK:
            job_stream._STREAMS.clear()
        purge_marks.clear()
        if hasattr(store, "_RECENTLY_PURGED"):
            store._RECENTLY_PURGED.clear()

    def _upload_synthetic_chat(self, client, suffix="a"):
        import io
        from test_p1 import payload

        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": token}
        base = json.loads(payload().decode("utf-8"))
        base["chatInfo"]["name"] = f"合成私聊{suffix}"
        raw = json.dumps(base, ensure_ascii=False).encode("utf-8")

        resp = client.post(
            "/upload",
            data={"file": (io.BytesIO(raw), f"synthetic_{suffix}.json")},
            headers=headers,
            content_type="multipart/form-data",
        )
        self.assertIn(resp.status_code, (200, 302))
        with client.session_transaction() as sess:
            filepath = sess.get("filepath")
            chat_hash = sess["chat_hash"]
            sid = sess.sid
        if filepath:
            self.addCleanup(lambda p=filepath: os.path.exists(p) and os.remove(p))
        self.addCleanup(lambda h=chat_hash: store._purge_chat_caches(h))
        return sid, chat_hash, headers

    def test_joblog_created_first_key_and_excluded_from_bundle(self):
        chat_hash = "abc123def456"
        job_id = "job001122"
        job_stream.init_job(
            job_id,
            sid="sid-1",
            chat_hash=chat_hash,
            dim="emotion",
            dim_label="情绪分析",
            scope_id="",
            total=1,
        )
        job_stream.append_event(
            job_id,
            {"kind": "thinking", "unit": "emotion:2025-01", "label": "2025-01 月", "text": "片段一"},
        )
        job_stream.append_event(
            job_id,
            {"kind": "thinking", "unit": "emotion:2025-01", "label": "2025-01 月", "text": "片段二"},
        )
        job_stream.finalize_job(job_id, status="done", done=1, total=1)

        path = Path(store.AI_CACHE_DIR) / f"joblog_{chat_hash}_{job_id}.json"
        self.assertTrue(path.is_file())
        raw = path.read_text(encoding="utf-8")
        self.assertTrue(raw.lstrip().startswith('{"_created":'), raw[:80])
        bundle = store.chat_bundle_files(chat_hash)
        self.assertFalse(any(name.startswith("joblog_") for name in bundle["ai_cache"]))

    def test_incremental_cursor_reload_post_restart_and_isolation(self):
        client = appmod.app.test_client()
        sid, chat_hash, headers = self._upload_synthetic_chat(client, "iso")

        # Simulate active job inside JOBS + job_stream
        job_id = "jobdeadbeef1"
        with jobs.JOBS_LOCK:
            jobs.JOBS[job_id] = {
                "status": "running",
                "dim": "emotion",
                "done": 0,
                "total": 2,
                "cancel": False,
                "refresh": False,
                "chat_hash": chat_hash,
                "sid": sid,
                "created": time.time(),
                "scope_id": "",
            }
        job_stream.init_job(
            job_id,
            sid=sid,
            chat_hash=chat_hash,
            dim="emotion",
            dim_label="情绪分析",
            scope_id="",
            total=2,
        )
        job_stream.append_event(
            job_id,
            {"kind": "thinking", "unit": "emotion:2025-01", "label": "2025-01 月", "text": "思考一段"},
        )

        r1 = client.get(f"/api/analyze-job/{job_id}?after=0").get_json()
        self.assertEqual(r1["status"], "running")
        self.assertEqual(len(r1["events"]), 1)
        cursor = r1["stream_seq"]

        r_empty = client.get(f"/api/analyze-job/{job_id}?after={cursor}").get_json()
        self.assertEqual(r_empty["events"], [])
        self.assertEqual(r_empty["stream_seq"], cursor)

        job_stream.append_event(
            job_id,
            {"kind": "content", "unit": "emotion:2025-01", "label": "2025-01 月", "text": '{"mood": "ok"}'},
        )
        r2 = client.get(f"/api/analyze-job/{job_id}?after={cursor}").get_json()
        self.assertEqual(len(r2["events"]), 1)
        self.assertEqual(r2["events"][0]["kind"], "content")

        # Reload endpoint returns full output history while running
        latest = client.get("/api/analyze-jobs/latest").get_json()
        self.assertTrue(latest["found"])
        self.assertEqual(latest["status"], "running")
        self.assertEqual(len(latest["events"]), 2)

        # Scope mismatch is isolated
        wrong_scope = client.get("/api/analyze-jobs/latest?scope_id=deadbeef0000").get_json()
        self.assertFalse(wrong_scope["found"])

        # Another browser session with same uploaded chat cannot read this job or stream
        other_client = appmod.app.test_client()
        self._upload_synthetic_chat(other_client, "iso")
        self.assertEqual(other_client.get(f"/api/analyze-job/{job_id}").status_code, 404)
        self.assertFalse(other_client.get("/api/analyze-jobs/latest").get_json()["found"])

        # Simulate server restart while job was still "running" on disk
        with jobs.JOBS_LOCK:
            jobs.JOBS.clear()
        with job_stream._STREAM_LOCK:
            job_stream._STREAMS.clear()
        recovered = client.get("/api/analyze-jobs/latest").get_json()
        self.assertTrue(recovered["found"])
        self.assertEqual(recovered["status"], "error")
        self.assertIn("中断", recovered["error"])
        self.assertTrue(any(e["kind"] == "thinking" for e in recovered["events"]))
        self.assertTrue(any(e["kind"] == "content" for e in recovered["events"]))

        # Deleting chat clears joblogs on disk and invalidates API recovery
        del_resp = client.post("/api/chat/delete", headers=headers)
        self.assertEqual(del_resp.status_code, 200)
        self.assertFalse(client.get("/api/analyze-jobs/latest").get_json()["found"])
        leftover = [n for n in os.listdir(store.AI_CACHE_DIR) if n.startswith(f"joblog_{chat_hash}_")]
        self.assertEqual(leftover, [])

    def test_panel_included_only_on_analysis_pages_without_emojis(self):
        client = appmod.app.test_client()
        self._upload_synthetic_chat(client, "tmpl")
        for path in ("/dashboard", "/emotion", "/topics", "/relationship", "/habits", "/profile", "/recap"):
            body = client.get(path).get_data(as_text=True)
            self.assertIn('id="aiOutputPanel"', body, path)
            self.assertIn("data-ai-output-slot", body, path)
        for path in ("/", "/messages", "/report"):
            body = client.get(path).get_data(as_text=True)
            self.assertNotIn('id="aiOutputPanel"', body, path)


if __name__ == "__main__":
    unittest.main()
