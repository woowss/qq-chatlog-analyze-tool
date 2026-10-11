# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Validated analysis scopes shared by preview, execution and evidence lookup."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import date


MAX_SCOPE_MESSAGES = 5000


def _date_value(value, field):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")
    return value


def normalize_scope(raw, chat):
    """Validate an untrusted scope payload against this chat and return canonical data."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("分析范围格式无效")
    unknown = set(raw) - {
        "date_from",
        "date_to",
        "target_uids",
        "allowed_uids",
        "message_ids",
        "message_positions",
    }
    if unknown:
        raise ValueError("分析范围包含未知字段")

    date_from = _date_value(raw.get("date_from"), "开始日期")
    date_to = _date_value(raw.get("date_to"), "结束日期")
    if date_from and date_to and date_from > date_to:
        raise ValueError("开始日期不能晚于结束日期")

    participant_uids = {p.uid for p in chat.participants() if p.uid}
    message_uids = {m.sender_uid for m in chat.messages if m.sender_uid}
    known_uids = participant_uids | message_uids

    def uid_list(field):
        value = raw.get(field)
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(uid, str) or not uid for uid in value)
        ):
            raise ValueError(f"{field} 必须包含至少一个成员")
        values = list(dict.fromkeys(value))
        if any(uid not in known_uids for uid in values):
            raise ValueError(f"{field} 包含当前聊天中不存在的成员")
        return sorted(values)

    target_uids = uid_list("target_uids")
    allowed_uids = uid_list("allowed_uids")

    message_positions = raw.get("message_positions")
    message_ids = raw.get("message_ids")
    if message_positions is not None and message_ids is not None:
        raise ValueError("消息片段格式无效，请重新选择消息")
    if message_ids is not None:
        if (
            not isinstance(message_ids, list)
            or not message_ids
            or any(not isinstance(item, str) or not item for item in message_ids)
        ):
            raise ValueError("消息片段必须包含至少一条消息")
        message_ids = list(dict.fromkeys(message_ids))
        if len(message_ids) > MAX_SCOPE_MESSAGES:
            raise ValueError(f"一次最多选择 {MAX_SCOPE_MESSAGES} 条消息")
        positions_by_id = {}
        for position, message in enumerate(chat.messages):
            if message.id:
                positions_by_id.setdefault(message.id, []).append(position)
        message_positions = []
        for message_id in message_ids:
            matches = positions_by_id.get(message_id, [])
            if not matches:
                raise ValueError("所选消息已不属于当前聊天，请重新选择")
            if len(matches) != 1:
                raise ValueError("旧版消息片段无法区分重复消息 ID，请重新选择消息")
            message_positions.append(matches[0])

    if message_positions is not None:
        if (
            not isinstance(message_positions, list)
            or not message_positions
            or any(type(position) is not int for position in message_positions)
        ):
            raise ValueError("消息片段必须包含至少一条有效消息")
        message_positions = list(dict.fromkeys(message_positions))
        if len(message_positions) > MAX_SCOPE_MESSAGES:
            raise ValueError(f"一次最多选择 {MAX_SCOPE_MESSAGES} 条消息")
        if any(position < 0 or position >= len(chat.messages) for position in message_positions):
            raise ValueError("所选消息已不属于当前聊天，请重新选择")
        message_positions.sort()

    scope = {
        "date_from": date_from,
        "date_to": date_to,
        "target_uids": target_uids,
        "allowed_uids": allowed_uids,
        "message_positions": message_positions,
    }
    if not any((date_from, date_to, target_uids, allowed_uids, message_positions)):
        return {}
    return scope


def scope_identity(scope):
    if not scope:
        return ""
    payload = json.dumps(scope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def apply_scope(chat, raw_scope):
    """Return an analysis view containing only explicitly allowed messages.

    Date strings are the parser's UTC+8 local timestamps. The end date is inclusive,
    equivalent to a half-open interval ending at midnight on the following day.
    Messages outside the scope are never added as reply or neighbor context.
    """
    scope = normalize_scope(raw_scope, chat)
    if not scope:
        return chat, scope

    start = scope.get("date_from", "")
    end = scope.get("date_to", "")
    allowed = set(scope["allowed_uids"]) if scope.get("allowed_uids") is not None else None
    selected = set(scope["message_positions"]) if scope.get("message_positions") is not None else None
    messages = []
    kept_positions = set()
    for position, message in enumerate(chat.messages):
        local_date = message.time_str[:10]
        if start and local_date < start:
            continue
        if end and local_date > end:
            continue
        if allowed is not None and message.sender_uid not in allowed:
            continue
        if selected is not None and position not in selected:
            continue
        messages.append(message)
        kept_positions.add(position)

    positions_by_id = {}
    for position, message in enumerate(chat.messages):
        if message.id:
            positions_by_id.setdefault(message.id, []).append(position)
    scoped_messages = []
    for position in sorted(kept_positions):
        message = chat.messages[position]
        if message.reply_to_id or message.reply_to_uid:
            matches = positions_by_id.get(message.reply_to_id, [])
            if len(matches) != 1 or matches[0] not in kept_positions:
                message = replace(message, reply_to_id="", reply_to_uid="")
        scoped_messages.append(message)
    scoped = copy.copy(chat)
    scoped.messages = scoped_messages
    # Export duration describes the original timeline, not a filtered view.
    # Target-only scopes keep the full input and therefore keep its duration.
    if start or end or allowed is not None or selected is not None:
        scoped.duration_days = 0
    # Keep the position in the immutable parsed timeline. Scoped positions are
    # local to this view and cannot be used to open the right source message
    # when IDs repeat outside the selected range.
    scoped._analysis_source_positions = sorted(kept_positions)
    scoped._stats_cache = None
    scoped._months_cache = None
    scoped._member_activity_cache = None
    scoped._participants_cache = None
    scoped._sender_counts_cache = None
    scoped._sender_names_cache = None
    scoped._matrix_cache = None
    scoped._message_index = None
    scoped._interaction_sparse_cache = None
    scoped.analysis_scope_id = scope_identity(scope)
    input_scope = {
        key: value for key, value in scope.items() if key != "target_uids" and value not in ("", None)
    }
    scoped.analysis_input_scope_id = scope_identity(input_scope)
    scoped.analysis_target_uids = set(scope["target_uids"]) if scope.get("target_uids") is not None else None
    return scoped, scope


def scope_summary(scope, names=None):
    if not scope:
        return "全量对话"
    pieces = []
    start, end = scope.get("date_from"), scope.get("date_to")
    if start or end:
        pieces.append(f"{start or '最早'} 至 {end or '最新'}（UTC+8，含结束日）")
    if scope.get("allowed_uids") is not None:
        pieces.append(f"仅发送 {len(scope['allowed_uids'])} 位成员的消息")
    if scope.get("target_uids") is not None:
        labels = [names.get(uid, uid) for uid in scope["target_uids"]] if names else []
        pieces.append("评价成员：" + "、".join(labels or scope["target_uids"]))
    if scope.get("message_positions") is not None:
        pieces.append(f"选中片段 {len(scope['message_positions'])} 条")
    pieces.append("不补入范围外上下文")
    return "；".join(pieces)


def participant_labels(chat):
    """UID-qualified labels where duplicate display names would be ambiguous."""
    people = chat.participants()
    counts = {}
    for person in people:
        counts[person.name] = counts.get(person.name, 0) + 1
    return {
        person.uid: f"{person.name} · {person.uid}" if counts.get(person.name, 0) > 1 else person.name
        for person in people
    }
