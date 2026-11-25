import hashlib
import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

import app  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer import month_cache, purge_marks  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import vision  # noqa: E402
from analyzer.deepseek_client import AnalysisIncompleteError, QuotaExhaustedError  # noqa: E402
from parser.group_identity import Participant  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from result_fixtures import emotion, profile  # noqa: E402
from webapp import analysis_plan, api, jobs, store  # noqa: E402


def _message(message_id, uid, name, month, day, text, *, has_image=False):
    dt = datetime(2025, month, day, 9, 0, tzinfo=CST)
    timestamp = int(dt.timestamp() * 1000)
    return Message(
        id=message_id,
        timestamp=timestamp,
        time_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=has_image,
        is_reply=False,
    )


def _chat(*, with_image=False):
    return ChatData(
        chat_name="合成私聊",
        self_name="甲",
        other_name="乙",
        self_uid="u_a",
        other_uid="u_b",
        messages=[
            _message("jan-a", "u_a", "甲", 1, 1, "合成的一月消息", has_image=with_image),
            _message("jan-b", "u_b", "乙", 1, 2, "合成的一月回复"),
            _message("feb-a", "u_a", "甲", 2, 1, "合成的二月消息"),
        ],
    )


class ReadOnlyPlanTests(unittest.TestCase):
    def test_dimension_and_month_cache_reads_do_not_touch_or_migrate(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(store, "AI_CACHE_DIR", directory):
                old_fp = dc.legacy_fingerprint_for_dimension("emotion")
                old_path = Path(store._cache_path("emotion", "readonly-chat", fingerprint=old_fp))
                old_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.write_text(json.dumps({"_created": 1, "result": {"paid": True}}), encoding="utf-8")
                old_time = time.time() - 3600
                os.utime(old_path, (old_time, old_time))
                current_fp = dc.fingerprint_for_dimension("emotion")
                self.assertEqual(
                    store._read_cache("emotion", "readonly-chat", fingerprint=current_fp, read_only=True),
                    {"paid": True},
                )
                self.assertTrue(old_path.exists())
                self.assertEqual(old_path.stat().st_mtime, old_time)

            mc.configure_month_cache(directory)
            self.addCleanup(mc.configure_month_cache, "")
            key = mc._month_key("sys", "readonly prompt")
            month_path = Path(mc.month_cache_path(key))
            month_path.write_text(json.dumps({"_created": 1, "paid": True}), encoding="utf-8")
            os.utime(month_path, (old_time, old_time))
            self.assertEqual(mc._read_month_cache(key, expect_thinking=False, read_only=True), {"paid": True})
            self.assertEqual(month_path.stat().st_mtime, old_time)
            self.assertNotIn("_thinking", json.loads(month_path.read_text(encoding="utf-8")))

    def test_vision_preview_cache_read_does_not_touch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vision-cache.json"
            path.write_text(json.dumps({"digest": "cached summary"}), encoding="utf-8")
            old_time = time.time() - 3600
            os.utime(path, (old_time, old_time))
            self.assertEqual(vision._read_cache(str(path), touch=False), "cached summary")
            self.assertEqual(path.stat().st_mtime, old_time)

    def test_plan_uses_shared_prompt_builders_and_reports_unknown_image_input(self):
        chat = _chat(with_image=True)
        with (
            mock.patch.object(vision, "preview_digest", return_value=("", True)) as preview,
            mock.patch.object(dc, "_call_api", side_effect=AssertionError("preview called text model")),
            mock.patch.object(dc, "_call_vision", side_effect=AssertionError("preview called vision model")),
        ):
            plan = analysis_plan.build_analysis_plan(
                chat, "synthetic-plan-hash", ["emotion", "habits", "profile"]
            )
        self.assertGreaterEqual(preview.call_count, 3)
        self.assertEqual(len(plan["units"]), 6)
        self.assertEqual(
            [unit["key"] for unit in plan["units"] if unit["dimension"] == "habits"],
            ["habits:person:self", "habits:person:other"],
        )
        image_unit = next(unit for unit in plan["units"] if unit["vision_uncertain"])
        self.assertEqual(image_unit["status"], "暂时无法确定")
        self.assertGreaterEqual(image_unit["vision_calls_max"], 1)

    def test_force_refresh_marks_uncached_images_and_bypasses_unit_cache(self):
        chat = _chat(with_image=True)
        with mock.patch.object(vision, "preview_digest", return_value=("", True)):
            plan = analysis_plan.build_analysis_plan(
                chat, "synthetic-refresh-hash", ["emotion"], refresh=True
            )
        self.assertTrue(plan["refresh"])
        self.assertTrue(plan["units"][0]["vision_uncertain"])
        self.assertEqual(plan["units"][0]["text_calls_min"], 1)
        self.assertEqual(plan["summary"]["vision_calls_min"], 1)


class AnalysisPlanApiTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.headers = {
                "Origin": "http://localhost:5000",
                "X-CSRF-Token": session["csrf_token"],
            }
            session["chat_mode"] = "private"
            session["chat_hash"] = "synthetic-api-plan"
            session["filepath"] = "virtual-chat.json"
        self.chat = _chat()
        self._session_patch = mock.patch.object(
            api, "_session_chat_file", return_value=("virtual-chat.json", None)
        )
        self._load_patch = mock.patch.object(store, "_load_chat_cached", return_value=self.chat)
        self._session_patch.start()
        self._load_patch.start()

    def tearDown(self):
        self._session_patch.stop()
        self._load_patch.stop()
        jobs.JOBS.clear()

    def test_plan_endpoint_and_start_revalidation(self):
        preview = self.client.get("/api/analysis-plan?dimensions=emotion")
        self.assertEqual(preview.status_code, 200)
        plan = preview.get_json()
        self.assertEqual(len(plan["units"]), 2)

        with (
            mock.patch.object(api, "is_api_configured", return_value=True),
            mock.patch.object(api.threading, "Thread") as thread,
        ):
            stale = self.client.post(
                "/api/analyze-plan",
                json={
                    "plan_id": "stale-plan",
                    "dimensions": ["emotion"],
                    "selected_units": [plan["units"][0]["key"]],
                },
                headers=self.headers,
            )
            self.assertEqual(stale.status_code, 409)
            self.assertTrue(stale.get_json()["changed"])
            thread.assert_not_called()

            started = self.client.post(
                "/api/analyze-plan",
                json={
                    "plan_id": plan["plan_id"],
                    "dimensions": ["emotion"],
                    "selected_units": [plan["units"][0]["key"]],
                },
                headers=self.headers,
            )
            self.assertEqual(started.status_code, 200)
            self.assertIn("job", started.get_json())
            thread.assert_called_once()

    def test_plan_start_rejects_malformed_dimensions_and_refresh_types(self):
        cases = (
            {"dimensions": 7},
            {"dimensions": ["emotion", 7]},
            {"dimensions": None},
            {"dimensions": ["emotion"], "refresh": "false"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/analyze-plan", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 400)

    def test_plan_start_rejects_units_without_analyzable_messages(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"date_from": "2025-01-02", "date_to": "2025-01-02"}},
            headers=self.headers,
        )
        plan = self.client.get("/api/analysis-plan?dimensions=profile").get_json()
        unavailable = next(unit for unit in plan["units"] if unit["status"] == "无可分析消息")

        with mock.patch.object(api.threading, "Thread") as thread:
            response = self.client.post(
                "/api/analyze-plan",
                json={
                    "plan_id": plan["plan_id"],
                    "dimensions": ["profile"],
                    "selected_units": [unavailable["key"]],
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 400)
        thread.assert_not_called()

    def test_failed_plan_job_api_returns_saved_partial_state(self):
        with self.client.session_transaction() as session:
            sid = session.sid
        job_id, _, _ = jobs._get_or_create_job(
            sid, "plan", "synthetic-api-plan", 2, conflict_dimension="*", identity="failed-partial-api"
        )
        jobs.JOBS[job_id].update(
            status="error",
            result={"emotion": "error", "profile": "done"},
            results={"profile": {"self": "synthetic saved result"}},
            summary={"emotion": "error", "profile": "done"},
            partial_results={"emotion": {"2025-01": "synthetic partial"}},
            partial=True,
            error="synthetic failure",
            finished_at=time.time(),
        )
        response = self.client.get("/api/analyze-job/" + job_id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["result"], {"emotion": "error", "profile": "done"})
        self.assertEqual(body["summary"], {"emotion": "error", "profile": "done"})
        self.assertEqual(body["partial_results"]["emotion"]["2025-01"], "synthetic partial")
        self.assertEqual(body["results"]["profile"]["self"], "synthetic saved result")

        jobs.JOBS[job_id].update(status="cancelled", error="分析已取消", partial=True)
        cancelled = self.client.get("/api/analyze-job/" + job_id).get_json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["result"], {"emotion": "error", "profile": "done"})
        self.assertEqual(cancelled["summary"], {"emotion": "error", "profile": "done"})
        self.assertEqual(cancelled["partial_results"]["emotion"]["2025-01"], "synthetic partial")

        with mock.patch.object(store, "_is_recently_purged", return_value=True):
            purged = self.client.get("/api/analyze-job/" + job_id).get_json()
        self.assertEqual(purged["status"], "cancelled")
        self.assertEqual(purged["results"], {})
        self.assertEqual(purged["partial_results"], {})
        self.assertEqual(jobs.JOBS[job_id]["result"], {})

    def test_chat_cleanup_redacts_legacy_dimension_job_results(self):
        with self.client.session_transaction() as session:
            sid = session.sid
        job_id, _, _ = jobs._get_or_create_job(
            sid, "emotion", "synthetic-api-plan", 1, identity="purged-legacy-dimension"
        )
        jobs.JOBS[job_id].update(
            status="done",
            result={"2025-01": {"synthetic": "paid result"}},
            finished_at=time.time(),
        )

        with mock.patch.object(store, "_is_recently_purged", return_value=True):
            response = self.client.get("/api/analyze-job/" + job_id)

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["result"], {})
        self.assertNotIn("paid result", response.get_data(as_text=True))
        self.assertEqual(jobs.JOBS[job_id]["result"], {})

    def test_non_object_json_requests_return_client_errors(self):
        scope = self.client.post(
            "/api/analysis-scope", data="[]", content_type="application/json", headers=self.headers
        )
        plan = self.client.post(
            "/api/analyze-plan", data="[]", content_type="application/json", headers=self.headers
        )
        with mock.patch.object(api, "is_api_configured", return_value=True):
            ask = self.client.post(
                "/api/ask", data="[]", content_type="application/json", headers=self.headers
            )

        self.assertEqual(scope.status_code, 400)
        self.assertEqual(plan.status_code, 400)
        self.assertEqual(ask.status_code, 400)


