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

"""Build a sanitized, static share copy without touching source caches."""

import re
from collections import Counter, defaultdict

from parser.qq_parser import ChatData


OPTION_KEYS = {
    "hide_nicknames",
    "hide_accounts",
    "hide_original_text",
    "hide_exact_dates",
    "hide_statistics",
}


def normalize_share_options(raw: dict | None) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - OPTION_KEYS:
        raise ValueError("脱敏选项格式无效")
    if any(not isinstance(value, bool) for value in raw.values()):
        raise ValueError("脱敏选项必须为布尔值")
    return {key: raw.get(key, True) for key in OPTION_KEYS}


def _alpha_alias(index: int) -> str:
    """Stable letter pseudonyms avoid numeric labels when counts are hidden."""
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return f"成员 {label}"


def _month_label(value: str) -> str:
    match = re.match(r"^(\d{4}-\d{2})-\d{2}", str(value or ""))
    return match.group(1) if match else "未知月份"


def _redact_dates(text: str) -> str:
    text = re.sub(
        r"(?<!\d)(\d{4})[-/](\d{1,2})[-/]\d{1,2}(?:[ Tt]\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)",
        lambda match: f"{match.group(1)}-{int(match.group(2)):02d}",
        text,
    )
    text = re.sub(r"(?<!\d)(\d{4})年(\d{1,2})月\d{1,2}[日号]", r"\1年\2月", text)
    text = re.sub(r"(?<!\d)(\d{1,2})月\d{1,2}[日号]", r"\1月", text)
    text = re.sub(
        r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2,4})(?!\d)",
        lambda match: f"{match.group(3)}-{int(match.group(1)):02d}",
        text,
    )
    return re.sub(
        r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)",
        lambda match: f"{int(match.group(1))}月",
        text,
    )


def _make_identity_maps(chat: ChatData, hide_names: bool):
    participants = sorted((p for p in chat.participants() if p.uid), key=lambda item: item.uid)
    aliases = {participant.uid: _alpha_alias(index) for index, participant in enumerate(participants)}
    all_uids = set(aliases)
    names_to_uids: dict[str, set[str]] = defaultdict(set)
    for message in chat.messages:
        if message.sender_uid and message.sender_name:
            uid = str(message.sender_uid)
            all_uids.add(uid)
            names_to_uids[message.sender_name.strip()].add(uid)
        elif message.sender_uid:
            all_uids.add(str(message.sender_uid))
        elif message.sender_name:
            names_to_uids[message.sender_name.strip()].add("")
    for participant in participants:
        if participant.name:
            names_to_uids[participant.name.strip()].add(participant.uid)
    for uid, name in ((chat.self_uid, chat.self_name), (chat.other_uid, chat.other_name)):
        if name:
            names_to_uids[name.strip()].add(uid or "")
    anonymous_aliases = {
        name: _alpha_alias(len(aliases) + index)
        for index, name in enumerate(sorted(name for name, uids in names_to_uids.items() if "" in uids))
    }

    def label(uid: str, name: str = "") -> str:
        if hide_names:
            return aliases.get(uid, "未知成员") if uid else anonymous_aliases.get(name.strip(), "未知成员")
        if name:
            return name
        return "未知成员" if not uid else "成员"

    return aliases, names_to_uids, all_uids, label


def _clean_original(
    text: str, options: dict, aliases: dict, names_to_uids: dict[str, set[str]], all_uids: set[str]
) -> str:
    value = str(text or "")
    if options["hide_accounts"]:
        # IDs never enter structured report fields. In opted-in excerpts, remove both known
        # account IDs and long numeric runs commonly used as QQ/account identifiers.
        for uid in sorted(all_uids, key=len, reverse=True):
            if uid:
                value = value.replace(uid, "[账号已隐藏]")
        value = re.sub(r"(?<!\d)\d{5,15}(?!\d)", "[账号已隐藏]", value)
    if options["hide_nicknames"]:
        replacements = []
        for name, uids in names_to_uids.items():
            if name:
                replacement = aliases.get(next(iter(uids)), "成员") if len(uids) == 1 else "[成员名已隐藏]"
                replacements.append((name, replacement))
        for name, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            value = value.replace(name, replacement)
    if options["hide_exact_dates"]:
        value = _redact_dates(value)
    return value


