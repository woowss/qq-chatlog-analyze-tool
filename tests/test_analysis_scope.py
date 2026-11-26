import tempfile
import unittest
from datetime import datetime
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

import app  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer import local_stats  # noqa: E402
from analyzer.recap_client import _recap_digest  # noqa: E402
from flask import session as flask_session  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from result_fixtures import ask  # noqa: E402
from result_fixtures import emotion  # noqa: E402
from webapp import analysis_plan, api, evidence, jobs, store, views  # noqa: E402
from webapp.analysis_scope import apply_scope, normalize_scope, scope_identity  # noqa: E402
from webapp.message_index import index_for  # noqa: E402


def _message(message_id, uid, name, year, month, day, hour=9, minute=0, *, reply_to_id="", reply_to_uid=""):
    dt = datetime(year, month, day, hour, minute, tzinfo=CST)
    text = f"合成消息 {message_id}"
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
        is_reply=bool(reply_to_id),
        reply_to_id=reply_to_id,
        reply_to_uid=reply_to_uid,
    )


def _chat():
    return ChatData(
        chat_name="合成范围对话",
        self_name="同名",
        other_name="同名",
        self_uid="u_a",
        other_uid="u_b",
        messages=[
            _message("jan-a", "u_a", "同名", 2025, 1, 31, 23, 59),
            _message("feb-b", "u_b", "同名", 2025, 2, 1, 0, 0, reply_to_id="jan-a", reply_to_uid="u_a"),
            _message("feb-a", "u_a", "同名", 2025, 2, 1, 23, 59),
            _message("mar-b", "u_b", "同名", 2025, 2, 2, 0, 0, reply_to_id="feb-a", reply_to_uid="u_a"),
        ],
    )


