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

"""统一的分析维度元数据。

分析器仍分别拥有各自的提示词、输入构造和缓存指纹；这个目录只负责 Web 层所需的
维度标签、适用模式、执行函数、进度单位与全量清单策略。
"""

from dataclasses import dataclass

from analyzer.deepseek_client import (
    analyze_emotion,
    analyze_habits,
    analyze_profile,
    analyze_relationship,
    analyze_topics,
)
from analyzer.group_client import GROUP_DIMENSIONS
from analyzer.recap_client import RECAP_DIMENSIONS


@dataclass(frozen=True)
class AnalysisDimension:
    key: str
    label: str
    mode: str
    runner: object
    unit: str = "月"
    include_in_all: bool = True
    private_only: bool = False


_PRIVATE_RUNNERS = {
    "emotion": analyze_emotion,
    "topics": analyze_topics,
    "relationship": analyze_relationship,
    "habits": analyze_habits,
    "profile": analyze_profile,
}
_PRIVATE_LABELS = {
    "emotion": "情绪分析",
    "topics": "话题趋势",
    "relationship": "人际关系",
    "habits": "个人习惯",
    "profile": "人物锐评",
}

ANALYSIS_CATALOG = {}
for _key, _runner in _PRIVATE_RUNNERS.items():
    ANALYSIS_CATALOG[_key] = AnalysisDimension(_key, _PRIVATE_LABELS[_key], "private", _runner)
for _key, (_label, _runner, _unit) in GROUP_DIMENSIONS.items():
    ANALYSIS_CATALOG[_key] = AnalysisDimension(_key, _label, "group", _runner, _unit)
for _key, (_label, _runner, _unit) in RECAP_DIMENSIONS.items():
    # recap 单独触发，避免改变既有「全量分析」的费用承诺。
    ANALYSIS_CATALOG[_key] = AnalysisDimension(
        _key, _label, "private", _runner, _unit, include_in_all=False, private_only=True
    )


def dimensions_for_mode(is_group: bool) -> list[str]:
    mode = "group" if is_group else "private"
    return [item.key for item in ANALYSIS_CATALOG.values() if item.mode == mode and item.include_in_all]


def label_for(dimension: str, default=None):
    item = ANALYSIS_CATALOG.get(dimension)
    return item.label if item else default


def mode_for(dimension: str):
    item = ANALYSIS_CATALOG.get(dimension)
    return item.mode if item else None


def unit_for(dimension: str) -> str:
    item = ANALYSIS_CATALOG.get(dimension)
    return item.unit if item else "月"


def is_private_only(dimension: str) -> bool:
    item = ANALYSIS_CATALOG.get(dimension)
    return bool(item and item.private_only)
