import unittest
from datetime import datetime
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from webapp import evidence  # noqa: E402
from result_fixtures import emotion  # noqa: E402
from webapp import analysis_plan, api, jobs, store  # noqa: E402
from webapp.message_index import index_for  # noqa: E402
from webapp.analysis_scope import apply_scope, normalize_scope, scope_identity  # noqa: E402


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
        with (
            mock.patch.object(store, "_read_cache", return_value=None) as read_cache,
            mock.patch.object(store, "_read_legacy_person_context_cache") as read_legacy,
        ):
            response = self.client.get("/api/analysis/emotion")
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(read_cache.call_args.kwargs["fingerprint"], dc._unsampled_prompt_fingerprint())
        read_legacy.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
