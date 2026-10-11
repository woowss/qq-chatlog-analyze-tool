"""Synthetic regressions for indexed keyword timeline summaries."""

import unittest
from datetime import datetime

from _bootstrap import bootstrap

bootstrap()

from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from webapp.api import _browse_body, _match_browse  # noqa: E402
from webapp.message_index import MAX_TIMELINE_QUERIES, MessageIndex  # noqa: E402


def message(message_id, local_time, text, sender="uid-1", name="小甲", **extra):
    dt = datetime.fromisoformat(local_time).replace(tzinfo=CST)
    values = {
        "id": message_id,
        "timestamp": int(dt.timestamp() * 1000),
        "time_str": local_time,
        "sender_name": name,
        "sender_uid": sender,
        "text": text,
        "raw_text": text,
        "msg_type": "type_1",
        "has_image": False,
        "is_reply": False,
    }
    values.update(extra)
    return Message(**values)


class KeywordTimelineTests(unittest.TestCase):
    def setUp(self):
        self.chat = ChatData(
            "编造记录",
            "我",
            "小甲",
            "self",
            "uid-1",
            [
                message("m1", "2025-01-01 09:00:00", "词词在这里", name="阿词"),
                message("m2", "2025-01-01 09:10:00", "词语"),
                message(
                    "m3",
                    "2025-01-01 10:00:00",
                    "",
                    sender="uid-2",
                    name="小乙",
                    media_kind="file",
                    media_label="词文件.csv",
                ),
                message(
                    "m4", "2025-01-02 10:00:00", "普通", sender="uid-2", name="词用户", face_names=["词贴图"]
                ),
            ],
        )
        self.index = MessageIndex(self.chat, _browse_body, _match_browse)

    def test_counts_match_search_messages_and_literal_sources_are_separate(self):
        result = self.index.timeline("词")
        self.assertEqual(result["matched_messages"], len(self.index.query("词")))
        self.assertEqual(result["matched_messages"], 4)
        self.assertEqual(result["literal_occurrences"], 7)
        self.assertEqual(
            result["source_counts"], {"正文": 3, "发言人名称": 2, "媒体或表情占位": 1, "表情名": 1}
        )
        self.assertEqual(
            result["daily"], [{"date": "2025-01-01", "count": 3}, {"date": "2025-01-02", "count": 1}]
        )
        self.assertEqual(result["monthly"], [{"month": "2025-01", "count": 4}])
        self.assertEqual(result["first_match"], "2025-01-01 09:00:00")
        self.assertEqual(result["last_match"], "2025-01-02 10:00:00")

    def test_hits_group_by_thirty_minute_gap_and_cached_summaries_are_bounded(self):
        result = self.index.timeline("词")
        self.assertEqual(result["total_segments"], 3)
        self.assertEqual([segment["count"] for segment in result["segments"]], [2, 1, 1])
        self.assertIs(self.index.timeline("词"), result)
        for query in ("词", "词语", "词文", "阿词", "贴图", "不存在", "小甲"):
            self.index.timeline(query)
        self.assertLessEqual(len(self.index.timelines), MAX_TIMELINE_QUERIES)

    def test_sender_date_and_month_filters_reuse_index_match_rules(self):
        result = self.index.timeline("词", side="uid-2", dt_from="2025-01-02", dt_to="2025-01-02")
        self.assertEqual(result["matched_messages"], 1)
        self.assertEqual(result["daily"], [{"date": "2025-01-02", "count": 1}])
        monthly = self.index.timeline("词", month="2025-01")
        self.assertEqual(monthly["matched_messages"], 4)
        self.assertEqual(self.index.timeline("词", month="2024-12")["matched_messages"], 0)

    def test_empty_query_is_rejected_and_no_hits_have_empty_buckets(self):
        with self.assertRaisesRegex(ValueError, "请输入关键词"):
            self.index.timeline("  ")
        result = self.index.timeline("不存在")
        self.assertEqual(result["matched_messages"], 0)
        self.assertEqual(result["literal_occurrences"], 0)
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["segments"], [])


if __name__ == "__main__":
    unittest.main()
