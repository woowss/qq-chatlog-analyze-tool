import unittest
from datetime import datetime
import json
import tempfile
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer import person_context  # noqa: E402
from analyzer.dialog import _message_line  # noqa: E402
from parser.group_identity import Participant  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from result_fixtures import profile as valid_profile  # noqa: E402
from webapp.evidence import _input_lines  # noqa: E402


def _message(uid, name, minute, text, *, message_id, reply_to_id=""):
    timestamp = int(datetime(2025, 1, 1, 9, minute, tzinfo=CST).timestamp() * 1000)
    return Message(
        id=message_id,
        timestamp=timestamp,
        time_str=datetime.fromtimestamp(timestamp / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="text",
        has_image=False,
        is_reply=bool(reply_to_id),
        reply_to_id=reply_to_id,
        reply_to_uid="u_other" if reply_to_id else "",
    )


def _chat(messages):
    return ChatData(
        chat_name="合成群",
        self_name="目标",
        other_name="对话者",
        self_uid="u_target",
        other_uid="",
        messages=messages,
        is_group_chat=True,
        mode="group",
        chat_type="group",
    )


class PersonContextSelectionTests(unittest.TestCase):
    def setUp(self):
        self.before = _message("u_other", "对话者", 0, "前面先问了路线", message_id="before")
        self.target = _message(
            "u_target", "目标", 1, "我周五可以核对", message_id="target", reply_to_id="before"
        )
        self.after = _message("u_other", "对话者", 2, "那我整理其他部分", message_id="after")
        self.messages = [self.before, self.target, self.after]
        self.names = {"u_target": "目标", "u_other": "对话者"}
        self.name_of = lambda message: self.names[message.sender_uid]

    def test_explicit_reply_and_nearby_messages_are_labeled_and_sorted(self):
        selection = person_context.select_person_context(
            self.messages, [self.target], self.name_of, 10, 10_000
        )
        self.assertEqual([entry.message.id for entry in selection.entries], ["before", "target", "after"])
        self.assertIn("[目标人物]", selection.text)
        self.assertEqual(selection.text.count("[仅供上下文]"), 2)
        self.assertEqual(selection.target_sample_count, 1)
        self.assertEqual(selection.context_count, 2)

    def test_explicit_reply_is_kept_before_neighbor_when_budget_is_tight(self):
        target_line = f"[目标人物] {_message_line(self.target, '目标')}"
        reply_line = f"[仅供上下文] {_message_line(self.before, '对话者')}"
        budget = len(target_line) + len(reply_line) + 2
        selection = person_context.select_person_context(
            self.messages, [self.target], self.name_of, 10, budget
        )
        self.assertEqual(selection.target_sample_count, 1)
        self.assertEqual(selection.context_count, 1)
        self.assertIn("before", [entry.message.id for entry in selection.entries])
        self.assertNotIn("after", [entry.message.id for entry in selection.entries])
        self.assertLessEqual(sum(len(entry.line) + 1 for entry in selection.entries), budget)

    def test_target_messages_keep_priority_under_tiny_budget(self):
        target_line = f"[目标人物] {_message_line(self.target, '目标')}"
        selection = person_context.select_person_context(
            self.messages,
            [self.target],
            self.name_of,
            10,
            len(target_line) + 1,
        )
        self.assertEqual(selection.target_sample_count, 1)
        self.assertEqual(selection.context_count, 0)
        self.assertLessEqual(sum(len(entry.line) + 1 for entry in selection.entries), len(target_line) + 1)

    def test_stratified_sampling_uses_full_budget_when_target_count_just_exceeds_limit(self):
        targets = [
            _message("u_target", "目标", 0, f"消息 {index}", message_id=f"target-{index}")
            for index in range(801)
        ]
        for index, message in enumerate(targets):
            message.timestamp += index * 1000

        selection = person_context.select_person_context(
            targets, targets, self.name_of, 800, 100_000, stratified=True
        )

        self.assertEqual(selection.target_sample_count, 800)
        self.assertEqual(len(selection.target_messages), 800)
        self.assertEqual(selection.target_messages[0].id, "target-0")
        self.assertEqual(selection.target_messages[-1].id, "target-800")

    def test_unknown_reply_falls_back_to_nearby_context_and_session_gap_blocks_neighbors(self):
        self.target.reply_to_id = "missing"
        selection = person_context.select_person_context(
            self.messages, [self.target], self.name_of, 10, 10_000
        )
        self.assertEqual(selection.context_count, 2)

        far = _message("u_other", "对话者", 40, "隔了很久", message_id="far")
        gap_selection = person_context.select_person_context(
            [self.target, far], [self.target], self.name_of, 10, 10_000
        )
        self.assertEqual(gap_selection.context_count, 0)

    def test_legacy_mode_matches_target_only_lines(self):
        selection = person_context.select_person_context(
            self.messages,
            [self.target],
            self.name_of,
            10,
            10_000,
            enabled=False,
        )
        self.assertEqual(len(selection.entries), 1)
        self.assertEqual(selection.entries[0].line, _message_line(self.target, "目标"))
        self.assertNotIn("[仅供上下文]", selection.text)

    def test_evidence_reconstructs_the_same_selected_message_lines(self):
        chat = _chat(self.messages)
        allowed = _input_lines(chat, "member_profiles", "u_target")
        selected = {message.id for message in self.messages if id(message) in allowed}
        self.assertEqual(selected, {"before", "target", "after"})
        legacy_allowed = _input_lines(chat, "member_profiles", "u_target", person_context_enabled=False)
        legacy_selected = {message.id for message in self.messages if id(message) in legacy_allowed}
        self.assertEqual(legacy_selected, {"target"})

    def test_only_profile_dimension_fingerprints_change_and_legacy_mode_rolls_back(self):
        chat = _chat(self.messages)
        self.assertNotEqual(dc.analysis_cache_fingerprint("profile", chat), dc.PROMPT_FINGERPRINT)
        self.assertEqual(dc.analysis_cache_fingerprint("emotion", chat), dc.PROMPT_FINGERPRINT)
        self.assertNotEqual(
            dc.analysis_cache_fingerprint("member_profiles", chat), gc.GROUP_PROMPT_FINGERPRINT
        )
        with mock.patch.object(person_context, "PERSON_CONTEXT_ENABLED", False):
            self.assertEqual(dc.analysis_cache_fingerprint("profile", chat), dc.PROMPT_FINGERPRINT)
            self.assertEqual(
                dc.analysis_cache_fingerprint("member_profiles", chat), gc.GROUP_PROMPT_FINGERPRINT
            )


class PersonContextClientIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.before = _message("u_other", "对话者", 0, "你能先看一下目录吗", message_id="before")
        self.target = _message(
            "u_target", "目标", 1, "我来检查第一页", message_id="target", reply_to_id="before"
        )
        self.after = _message("u_other", "对话者", 2, "谢谢，其他页我处理", message_id="after")
        self.messages = [self.before, self.target, self.after]
        self.chat = _chat(self.messages)

    def test_private_profile_prompt_includes_context_but_counts_target_only(self):
        captured = []

        def call(_system, user_content, **_kwargs):
            captured.append(user_content)
            return valid_profile("目标")

        with (
            mock.patch.object(dc, "_call_api", side_effect=call),
            mock.patch.object(dc, "_read_result_cache", return_value=None),
            mock.patch.object(dc, "_write_month_cache"),
        ):
            result = dc._analyze_person(
                "sys",
                800,
                [self.target],
                "目标",
                "{dialog}",
                1000,
                tag="profile",
                stratified=True,
                context_messages=self.messages,
                speaker_name_of=lambda message: "目标" if message.sender_uid == "u_target" else "对话者",
            )
        self.assertIsNotNone(result)
        prompt = captured[0]
        self.assertIn("共发言 1 条", prompt)
        self.assertIn("[目标人物]", prompt)
        self.assertIn("[仅供上下文]", prompt)
        self.assertLess(prompt.index("你能先看一下目录吗"), prompt.index("我来检查第一页"))

    def test_group_member_profile_uses_the_same_context_selector(self):
        captured = []
        member = Participant(uid="u_target", name="目标", is_self=True)

        def call(_system, user_content, **_kwargs):
            captured.append(user_content)
            return valid_profile("目标", group=True)

        with (
            mock.patch.object(gc, "_call_api", side_effect=call),
            mock.patch.object(dc, "_read_result_cache", return_value=None),
            mock.patch.object(dc, "_write_month_cache"),
        ):
            result = gc._analyze_member(
                self.chat,
                member,
                "sys",
                "{dialog}",
                1000,
                "member_profiles",
            )
        self.assertIsNotNone(result)
        prompt = captured[0]
        self.assertIn("共发言 1 条", prompt)
        self.assertIn("[目标人物]", prompt)
        self.assertIn("[仅供上下文]", prompt)
        self.assertLess(prompt.index("你能先看一下目录吗"), prompt.index("我来检查第一页"))


class LegacyPersonContextCacheTests(unittest.TestCase):
    def test_old_result_is_visible_and_evidence_uses_legacy_selection_without_promotion(self):
        import app as app_module
        from webapp import api, store

        before = _message("u_other", "对话者", 0, "先看目录", message_id="before")
        target = _message("u_target", "目标", 1, "我看第一页", message_id="target", reply_to_id="before")
        chat = _chat([before, target])
        payload = {"self": {"name": "目标", "verdict": "旧策略样本"}}

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(store, "AI_CACHE_DIR", directory):
                legacy_path = Path(
                    store._cache_path(
                        "profile",
                        "synthetic-chat-hash",
                        fingerprint=dc.PROMPT_FINGERPRINT,
                    )
                )
                legacy_path.write_text(
                    json.dumps({"_created": 1.0, "result": payload}, ensure_ascii=False),
                    encoding="utf-8",
                )
                app_module.app.config["TESTING"] = True
                client = app_module.app.test_client()
                client.get("/")
                with client.session_transaction() as session:
                    session["chat_mode"] = "private"
                    session["chat_hash"] = "synthetic-chat-hash"
                with (
                    mock.patch.object(api, "_session_chat_file", return_value=("synthetic.json", None)),
                    mock.patch.object(store, "_load_chat_cached", return_value=chat),
                ):
                    result_response = client.get("/api/analysis/profile")
                    evidence_response = client.get("/api/evidence/profile")

                new_fp = store.analysis_cache_fingerprint("profile", chat)
                new_path = Path(store._cache_path("profile", "synthetic-chat-hash", fingerprint=new_fp))
                self.assertEqual(result_response.status_code, 200)
                self.assertTrue(result_response.get_json()["stale_strategy"])
                self.assertEqual(result_response.get_json()["result"], payload)
                self.assertEqual(evidence_response.status_code, 200)
                self.assertTrue(evidence_response.get_json()["stale_strategy"])
                self.assertTrue(legacy_path.exists())
                self.assertFalse(new_path.exists())

    def test_context_free_cache_does_not_match_new_strategy_key(self):
        from webapp import store

        chat = _chat([_message("u_target", "目标", 1, "合成消息", message_id="target")])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(store, "AI_CACHE_DIR", directory):
                old_path = Path(
                    store._cache_path(
                        "profile",
                        "synthetic-chat-hash",
                        fingerprint=dc.PROMPT_FINGERPRINT,
                    )
                )
                old_path.write_text(json.dumps({"_created": 1.0, "result": {"old": True}}), encoding="utf-8")
                current_fp = store.analysis_cache_fingerprint("profile", chat)
                self.assertIsNone(store._read_cache("profile", "synthetic-chat-hash", fingerprint=current_fp))
                self.assertEqual(
                    store._read_legacy_person_context_cache("profile", "synthetic-chat-hash"),
                    {"old": True},
                )
                with mock.patch.object(person_context, "PERSON_CONTEXT_ENABLED", False):
                    self.assertEqual(
                        store._read_cache(
                            "profile",
                            "synthetic-chat-hash",
                            fingerprint=store.analysis_cache_fingerprint("profile", chat),
                        ),
                        {"old": True},
                    )


if __name__ == "__main__":
    unittest.main()
