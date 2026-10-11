"""Regression checks for the eleven findings, using synthetic chat data only."""

import json
import hashlib
import time
import unittest
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

import app  # noqa: E402
from analyzer import deepseek_client as dc, group_client as gc, local_stats, person_context  # noqa: E402
from analyzer.recap_client import _recap_digest  # noqa: E402
from parser.qq_parser import ChatData  # noqa: E402
from test_analysis_plan import _chat  # noqa: E402
from test_person_context import _message  # noqa: E402
from webapp import analysis_plan, api, jobs, store  # noqa: E402
from webapp.analysis_scope import apply_scope  # noqa: E402
from webapp.share_report import build_share_model  # noqa: E402


class ReviewDataTests(unittest.TestCase):
    def test_nameless_uid_is_redacted_in_group_statistics_and_originals(self):
        anonymous = _message("", "合成匿名昵称", 1, "合成匿名昵称你好", message_id="anonymous")
        chat = ChatData("合成群", "本人", "", "u_self", "", [anonymous], is_group_chat=True)
        stats = {
            "member_activity": [{"uid": "", "name": anonymous.sender_name}],
            "interaction": {"members": [{"uid": "", "name": anonymous.sender_name}]},
        }
        options = {"hide_statistics": False, "hide_original_text": False}
        model, _, _ = build_share_model(chat, stats, options)
        self.assertNotIn(anonymous.sender_name, json.dumps(model, ensure_ascii=False))
        self.assertTrue(model["members"][0]["name"].startswith("成员 "))
        self.assertEqual(model["members"][0]["name"], model["original_messages"][0]["sender"])
        model, _, _ = build_share_model(chat, stats, dict(options, hide_nicknames=False))
        self.assertEqual(model["original_messages"][0]["sender"], anonymous.sender_name)

    def test_private_overview_without_uid_hides_names_and_mentions(self):
        message = _message("", "合成本人", 1, "合成本人叫合成对方", message_id="private-anonymous")
        chat = ChatData("合成对话", "合成本人", "合成对方", "", "", [message])
        model, _, _ = build_share_model(
            chat, {"overview": {}}, {"hide_statistics": False, "hide_original_text": False}
        )
        serialized = json.dumps(model, ensure_ascii=False)
        self.assertNotIn("合成本人", serialized)
        self.assertNotIn("合成对方", serialized)

    def test_scoped_overview_and_recap_recompute_duration(self):
        chat = _chat()
        chat.duration_days = 365
        for scope in (
            {"date_from": "2025-01-01", "date_to": "2025-01-02"},
            {"message_positions": [0, 1]},
            {"allowed_uids": ["u_a"]},
        ):
            with self.subTest(scope=scope):
                scoped, _ = apply_scope(chat, scope)
                expected = (
                    max(message.time_str[:10] for message in scoped.messages),
                    min(message.time_str[:10] for message in scoped.messages),
                )
                from datetime import date

                days = (date.fromisoformat(expected[0]) - date.fromisoformat(expected[1])).days + 1
                overview = local_stats.calc_overview(scoped)
                self.assertEqual(overview["total_days"], days)
                self.assertEqual(overview["days_basis"], "computed")
                self.assertEqual(overview["avg_daily"], round(len(scoped.messages) / days, 1))
                self.assertIn(f"日均 {overview['avg_daily']}", _recap_digest(scoped))
        self.assertEqual(chat.duration_days, 365)
        targeted, _ = apply_scope(chat, {"target_uids": ["u_a"]})
        self.assertEqual(targeted.duration_days, 365)

    def test_fixed_prompt_inputs_do_not_reuse_incorrect_cached_results(self):
        chat = _chat()
        scoped, _ = apply_scope(chat, {"date_from": "2025-01-01", "date_to": "2025-01-02"})
        old_recap_key = dc.scope_cache_fingerprint(scoped, dc.fingerprint_for_dimension("recap"))
        self.assertNotEqual(store.analysis_cache_fingerprint("recap", scoped), old_recap_key)
        self.assertEqual(
            store.analysis_cache_fingerprint("recap", chat), dc.fingerprint_for_dimension("recap")
        )
        with mock.patch.object(person_context, "PERSON_CONTEXT_ENABLED", True):
            for dimension, base in (
                ("profile", dc.fingerprint_for_dimension("profile")),
                ("member_profiles", gc.GROUP_PROMPT_FINGERPRINT),
            ):
                old_key = hashlib.sha256(f"{base}:{dimension}:person-context-v2".encode()).hexdigest()[:12]
                self.assertNotEqual(store.analysis_cache_fingerprint(dimension, chat), old_key)

    def test_private_person_digest_does_not_attribute_neighbor_images(self):
        target = _message("u_a", "甲", 1, "我来整理", message_id="target")
        neighbor = _message("u_b", "乙", 2, "乙的图片", message_id="neighbor")
        neighbor.has_image = True
        target.has_image = True
        with (
            mock.patch.object(person_context, "PERSON_CONTEXT_ENABLED", True),
            mock.patch.object(dc, "_vision_digest", return_value="目标图片摘要") as digest,
        ):
            request = dc._prepare_person_request(
                "system",
                10,
                [target],
                "甲",
                "{display_name}\n{dialog}",
                tag="profile",
                context_messages=[target, neighbor],
                speaker_name_of=lambda message: message.sender_name,
            )
        self.assertEqual([m.id for m in digest.call_args.args[0]], ["target"])
        self.assertIn("乙的图片", request["user_content"])
        self.assertIn("仅供上下文", request["user_content"])

    def test_member_digest_does_not_attribute_neighbor_images(self):
        chat = ChatData(
            "合成群",
            "甲",
            "",
            "u_a",
            "",
            [
                _message("u_a", "甲", 1, "我来整理", message_id="target"),
                _message("u_b", "乙", 2, "乙的图片", message_id="neighbor"),
            ],
            is_group_chat=True,
            mode="group",
        )
        for message in chat.messages:
            message.has_image = True
        member = next(person for person in chat.participants() if person.uid == "u_a")
        with (
            mock.patch.object(person_context, "PERSON_CONTEXT_ENABLED", True),
            mock.patch.object(gc, "_vision_digest", return_value="目标图片摘要") as digest,
        ):
            request = gc._prepare_member_request(
                chat, member, gc._member_prompt_template(), "member_profiles"
            )
        self.assertTrue(digest.call_args.args[0])
        self.assertEqual({m.sender_uid for m in digest.call_args.args[0]}, {member.uid})
        self.assertIn("仅供上下文", request["prompt"])


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
