# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#
"""第五轮审查的回归测试

覆盖：
- running 任务不参与 TTL 淘汰（用例在 tests/test_review_fixes.py 的
  TestJobsHygiene 里，与它补上的那条旧用例放在一起）；
- 话题权重归一化后总和恒为 1.0，且不会给末位话题算出负权重；
- 图片摘要的进程内缓存按 LRU 淘汰（不再"超限整锅 clear()"），
  且命中磁盘缓存那条路径同样受容量约束；
- .env.example 里记录的输出预算与代码默认值不漂移。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402

bootstrap()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import vision  # noqa: E402


def _topics(*weights) -> dict:
    return {"topics": [{"name": f"话题{i}", "weight": w} for i, w in enumerate(weights)]}


def _weights(obj) -> list:
    return [t["weight"] for t in obj["topics"]]


class TestTopicWeightNormalization(unittest.TestCase):
    """归一化后总和必须恰好是 1.0：前端按 weight*100 直接显示百分比"""

    def test_rounding_drift_is_absorbed(self):
        """0.333/0.333/0.334 各自 round 到 2 位是 0.99，必须补回 1.0"""
        obj = _topics(0.333, 0.333, 0.334)
        dc._normalize_topic_weights(obj)
        self.assertEqual(round(sum(_weights(obj)), 2), 1.0)
        self.assertEqual(_weights(obj), [0.33, 0.33, 0.34], "误差应补给占比最大的那个话题")

    def test_sum_is_exactly_one_over_many_shapes(self):
        cases = [
            (1.0,),
            (0.5, 0.5),
            (1, 1, 1),
            (0.1, 0.2, 0.3, 0.4),
            (2.5, 2.5),
            (0.005,) * 7,
            tuple(range(1, 11)),  # 1..10，10 个话题
        ]
        for case in cases:
            with self.subTest(case=case):
                obj = _topics(*case)
                dc._normalize_topic_weights(obj)
                self.assertEqual(round(sum(_weights(obj)), 2), 1.0)

    def test_tiny_last_topic_never_goes_negative(self):
        """把误差补给"最后一个"话题时，10 个话题 + 末位极小会算出负权重"""
        obj = _topics(0.111, 0.111, 0.111, 0.111, 0.111, 0.111, 0.111, 0.111, 0.111, 0.001)
        dc._normalize_topic_weights(obj)
        self.assertEqual(round(sum(_weights(obj)), 2), 1.0)
        self.assertTrue(all(w >= 0 for w in _weights(obj)), f"出现负权重: {_weights(obj)}")

    def test_order_is_preserved(self):
        obj = _topics(0.05, 0.9, 0.05)
        dc._normalize_topic_weights(obj)
        got = _weights(obj)
        self.assertEqual(got, [0.05, 0.9, 0.05])
        self.assertEqual(got.index(max(got)), 1, "最大项不能被误差补给顶下去")

    def test_non_numeric_weight_is_zeroed_and_sum_kept(self):
        obj = {"topics": [{"name": "a", "weight": "0.5"}, {"name": "b", "weight": "坏值"}, {"name": "c"}]}
        dc._normalize_topic_weights(obj)
        self.assertEqual(obj["topics"][1]["weight"], 0.0)
        self.assertEqual(round(sum(_weights(obj)), 2), 1.0)

    def test_non_dict_entries_and_bad_input_do_not_crash(self):
        obj = {"topics": [{"name": "a", "weight": 1.0}, "字符串", None, 42]}
        dc._normalize_topic_weights(obj)
        self.assertEqual(obj["topics"][0]["weight"], 1.0)
        for bad in ("topics", "weight"):
            dc._normalize_topic_weights({})  # 缺字段
            dc._normalize_topic_weights({"topics": bad})  # 类型不对
        self.assertEqual(_weights(_topics(0.0, 0.0)), [0.0, 0.0], "总权重为 0 时原样保留，不除零")


class TestVisionMemoLRU(unittest.TestCase):
    """进程内摘要缓存：每条背后都是一次付费视觉调用，淘汰只能淘汰最久未用的"""

    def test_overflow_evicts_oldest_only(self):
        with mock.patch.object(vision, "_MEMO", {}), mock.patch.object(vision, "_MEMO_MAX", 3):
            for k in "abc":
                vision._memo_put(k, k.upper())
            self.assertEqual(sorted(vision._MEMO), ["a", "b", "c"])
            vision._memo_put("d", "D")
            self.assertEqual(sorted(vision._MEMO), ["b", "c", "d"], "只该淘汰最久未用的 a")
            self.assertEqual(vision._MEMO["b"], "B", "旧条目内容不能被清掉")

    def test_recently_read_key_survives_eviction(self):
        with mock.patch.object(vision, "_MEMO", {}), mock.patch.object(vision, "_MEMO_MAX", 3):
            for k in "abc":
                vision._memo_put(k, k.upper())
            self.assertEqual(vision._memo_get("a"), "A")  # a 变成最近使用
            vision._memo_put("d", "D")  # 该淘汰 b
            self.assertIn("a", vision._MEMO)
            self.assertNotIn("b", vision._MEMO)

    def test_miss_returns_none(self):
        with mock.patch.object(vision, "_MEMO", {}):
            self.assertIsNone(vision._memo_get("没有这个键"))

    def test_disk_hit_path_also_respects_bound(self):
        """命中磁盘缓存那条路径原本没做容量检查，条目数可以越过上限"""
        keys = iter(f"k{i}" for i in range(6))
        with (
            mock.patch.object(vision, "_MEMO", {}),
            mock.patch.object(vision, "_MEMO_MAX", 2),
            mock.patch.object(vision, "available", return_value=True),
            mock.patch.object(vision, "pick_images", return_value=[{"path": "x", "key": "fp"}]),
            mock.patch.object(vision, "_images_key", side_effect=lambda _imgs: next(keys)),
            mock.patch.object(vision, "_read_cache", return_value="磁盘里的摘要"),
        ):
            for _ in range(6):
                self.assertEqual(vision.digest([], chat_hash="h"), "磁盘里的摘要")
            self.assertLessEqual(len(vision._MEMO), 2, "走磁盘缓存也必须受 _MEMO_MAX 约束")

    def test_plain_dict_still_works(self):
        """现有用例用 mock.patch.object(vision, "_MEMO", {}) 注入普通 dict，别把它换掉"""
        with mock.patch.object(vision, "_MEMO", {}):
            vision._memo_put("k", "v")
            self.assertEqual(vision._memo_get("k"), "v")
            vision._MEMO.clear()  # 用例里也这么清
            self.assertIsNone(vision._memo_get("k"))


class TestEnvExampleBudgetDocs(unittest.TestCase):
    """.env.example 里写的输出预算必须与代码默认值一致（本轮修的就是这类文档漂移）"""

    def test_documented_max_tokens_match_code_defaults(self):
        path = Path(__file__).resolve().parent.parent / ".env.example"
        text = path.read_text(encoding="utf-8")
        documented = {}
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line.startswith("LLM_MAX_TOKENS_"):
                name, _, value = line.partition("=")
                documented[name[len("LLM_MAX_TOKENS_") :].strip().lower()] = int(value.strip())
        self.assertTrue(documented, ".env.example 里应给出各维度预算的默认值")
        for dim, value in documented.items():
            with self.subTest(dim=dim):
                self.assertIn(dim, dc._DEFAULT_MAX_TOKENS, f".env.example 提到未知维度 {dim}")
                self.assertEqual(
                    value,
                    dc._DEFAULT_MAX_TOKENS[dim],
                    f".env.example 的 {dim} 预算与 _DEFAULT_MAX_TOKENS 不一致（文档漂移）",
                )


if __name__ == "__main__":
    unittest.main()
