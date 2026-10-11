"""Synthetic regression checks for the sanitized share-copy pipeline."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

import app  # noqa: E402
from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from webapp import store  # noqa: E402
from webapp.share_report import build_share_model, render_share_html  # noqa: E402


def _message(message_id, local_time, uid, name, text):
    dt = datetime.fromisoformat(local_time).replace(tzinfo=CST)
    return Message(
        id=message_id,
        timestamp=int(dt.timestamp() * 1000),
        time_str=local_time,
        sender_name=name,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


def _group_chat():
    return ChatData(
        "私密群名",
        "同名",
        "",
        "QQ1234567",
        "",
        [
            _message("m1", "2025-06-01 10:01:00", "QQ1234567", "同名", "今天2025-06-01见，账号QQ1234567"),
            _message(
                "m2",
                "2025-06-01 10:03:00",
                "QQ7654321",
                "同名",
                "OriginalUnique phrase <script>alert(1)</script>",
            ),
            _message(
                "m3",
                "2025-06-01 10:05:00",
                "unknownAccount",
                "未知用户",
                "placeholder UID unknownAccount",
            ),
        ],
        is_group_chat=True,
        mode="group",
    )


def _group_stats():
    return {
        "overview": {
            "total_messages": 98765,
            "member_count": 2,
            "total_days": 1,
            "active_days": 1,
            "avg_daily": 98765.0,
            "first_day": "2025-06-01",
            "last_day": "2025-06-01",
        },
        "daily_counts": [{"date": "2025-06-01", "self": 98765, "other": 0}],
        "hourly_dist": [{"hour": 10, "self": 98765, "other": 0}],
        "member_activity": [
            {
                "uid": "QQ1234567",
                "name": "同名",
                "msg_count": 1,
                "share": 0.5,
                "avg_chars": 18,
                "active_days": 1,
            },
            {
                "uid": "QQ7654321",
                "name": "同名",
                "msg_count": 1,
                "share": 0.5,
                "avg_chars": 20,
                "active_days": 1,
            },
        ],
        "interaction": {
            "members": [
                {"uid": "QQ1234567", "name": "同名"},
                {"uid": "QQ7654321", "name": "同名"},
            ],
            "directed": [[0, 2], [1, 0]],
            "explicit_edges": [{"source": 0, "target": 1, "value": 1}],
            "truncated": False,
        },
        "milestones": {"first_day": "2025-06-01", "last_day": "2025-06-01"},
    }


class ShareReportTests(unittest.TestCase):
    def setUp(self):
        self.chat = _group_chat()
        self.stats = _group_stats()

    def test_default_share_copy_omits_names_originals_and_all_statistics(self):
        with app.app.test_request_context("/"):
            html, warnings, options = render_share_html(self.chat, self.stats)
        self.assertTrue(all(options.values()))
        self.assertNotIn("私密群名", html)
        self.assertNotIn("同名", html)
        self.assertNotIn("QQ1234567", html)
        self.assertNotIn("QQ7654321", html)
        self.assertNotIn("unknownAccount", html)
        self.assertNotIn("OriginalUnique", html)
        self.assertNotIn("98765", html)
        self.assertNotIn("2025-06-01", html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("href=", html.lower())
        self.assertIn("AI 自由文本", " ".join(warnings))

    def test_included_originals_use_stable_pseudonyms_and_selected_redactions(self):
        options = {
            "hide_nicknames": True,
            "hide_accounts": True,
            "hide_original_text": False,
            "hide_exact_dates": True,
            "hide_statistics": False,
        }
        with app.app.test_request_context("/"):
            html, _, normalized = render_share_html(self.chat, self.stats, options)
            model, _, _ = build_share_model(self.chat, self.stats, options)
            repeated, _, _ = build_share_model(self.chat, self.stats, options)
        labels = [row["name"] for row in model["members"]]
        self.assertEqual(labels, ["成员 A", "成员 B"])
        self.assertEqual(labels, [row["name"] for row in repeated["members"]])
        self.assertFalse(normalized["hide_statistics"])
        self.assertIn("OriginalUnique", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("成员 A", html)
        self.assertIn("成员 B", html)
        self.assertNotIn("同名", html)
        self.assertNotIn("QQ1234567", html)
        self.assertNotIn("QQ7654321", html)
        self.assertNotIn("2025-06-01", html)
        self.assertIn("2025-06", html)
        self.assertIn("接话矩阵", html)
        self.assertIn("98765", html)

    def test_unchecked_privacy_options_allow_selected_raw_fields(self):
        options = {
            "hide_nicknames": False,
            "hide_accounts": False,
            "hide_original_text": False,
            "hide_exact_dates": False,
            "hide_statistics": True,
        }
        with app.app.test_request_context("/"):
            html, _, _ = render_share_html(self.chat, self.stats, options)
        self.assertIn("同名", html)
        self.assertIn("QQ1234567", html)
        self.assertIn("2025-06-01", html)
        self.assertIn("OriginalUnique", html)
        self.assertNotIn("98765", html)
        self.assertNotIn("接话矩阵", html)

    def test_invalid_options_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "脱敏选项"):
            build_share_model(self.chat, self.stats, {"hide_names": True})
        with self.assertRaisesRegex(ValueError, "布尔值"):
            build_share_model(self.chat, self.stats, {"hide_accounts": "yes"})

    def test_common_date_forms_are_reduced_when_exact_dates_are_hidden(self):
        from webapp.share_report import _redact_dates

        self.assertEqual(
            _redact_dates("2025/06/01, 2025年6月1日, 6月1号, 6/1/2025, 6/1"),
            "2025-06, 2025年6月, 6月, 2025-06, 6月",
        )

    def test_read_only_stats_load_does_not_extend_cache_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.json"
            path.write_text(
                json.dumps({"_v": store.GROUP_STATS_SCHEMA_VERSION, "mode": "group", "overview": {}}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(store, "_stats_path", return_value=str(path)),
                mock.patch.object(store.os, "utime") as touch,
            ):
                loaded = store._load_stats("synthetic", expect_mode="group", read_only=True)
            self.assertIsNotNone(loaded)
            touch.assert_not_called()
            self.assertNotIn("_created", loaded)


if __name__ == "__main__":
    unittest.main()
