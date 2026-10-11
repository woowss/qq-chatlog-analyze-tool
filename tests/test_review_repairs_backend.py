"""Regression coverage for the backend fixes from the 2026-10-11 review."""

import hashlib
import inspect
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

import app  # noqa: E402
from flask import session as flask_session  # noqa: E402
from analyzer import deepseek_client as dc, group_client as gc, month_cache, purge_marks  # noqa: E402
from analyzer.deepseek_client import AnalysisIncompleteError, QuotaExhaustedError  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from parser.group_identity import Participant  # noqa: E402
from result_fixtures import ask, emotion, profile  # noqa: E402
from webapp import analysis_plan, api, jobs, store, views  # noqa: E402
from webapp.analysis_scope import apply_scope, scope_identity  # noqa: E402


def _message(message_id, uid, name, month, day, text):
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


def _chat(*, group=False):
    return ChatData(
        chat_name="合成审核对话",
        self_name="合成甲",
        other_name="合成乙",
        self_uid="u_a",
        other_uid="u_b",
        messages=[
            _message("jan-a", "u_a", "合成甲", 1, 1, "合成一月甲消息"),
            _message("jan-b", "u_b", "合成乙", 1, 2, "合成一月乙消息"),
            _message("feb-a", "u_a", "合成甲", 2, 1, "合成二月甲消息"),
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
            _chat(),
            "emotion",
            ["emotion:month:2025-01"],
            run_selected=mock.Mock(return_value={"2025-01": {"nonsense": "synthetic malformed"}}),
        )

        self.assertEqual(job["status"], "error")
        self.assertEqual(job["summary"]["emotion"], "error")
        self.assertNotIn("nonsense", str(job.get("results", {})))

    def test_fresh_month_result_is_validated_when_month_cache_is_disabled(self):
        chat = _chat()
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
        chat = _chat()
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
        chat = _chat(group=True)
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
        chat = _chat(group=True)
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
        chat = _chat(group=True)
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
        chat = _chat()
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
        chat = _chat()
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
        chat = _chat()
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
        chat = _chat(group=True)
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


class BackendIdentityAndAskCacheTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.sid = session.sid
            self.headers = {
                "Origin": "http://localhost:5000",
                "X-CSRF-Token": session["csrf_token"],
            }
            session.update(
                chat_mode="private",
                chat_hash="synthetic-backend-identity",
                filepath="synthetic-chat.json",
            )
        self.chat = _chat()
        self._patchers = [
            mock.patch.object(api, "_session_chat_file", return_value=("synthetic-chat.json", None)),
            mock.patch.object(store, "_load_chat_cached", return_value=self.chat),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(jobs.JOBS.clear)

    def _set_scope(self, scope):
        response = self.client.post("/api/analysis-scope", json={"scope": scope}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body.get("chat_hash"), "synthetic-backend-identity")
        return body

    def test_scope_and_plan_endpoints_return_current_chat_identity(self):
        scope_response = self.client.get("/api/analysis-scope").get_json()
        self.assertEqual(scope_response.get("chat_hash"), "synthetic-backend-identity")
        plan_response = self.client.get("/api/analysis-plan?dimensions=emotion").get_json()
        self.assertEqual(plan_response.get("chat_hash"), "synthetic-backend-identity")
        self.assertEqual(plan_response["scope_id"], scope_response["scope_id"])

    def test_rendered_page_identity_uses_canonical_session_scope_without_chat_parse(self):
        scoped = self._set_scope({"date_from": "2025-01-01", "date_to": "2025-01-31"})
        with app.app.test_request_context("/"):
            flask_session["chat_hash"] = "synthetic-backend-identity"
            flask_session["analysis_scope"] = scoped["scope"]
            with mock.patch.object(views, "is_api_configured", return_value=True):
                identity = views.inject_stats_flag().get("analysis_page_identity")
                self.assertEqual(
                    identity,
                    {"chat_hash": "synthetic-backend-identity", "scope_id": scoped["scope_id"]},
                )

                flask_session["overview"] = {}
                compatibility_identity = views.inject_stats_flag().get("analysis_page_identity")
                self.assertEqual(compatibility_identity, identity)

                flask_session["analysis_scope"] = None
                self.assertEqual(
                    views.inject_stats_flag().get("analysis_page_identity"),
                    {"chat_hash": "synthetic-backend-identity", "scope_id": ""},
                )

                flask_session["analysis_scope"] = {"date_from": "2025-01-01"}
                self.assertEqual(
                    views.inject_stats_flag().get("analysis_page_identity"),
                    {
                        "chat_hash": "synthetic-backend-identity",
                        "scope_id": scope_identity(
                            {
                                "date_from": "2025-01-01",
                                "date_to": "",
                                "target_uids": None,
                                "allowed_uids": None,
                                "message_positions": None,
                            }
                        ),
                    },
                )

                flask_session["analysis_scope"] = {
                    **scoped["scope"],
                    "date_from": "not-a-date",
                }
                self.assertIsNone(views.inject_stats_flag().get("analysis_page_identity"))

    def test_plan_start_and_poll_keep_the_scope_identity_created_with_job(self):
        scoped = self._set_scope({"date_from": "2025-01-01", "date_to": "2025-01-31"})
        plan = self.client.get("/api/analysis-plan?dimensions=emotion").get_json()
        selected = [unit["key"] for unit in plan["units"][:1]]
        with (
            mock.patch.object(api, "is_api_configured", return_value=True),
            mock.patch.object(api.threading, "Thread"),
        ):
            started = self.client.post(
                "/api/analyze-plan",
                json={
                    "plan_id": plan["plan_id"],
                    "dimensions": ["emotion"],
                    "selected_units": selected,
                },
                headers=self.headers,
            )
        self.assertEqual(started.status_code, 200)
        start_body = started.get_json()
        self.assertEqual(start_body.get("chat_hash"), "synthetic-backend-identity")
        self.assertEqual(start_body.get("scope_id"), scoped["scope_id"])

        new_scope = self._set_scope({"date_from": "2025-02-01", "date_to": "2025-02-28"})
        polled = self.client.get(f"/api/analyze-job/{start_body['job']}").get_json()
        self.assertEqual(polled["chat_hash"], "synthetic-backend-identity")
        self.assertEqual(polled["scope_id"], scoped["scope_id"])
        self.assertNotEqual(polled["scope_id"], new_scope["scope_id"])
        job = jobs.JOBS[start_body["job"]]
        for status in ("done", "error", "cancelled"):
            job.update(
                status=status,
                result={"emotion": {}},
                results={"emotion": {}},
                summary={"emotion": status},
                partial_results={},
                error="synthetic job result",
            )
            polled = self.client.get(f"/api/analyze-job/{start_body['job']}").get_json()
            self.assertEqual(polled["chat_hash"], "synthetic-backend-identity")
            self.assertEqual(polled["scope_id"], scoped["scope_id"])

    def test_cached_analysis_response_has_true_chat_and_scope_identity(self):
        scoped = self._set_scope({"message_positions": [1]})
        with mock.patch.object(store, "_read_cache", return_value={"2025-01": emotion()}):
            response = self.client.get("/api/analysis/emotion")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body.get("chat_hash"), "synthetic-backend-identity")
        self.assertEqual(body.get("scope_id"), scoped["scope_id"])

    def test_analysis_404_and_immediate_analyze_cache_hit_return_identity(self):
        with mock.patch.object(store, "_read_cache", return_value=None):
            missing = self.client.get("/api/analysis/emotion")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json().get("chat_hash"), "synthetic-backend-identity")
        self.assertEqual(missing.get_json().get("scope_id"), "")

        with (
            mock.patch.object(api, "is_api_configured", return_value=True),
            mock.patch.object(store, "_read_cache", return_value={"2025-01": emotion()}),
        ):
            immediate = self.client.post("/api/analyze/emotion", headers=self.headers)
        self.assertEqual(immediate.status_code, 200)
        self.assertTrue(immediate.get_json().get("cached"))
        self.assertEqual(immediate.get_json().get("chat_hash"), "synthetic-backend-identity")
        self.assertEqual(immediate.get_json().get("scope_id"), "")

    def test_target_only_scope_reuses_full_chat_ask_cache(self):
        question = "这段对话主要聊了什么？"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(store, "AI_CACHE_DIR", directory):
                store.ask_cache_write("synthetic-backend-identity", question, ask())
                scoped = self._set_scope({"target_uids": ["u_b"]})
                with (
                    mock.patch.object(api, "is_api_configured", return_value=True),
                    mock.patch(
                        "analyzer.recap_client.answer_question",
                        return_value={"answer": "合成答案", "confidence": "medium", "evidence": []},
                    ) as answer,
                ):
                    response = self.client.post("/api/ask", json={"question": question}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertTrue(body.get("cached", False))
                self.assertEqual(body["chat_hash"], "synthetic-backend-identity")
                self.assertEqual(body["scope_id"], scoped["scope_id"])
                answer.assert_not_called()

    def test_target_only_scope_shares_ask_inflight_identity_with_full_chat(self):
        question = "这段对话主要聊了什么？"
        full_chat, _ = apply_scope(self.chat, {})
        target_chat, _ = apply_scope(self.chat, {"target_uids": ["u_b"]})
        key_builder = getattr(api, "_ask_cache_question", None)
        self.assertIsNotNone(key_builder)
        full_key = key_builder(question, full_chat)
        target_key = key_builder(question, target_chat)
        self.assertEqual(full_key, target_key)

        api._ASK_IN_FLIGHT.clear()
        try:
            with app.app.test_request_context("/"):
                token, busy = api._ask_acquire("synthetic-backend-identity", full_key)
                self.assertIsNone(busy)
                duplicate_token, busy = api._ask_acquire("synthetic-backend-identity", target_key)
                self.assertIsNone(duplicate_token)
                self.assertIsNotNone(busy)
                api._ask_release("synthetic-backend-identity", full_key, token)
        finally:
            api._ASK_IN_FLIGHT.clear()

    def test_range_ask_does_not_hit_old_duration_semantics_cache(self):
        question = "这段对话主要聊了什么？"
        scoped = self._set_scope({"date_from": "2025-02-01", "date_to": "2025-02-28"})
        old_cache_question = f"{question}\0{scoped['scope_id']}"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(store, "AI_CACHE_DIR", directory):
                store.ask_cache_write("synthetic-backend-identity", old_cache_question, ask())
                with (
                    mock.patch.object(api, "is_api_configured", return_value=True),
                    mock.patch("analyzer.recap_client.answer_question", return_value=ask()) as answer,
                ):
                    response = self.client.post("/api/ask", json={"question": question}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json().get("cached", False))
                self.assertEqual(response.get_json()["chat_hash"], "synthetic-backend-identity")
                self.assertEqual(response.get_json()["scope_id"], scoped["scope_id"])
                answer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
