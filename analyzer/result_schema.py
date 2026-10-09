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

"""Validation for structured LLM results.

The model is allowed to add fields, but fields the application relies on must
have the type and vocabulary promised by the corresponding prompt.  This
module deliberately has no logging and never includes a result value in an
error.  Chat text and provider error payloads therefore cannot leak through a
validation failure.

Validation is applied to newly returned model results before they are cached.
Readers remain migration-compatible when older entries omit required fields;
present known fields still satisfy their type, vocabulary, bounds and limits.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Callable


class CachedModelResult(dict):
    """Disk-cache provenance retained in memory, never added to the JSON payload.

    Older paid results remain readable even when a newer schema adds required
    fields. Only the cache reader constructs this type; model JSON cannot opt
    out of validation by supplying a flag in its payload.
    """


class _LegacyObject(dict):
    """Validation-only view that permits absent keys, never invalid present values."""


_MISSING = object()


def _legacy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _LegacyObject(value)
    if isinstance(value, list):
        return [_LegacyObject(item) if isinstance(item, dict) else item for item in value]
    return value


class ResultValidationError(ValueError):
    """A safe, user-facing description of a result contract violation."""

    def __init__(self, dimension: str, path: str, reason: str):
        self.dimension = dimension
        self.path = path or "$"
        self.reason = reason
        super().__init__(self.safe_message())

    def safe_message(self) -> str:
        return f"{self.dimension} 结果结构无效：字段 {self.path}{self.reason}"


_CONFIDENCE = ("high", "medium", "low")
_EMOTIONS = ("快乐", "平静", "焦虑", "沮丧", "愤怒", "兴奋", "疲惫", "无奈", "调侃", "紧张", "数据不足")
_OVERALL_TONES = ("轻松愉快", "严肃认真", "平淡日常", "紧张焦虑", "温馨亲密", "数据不足")
_SUPPORT = ("high", "medium", "low")
_MAX_TEXT = 20_000
_MAX_SHORT_TEXT = 2_000
_MAX_LIST = 64


def _fail(dimension: str, path: str, reason: str) -> None:
    raise ResultValidationError(dimension, path, reason)


def _object(value: Any, dimension: str, path: str) -> dict:
    if value is _MISSING:
        return _LegacyObject()
    if not isinstance(value, dict):
        _fail(dimension, path, "必须是对象")
    return value


def _string(value: Any, dimension: str, path: str, *, limit: int = _MAX_TEXT) -> str:
    if value is _MISSING:
        return ""
    if not isinstance(value, str):
        _fail(dimension, path, "必须是字符串")
    if len(value) > limit:
        _fail(dimension, path, f"长度不能超过 {limit} 个字符")
    return value


def _number(
    value: Any, dimension: str, path: str, *, lo: float | None = None, hi: float | None = None
) -> float:
    if value is _MISSING:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(dimension, path, "必须是数字")
    if not isfinite(float(value)):
        _fail(dimension, path, "必须是有限数字")
    if lo is not None and value < lo:
        _fail(dimension, path, "数值超出允许范围")
    if hi is not None and value > hi:
        _fail(dimension, path, "数值超出允许范围")
    return float(value)


def _integer(value: Any, dimension: str, path: str, *, lo: int | None = None, hi: int | None = None) -> int:
    if value is _MISSING:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(dimension, path, "必须是整数")
    if lo is not None and value < lo:
        _fail(dimension, path, "数值超出允许范围")
    if hi is not None and value > hi:
        _fail(dimension, path, "数值超出允许范围")
    return value


def _boolean(value: Any, dimension: str, path: str) -> bool:
    if value is _MISSING:
        return False
    if not isinstance(value, bool):
        _fail(dimension, path, "必须是布尔值")
    return value


def _enum(value: Any, dimension: str, path: str, choices: tuple[str, ...]) -> str:
    if value is _MISSING:
        return ""
    _string(value, dimension, path, limit=_MAX_SHORT_TEXT)
    if value not in choices:
        _fail(dimension, path, "不是允许的枚举值")
    return value


def _required(obj: dict, key: str, dimension: str, path: str | None = None) -> Any:
    actual_path = path or key
    if key not in obj:
        if isinstance(obj, _LegacyObject):
            return _MISSING
        _fail(dimension, actual_path, "缺少必填字段")
    return _legacy_value(obj[key]) if isinstance(obj, _LegacyObject) else obj[key]


def _list(
    value: Any, dimension: str, path: str, item: Callable[[Any, str], None], *, max_items: int = _MAX_LIST
) -> list:
    if value is _MISSING:
        return []
    if not isinstance(value, list):
        _fail(dimension, path, "必须是数组")
    if len(value) > max_items:
        _fail(dimension, path, f"元素数量不能超过 {max_items}")
    for index, entry in enumerate(value):
        item(entry, f"{path}[{index}]")
    return value


def _text_item(value: Any, dimension: str, path: str) -> None:
    _string(value, dimension, path)


def _required_text(obj: dict, key: str, dimension: str, *, limit: int = _MAX_TEXT) -> None:
    _string(_required(obj, key, dimension), dimension, key, limit=limit)


def _required_enum(obj: dict, key: str, dimension: str, choices: tuple[str, ...]) -> None:
    _enum(_required(obj, key, dimension), dimension, key, choices)


def _required_list(obj: dict, key: str, dimension: str, *, max_items: int = _MAX_LIST) -> None:
    _list(
        _required(obj, key, dimension),
        dimension,
        key,
        lambda value, path: _text_item(value, dimension, path),
        max_items=max_items,
    )


def _validate_emotion(result: dict, dimension: str) -> None:
    for key in ("self_emotion", "other_emotion"):
        _required_enum(result, key, dimension, _EMOTIONS)
    for key in ("self_intensity", "other_intensity"):
        _integer(_required(result, key, dimension), dimension, key, lo=0, hi=10)
    _required_list(result, "self_keywords", dimension, max_items=16)
    _required_list(result, "other_keywords", dimension, max_items=16)
    _required_enum(result, "overall_tone", dimension, _OVERALL_TONES)
    for key in ("self_evidence", "other_evidence"):
        _required_text(result, key, dimension, limit=_MAX_SHORT_TEXT)
    _required_text(result, "month_vibe", dimension, limit=500)
    _required_text(result, "turning_point", dimension, limit=_MAX_SHORT_TEXT)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _validate_topic_item(value: Any, path: str, dimension: str, *, group: bool = False) -> None:
    item = _object(value, dimension, path)
    for key in ("name", "one_liner"):
        _string(_required(item, key, dimension, f"{path}.{key}"), dimension, f"{path}.{key}", limit=500)
    _number(_required(item, "weight", dimension, f"{path}.weight"), dimension, f"{path}.weight", lo=0, hi=1)
    _list(
        _required(item, "keywords", dimension, f"{path}.keywords"),
        dimension,
        f"{path}.keywords",
        lambda value, item_path: _string(value, dimension, item_path, limit=500),
        max_items=16,
    )
    if group:
        _list(
            _required(item, "key_members", dimension, f"{path}.key_members"),
            dimension,
            f"{path}.key_members",
            lambda value, item_path: _string(value, dimension, item_path, limit=500),
            max_items=16,
        )


def _validate_topics(result: dict, dimension: str, *, group: bool = False) -> None:
    _required_text(result, "month_title", dimension, limit=500)
    topics = _required(result, "topics", dimension)
    _list(
        topics,
        dimension,
        "topics",
        lambda value, path: _validate_topic_item(value, path, dimension, group=group),
        max_items=16,
    )
    _required_text(result, "summary", dimension, limit=_MAX_SHORT_TEXT)
    _boolean(_required(result, "topic_shift_detected", dimension), dimension, "topic_shift_detected")
    _required_text(result, "shift_description", dimension, limit=_MAX_SHORT_TEXT)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _validate_relationship(result: dict, dimension: str) -> None:
    _required_enum(result, "initiator_tendency", dimension, ("self", "other", "balanced"))
    _number(
        _required(result, "initiator_ratio_self", dimension), dimension, "initiator_ratio_self", lo=0, hi=1
    )
    _required_enum(
        result, "interaction_style", dimension, ("轻松调侃", "深度交流", "互助协作", "日常问候", "混合")
    )
    _integer(_required(result, "closeness_score", dimension), dimension, "closeness_score", lo=1, hi=10)
    _required_enum(result, "closeness_trend", dimension, ("上升", "下降", "稳定"))
    role_choices = ("倾诉者", "倾听者", "建议者", "吐槽伙伴", "并肩作战", "数据不足")
    _required_enum(result, "self_role", dimension, role_choices)
    _required_enum(result, "other_role", dimension, role_choices)
    _required_enum(result, "emotional_support_self_to_other", dimension, _SUPPORT)
    _required_enum(result, "emotional_support_other_to_self", dimension, _SUPPORT)
    for key in ("secret_language", "push_pull", "relationship_summary"):
        _required_text(result, key, dimension, limit=_MAX_SHORT_TEXT)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _validate_habits(result: dict, dimension: str) -> None:
    for key, max_items in (
        ("personality_tags", 16),
        ("common_phrases", 16),
        ("top_emojis", 32),
        ("unique_traits", 32),
    ):
        _required_list(result, key, dimension, max_items=max_items)
    _required_enum(result, "emoji_style", dimension, ("丰富", "适中", "极少"))
    _required_enum(result, "sentence_length", dimension, ("短句为主", "长短混合", "长句较多"))
    _required_enum(result, "reply_speed", dimension, ("秒回型", "适中", "深思熟虑型"))
    _required_enum(result, "topic_jumping", dimension, ("经常跳跃", "偶尔", "专注一个话题"))
    for key in ("language_fingerprint", "typing_persona", "signature_moment"):
        _required_text(result, key, dimension, limit=_MAX_SHORT_TEXT)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _scalar_text_or_number(value: Any, dimension: str, path: str) -> None:
    if value is _MISSING:
        return
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        _fail(dimension, path, "必须是字符串或数字")
    if isinstance(value, str) and len(value) > _MAX_SHORT_TEXT:
        _fail(dimension, path, f"长度不能超过 {_MAX_SHORT_TEXT} 个字符")
    if isinstance(value, float) and not isfinite(value):
        _fail(dimension, path, "必须是有限数字")


def _validate_profile(result: dict, dimension: str, *, group: bool = False) -> None:
    for key in ("name", "overall_impression", "one_line_bio"):
        _required_text(result, key, dimension, limit=500)

    personality = _object(
        _required(result, "personality_analysis", dimension), dimension, "personality_analysis"
    )
    for key in ("core_type", "social_tendency"):
        _string(
            _required(personality, key, dimension, f"personality_analysis.{key}"),
            dimension,
            f"personality_analysis.{key}",
            limit=_MAX_SHORT_TEXT,
        )
    # 契约审计已确认这两项可缺失，渲染器会跳过；存在时仍须符合文本约束。
    for key in ("thinking_style", "humor_style"):
        if key in personality:
            _string(personality[key], dimension, f"personality_analysis.{key}", limit=_MAX_SHORT_TEXT)
    for key in ("strengths", "weaknesses", "quirks"):
        _list(
            _required(personality, key, dimension, f"personality_analysis.{key}"),
            dimension,
            f"personality_analysis.{key}",
            lambda value, path: _text_item(value, dimension, path),
            max_items=32,
        )

    chat_style = _object(
        _required(result, "chat_style_analysis", dimension), dimension, "chat_style_analysis"
    )
    for key in ("opener", "responder", "punctuation_style", "emoji_usage"):
        _string(
            _required(chat_style, key, dimension, f"chat_style_analysis.{key}"),
            dimension,
            f"chat_style_analysis.{key}",
            limit=_MAX_SHORT_TEXT,
        )
    for key in ("signature_phrases", "topic_preference", "topic_avoid"):
        _list(
            _required(chat_style, key, dimension, f"chat_style_analysis.{key}"),
            dimension,
            f"chat_style_analysis.{key}",
            lambda value, path: _text_item(value, dimension, path),
            max_items=32,
        )

    emotional = _object(_required(result, "emotional_pattern", dimension), dimension, "emotional_pattern")
    _required_enum(emotional, "frequency", dimension, _CONFIDENCE)
    for key in ("typical_state", "stress_response", "support_style"):
        _string(
            _required(emotional, key, dimension, f"emotional_pattern.{key}"),
            dimension,
            f"emotional_pattern.{key}",
            limit=_MAX_SHORT_TEXT,
        )
    _list(
        _required(emotional, "trigger_topics", dimension, "emotional_pattern.trigger_topics"),
        dimension,
        "emotional_pattern.trigger_topics",
        lambda value, path: _text_item(value, dimension, path),
        max_items=32,
    )
    _required_enum(emotional, "recovery_speed", dimension, ("fast", "medium", "slow"))

    intelligence = _object(
        _required(result, "intelligence_indicators", dimension), dimension, "intelligence_indicators"
    )
    for key in ("thinking_depth", "learning_style"):
        _string(
            _required(intelligence, key, dimension, f"intelligence_indicators.{key}"),
            dimension,
            f"intelligence_indicators.{key}",
            limit=_MAX_SHORT_TEXT,
        )
    _required_enum(intelligence, "language_richness", dimension, ("rich", "medium", "simple"))
    _required_enum(intelligence, "logic_consistency", dimension, _CONFIDENCE)

    dynamics = _object(
        _required(result, "relationship_dynamics", dimension), dimension, "relationship_dynamics"
    )
    for key in ("role_in_relationship", "initiation_pattern", "response_to_conflict"):
        _string(
            _required(dynamics, key, dimension, f"relationship_dynamics.{key}"),
            dimension,
            f"relationship_dynamics.{key}",
            limit=_MAX_SHORT_TEXT,
        )
    _required_enum(dynamics, "vulnerability_level", dimension, _CONFIDENCE)
    _list(
        _required(dynamics, "what_they_seek", dimension, "relationship_dynamics.what_they_seek"),
        dimension,
        "relationship_dynamics.what_they_seek",
        lambda value, path: _text_item(value, dimension, path),
        max_items=32,
    )

    growth = _object(_required(result, "growth_observation", dimension), dimension, "growth_observation")
    _boolean(_required(growth, "has_changed", dimension), dimension, "growth_observation.has_changed")
    _required_text(growth, "change_description", dimension, limit=_MAX_SHORT_TEXT)
    _list(
        _required(growth, "possible_reasons", dimension, "growth_observation.possible_reasons"),
        dimension,
        "growth_observation.possible_reasons",
        lambda value, path: _text_item(value, dimension, path),
        max_items=32,
    )

    _required_list(result, "fun_facts", dimension, max_items=32)
    scoring = _object(_required(result, "scoring", dimension), dimension, "scoring")
    for key in ("expressiveness", "emotional_richness", "logical_ratio", "social_energy", "uniqueness"):
        _scalar_text_or_number(
            _required(scoring, key, dimension, f"scoring.{key}"), dimension, f"scoring.{key}"
        )
    _required_list(result, "counter_evidence", dimension, max_items=32)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)
    _required_text(result, "roast_note", dimension, limit=_MAX_SHORT_TEXT)
    _required_text(result, "verdict", dimension, limit=500)

    if group:
        group_specific = _object(_required(result, "group_specific", dimension), dimension, "group_specific")
        for key in ("group_role", "reply_pattern", "presence"):
            _required_text(group_specific, key, dimension, limit=_MAX_SHORT_TEXT)


def _validate_group_dynamics(result: dict, dimension: str) -> None:
    _required_text(result, "group_vibe", dimension, limit=500)

    def core_member(value: Any, path: str) -> None:
        item = _object(value, dimension, path)
        for key in ("name", "role", "evidence"):
            _required_text(item, key, dimension, limit=_MAX_SHORT_TEXT)

    _list(_required(result, "core_members", dimension), dimension, "core_members", core_member, max_items=16)

    def subgroup(value: Any, path: str) -> None:
        item = _object(value, dimension, path)
        _list(
            _required(item, "members", dimension, f"{path}.members"),
            dimension,
            f"{path}.members",
            lambda entry, entry_path: _string(entry, dimension, entry_path, limit=500),
            max_items=16,
        )
        _required_text(item, "evidence", dimension, limit=_MAX_SHORT_TEXT)

    _list(_required(result, "sub_groups", dimension), dimension, "sub_groups", subgroup, max_items=16)
    for key in ("power_structure", "newcomer_or_outsider", "self_role"):
        _required_text(result, key, dimension, limit=_MAX_SHORT_TEXT)
    _number(_required(result, "lurker_ratio", dimension), dimension, "lurker_ratio", lo=0, hi=1)
    _required_list(result, "conflict_moments", dimension, max_items=32)
    _required_enum(result, "pace", dimension, ("日常续命型", "事件驱动型", "深夜爆发型", "常年静默型"))
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _validate_group_emotion(result: dict, dimension: str) -> None:
    _required_enum(
        result,
        "group_emotion",
        dimension,
        ("热闹", "轻松", "温馨", "平淡", "焦虑", "低落", "紧绷", "亢奋", "数据不足"),
    )
    _integer(_required(result, "group_intensity", dimension), dimension, "group_intensity", lo=0, hi=10)
    _required_text(result, "group_evidence", dimension, limit=_MAX_SHORT_TEXT)

    def member_emotion(value: Any, path: str) -> None:
        item = _object(value, dimension, path)
        _required_text(item, "name", dimension, limit=500)
        # 群聊提示词只要求成员的情绪标签，不限定为私聊的枚举词表。
        _required_text(item, "emotion", dimension, limit=_MAX_SHORT_TEXT)
        _integer(
            _required(item, "intensity", dimension, f"{path}.intensity"),
            dimension,
            f"{path}.intensity",
            lo=0,
            hi=10,
        )
        _required_text(item, "evidence", dimension, limit=_MAX_SHORT_TEXT)

    _list(
        _required(result, "member_emotions", dimension),
        dimension,
        "member_emotions",
        member_emotion,
        max_items=16,
    )
    for key in ("emotion_flow", "turning_point", "atmosphere_maker", "atmosphere_killer"):
        _required_text(result, key, dimension, limit=_MAX_SHORT_TEXT)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)


def _validate_recap(result: dict, dimension: str) -> None:
    _required_text(result, "overall", dimension, limit=_MAX_TEXT)

    def arc_item(value: Any, path: str) -> None:
        item = _object(value, dimension, path)
        for key in ("period", "phase", "text"):
            _required_text(item, key, dimension, limit=_MAX_SHORT_TEXT)

    _list(_required(result, "arc", dimension), dimension, "arc", arc_item, max_items=64)

    def turning_item(value: Any, path: str) -> None:
        item = _object(value, dimension, path)
        for key in ("month", "what_changed", "evidence"):
            _required_text(item, key, dimension, limit=_MAX_SHORT_TEXT)

    _list(
        _required(result, "turning_points", dimension),
        dimension,
        "turning_points",
        turning_item,
        max_items=64,
    )
    _required_text(result, "who_drives", dimension, limit=_MAX_SHORT_TEXT)
    _required_list(result, "unread_between_lines", dimension, max_items=16)
    _required_text(result, "closing", dimension, limit=_MAX_SHORT_TEXT)


def _validate_ask(result: dict, dimension: str) -> None:
    _required_text(result, "answer", dimension, limit=2_000)
    _required_enum(result, "confidence", dimension, _CONFIDENCE)
    _required_list(result, "evidence", dimension, max_items=4)


_VALIDATORS: dict[str, Callable[[dict, str], None]] = {
    "emotion": _validate_emotion,
    "topics": _validate_topics,
    "relationship": _validate_relationship,
    "habits": _validate_habits,
    "profile": _validate_profile,
    "group_dynamics": _validate_group_dynamics,
    "group_topics": lambda result, dimension: _validate_topics(result, dimension, group=True),
    "group_emotion": _validate_group_emotion,
    "member_profiles": lambda result, dimension: _validate_profile(result, dimension, group=True),
    "recap": _validate_recap,
    "ask": _validate_ask,
}

_MONTHLY_DIMENSIONS = frozenset(
    {"emotion", "topics", "relationship", "group_dynamics", "group_topics", "group_emotion"}
)
_PEOPLE_DIMENSIONS = frozenset({"habits", "profile"})


def validate_result(dimension: str, result: Any, *, scope: str | None = None) -> None:
    """Validate one model result, raising only a safe structured error."""
    validator = _VALIDATORS.get(dimension)
    if validator is None:
        _fail(dimension, "$", "不支持的分析维度")
    if not isinstance(result, dict):
        _fail(dimension, "$", "必须是对象")
    validator(result, dimension)


def validate_dimension_result(dimension: str, result: Any) -> None:
    """Validate aggregate keys, complete fresh results and present cached fields."""
    if dimension in _MONTHLY_DIMENSIONS:
        aggregate = _object(result, dimension, "$")
        if not aggregate:
            _fail(dimension, "$", "结果不能为空")
        for period, item in aggregate.items():
            if not isinstance(period, str):
                _fail(dimension, "$", "月份键必须是字符串")
            _validate_aggregate_item(dimension, item)
        return
    if dimension in _PEOPLE_DIMENSIONS:
        aggregate = _object(result, dimension, "$")
        if not aggregate:
            _fail(dimension, "$", "结果不能为空")
        for person, item in aggregate.items():
            if person not in ("self", "other"):
                _fail(dimension, "members", "存在不允许的成员键")
            _validate_aggregate_item(dimension, item)
        return
    if dimension == "member_profiles":
        aggregate = _object(result, dimension, "$")
        if not aggregate:
            _fail(dimension, "$", "结果不能为空")
        for uid, item in aggregate.items():
            if not isinstance(uid, str):
                _fail(dimension, "$", "成员键必须是字符串")
            _validate_aggregate_item(dimension, item)
        return
    validate_result(dimension, result)


def validate_cached_result(dimension: str, result: Any) -> None:
    """Check every present known field, permitting missing legacy keys at any depth.

    The temporary view only affects required-key lookup. It never adds defaults
    to the actual payload or relaxes type, vocabulary, bounds or size checks.
    """
    validate_result(dimension, _legacy_value(result))


def _validate_aggregate_item(dimension: str, result: Any) -> None:
    if isinstance(result, CachedModelResult):
        validate_cached_result(dimension, result)
    else:
        validate_result(dimension, result)


def dimension_is_monthly(dimension: str) -> bool:
    """Whether ``dimension`` stores one validated result per month."""
    return dimension in _MONTHLY_DIMENSIONS


def supports_dimension(dimension: str) -> bool:
    """Whether a model-result schema is defined for ``dimension``."""
    return dimension in _VALIDATORS