def _activity_buckets(stats: dict, hide_dates: bool) -> list[dict]:
    counts: Counter = Counter()
    for row in stats.get("daily_counts") or []:
        label = _month_label(row.get("date", "")) if hide_dates else str(row.get("date", ""))
        if not label:
            continue
        counts[label] += int(row.get("self", 0) or 0) + int(row.get("other", 0) or 0)
    maximum = max(counts.values(), default=0)
    return [
        {"label": label, "count": count, "width": round(count / maximum * 100, 1) if maximum else 0}
        for label, count in sorted(counts.items())
    ]


def _hourly_buckets(stats: dict) -> list[dict]:
    rows = []
    for item in stats.get("hourly_dist") or []:
        hour = int(item.get("hour", 0) or 0)
        count = int(item.get("self", 0) or 0) + int(item.get("other", 0) or 0)
        rows.append({"label": f"{hour:02d}:00", "count": count})
    maximum = max((row["count"] for row in rows), default=0)
    for row in rows:
        row["width"] = round(row["count"] / maximum * 100, 1) if maximum else 0
    return rows


def _member_rows(stats: dict, label) -> list[dict]:
    return [
        {
            "name": label(str(member.get("uid") or ""), str(member.get("name") or "")),
            "messages": int(member.get("msg_count", 0) or 0),
            "share": round(float(member.get("share", 0) or 0) * 100, 1),
            "avg_chars": member.get("avg_chars", 0),
            "active_days": int(member.get("active_days", 0) or 0),
        }
        for member in (stats.get("member_activity") or [])
    ]


def _interaction_model(stats: dict, label) -> tuple[list[str], list[list[int]], list[dict], bool]:
    interaction = stats.get("interaction") or {}
    members = interaction.get("members") or []
    names = [label(str(member.get("uid") or ""), str(member.get("name") or "")) for member in members]
    matrix = interaction.get("directed") or []
    rows = []
    for edge in interaction.get("explicit_edges") or []:
        source, target = edge.get("source"), edge.get("target")
        if (
            isinstance(source, int)
            and isinstance(target, int)
            and source < len(names)
            and target < len(names)
        ):
            rows.append({"from": names[source], "to": names[target], "count": int(edge.get("value", 0) or 0)})
    return names, matrix, rows, bool(interaction.get("truncated"))


