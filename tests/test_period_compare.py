"""Synthetic regression coverage for local date-range comparisons."""

import unittest
from datetime import datetime

from analyzer.period_compare import compare_periods
from parser.qq_parser import CST, ChatData, Message


def message(message_id, local_time, uid, text="hi", name="成员"):
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


def chat(messages, group=False):
    return ChatData(
        chat_name="编造记录",
        self_name="我",
        other_name="成员",
        self_uid="self",
        other_uid="other",
        messages=messages,
        is_group_chat=group,
        mode="group" if group else "private",
    )


class PeriodCompareTests(unittest.TestCase):
    def test_private_ranges_are_inclusive_and_outside_replies_do_not_count(self):
        data = chat(
            [
                message("before", "2025-01-01 23:55:00", "self"),
                message("a1", "2025-01-02 00:05:00", "other", "one"),
                message("a2", "2025-01-02 00:08:00", "self", "two"),
                message("a3", "2025-01-02 23:55:00", "self", "late"),
                message("after", "2025-01-03 00:10:00", "other"),
                message("b1", "2025-01-04 12:00:00", "self", "later"),
            ]
        )
        result = compare_periods(
            data,
            {"from": "2025-01-02", "to": "2025-01-02"},
            {"from": "2025-01-04", "to": "2025-01-04"},
        )
        a, b = result["a"]["metrics"], result["b"]["metrics"]
        self.assertEqual(a["total_messages"], 3)
        self.assertEqual(result["a"]["calendar_days"], 1)
        self.assertEqual(a["session_starts"], 2)
        self.assertEqual(a["reply_median_seconds"]["self"], {"value": 180.0, "samples": 1})
        self.assertEqual(a["reply_median_seconds"]["other"]["samples"], 0)
        self.assertEqual(b["total_messages"], 1)
        self.assertTrue(result["a"]["boundary_truncated"]["before"])
        self.assertTrue(result["a"]["boundary_truncated"]["after"])

    def test_zero_baseline_has_no_infinite_percentage_and_empty_ranges_are_valid(self):
        data = chat([message("only", "2025-02-02 10:00:00", "self")])
        result = compare_periods(
            data,
            {"from": "2025-02-01", "to": "2025-02-01"},
            {"from": "2025-02-02", "to": "2025-02-03"},
        )
        self.assertEqual(result["a"]["metrics"]["total_messages"], 0)
        self.assertEqual(result["b"]["metrics"]["avg_daily"], 0.5)
        self.assertIsNone(result["deltas"]["total_messages"]["percent"])
        self.assertIsNone(result["a"]["metrics"]["avg_message_chars"])

    def test_equal_timestamps_preserve_source_order_for_reply_samples(self):
        same_time = "2025-02-10 10:00:00"
        data = chat(
            [
                message("z-other", same_time, "other"),
                message("a-self", same_time, "self"),
                message("m-other", "2025-02-10 10:05:00", "other"),
            ]
        )
        result = compare_periods(
            data,
            {"from": "2025-02-10", "to": "2025-02-10"},
            {"from": "2025-02-11", "to": "2025-02-11"},
        )
        self.assertEqual(
            result["a"]["metrics"]["reply_median_seconds"]["other"], {"value": 300.0, "samples": 1}
        )

    def test_out_of_range_neighbor_only_changes_boundary_warning(self):
        selected = [
            message("in-a", "2025-06-02 00:05:00", "self"),
            message("in-b", "2025-06-02 00:08:00", "other"),
        ]
        with_neighbor = chat([message("out", "2025-06-01 23:55:00", "other"), *selected])
        without_neighbor = chat(selected)
        a = {"from": "2025-06-02", "to": "2025-06-02"}
        b = {"from": "2025-06-03", "to": "2025-06-03"}
        with_result = compare_periods(with_neighbor, a, b)["a"]
        without_result = compare_periods(without_neighbor, a, b)["a"]
        self.assertEqual(with_result["metrics"]["session_starts"], 1)
        self.assertEqual(
            with_result["metrics"]["session_starts"], without_result["metrics"]["session_starts"]
        )
        self.assertTrue(with_result["boundary_truncated"]["before"])
        self.assertFalse(without_result["boundary_truncated"]["before"])

    def test_bad_date_order_is_rejected(self):
        data = chat([])
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            compare_periods(
                data,
                {"from": "2025-03-02", "to": "2025-03-01"},
                {"from": "2025-03-03", "to": "2025-03-03"},
            )

    def test_group_members_are_aligned_by_uid_and_hours_use_local_time(self):
        data = chat(
            [
                message("a1", "2025-04-01 23:59:00", "uid-1", name="同名"),
                message("a2", "2025-04-01 23:59:30", "uid-2", name="同名"),
                message("b1", "2025-04-02 00:01:00", "uid-2", name="同名"),
                message("b2", "2025-04-02 08:00:00", "uid-2", name="同名"),
            ],
            group=True,
        )
        result = compare_periods(
            data,
            {"from": "2025-04-01", "to": "2025-04-01"},
            {"from": "2025-04-02", "to": "2025-04-02"},
        )
        a, b = result["a"]["metrics"], result["b"]["metrics"]
        self.assertEqual(a["active_members"], 2)
        self.assertEqual(b["active_members"], 1)
        self.assertEqual(a["hour_counts"][23], 2)
        self.assertEqual(b["hour_counts"][0], 1)
        self.assertEqual(b["hour_counts"][8], 1)
        changes = {item["uid"]: item["percentage_points"] for item in result["deltas"]["member_shares"]}
        self.assertEqual(changes["uid-1"], -50.0)
        self.assertEqual(changes["uid-2"], 50.0)

    def test_group_comparison_includes_members_beyond_top_ten(self):
        messages = []
        for index in range(12):
            uid = f"uid-{index}"
            name = f"成员{index}"
            messages.append(message(f"a-{index}", "2025-05-01 09:00:00", uid, name=name))
            messages.append(message(f"b-{index}", "2025-05-02 09:00:00", uid, name=name))
        result = compare_periods(
            chat(messages, group=True),
            {"from": "2025-05-01", "to": "2025-05-01"},
            {"from": "2025-05-02", "to": "2025-05-02"},
        )
        self.assertEqual(len(result["a"]["metrics"]["member_shares"]), 12)
        self.assertEqual(len(result["b"]["metrics"]["member_shares"]), 12)
        self.assertEqual(len(result["deltas"]["member_shares"]), 12)


if __name__ == "__main__":
    unittest.main()
