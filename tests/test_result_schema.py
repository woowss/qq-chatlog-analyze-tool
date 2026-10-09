# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Regression tests for model-result contracts and cache write boundaries."""

import os
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer.cache_policy import content_cache_policy  # noqa: E402
from analyzer.result_schema import (  # noqa: E402
    CachedModelResult,
    ResultValidationError,
    validate_dimension_result,
    validate_result,
)
from parser.qq_parser import ChatData, Message, load_chat  # noqa: E402
from result_fixtures import for_dimension  # noqa: E402
from webapp import jobs  # noqa: E402


class TestResultSchemas(unittest.TestCase):
    def test_all_dimensions_accept_contract_valid_results(self):
        direct = (
            "emotion",
            "topics",
            "relationship",
            "habits",
            "profile",
            "group_dynamics",
            "group_topics",
            "group_emotion",
            "recap",
            "ask",
        )
        for dimension in direct:
            with self.subTest(dimension=dimension):
                validate_result(dimension, for_dimension(dimension))

        aggregates = {
            "emotion": {"2025-01": for_dimension("emotion")},
            "topics": {"2025-01": for_dimension("topics")},
            "relationship": {"2025-01": for_dimension("relationship")},
            "habits": {"self": for_dimension("habits")},
            "profile": {"self": for_dimension("profile")},
            "group_dynamics": {"2025-01": for_dimension("group_dynamics")},
            "group_topics": {"2025-01": for_dimension("group_topics")},
            "group_emotion": {"2025-01": for_dimension("group_emotion")},
            "member_profiles": {"uid-1": for_dimension("member_profiles")},
        }
        for dimension, result in aggregates.items():
            with self.subTest(aggregate=dimension):
                validate_dimension_result(dimension, result)

    def test_missing_field_is_rejected(self):
        result = for_dimension("emotion")
        result.pop("self_evidence")
        with self.assertRaisesRegex(ResultValidationError, "self_evidence"):
            validate_result("emotion", result)

    def test_wrong_type_is_rejected(self):
        result = for_dimension("relationship")
        result["initiator_ratio_self"] = "一半"
        with self.assertRaisesRegex(ResultValidationError, "initiator_ratio_self"):
            validate_result("relationship", result)

    def test_group_member_emotion_accepts_prompt_labels_but_requires_short_text(self):
        for label in ("轻松", "温馨", "低落", "期待"):
            with self.subTest(label=label):
                result = for_dimension("group_emotion")
                result["member_emotions"][0]["emotion"] = label
                validate_result("group_emotion", result)
        for value in (None, [], 1, "字" * 2_001):
            with self.subTest(value_type=type(value).__name__):
                result = for_dimension("group_emotion")
                result["member_emotions"][0]["emotion"] = value
                with self.assertRaises(ResultValidationError):
                    validate_result("group_emotion", result)

    def test_optional_profile_fields_can_be_absent_but_must_be_text_when_present(self):
        for dimension in ("profile", "member_profiles"):
            for key in ("thinking_style", "humor_style"):
                with self.subTest(dimension=dimension, key=key):
                    result = for_dimension(dimension)
                    result["personality_analysis"].pop(key)
                    validate_result(dimension, result)
                    result["personality_analysis"][key] = []
                    with self.assertRaises(ResultValidationError):
                        validate_result(dimension, result)

    def test_cached_results_check_present_fields_and_preserve_missing_fields(self):
        cases = (
            ("profile", {"personality_analysis": {"strengths": "not an array"}}),
            ("profile", {"personality_analysis": None}),
            ("profile", {"personality_analysis": {"strengths": [None]}}),
            ("profile", {"scoring": {"expressiveness": float("nan")}}),
            ("emotion", {"self_emotion": "unknown"}),
            ("emotion", {"self_intensity": 11}),
            ("emotion", {"self_intensity": True}),
            ("emotion", {"self_evidence": "x" * 2_001}),
            ("emotion", {"self_keywords": ["x"] * 17}),
            ("group_topics", {"topics": [{"weight": 2}]}),
            ("group_topics", {"topics": [None]}),
            ("group_emotion", {"member_emotions": [{"intensity": -1}]}),
            ("habits", {"signature_moment": None}),
            ("member_profiles", {"group_specific": {"presence": []}}),
        )
        for dimension, payload in cases:
            with self.subTest(dimension=dimension, payload_key=next(iter(payload))):
                key = (
                    "self"
                    if dimension in ("profile", "habits")
                    else "uid-1"
                    if dimension == "member_profiles"
                    else "2025-01"
                )
                with self.assertRaises(ResultValidationError):
                    validate_dimension_result(dimension, {key: CachedModelResult(payload)})
        partial = {"personality_analysis": {"strengths": ["合成优点"]}}
        before = json.dumps(partial, ensure_ascii=False)
        validate_dimension_result("profile", {"self": CachedModelResult(partial)})
        self.assertEqual(json.dumps(partial, ensure_ascii=False), before)
        with self.assertRaises(ResultValidationError):
            validate_result("profile", partial)

    def test_unknown_enum_is_rejected_without_echoing_model_value(self):
        result = for_dimension("group_emotion")
        result["group_emotion"] = "sk-secret-not-a-label"
        with self.assertRaises(ResultValidationError) as caught:
            validate_result("group_emotion", result)
        self.assertIn("group_emotion", str(caught.exception))
        self.assertNotIn("sk-secret-not-a-label", str(caught.exception))

    def test_overlong_text_and_list_shape_are_rejected(self):
        result = for_dimension("ask")
        result["answer"] = "字" * 2_001
        with self.assertRaisesRegex(ResultValidationError, "answer"):
            validate_result("ask", result)

        result = for_dimension("emotion")
        result["self_keywords"] = ["词"] * 17
        with self.assertRaisesRegex(ResultValidationError, "self_keywords"):
            validate_result("emotion", result)