class PartialPlanExecutionTests(unittest.TestCase):
    def test_all_runnable_person_units_count_as_a_complete_dimension(self):
        chat = _chat()
        scope = {"date_from": "2025-01-02", "date_to": "2025-01-02"}
        scoped, _ = api.apply_scope(chat, scope)
        plan = analysis_plan.build_analysis_plan(scoped, "synthetic-target-profile", ["profile"])
        self.assertEqual(
            [(unit["key"], unit["status"]) for unit in plan["units"]],
            [
                ("profile:person:self", "无可分析消息"),
                ("profile:person:other", "待调用"),
            ],
        )
        selected = [unit["key"] for unit in plan["units"] if unit["status"] != "无可分析消息"]

        def fake_profile(chat_arg, **_kwargs):
            self.assertEqual([message.id for message in chat_arg.messages], ["jan-b"])
            return {"other": {"verdict": "synthetic"}}

        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(jobs, "analyze_func_for", return_value=fake_profile),
            mock.patch.object(jobs, "validate_dimension_result"),
            mock.patch.object(store, "_write_cache") as write_cache,
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-target-profile-sid",
                "plan",
                "synthetic-target-profile",
                len(selected),
                conflict_dimension="*",
                identity="target-profile-plan",
            )
            jobs._run_selected_plan(
                jid,
                "virtual-chat.json",
                "synthetic-target-profile",
                False,
                plan,
                selected,
                scope,
            )

        write_cache.assert_called_once()
        self.assertEqual(jobs.JOBS[jid]["summary"]["profile"], "done")
        self.assertFalse(jobs.JOBS[jid]["partial"])

    def test_partial_selection_slices_complete_dimension_cache_by_unit_key(self):
        self.assertEqual(
            jobs._plan_result_slice(
                "profile", {"self": "self-result", "other": "other-result"}, ["profile:person:other"]
            ),
            {"other": "other-result"},
        )
        self.assertEqual(
            jobs._plan_result_slice(
                "member_profiles", {"u-a": "a-result", "u-b": "b-result"}, ["member_profiles:member:u-b"]
            ),
            {"u-b": "b-result"},
        )
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-cached-partial", ["emotion"])
        selected = [plan["units"][0]["key"]]
        cached = {"2025-01": emotion(), "2025-02": {"marker": "not-selected"}}
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=cached),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-cached-sid",
                "plan",
                "synthetic-cached-partial",
                len(selected),
                conflict_dimension="*",
                identity="cached-partial-plan",
            )
            jobs._run_selected_plan(
                jid, "virtual-chat.json", "synthetic-cached-partial", False, plan, selected
            )
        self.assertEqual(jobs.JOBS[jid]["result"], {"2025-01": cached["2025-01"]})
        self.assertEqual(jobs.JOBS[jid]["summary"]["emotion"], "partial")

    def test_partial_month_run_does_not_write_dimension_cache(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-partial-hash", ["emotion"])
        selected = [plan["units"][0]["key"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(dc, "_call_api", return_value=emotion()) as call,
            mock.patch.object(store, "_write_cache") as write_dimension_cache,
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-sid",
                "plan",
                "synthetic-partial-hash",
                len(selected),
                conflict_dimension="*",
                identity="partial-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-partial-hash", False, plan, selected)
        self.assertEqual(call.call_count, 1)
        write_dimension_cache.assert_not_called()
        self.assertEqual(jobs.JOBS[jid]["status"], "done")
        self.assertTrue(jobs.JOBS[jid]["partial"])
        self.assertEqual(jobs.JOBS[jid]["summary"]["emotion"], "partial")
        self.assertEqual(len(jobs.JOBS[jid]["result"]), 1)

    def test_all_selected_months_write_complete_dimension_cache(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-full-hash", ["emotion"])
        selected = [unit["key"] for unit in plan["units"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(dc, "_call_api", return_value=emotion()) as call,
            mock.patch.object(store, "_write_cache") as write_dimension_cache,
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-sid-full",
                "plan",
                "synthetic-full-hash",
                len(selected),
                conflict_dimension="*",
                identity="full-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-full-hash", False, plan, selected)
        self.assertEqual(call.call_count, 2)
        write_dimension_cache.assert_called_once()
        self.assertFalse(jobs.JOBS[jid]["partial"])
        self.assertEqual(jobs.JOBS[jid]["summary"]["emotion"], "done")

    def test_plan_worker_reports_error_instead_of_done(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-failed-plan", ["emotion"])
        selected = [plan["units"][0]["key"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(
                jobs,
                "_run_selected_dimension",
                side_effect=RuntimeError("synthetic analysis failure"),
            ),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-error-sid",
                "plan",
                "synthetic-failed-plan",
                len(selected),
                conflict_dimension="*",
                identity="failed-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-failed-plan", False, plan, selected)
        self.assertEqual(jobs.JOBS[jid]["summary"]["emotion"], "error")
        self.assertEqual(jobs.JOBS[jid]["status"], "error")

    def test_quota_failure_persists_terminal_plan_summary_and_partial_fields(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-quota-plan", ["emotion"])
        selected = [plan["units"][0]["key"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(
                jobs,
                "_run_selected_dimension",
                side_effect=jobs.QuotaExhaustedError("synthetic quota"),
            ),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-quota-sid",
                "plan",
                "synthetic-quota-plan",
                len(selected),
                conflict_dimension="*",
                identity="quota-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-quota-plan", False, plan, selected)
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["summary"]["emotion"], "aborted")
        self.assertTrue(job["partial"])
        self.assertIn("synthetic quota", job["error"])
        self.assertIn("result", job)
        self.assertIn("partial_results", job)

    def test_plan_failure_before_chat_load_still_has_terminal_result_fields(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-missing-plan", ["emotion"])
        jid, _, _ = jobs._get_or_create_job(
            "synthetic-missing-sid",
            "plan",
            "synthetic-missing-plan",
            1,
            conflict_dimension="*",
            identity="missing-plan",
        )
        with mock.patch.object(jobs.os.path, "exists", return_value=False):
            jobs._run_selected_plan(
                jid, "missing-chat.json", "synthetic-missing-plan", False, plan, [plan["units"][0]["key"]]
            )
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["partial"])
        self.assertEqual(job["result"], {})
        self.assertEqual(job["summary"], {})
        self.assertEqual(job["partial_results"], {})

    def test_chat_cleanup_discards_in_memory_plan_results(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-purged-plan", ["emotion"])
        selected = [plan["units"][0]["key"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(jobs.purge_marks, "is_marked", return_value=True),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-purged-sid",
                "plan",
                "synthetic-purged-plan",
                len(selected),
                conflict_dimension="*",
                identity="purged-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-purged-plan", False, plan, selected)
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["results"], {})
        self.assertEqual(job["partial_results"], {})
        self.assertEqual(job["result"], {})

    def test_cancelled_plan_keeps_results_from_the_running_unit(self):
        chat = _chat()
        plan = analysis_plan.build_analysis_plan(chat, "synthetic-cancel-plan", ["emotion", "habits"])
        selected = [
            next(unit["key"] for unit in plan["units"] if unit["dimension"] == dimension)
            for dimension in ("emotion", "habits")
        ]
        jid = None

        def cancel_after_result(*_args, **_kwargs):
            with jobs.JOBS_LOCK:
                jobs.JOBS[jid]["cancel"] = True
            return {"2025-01": emotion()}

        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(jobs, "_run_selected_dimension", side_effect=cancel_after_result),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "synthetic-cancel-sid",
                "plan",
                "synthetic-cancel-plan",
                len(selected),
                conflict_dimension="*",
                identity="cancel-plan",
            )
            jobs._run_selected_plan(jid, "virtual-chat.json", "synthetic-cancel-plan", False, plan, selected)
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["partial"])
        self.assertEqual(job["results"]["emotion"], {"2025-01": emotion()})
        self.assertEqual(job["partial_results"]["emotion"], {"2025-01": emotion()})
        self.assertEqual(job["summary"]["habits"], "cancelled")


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.sid = session.sid
            self.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
            session.update(chat_mode="private", chat_hash="synthetic-review-chat", filepath="virtual.json")
        self.chat = _chat()
        self.patchers = [
            mock.patch.object(api, "_session_chat_file", return_value=("virtual.json", None)),
            mock.patch.object(store, "_load_chat_cached", return_value=self.chat),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(jobs.JOBS.clear)

    def completed_job(self, status="done"):
        job_id, _, _ = jobs._get_or_create_job(self.sid, "profile", "synthetic-review-chat", 1)
        jobs.JOBS[job_id].update(
            status=status, result={"self": {"verdict": "synthetic saved result"}}, finished_at=time.time()
        )
        return job_id

    def test_delete_with_shared_cache_revokes_old_job_access_and_clears_scope(self):
        job_id = self.completed_job()
        with self.client.session_transaction() as session:
            session["analysis_scope"] = {"message_positions": [0], "target_uids": ["u_a"]}
        other_client = app.app.test_client()
        other_client.get("/")
        with other_client.session_transaction() as session:
            other_sid = session.sid
            session.update(chat_hash="synthetic-review-chat", filepath="another.json")
        other_job, _, _ = jobs._get_or_create_job(other_sid, "profile", "synthetic-review-chat", 1)
        jobs.JOBS[other_job].update(
            status="done", result={"paid": "another session"}, finished_at=time.time()
        )
        with (
            mock.patch.object(store, "other_live_sessions", return_value={other_sid}),
            mock.patch.object(store, "_purge_chat_caches") as purge,
            mock.patch.object(store, "_is_recently_purged", return_value=False),
        ):
            deleted = self.client.post("/api/chat/delete", headers=self.headers)
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.get_json()["kept_for_other_sessions"])
            purge.assert_not_called()
            denied = self.client.get("/api/analyze-job/" + job_id)
            self.assertEqual(denied.status_code, 404)
            self.assertNotIn("synthetic saved result", denied.get_data(as_text=True))
            self.assertEqual(other_client.get("/api/analyze-job/" + other_job).status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("analysis_scope", session)

    def test_switching_chat_denies_old_job(self):
        job_id = self.completed_job()
        with self.client.session_transaction() as session:
            session["chat_hash"] = "synthetic-different-chat"
        self.assertEqual(self.client.get("/api/analyze-job/" + job_id).status_code, 404)

    def test_delete_clears_scope_even_when_chat_cache_is_purged(self):
        with self.client.session_transaction() as session:
            session["analysis_scope"] = {"date_from": "2025-01-01"}
        with (
            mock.patch.object(store, "other_live_sessions", return_value=set()),
            mock.patch.object(store, "_purge_chat_caches", return_value=0),
        ):
            self.assertEqual(self.client.post("/api/chat/delete", headers=self.headers).status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("analysis_scope", session)

    def test_thread_start_failure_is_terminal_and_retry_creates_a_new_job(self):
        preview = self.client.get("/api/analysis-plan?dimensions=profile").get_json()
        payload = {
            "plan_id": preview["plan_id"],
            "dimensions": ["profile"],
            "selected_units": [unit["key"] for unit in preview["units"]],
        }
        with (
            mock.patch.object(api, "is_api_configured", return_value=True),
            mock.patch.object(api.threading, "Thread") as thread,
        ):
            thread.return_value.start.side_effect = RuntimeError("synthetic thread failure")
            failed = self.client.post("/api/analyze-plan", json=payload, headers=self.headers)
            self.assertEqual(failed.status_code, 503)
            job_id = next(iter(jobs.JOBS))
            self.assertEqual(jobs.JOBS[job_id]["status"], "error")
            self.assertIn("finished_at", jobs.JOBS[job_id])
            thread.return_value.start.side_effect = None
            retry = self.client.post("/api/analyze-plan", json=payload, headers=self.headers)
            self.assertEqual(retry.status_code, 200)
            self.assertNotEqual(retry.get_json()["job"], job_id)
            self.assertFalse(retry.get_json().get("reused", False))

    def test_read_failures_are_json_errors_for_new_apis(self):
        endpoints = [
            ("get", "/api/analysis-plan?dimensions=profile", None),
            ("post", "/api/analyze-plan", {"dimensions": ["profile"]}),
            ("get", "/api/analysis-scope", None),
            ("post", "/api/analysis-scope", {"scope": {}}),
            ("get", "/api/messages/timeline?q=合成", None),
            ("post", "/api/stats/compare", {"a": {}, "b": {}}),
            ("post", "/api/report/share", {"options": {}}),
            ("get", "/api/analysis/profile", None),
            ("get", "/api/evidence/profile", None),
        ]
        for error, expected in (
            (FileNotFoundError("synthetic-path"), 410),
            (ValueError("invalid"), 400),
            (OSError("unreadable"), 400),
            (KeyError("malformed"), 400),
        ):
            for method, url, payload in endpoints:
                with self.subTest(error=type(error).__name__, url=url):
                    with mock.patch.object(store, "_load_chat_cached", side_effect=error):
                        kwargs = {"json": payload} if payload is not None else {}
                        response = getattr(self.client, method)(url, headers=self.headers, **kwargs)
                    self.assertEqual(response.status_code, expected)
                    self.assertTrue(response.is_json)
                    self.assertIn("error", response.get_json())
                    self.assertNotIn("synthetic-path", response.get_data(as_text=True))


class ReviewPlanTests(unittest.TestCase):
    def test_empty_person_results_fail_the_plan_and_never_enter_dimension_cache(self):
        for dimension in ("profile", "member_profiles"):
            for selected_all in (True, False):
                with self.subTest(dimension=dimension, selected_all=selected_all):
                    chat = _chat()
                    if dimension == "member_profiles":
                        chat.is_group_chat = True
                    plan = analysis_plan.build_analysis_plan(chat, "synthetic-empty-review", [dimension])
                    selected = [unit["key"] for unit in plan["units"]]
                    if not selected_all:
                        selected = selected[:1]
                    jid, _, _ = jobs._get_or_create_job(
                        "synthetic-empty-sid", "plan", "synthetic-empty-review", len(selected)
                    )
                    with (
                        mock.patch.object(jobs.os.path, "exists", return_value=True),
                        mock.patch.object(store, "_load_chat_cached", return_value=chat),
                        mock.patch.object(store, "_read_cache", return_value=None),
                        mock.patch.object(jobs, "analyze_func_for", return_value=lambda *a, **k: {}),
                        mock.patch.object(dc, "_analyze_person", return_value=None),
                        mock.patch.object(gc, "_analyze_member", return_value=None),
                        mock.patch.object(store, "_write_cache") as write,
                        mock.patch.object(store, "append_job_history"),
                    ):
                        jobs._run_selected_plan(
                            jid, "virtual.json", "synthetic-empty-review", False, plan, selected
                        )
                    job = jobs.JOBS.pop(jid)
                    expected_summary = (
                        "error" if dimension == "member_profiles" and not selected_all else "empty"
                    )
                    self.assertEqual(job["summary"][dimension], expected_summary)
                    self.assertEqual(job["status"], "error")
                    self.assertTrue(job["partial"])
                    self.assertNotIn(dimension, job.get("results", {}))
                    if expected_summary == "error":
                        self.assertIn("成员画像分析未取得", job["error"])
                    else:
                        self.assertIn("未返回分析结果", job["error"])
                    write.assert_not_called()


def _rep_message(message_id, uid, name, month, day, text):
    dt = datetime(2025, month, day, 9, 0, tzinfo=CST)
    return Message(
        id=message_id,
        timestamp=int(dt.timestamp() * 1000),
        time_str=dt.strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


def _rep_chat(*, group=False):
    return ChatData(
        chat_name="合成审核对话",
        self_name="合成甲",
        other_name="合成乙",
        self_uid="u_a",
        other_uid="u_b",
        messages=[
            _rep_message("jan-a", "u_a", "合成甲", 1, 1, "合成一月甲消息"),
            _rep_message("jan-b", "u_b", "合成乙", 1, 2, "合成一月乙消息"),
            _rep_message("feb-a", "u_a", "合成甲", 2, 1, "合成二月甲消息"),
        ],
        is_group_chat=group,
    )


class SelectedPlanRepairTests(unittest.TestCase):
    def _run_plan(self, chat, dimension, selected, *, run_selected=None):
        chat_hash = f"synthetic-{dimension}-repair"
        plan = analysis_plan.build_analysis_plan(chat, chat_hash, [dimension])
        if selected is None:
            selected = [unit["key"] for unit in plan["units"] if unit["status"] != "无可分析消息"]
        jid, _, _ = jobs._get_or_create_job(
            f"synthetic-{dimension}-sid",
            "plan",
            chat_hash,
            len(selected),
            conflict_dimension="*",
            identity=f"synthetic-{dimension}-plan",
        )
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(store, "append_job_history"),
            mock.patch.object(jobs, "_run_selected_dimension", run_selected)
            if run_selected is not None
            else mock.patch.object(jobs, "_run_selected_dimension", wraps=jobs._run_selected_dimension),
        ):
            jobs._run_selected_plan(jid, "synthetic-chat.json", chat_hash, False, plan, selected)
        return jobs.JOBS[jid]

    def test_partial_dimension_result_is_validated_before_reporting_success(self):
        job = self._run_plan(
            _rep_chat(),
            "emotion",
            ["emotion:month:2025-01"],
            run_selected=mock.Mock(return_value={"2025-01": {"nonsense": "synthetic malformed"}}),
        )

        self.assertEqual(job["status"], "error")
        self.assertEqual(job["summary"]["emotion"], "error")
        self.assertNotIn("nonsense", str(job.get("results", {})))

    def test_fresh_month_result_is_validated_when_month_cache_is_disabled(self):
        chat = _rep_chat()
        with (
            mock.patch.object(dc, "month_cache_enabled", return_value=False),
            mock.patch.object(dc, "_call_api", return_value={"nonsense": "synthetic malformed"}),
            self.assertRaises(AnalysisIncompleteError),
        ):
            dc._analyze_periods(
                {"2025-01": chat.months()["2025-01"]},
                "synthetic system",
                lambda _period, _messages: "synthetic prompt",
                100,
                tag="emotion",
                chat_hash="synthetic-month-chat",
            )

    def test_fresh_person_result_is_validated_when_month_cache_is_disabled(self):
        chat = _rep_chat()
        with (
            mock.patch.object(dc, "month_cache_enabled", return_value=False),
            mock.patch.object(dc, "_call_api", return_value={"nonsense": "synthetic malformed"}),
            self.assertRaises(AnalysisIncompleteError),
        ):
            dc._analyze_person(
                "synthetic system",
                100,
                [message for message in chat.messages if message.sender_uid == chat.self_uid],
                chat.self_name,
                "{display_name}\n{dialog}",
                100,
                tag="profile",
                chat_hash="synthetic-person-chat",
            )

    def test_fresh_member_result_is_validated_when_month_cache_is_disabled(self):
        chat = _rep_chat(group=True)
        member = Participant(uid="u_a", name="合成甲", is_self=True)
        request = {
            "messages": chat.messages[:1],
            "prompt": "synthetic prompt",
            "cache_key": "synthetic-member-schema-off",
            "thinking": False,
        }
        with (
            mock.patch.object(gc, "month_cache_enabled", return_value=False, create=True),
            mock.patch.object(gc, "_prepare_member_request", return_value=request),
            mock.patch.object(dc, "_read_result_cache", return_value=None),
            mock.patch.object(gc, "_call_api", return_value={"nonsense": "synthetic malformed"}),
            self.assertRaises(AnalysisIncompleteError),
        ):
            gc._analyze_member(
                chat,
                member,
                "synthetic system",
                "{display_name} {group_name} {context} {dialog}",
                100,
                "member_profiles",
                chat_hash="synthetic-member-schema-off",
            )

    def test_full_member_analyzer_attaches_all_prior_successes_on_incomplete_error(self):
        chat = _rep_chat(group=True)
        members = [
            Participant(uid="u_a", name="合成甲", is_self=True),
            Participant(uid="u_b", name="合成乙"),
        ]
        first = profile(members[0].name, group=True)
        with (
            mock.patch.object(gc, "select_ai_members", return_value=members),
            mock.patch.object(
                gc, "_analyze_member", side_effect=[first, AnalysisIncompleteError("synthetic miss")]
            ),
            mock.patch.object(gc, "_record_member_usage"),
            self.assertRaises(AnalysisIncompleteError) as caught,
        ):
            gc.analyze_member_profiles(chat, chat_hash="synthetic-full-member-partial")
        self.assertEqual(getattr(caught.exception, "partial_result", None), {members[0].uid: first})

    def test_partial_member_quota_failure_records_real_manifest_and_aborts_next_dimension(self):
        chat = _rep_chat(group=True)
        chat_hash = "synthetic-real-member-manifest"
        members = [
            Participant(uid="u_a", name="合成甲", is_self=True),
            Participant(uid="u_b", name="合成乙"),
            Participant(uid="u_c", name="合成丙"),
        ]
        keys = {member.uid: f"review-paid-{member.uid}" for member in members}
        plan = {
            "dimensions": ["member_profiles", "group_topics"],
            "units": [
                {
                    "key": f"member_profiles:member:{member.uid}",
                    "dimension": "member_profiles",
                    "status": "待调用",
                }
                for member in members
            ]
            + [
                {
                    "key": f"group_topics:month:{month}",
                    "dimension": "group_topics",
                    "status": "待调用",
                }
                for month in chat.months()
            ],
        }
        selected = [unit["key"] for unit in plan["units"] if not unit["key"].endswith(":u_c")]
        selected_member_keys = [key for key in selected if key.startswith("member_profiles:")]
        selected_topic_keys = [key for key in selected if key.startswith("group_topics:")]
        self.assertEqual(len(selected_member_keys), 2)
        self.assertEqual(len(selected_topic_keys), len(chat.months()))

        with tempfile.TemporaryDirectory() as cache_dir:
            old_cache_dir = month_cache._MONTH_CACHE_DIR
            old_grace = month_cache.MONTH_CACHE_GRACE_SECONDS
            month_cache.configure_month_cache(cache_dir)
            month_cache.MONTH_CACHE_GRACE_SECONDS = 0
            self.addCleanup(month_cache.configure_month_cache, old_cache_dir)
            self.addCleanup(setattr, month_cache, "MONTH_CACHE_GRACE_SECONDS", old_grace)

            def prepare(_chat, member, *_args, **_kwargs):
                return {
                    "messages": chat.messages[:1],
                    "prompt": "synthetic member prompt",
                    "cache_key": keys[member.uid],
                    "thinking": False,
                }

            jid, _, _ = jobs._get_or_create_job(
                "synthetic-manifest-sid",
                "plan",
                chat_hash,
                len(selected),
                conflict_dimension="*",
                identity="synthetic-manifest-plan",
            )
            quota = QuotaExhaustedError("synthetic quota exhaustion")
            with (
                mock.patch.object(jobs.os.path, "exists", return_value=True),
                mock.patch.object(store, "_load_chat_cached", return_value=chat),
                mock.patch.object(store, "_read_cache", return_value=None),
                mock.patch.object(store, "append_job_history"),
                mock.patch.object(gc, "select_ai_members", return_value=members),
                mock.patch.object(gc, "_prepare_member_request", side_effect=prepare),
                mock.patch.object(dc, "_read_result_cache", return_value=None),
                mock.patch.object(
                    gc, "_call_api", side_effect=[profile("合成甲", group=True), quota]
                ) as call_api,
            ):
                jobs._run_selected_plan(jid, "synthetic-chat.json", chat_hash, False, plan, selected)

            job = jobs.JOBS[jid]
            self.assertEqual(job["status"], "error")
            self.assertEqual(job["summary"]["member_profiles"], "aborted")
            self.assertEqual(job["summary"]["group_topics"], "aborted")
            self.assertIn("u_a", job.get("results", {}).get("member_profiles", {}))
            self.assertEqual(call_api.call_count, 2)

            manifest_path = month_cache._manifest_path(chat_hash)
            self.assertTrue(os.path.exists(manifest_path))
            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            self.assertIn(keys["u_a"], manifest["months"])
            self.assertTrue(os.path.exists(month_cache.month_cache_path(keys["u_a"])))
            self.assertEqual(month_cache.sweep_orphan_month_cache(), 0)
            self.assertTrue(os.path.exists(month_cache.month_cache_path(keys["u_a"])))

    def test_plan_purge_during_model_call_discards_partial_result(self):
        chat = _rep_chat()
        chat_hash = "synthetic-plan-purged"
        plan = analysis_plan.build_analysis_plan(chat, chat_hash, ["profile"])
        selected = [unit["key"] for unit in plan["units"] if unit["key"].endswith(":self")]
        self.assertEqual(len(selected), 1)
        jid, _, _ = jobs._get_or_create_job(
            "synthetic-purge-sid",
            "plan",
            chat_hash,
            len(selected),
            conflict_dimension="*",
            identity="synthetic-purge-plan",
        )

        def finish_then_purge(*_args, **_kwargs):
            purge_marks.mark(chat_hash)
            return {"self": profile("合成甲")}

        try:
            with (
                mock.patch.object(jobs.os.path, "exists", return_value=True),
                mock.patch.object(store, "_load_chat_cached", return_value=chat),
                mock.patch.object(store, "_read_cache", return_value=None),
                mock.patch.object(store, "append_job_history"),
                mock.patch.object(jobs, "_run_selected_dimension", side_effect=finish_then_purge),
                mock.patch.object(store, "_write_cache") as write_cache,
            ):
                jobs._run_selected_plan(jid, "synthetic-chat.json", chat_hash, False, plan, selected)
            job = jobs.JOBS[jid]
            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(job.get("results"), {})
            self.assertEqual(job.get("partial_results"), {})
            write_cache.assert_not_called()
        finally:
            purge_marks.unmark(chat_hash)

    def test_plan_purge_at_terminal_lock_cannot_restore_partial_results(self):
        chat = _rep_chat()
        chat_hash = "synthetic-terminal-purge-race"
        plan = analysis_plan.build_analysis_plan(chat, chat_hash, ["emotion"])
        selected = ["emotion:month:2025-01"]
        jid, _, _ = jobs._get_or_create_job(
            "synthetic-terminal-purge-sid",
            "plan",
            chat_hash,
            len(selected),
            conflict_dimension="*",
            identity="terminal-purge-race",
        )
        original_lock = threading.Lock()

        class PurgeBeforeTerminalLock:
            def __init__(self):
                self.direct_plan_acquires = 0

            def __enter__(self):
                caller = inspect.currentframe().f_back
                if caller and caller.f_code.co_name == "_run_selected_plan":
                    self.direct_plan_acquires += 1
                    if self.direct_plan_acquires == 2:
                        # The first direct acquisition updates progress; mark the
                        # chat immediately before the terminal result lock instead.
                        purge_marks.mark(chat_hash)
                original_lock.acquire()
                return self

            def __exit__(self, *_exc):
                original_lock.release()

        purge_marks.unmark(chat_hash)
        self.addCleanup(purge_marks.unmark, chat_hash)
        with (
            mock.patch.object(jobs, "JOBS_LOCK", PurgeBeforeTerminalLock()),
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(store, "append_job_history"),
            mock.patch.object(jobs, "_run_selected_dimension", return_value={"2025-01": emotion()}),
        ):
            jobs._run_selected_plan(jid, "synthetic-chat.json", chat_hash, False, plan, selected)

        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["results"], {})
        self.assertEqual(job["partial_results"], {})

    def test_ordinary_job_does_not_store_partial_result_after_chat_purge(self):
        chat = _rep_chat()
        chat_hash = "synthetic-ordinary-job-purged"
        jid, _, _ = jobs._get_or_create_job(
            "synthetic-ordinary-purge-sid", "profile", chat_hash, 1, identity="synthetic-scope"
        )

        def fail_after_purge(*_args, **_kwargs):
            error = AnalysisIncompleteError("synthetic incomplete")
            error.partial_result = {"self": profile("合成甲")}
            purge_marks.mark(chat_hash)
            raise error

        try:
            with (
                mock.patch.object(jobs.os.path, "exists", return_value=True),
                mock.patch.object(store, "_load_chat_cached", return_value=chat),
                mock.patch.object(jobs, "analyze_func_for", return_value=fail_after_purge),
            ):
                jobs._run_job(jid, "profile", "synthetic-chat.json", chat_hash, False)
            job = jobs.JOBS[jid]
            self.assertEqual(job["status"], "error")
            self.assertNotIn("result", job)
            self.assertNotIn("partial_results", job)
        finally:
            purge_marks.unmark(chat_hash)

    def test_chat_purge_clears_only_that_vision_memo_namespace(self):
        from analyzer import vision

        chat_hash = "synthetic-vision-memo-purge"
        other_hash = "synthetic-vision-memo-keep"
        target_key = f"{hashlib.sha256(chat_hash.encode('utf-8')).hexdigest()}:synthetic-image"
        other_key = f"{hashlib.sha256(other_hash.encode('utf-8')).hexdigest()}:synthetic-image"
        purge_marks.unmark(chat_hash)
        vision._MEMO[target_key] = "synthetic target summary"
        vision._MEMO[other_key] = "synthetic other summary"
        try:
            with (
                tempfile.TemporaryDirectory() as ai_cache,
                tempfile.TemporaryDirectory() as stats_cache,
                mock.patch.object(store, "AI_CACHE_DIR", ai_cache),
                mock.patch.object(store, "STATS_CACHE_DIR", stats_cache),
                mock.patch.object(store, "purge_month_cache", return_value=0),
                mock.patch.object(store, "purge_job_history", return_value=0),
                mock.patch.object(vision, "purge_session_media", return_value=0),
            ):
                store._purge_chat_caches(chat_hash)
            self.assertIsNone(vision._memo_get(target_key))
            self.assertEqual(vision._memo_get(other_key), "synthetic other summary")
        finally:
            purge_marks.unmark(chat_hash)
            vision._MEMO.pop(target_key, None)
            vision._MEMO.pop(other_key, None)

    def test_member_failure_registers_successful_cache_and_keeps_partial_result(self):
        chat = _rep_chat(group=True)
        chat_hash = "synthetic-member-manifest"
        plan = analysis_plan.build_analysis_plan(chat, chat_hash, ["member_profiles"])
        selected = [unit["key"] for unit in plan["units"]]
        first_uid = selected[0].rsplit(":", 1)[-1]
        calls = []

        def fake_analyze_member(_chat, member, *_args, used_keys=None, **_kwargs):
            calls.append(member.uid)
            if member.uid == first_uid:
                used_keys.add("synthetic-paid-member-key")
                result = profile(member.name, group=True)
                result.update(uid=member.uid, is_self=member.is_self, total_messages=1)
                return result
            raise AnalysisIncompleteError("synthetic later member failure")

        jid, _, _ = jobs._get_or_create_job(
            "synthetic-member-sid",
            "plan",
            chat_hash,
            len(selected),
            conflict_dimension="*",
            identity="synthetic-member-plan",
        )
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(gc, "_analyze_member", side_effect=fake_analyze_member),
            mock.patch.object(gc, "_record_member_usage") as record_usage,
            mock.patch.object(store, "append_job_history"),
        ):
            jobs._run_selected_plan(jid, "synthetic-chat.json", chat_hash, False, plan, selected)

        record_usage.assert_called_once_with(chat_hash, {"synthetic-paid-member-key"})
        job = jobs.JOBS[jid]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["summary"]["member_profiles"], "error")
        self.assertIn(first_uid, job.get("results", {}).get("member_profiles", {}))
        self.assertIn(first_uid, job.get("partial_results", {}).get("member_profiles", {}))


if __name__ == "__main__":
    unittest.main()