class AnalysisScopeSelectionTests(unittest.TestCase):
    def test_date_range_is_local_utc8_and_end_date_inclusive(self):
        scoped, normalized = apply_scope(_chat(), {"date_from": "2025-01-31", "date_to": "2025-02-01"})
        self.assertEqual([message.id for message in scoped.messages], ["jan-a", "feb-b", "feb-a"])
        self.assertEqual(normalized["date_to"], "2025-02-01")
        self.assertEqual(scoped.analysis_scope_id, scope_identity(normalized))

    def test_sender_and_selected_message_filters_intersect_and_drop_out_of_scope_reply(self):
        scoped, _ = apply_scope(
            _chat(),
            {
                "allowed_uids": ["u_b"],
                "message_ids": ["feb-b", "mar-b"],
            },
        )
        self.assertEqual([message.id for message in scoped.messages], ["feb-b", "mar-b"])
        self.assertEqual(scoped.messages[0].reply_to_id, "")
        self.assertEqual(scoped.messages[0].reply_to_uid, "")
        self.assertTrue(scoped.messages[1].reply_to_id == "")

    def test_selected_fragments_use_positions_for_duplicate_and_empty_message_ids(self):
        chat = _chat()
        chat.messages[1].id = "reused"
        chat.messages[2].id = "reused"
        chat.messages[3].id = ""
        scoped, normalized = apply_scope(chat, {"message_positions": [2, 3]})
        self.assertEqual([message.text for message in scoped.messages], ["合成消息 feb-a", "合成消息 mar-b"])
        self.assertEqual(normalized["message_positions"], [2, 3])
        # A reply to a duplicate ID is ambiguous, so it cannot retain a misleading link.
        self.assertEqual(scoped.messages[1].reply_to_id, "")
        self.assertEqual(scoped.messages[1].reply_to_uid, "")

    def test_legacy_duplicate_ids_are_rejected_for_reselection(self):
        chat = _chat()
        chat.messages[1].id = "reused"
        chat.messages[2].id = "reused"
        with self.assertRaisesRegex(ValueError, "无法区分重复消息 ID"):
            normalize_scope({"message_ids": ["reused"]}, chat)

    def test_message_positions_are_validated_as_integer_indices(self):
        for positions in ([True], [-1], [len(_chat().messages)], []):
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                normalize_scope({"message_positions": positions}, _chat())

    def test_same_display_name_keeps_uid_selection_unambiguous(self):
        scope = normalize_scope({"target_uids": ["u_b"]}, _chat())
        self.assertEqual(scope["target_uids"], ["u_b"])

    def test_invalid_dates_members_and_empty_fragment_are_rejected(self):
        for scope in (
            {"date_from": "2025-02-02", "date_to": "2025-02-01"},
            {"date_from": "2025-2-01"},
            {"allowed_uids": ["unknown"]},
            {"target_uids": []},
            {"message_ids": []},
            {"message_ids": ["gone"]},
        ):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                normalize_scope(scope, _chat())

    def test_scope_resets_derived_caches_and_changes_analysis_cache_identity(self):
        chat = _chat()
        chat.months()
        chat.participants()
        scoped, _ = apply_scope(chat, {"date_from": "2025-02-01", "date_to": "2025-02-01"})
        self.assertIsNone(scoped._months_cache)
        self.assertIsNone(scoped._participants_cache)
        self.assertEqual(dc.analysis_cache_fingerprint("emotion", chat), dc._unsampled_prompt_fingerprint())
        self.assertNotEqual(
            dc.analysis_cache_fingerprint("emotion", scoped), dc.analysis_cache_fingerprint("emotion", chat)
        )
        targeted, _ = apply_scope(chat, {"target_uids": ["u_b"]})
        self.assertEqual(
            dc.analysis_cache_fingerprint("emotion", targeted), dc.analysis_cache_fingerprint("emotion", chat)
        )
        self.assertNotEqual(
            dc.analysis_cache_fingerprint("profile", targeted), dc.analysis_cache_fingerprint("profile", chat)
        )

    def test_person_target_selection_only_emits_selected_person(self):
        scoped, _ = apply_scope(_chat(), {"target_uids": ["u_b"]})
        with mock.patch.object(
            dc,
            "_analyze_person",
            side_effect=lambda _system, _size, messages, *_args, **_kwargs: (
                {"summary": "synthetic"} if messages else None
            ),
        ) as analyze:
            result = dc.analyze_profile(scoped, chat_hash="scope-test")
        self.assertEqual(set(result), {"other"})
        self.assertEqual(analyze.call_count, 1)

    def test_partial_person_plan_filters_samples_and_uses_scope_cache_identity(self):
        chat = _chat()
        third_party = _message("c-only", "u_c", "第三者", 2025, 2, 1, 12, 0)
        chat.messages.append(third_party)
        scope = {"target_uids": ["u_a", "u_b"]}
        scoped, _ = apply_scope(chat, scope)
        plan = analysis_plan.build_analysis_plan(scoped, "partial-person-scope", ["profile"])
        selected = [unit["key"] for unit in plan["units"] if unit["key"] == "profile:person:other"]
        captured = {}

        def analyze(_system, _size, messages, *_args, **kwargs):
            captured["uids"] = {message.sender_uid for message in messages}
            captured["scope_id"] = kwargs.get("scope_id")
            return {"summary": "synthetic"}

        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(store, "_read_cache", return_value=None),
            mock.patch.object(dc, "_analyze_person", side_effect=analyze),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "partial-person-scope-sid",
                "plan",
                "partial-person-scope",
                len(selected),
                identity="partial-person-scope",
            )
            jobs._run_selected_plan(
                jid, "virtual-chat.json", "partial-person-scope", False, plan, selected, scope
            )

        self.assertEqual(captured["uids"], {"u_b"})
        self.assertEqual(captured["scope_id"], scoped.analysis_scope_id)

    def test_evidence_profile_marks_unselected_sender_as_context_not_target(self):
        chat = _chat()
        third_party = _message("c-neighbor", "u_c", "第三者", 2025, 2, 1, 0, 1)
        chat.messages.insert(2, third_party)
        scoped, _ = apply_scope(chat, {"target_uids": ["u_b"]})
        allowed = evidence._input_lines(scoped, "profile", "other")
        self.assertIn(id(chat.messages[1]), allowed)
        self.assertIn("[目标人物]", allowed[id(chat.messages[1])])
        self.assertIn(id(third_party), allowed)
        self.assertIn("[仅供上下文]", allowed[id(third_party)])

    def test_profile_evidence_can_use_context_sender_and_keeps_original_position(self):
        chat = _chat()
        chat.self_name, chat.other_name = "甲", "乙"
        chat.messages[0].sender_name = "甲"
        chat.messages[1].sender_name = "乙"
        chat.messages[2].sender_name = "甲"
        chat.messages[3].sender_name = "乙"
        chat._participants_cache = None
        # The target's reply context includes an adjacent message from the other
        # participant; citations must be checked against the actual selected input.
        contextual, _ = apply_scope(chat, {"target_uids": ["u_b"], "date_from": "2025-01-31"})
        index = index_for(contextual, api._browse_body, api._match_browse)
        result = {
            "other": {
                "name": "乙",
                "evidence": {"quote": "合成消息 jan-a", "evidence_ids": ["jan-a"]},
            }
        }
        entry = evidence.verify_sources(contextual, "profile", result, index, api._fmt_browse_message)[
            "entries"
        ][0]

        self.assertEqual(entry["status"], "unique")
        self.assertEqual(entry["candidates"][0]["position"], 0)

    def test_scoped_evidence_position_maps_back_to_duplicate_raw_id(self):
        chat = _chat()
        chat.messages[0].id = "reused-id"
        chat.messages[2].id = "reused-id"
        scoped, _ = apply_scope(chat, {"message_positions": [2]})
        index = index_for(scoped, api._browse_body, api._match_browse)
        entry = evidence.verify_sources(
            scoped,
            "emotion",
            {"2025-02": {"evidence": {"quote": "合成消息 feb-a", "evidence_ids": ["reused-id"]}}},
            index,
            api._fmt_browse_message,
        )["entries"][0]

        self.assertEqual(entry["status"], "unique")
        self.assertEqual(entry["candidates"][0]["position"], 2)

    def test_group_profile_targets_use_uids_and_do_not_reselect_unchecked_self(self):
        group = ChatData(
            chat_name="合成群",
            self_name="同名",
            other_name="",
            self_uid="u_a",
            other_uid="",
            is_group_chat=True,
            mode="group",
            messages=[
                _message("g-a", "u_a", "同名", 2025, 1, 1),
                _message("g-b", "u_b", "同名", 2025, 1, 1, 9, 1),
            ],
        )
        scoped, _ = apply_scope(group, {"target_uids": ["u_b"]})
        self.assertEqual([member.uid for member in gc.select_ai_members(scoped)], ["u_b"])


