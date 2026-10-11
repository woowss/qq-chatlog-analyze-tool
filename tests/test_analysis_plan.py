import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import vision  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from result_fixtures import emotion  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