class TestCacheValidationBoundary(unittest.TestCase):
    @staticmethod
    def _message() -> Message:
        timestamp = int(datetime(2025, 1, 2, 12, tzinfo=timezone.utc).timestamp() * 1000)
        return Message(
            "m1",
            timestamp,
            "2025-01-02 12:00:00",
            "我",
            "self",
            "测试消息",
            "测试消息",
            "text",
            False,
            False,
        )

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp(prefix="qqchatlog-schema-")
        mc.configure_month_cache(self.cache_dir)
        self.addCleanup(mc.configure_month_cache, "")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.cache_dir, ignore_errors=True))

    def _private_chat(self):
        message = self._message()
        other = replace(message, id="m2", sender_uid="other", sender_name="对方")
        return ChatData("测试对话", "我", "对方", "self", "other", [message, other])

    def test_mixed_old_and_new_months_complete_single_and_all_jobs(self):
        old = for_dimension("emotion")
        old.pop("self_evidence")
        key = mc._month_key("sys", "legacy-job-2025-01")
        mc._write_month_cache(key, old)

        def runner(chat, **kwargs):
            return dc._analyze_periods(
                {"2025-01": [], "2025-02": []},
                "sys",
                lambda period, _: "legacy-job-" + period,
                1024,
                tag="emotion",
                chat_hash="schema-mixed",
            )

        with (
            mock.patch.object(jobs.os.path, "exists", return_value=True),
            mock.patch.object(jobs.store, "_load_chat_cached", return_value=self._private_chat()),
            mock.patch.object(jobs.store, "_read_cache", return_value=None),
            mock.patch.object(jobs.store, "_write_cache") as write,
            mock.patch.object(jobs.store, "append_job_history"),
            mock.patch.object(jobs, "dimensions_for_mode", return_value=["emotion"]),
            mock.patch.object(jobs, "analyze_func_for", return_value=runner),
            mock.patch.object(dc, "_call_api", side_effect=lambda *a, **k: for_dimension("emotion")) as call,
            mock.patch.dict(jobs.JOBS, {}, clear=True),
        ):
            for run_all in (False, True):
                jid = "schema-job-" + str(run_all)
                jobs.JOBS[jid] = {"status": "running"}
                if run_all:
                    jobs._run_analyze_all(jid, "synthetic.json", "schema-mixed", False)
                else:
                    jobs._run_job(jid, "emotion", "synthetic.json", "schema-mixed")
                self.assertEqual(jobs.JOBS[jid]["status"], "done")
                if run_all:
                    self.assertEqual(jobs.JOBS[jid]["result"], {"emotion": "done"})
                aggregate = write.call_args.args[2]
                self.assertEqual(set(aggregate), {"2025-01", "2025-02"})
                self.assertNotIn("self_evidence", aggregate["2025-01"])
                self.assertEqual(json.loads(json.dumps(aggregate)), aggregate)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(write.call_count, 2)

    def test_cache_provenance_does_not_exempt_fresh_results_or_json_flags(self):
        old = for_dimension("emotion")
        old.pop("self_evidence")
        key = mc._month_key("sys", "legacy-mixed")
        mc._write_month_cache(key, old)
        cached = mc._read_month_cache(key)
        validate_dimension_result("emotion", {"2025-01": cached})
        with self.assertRaises(ResultValidationError):
            validate_result("emotion", cached)
        old["_from_cache"] = True
        with self.assertRaises(ResultValidationError):
            validate_dimension_result("emotion", {"2025-01": cached, "2025-02": old})

    def test_legacy_person_and_member_caches_remain_aggregate_compatible(self):
        for dimension, aggregate_key, missing in (
            ("habits", "self", "signature_moment"),
            ("profile", "self", "one_line_bio"),
            ("member_profiles", "uid-1", "one_line_bio"),
        ):
            with self.subTest(dimension=dimension):
                old = for_dimension(dimension)
                old.pop(missing)
                key = mc._month_key("sys", dimension)
                mc._write_month_cache(key, old)
                cached = mc._read_month_cache(key)
                validate_dimension_result(dimension, {aggregate_key: cached})
                with self.assertRaises(ResultValidationError):
                    validate_dimension_result(dimension, {aggregate_key: old})

    def test_invalid_forced_refresh_preserves_compatible_old_month(self):
        old = for_dimension("emotion")
        old.pop("self_evidence")
        prompt = "legacy-refresh"
        key = mc._month_key("sys", prompt)
        mc._write_month_cache(key, old)
        with content_cache_policy(True), mock.patch.object(dc, "_call_api", return_value=old):
            with self.assertRaises(dc.AnalysisIncompleteError):
                dc._analyze_periods({"2025-01": []}, "sys", lambda *a: prompt, 1024, tag="emotion")
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("old cache lost")):
            result = dc._analyze_periods({"2025-01": []}, "sys", lambda *a: prompt, 1024, tag="emotion")
        validate_dimension_result("emotion", result)
        self.assertNotIn("self_evidence", result["2025-01"])

    def test_private_invalid_first_person_continues_and_retry_only_calls_failed_person(self):
        for dimension, runner in (("habits", dc.analyze_habits), ("profile", dc.analyze_profile)):
            with self.subTest(dimension=dimension):
                progress = []
                with mock.patch.object(
                    dc, "_call_api", side_effect=[{"name": "无效成员"}, for_dimension(dimension)]
                ) as call:
                    with self.assertRaises(dc.AnalysisIncompleteError):
                        runner(
                            self._private_chat(), on_progress=lambda d, t, seen=progress: seen.append((d, t))
                        )
                self.assertEqual(call.call_count, 2)
                self.assertEqual(progress, [(1, 2), (2, 2)])
                with mock.patch.object(dc, "_call_api", return_value=for_dimension(dimension)) as call:
                    result = runner(self._private_chat())
                self.assertEqual(call.call_count, 1)
                self.assertEqual(set(result), {"self", "other"})
                validate_dimension_result(dimension, result)

    def test_private_all_invalid_reports_incomplete_and_quota_stops_immediately(self):
        for runner in (dc.analyze_habits, dc.analyze_profile):
            with self.subTest(runner=runner.__name__):
                with mock.patch.object(dc, "_call_api", return_value={"name": "无效成员"}) as call:
                    with self.assertRaises(dc.AnalysisIncompleteError):
                        runner(self._private_chat())
                self.assertEqual(call.call_count, 2)
                with mock.patch.object(
                    dc, "_call_api", side_effect=dc.QuotaExhaustedError("配额耗尽")
                ) as call:
                    with self.assertRaises(dc.QuotaExhaustedError):
                        runner(self._private_chat())
                self.assertEqual(call.call_count, 1)

    def test_group_invalid_first_member_continues_and_retry_reuses_other_member(self):
        chat = load_chat(Path(__file__).parent / "fixtures/group_5p.json")
        members = gc.select_ai_members(chat)[:2]
        with mock.patch.object(gc, "select_ai_members", return_value=members):
            with mock.patch.object(
                gc, "_call_api", side_effect=[{"name": "无效成员"}, for_dimension("member_profiles")]
            ) as call:
                with self.assertRaises(dc.AnalysisIncompleteError):
                    gc.analyze_member_profiles(chat, chat_hash="schema-group")
            self.assertEqual(call.call_count, 2)
            with mock.patch.object(gc, "_call_api", return_value=for_dimension("member_profiles")) as call:
                result = gc.analyze_member_profiles(chat, chat_hash="schema-group")
            self.assertEqual(call.call_count, 1)
        self.assertEqual(set(result), {member.uid for member in members})
        validate_dimension_result("member_profiles", result)

    def test_invalid_person_does_not_continue_after_cancellation(self):
        for runner in (dc.analyze_habits, dc.analyze_profile):
            with self.subTest(runner=runner.__name__):
                progress = []
                with mock.patch.object(dc, "_call_api", return_value={"name": "无效成员"}) as call:
                    result = runner(
                        self._private_chat(),
                        on_progress=lambda d, t, seen=progress: seen.append((d, t)),
                        should_cancel=lambda seen=progress: bool(seen),
                    )
                self.assertEqual(call.call_count, 1)
                self.assertEqual(result, {})

    def test_group_invalid_member_still_honors_cancellation(self):
        chat = load_chat(Path(__file__).parent / "fixtures/group_5p.json")
        progress = []
        with mock.patch.object(gc, "_call_api", return_value={"name": "无效成员"}) as call:
            result = gc.analyze_member_profiles(
                chat,
                on_progress=lambda d, t: progress.append((d, t)),
                should_cancel=lambda: bool(progress),
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result, {})

    def test_bad_person_cache_recomputes_only_affected_person(self):
        for dimension, runner, field in (
            ("habits", dc.analyze_habits, "personality_tags"),
            ("profile", dc.analyze_profile, "strengths"),
        ):
            with self.subTest(dimension=dimension):
                with (
                    mock.patch.object(
                        dc, "_call_api", side_effect=lambda *a, dim=dimension, **k: for_dimension(dim)
                    ),
                    mock.patch.object(dc, "_write_month_cache", wraps=mc._write_month_cache) as writes,
                ):
                    original = runner(self._private_chat())
                path = Path(mc.month_cache_path(writes.call_args_list[0].args[0]))
                payload = json.loads(path.read_text(encoding="utf-8"))
                if dimension == "profile":
                    payload["personality_analysis"][field] = "invalid legacy field"
                else:
                    payload[field] = "invalid legacy field"
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(dc, "_call_api", return_value=for_dimension(dimension)) as call:
                    repaired = runner(self._private_chat())
                self.assertEqual(call.call_count, 1)
                self.assertEqual(set(repaired), {"self", "other"})
                self.assertEqual(repaired["other"], original["other"])
                validate_dimension_result(dimension, repaired)

    def test_bad_month_cache_recomputes_and_failed_recompute_stays_retryable(self):
        key = mc._month_key("sys", "bad-old-month")
        invalid = for_dimension("emotion")
        invalid["self_intensity"] = "wrong type"
        mc._write_month_cache(key, invalid)

        def run():
            return dc._analyze_periods(
                {"2025-01": []}, "sys", lambda *a: "bad-old-month", 1024, tag="emotion"
            )

        with mock.patch.object(dc, "_call_api", return_value=invalid) as call:
            with self.assertRaises(dc.AnalysisIncompleteError):
                run()
            self.assertEqual(call.call_count, 1)
        with mock.patch.object(dc, "_call_api", return_value=for_dimension("emotion")) as call:
            repaired = run()
            self.assertEqual(call.call_count, 1)
        validate_dimension_result("emotion", repaired)
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("repaired cache missed")):
            validate_dimension_result("emotion", run())

    def test_bad_member_cache_recomputes_only_affected_member(self):
        chat = load_chat(Path(__file__).parent / "fixtures/group_5p.json")
        members = gc.select_ai_members(chat)[:2]
        with mock.patch.object(gc, "select_ai_members", return_value=members):
            with (
                mock.patch.object(
                    gc, "_call_api", side_effect=lambda *a, **k: for_dimension("member_profiles")
                ),
                mock.patch.object(dc, "_write_month_cache", wraps=mc._write_month_cache) as writes,
            ):
                original = gc.analyze_member_profiles(chat)
            path = Path(mc.month_cache_path(writes.call_args_list[0].args[0]))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["personality_analysis"]["strengths"] = "invalid legacy field"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(gc, "_call_api", return_value=for_dimension("member_profiles")) as call:
                repaired = gc.analyze_member_profiles(chat)
                self.assertEqual(call.call_count, 1)
            self.assertEqual(repaired[members[1].uid], original[members[1].uid])
            validate_dimension_result("member_profiles", repaired)

    def test_valid_legacy_cache_replaces_bad_current_key_without_model_calls(self):
        prompt = "legacy-repair"
        current_key = mc._month_key("sys", prompt, "active")
        old_key = mc._month_key("sys", prompt, "legacy")
        bad = for_dimension("emotion")
        bad["self_intensity"] = "wrong type"
        mc._write_month_cache(current_key, bad)
        old = for_dimension("emotion")
        old.pop("self_evidence")
        mc._write_month_cache(old_key, old)
        with (
            mock.patch.object(dc, "_legacy_chain_for_fingerprint_value", return_value=("legacy",)),
            mock.patch.object(dc, "_call_api", side_effect=AssertionError("usable legacy cache missed")),
        ):
            for _ in range(2):
                result = dc._analyze_periods(
                    {"2025-01": []},
                    "sys",
                    lambda *a: prompt,
                    1024,
                    fingerprint="active",
                    tag="emotion",
                )
                validate_dimension_result("emotion", result)
                self.assertNotIn("self_evidence", result["2025-01"])
        self.assertFalse(Path(mc.month_cache_path(old_key)).exists())
        self.assertEqual(mc._read_month_cache(current_key)["self_intensity"], old["self_intensity"])

    def test_legacy_migration_preserves_valid_current_destination(self):
        current_key = mc._month_key("sys", "valid-current")
        old_key = mc._month_key("sys", "valid-old")
        current = for_dimension("emotion")
        old = for_dimension("emotion")
        old["self_intensity"] = 1
        mc._write_month_cache(current_key, current)
        mc._write_month_cache(old_key, old)
        self.assertFalse(mc.migrate_month_cache(old_key, current_key, dimension="emotion"))
        self.assertEqual(mc._read_month_cache(current_key)["self_intensity"], current["self_intensity"])

    def test_invalid_month_is_not_cached_and_retry_only_needs_that_month(self):
        invalid = {"self_emotion": "平静"}
        with mock.patch.object(dc, "_call_api", return_value=invalid):
            with self.assertRaisesRegex(dc.AnalysisIncompleteError, "结构无效"):
                dc._analyze_periods(
                    {"2025-02": [self._message()]},
                    "sys",
                    lambda _period, _messages: "new-invalid-prompt",
                    max_tokens=1024,
                    tag="emotion",
                    chat_hash="schema-invalid",
                )
        self.assertEqual([], [name for name in os.listdir(self.cache_dir) if name.startswith("month_")])

        with mock.patch.object(dc, "_call_api", return_value=for_dimension("emotion")) as call:
            result = dc._analyze_periods(
                {"2025-02": [self._message()]},
                "sys",
                lambda _period, _messages: "new-invalid-prompt",
                max_tokens=1024,
                tag="emotion",
                chat_hash="schema-invalid",
            )
        self.assertEqual(call.call_count, 1)
        self.assertIn("2025-02", result)
        self.assertTrue([name for name in os.listdir(self.cache_dir) if name.startswith("month_")])

    def test_existing_valid_cache_is_read_without_model_calls(self):
        prompt = "legacy-valid-prompt"
        key = mc._month_key("sys", prompt)
        mc._write_month_cache(key, for_dimension("emotion"))
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("cache miss")):
            result = dc._analyze_periods(
                {"2025-01": []},
                "sys",
                lambda _period, _messages: prompt,
                max_tokens=1024,
                tag="emotion",
                chat_hash="schema-legacy",
            )
        self.assertEqual(result["2025-01"]["self_emotion"], "平静")

    def test_invalid_member_result_is_not_written(self):
        invalid = {"name": "测试成员"}
        with mock.patch.object(dc, "_call_api", return_value=invalid):
            with self.assertRaisesRegex(dc.AnalysisIncompleteError, "成员"):
                dc._analyze_person(
                    "system",
                    10,
                    [self._message()],
                    "测试成员",
                    "{display_name}: {dialog}",
                    1024,
                    tag="habits",
                    chat_hash="schema-member",
                )
        self.assertEqual([], [name for name in os.listdir(self.cache_dir) if name.startswith("month_")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