class AnalysisScopePlanTests(unittest.TestCase):
    def test_plan_cache_id_and_month_list_follow_scope(self):
        chat = _chat()
        january, scope = apply_scope(chat, {"date_from": "2025-01-01", "date_to": "2025-01-31"})
        full_plan = analysis_plan.build_analysis_plan(chat, "scope-plan-hash", ["emotion"])
        scoped_plan = analysis_plan.build_analysis_plan(january, "scope-plan-hash", ["emotion"])
        self.assertNotEqual(full_plan["plan_id"], scoped_plan["plan_id"])
        self.assertEqual([unit["label"] for unit in scoped_plan["units"]], ["2025-01"])
        self.assertEqual(scope_identity(scope), january.analysis_scope_id)

    def test_plan_worker_rebuilds_scoped_chat_and_only_calls_selected_month(self):
        chat = _chat()
        scope = {"date_from": "2025-01-01", "date_to": "2025-01-31"}
        scoped, _ = apply_scope(chat, scope)
        plan = analysis_plan.build_analysis_plan(scoped, "scope-worker-hash", ["emotion"])
        selected = [unit["key"] for unit in plan["units"]]
        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(store, "_load_chat_cached", return_value=chat),
            mock.patch.object(dc, "_call_api", return_value=emotion()) as call,
            mock.patch.object(store, "_write_cache"),
            mock.patch.object(store, "append_job_history"),
        ):
            jid, _, _ = jobs._get_or_create_job(
                "scope-worker-sid", "plan", "scope-worker-hash", len(selected), identity="scope-worker"
            )
            jobs._run_selected_plan(
                jid, "virtual-chat.json", "scope-worker-hash", False, plan, selected, scope
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(jobs.JOBS[jid]["status"], "done")


class AnalysisScopeApiTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        self.client = app_module.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
            session["chat_mode"] = "private"
            session["chat_hash"] = "scope-api-hash"
            session["filepath"] = "virtual-scope-chat.json"
        self.chat = _chat()
        self._session_patch = mock.patch.object(
            api, "_session_chat_file", return_value=("virtual-scope-chat.json", None)
        )
        self._load_patch = mock.patch.object(store, "_load_chat_cached", return_value=self.chat)
        self._session_patch.start()
        self._load_patch.start()

    def tearDown(self):
        self._session_patch.stop()
        self._load_patch.stop()
        jobs.JOBS.clear()

    def test_scope_endpoint_updates_session_and_restricts_plan(self):
        saved = self.client.post(
            "/api/analysis-scope",
            json={"scope": {"date_from": "2025-02-01", "date_to": "2025-02-01", "target_uids": ["u_b"]}},
            headers=self.headers,
        )
        self.assertEqual(saved.status_code, 200)
        plan = self.client.get("/api/analysis-plan?dimensions=emotion,profile")
        self.assertEqual(plan.status_code, 200)
        data = plan.get_json()
        self.assertEqual(
            [unit["label"] for unit in data["units"] if unit["dimension"] == "emotion"], ["2025-02"]
        )
        self.assertEqual(
            [unit["key"] for unit in data["units"] if unit["dimension"] == "profile"],
            ["profile:person:other"],
        )
        self.assertIn("含结束日", data["scope_summary"])

    def test_invalid_scope_does_not_replace_existing_scope(self):
        valid = {"date_from": "2025-02-01", "date_to": "2025-02-01"}
        self.assertEqual(
            self.client.post("/api/analysis-scope", json={"scope": valid}, headers=self.headers).status_code,
            200,
        )
        invalid = self.client.post(
            "/api/analysis-scope", json={"scope": {"allowed_uids": ["missing"]}}, headers=self.headers
        )
        self.assertEqual(invalid.status_code, 400)
        current = self.client.get("/api/analysis-scope").get_json()["scope"]
        self.assertEqual(current["date_from"], "2025-02-01")

    def test_ambiguous_legacy_fragment_can_be_replaced_from_scope_ui(self):
        self.chat.messages[1].id = "duplicate"
        self.chat.messages[2].id = "duplicate"
        with self.client.session_transaction() as session:
            session["analysis_scope"] = {"message_ids": ["duplicate"]}
        current = self.client.get("/api/analysis-scope")
        self.assertEqual(current.status_code, 200)
        self.assertIn("无法精确定位", current.get_json()["warning"])
        saved = self.client.post(
            "/api/analysis-scope",
            json={"scope": {"message_positions": [2]}},
            headers=self.headers,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["scope"]["message_positions"], [2])

    def test_message_api_returns_positions_and_context_targets_duplicate_ids_exactly(self):
        self.chat.messages[1].id = "duplicate"
        self.chat.messages[2].id = "duplicate"
        self.chat._message_index = None
        page = self.client.get("/api/messages?per_page=4")
        self.assertEqual([item["position"] for item in page.get_json()["messages"]], [0, 1, 2, 3])
        context = self.client.get("/api/messages?around_pos=2&context=1")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.get_json()["around_position"], 2)
        self.assertEqual(context.get_json()["messages"][2]["position"], 2)
        self.assertEqual(context.get_json()["messages"][2]["time"], self.chat.messages[2].time_str)

    def test_scoped_evidence_context_does_not_return_out_of_scope_neighbors(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"date_from": "2025-02-01", "date_to": "2025-02-01"}},
            headers=self.headers,
        )

        context = self.client.get("/api/messages?around_pos=1&context=1&scope_only=1")

        self.assertEqual(context.status_code, 200)
        messages = context.get_json()["messages"]
        self.assertEqual([item["position"] for item in messages], [1, 2])
        self.assertEqual([item["id"] for item in messages], ["feb-b", "feb-a"])
        self.assertEqual(context.get_json()["around_position"], 1)

    def test_scoped_evidence_context_rejects_a_target_outside_scope(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"message_positions": [2]}},
            headers=self.headers,
        )

        context = self.client.get("/api/messages?around_pos=1&context=1&scope_only=1")

        self.assertEqual(context.status_code, 404)

    def test_normal_message_context_remains_full_with_analysis_scope_active(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"message_positions": [2]}},
            headers=self.headers,
        )

        context = self.client.get("/api/messages?around_pos=2&context=1")

        self.assertEqual(context.status_code, 200)
        self.assertEqual([item["position"] for item in context.get_json()["messages"]], [0, 1, 2, 3])

    def test_scoped_result_lookup_does_not_fall_back_to_full_cache(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"date_from": "2025-02-01", "date_to": "2025-02-01"}},
            headers=self.headers,
        )
        with mock.patch.object(store, "_read_cache", return_value=None) as read_cache:
            response = self.client.get("/api/analysis/emotion")
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(read_cache.call_args.kwargs["fingerprint"], dc._unsampled_prompt_fingerprint())

    def test_ask_uses_the_active_message_range_and_scope_specific_cache_key(self):
        self.client.post(
            "/api/analysis-scope",
            json={"scope": {"date_from": "2025-02-01", "date_to": "2025-02-01"}},
            headers=self.headers,
        )
        with (
            mock.patch.object(api, "is_api_configured", return_value=True),
            mock.patch.object(store, "ask_cache_read", return_value=None) as cache_read,
            mock.patch("analyzer.recap_client.answer_question", return_value=None) as answer,
        ):
            response = self.client.post(
                "/api/ask", json={"question": "范围里说了什么？"}, headers=self.headers
            )
        self.assertEqual(response.status_code, 502)
        sent_chat = answer.call_args.args[0]
        self.assertEqual([message.id for message in sent_chat.messages], ["feb-b", "feb-a"])
        self.assertEqual(answer.call_args.args[1], "范围里说了什么？")
        self.assertNotEqual(cache_read.call_args.args[1], "范围里说了什么？")


def _plan_message(message_id, uid, name, month, day, text, *, has_image=False):
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
        has_image=has_image,
        is_reply=False,
    )


def _plan_chat(*, with_image=False):
    return ChatData(
        chat_name="合成私聊",
        self_name="甲",
        other_name="乙",
        self_uid="u_a",
        other_uid="u_b",
        messages=[
            _plan_message("jan-a", "u_a", "甲", 1, 1, "合成的一月消息", has_image=with_image),
            _plan_message("jan-b", "u_b", "乙", 1, 2, "合成的一月回复"),
            _plan_message("feb-a", "u_a", "甲", 2, 1, "合成的二月消息"),
        ],
    )


class AnalysisScopeDurationFollowupTests(unittest.TestCase):
    def test_scoped_overview_and_recap_recompute_duration(self):
        chat = _plan_chat()
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
        self.chat = _rep_chat()
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