def build_share_model(
    chat: ChatData, stats: dict, raw_options: dict | None = None
) -> tuple[dict, list[str], dict]:
    options = normalize_share_options(raw_options)
    aliases, names_to_uids, all_uids, label = _make_identity_maps(chat, options["hide_nicknames"])
    overview = stats.get("overview") or {}
    group = bool(chat.is_group_chat)
    warnings = [
        "AI 自由文本、人物画像段落和证据引用已省略：这些段落可能混有无法可靠识别的"
        "昵称、原文、账号或精确日期。",
        "词频词项和表情名称未放入分享副本，避免把聊天原词误作普通统计标签。",
    ]
    if options["hide_accounts"]:
        warnings.append("账号脱敏会清理成员 UID 和连续 5–15 位数字；其他格式请在预览中检查。")
    if options["hide_exact_dates"]:
        warnings.append("日期脱敏会转换常见日期格式；原文中的不规则写法请在预览中检查。")
    if not options["hide_statistics"]:
        if group:
            metrics = [
                {"label": "总消息数", "value": overview.get("total_messages", 0)},
                {"label": "发言成员", "value": overview.get("member_count", 0)},
                {"label": "记录天数", "value": overview.get("total_days", 0)},
                {"label": "活跃天数", "value": overview.get("active_days", 0)},
                {"label": "日均消息", "value": overview.get("avg_daily", 0)},
            ]
            metrics.extend(
                {"label": name, "value": overview.get(key, 0)}
                for key, name in (
                    ("total_images", "图片数"),
                    ("total_files", "文件数"),
                    ("total_videos", "视频数"),
                    ("total_faces", "表情数"),
                )
            )
        else:
            self_label = label(chat.self_uid, overview.get("self_name", chat.self_name))
            other_label = label(chat.other_uid, overview.get("other_name", chat.other_name))
            metrics = [
                {"label": "总消息数", "value": overview.get("total_messages", 0)},
                {"label": "记录天数", "value": overview.get("total_days", 0)},
                {"label": "活跃天数", "value": overview.get("active_days", 0)},
                {"label": "日均消息", "value": overview.get("avg_daily", 0)},
                {"label": f"{self_label}发言数", "value": overview.get("self_count", 0)},
                {"label": f"{other_label}发言数", "value": overview.get("other_count", 0)},
                {"label": f"{self_label}字数", "value": overview.get("self_chars", 0)},
                {"label": f"{other_label}字数", "value": overview.get("other_chars", 0)},
            ]
            response = stats.get("response_time") or {}
            if response:
                metrics.extend(
                    {
                        "label": f"{who}回复中位数（秒）",
                        "value": (response.get(side) or {}).get("p50", "样本不足")
                        if (response.get(side) or {}).get("count", 0)
                        else "样本不足",
                    }
                    for side, who in (("self", self_label), ("other", other_label))
                )
        activity = _activity_buckets(stats, options["hide_exact_dates"])
        hourly = _hourly_buckets(stats)
        members = _member_rows(stats, label) if group else []
        member_names, interaction_matrix, explicit_edges, interaction_truncated = (
            _interaction_model(stats, label) if group else ([], [], [], False)
        )
    else:
        metrics, activity, hourly, members = [], [], [], []
        member_names, interaction_matrix, explicit_edges, interaction_truncated = [], [], [], False
        warnings.append("统计数字及其图表、表格和矩阵已一并移除。")

    original_messages = []
    if not options["hide_original_text"]:
        for message in chat.statistical()[:20]:
            body = message.raw_text or message.text or ""
            if not body:
                continue
            original_messages.append(
                {
                    "time": _month_label(message.time_str)
                    if options["hide_exact_dates"]
                    else message.time_str,
                    "sender": label(message.sender_uid, message.sender_name),
                    "text": _clean_original(body, options, aliases, names_to_uids, all_uids),
                }
            )
        warnings.append("已按你的选择纳入最多 20 条原始消息；请逐条检查预览内容。")

    milestones = stats.get("milestones") or {}
    if options["hide_exact_dates"]:
        period_label = "日期已按月份呈现"
    else:
        start = str(overview.get("first_day") or milestones.get("first_day") or "")
        end = str(overview.get("last_day") or milestones.get("last_day") or "")
        period_label = f"记录期间：{start} 至 {end}" if start and end else "记录期间"

    model = {
        "group": group,
        "hide_statistics": options["hide_statistics"],
        "include_original_text": not options["hide_original_text"],
        "period_label": period_label,
        "metrics": metrics,
        "activity": activity,
        "activity_granularity": "按月" if options["hide_exact_dates"] else "按日",
        "hourly": hourly,
        "members": members,
        "interaction_names": member_names,
        "interaction_matrix": interaction_matrix,
        "explicit_edges": explicit_edges,
        "interaction_truncated": interaction_truncated,
        "original_messages": original_messages,
    }
    return model, warnings, options


def render_share_html(
    chat: ChatData, stats: dict, raw_options: dict | None = None
) -> tuple[str, list[str], dict]:
    from flask import render_template

    model, warnings, options = build_share_model(chat, stats, raw_options)
    html = render_template("share_report_copy.html", report=model)
    return html, warnings, options
