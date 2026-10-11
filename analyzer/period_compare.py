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

"""本地双时段对比；区间外消息只用于判断会话边界是否被截断。"""

from collections import Counter
from datetime import date
from statistics import median

from analyzer.local_stats import SESSION_GAP_MS, is_session_start
from analyzer.group_stats import is_unknown_message
from parser.qq_parser import ChatData


def _period_dates(value: dict) -> tuple[date, date]:
    try:
        start = date.fromisoformat(str(value.get("from") or ""))
        end = date.fromisoformat(str(value.get("to") or ""))
    except ValueError as exc:
        raise ValueError("请选择有效的开始和结束日期") from exc
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return start, end


def _boundary_flags(all_messages: list, selected: list) -> dict:
    if not selected:
        return {"before": False, "after": False}
    selected_ids = {id(message) for message in selected}
    first_ts, last_ts = selected[0].timestamp, selected[-1].timestamp
    before = [m for m in all_messages if id(m) not in selected_ids and m.timestamp < first_ts]
    after = [m for m in all_messages if id(m) not in selected_ids and m.timestamp > last_ts]
    return {
        "before": bool(before and first_ts - before[-1].timestamp <= SESSION_GAP_MS),
        "after": bool(after and after[0].timestamp - last_ts <= SESSION_GAP_MS),
    }


def _compare_value(a, b) -> dict:
    if a is None or b is None:
        return {"absolute": None, "percent": None}
    delta = round(b - a, 2)
    return {"absolute": delta, "percent": round(delta / a * 100, 1) if a else None}


def _private_metrics(chat: ChatData, messages: list, calendar_days: int) -> dict:
    active_days = len({message.time_str[:10] for message in messages})
    total_chars = sum(len(message.text or "") for message in messages)
    session_starts = sum(
        1
        for index, message in enumerate(messages)
        if is_session_start(messages[index - 1].timestamp if index else None, message.timestamp)
    )
    reply_gaps = {"self": [], "other": []}
    previous = None
    for message in messages:
        if previous is not None and previous.sender_uid != message.sender_uid:
            gap = (message.timestamp - previous.timestamp) / 1000
            if 0 < gap <= 6 * 60 * 60:
                side = "self" if message.sender_uid == chat.self_uid else "other"
                reply_gaps[side].append(gap)
        previous = message
    return {
        "total_messages": len(messages),
        "active_days": active_days,
        "avg_daily": round(len(messages) / calendar_days, 1) if calendar_days else 0.0,
        "avg_message_chars": round(total_chars / len(messages), 1) if messages else None,
        "session_starts": session_starts,
        "reply_median_seconds": {
            side: {"value": round(median(gaps), 1) if gaps else None, "samples": len(gaps)}
            for side, gaps in reply_gaps.items()
        },
    }


def _group_metrics(chat: ChatData, messages: list, calendar_days: int) -> dict:
    member_counts: Counter = Counter()
    hour_counts = [0] * 24
    for message in messages:
        dt = message.time_str
        try:
            hour = int(dt[11:13])
        except (TypeError, ValueError):
            continue
        if 0 <= hour < 24:
            hour_counts[hour] += 1
        if not is_unknown_message(message):
            member_counts[message.sender_uid] += 1

    names = {participant.uid: participant.name for participant in chat.participants()}
    total = len(messages)
    shares = [
        {
            "uid": uid,
            "name": names.get(uid)
            or next((m.sender_name for m in messages if m.sender_uid == uid and m.sender_name), "未知成员"),
            "count": count,
            "share": round(count / total, 4) if total else 0.0,
        }
        for uid, count in member_counts.most_common()
    ]
    return {
        "total_messages": total,
        "active_members": len(member_counts),
        "active_days": len({message.time_str[:10] for message in messages}),
        "avg_daily": round(total / calendar_days, 1) if calendar_days else 0.0,
        "hour_counts": hour_counts,
        "active_hours": sum(count > 0 for count in hour_counts),
        "member_shares": shares,
    }


def _one_period(chat: ChatData, raw_period: dict, all_messages: list) -> dict:
    start, end = _period_dates(raw_period)
    selected = [
        message for message in all_messages if start.isoformat() <= message.time_str[:10] <= end.isoformat()
    ]
    calendar_days = (end - start).days + 1
    result = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "calendar_days": calendar_days,
        "boundary_truncated": _boundary_flags(all_messages, selected),
    }
    if chat.is_group_chat:
        result["metrics"] = _group_metrics(chat, selected, calendar_days)
    else:
        result["metrics"] = _private_metrics(chat, selected, calendar_days)
    return result


def compare_periods(chat: ChatData, period_a: dict, period_b: dict) -> dict:
    """比较两个包含首尾日期的区间；日期按解析器存储的 UTC+8 时间解释。"""
    # Stable sorting preserves the parser's source order for messages with equal timestamps.
    all_messages = sorted(chat.statistical(), key=lambda message: message.timestamp)
    a = _one_period(chat, period_a, all_messages)
    b = _one_period(chat, period_b, all_messages)
    metrics_a, metrics_b = a["metrics"], b["metrics"]
    fields = ["total_messages", "active_days", "avg_daily"]
    if chat.is_group_chat:
        fields.extend(("active_members", "active_hours"))
    else:
        fields.extend(("avg_message_chars", "session_starts"))
    deltas = {field: _compare_value(metrics_a.get(field), metrics_b.get(field)) for field in fields}
    if not chat.is_group_chat:
        deltas["reply_median_seconds"] = {
            side: _compare_value(
                metrics_a["reply_median_seconds"][side]["value"],
                metrics_b["reply_median_seconds"][side]["value"],
            )
            for side in ("self", "other")
        }
    else:
        shares_a = {item["uid"]: item for item in metrics_a["member_shares"]}
        shares_b = {item["uid"]: item for item in metrics_b["member_shares"]}
        deltas["member_shares"] = [
            {
                "uid": uid,
                "name": (shares_b.get(uid) or shares_a[uid])["name"],
                "a": shares_a.get(uid, {}).get("share", 0),
                "b": shares_b.get(uid, {}).get("share", 0),
                "percentage_points": round(
                    (shares_b.get(uid, {}).get("share", 0) - shares_a.get(uid, {}).get("share", 0)) * 100,
                    2,
                ),
            }
            for uid in dict.fromkeys([*shares_a, *shares_b])
        ]
    return {"a": a, "b": b, "deltas": deltas, "mode": "group" if chat.is_group_chat else "private"}
