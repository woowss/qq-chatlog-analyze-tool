# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Regression tests for model-result contracts and cache write boundaries."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer.result_schema import (  # noqa: E402
    ResultValidationError,
    validate_dimension_result,
    validate_result,
)
from parser.qq_parser import Message  # noqa: E402
from result_fixtures import for_dimension  # noqa: E402


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

    def test_existing_valid_cache_is_read_without_revalidation(self):
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
