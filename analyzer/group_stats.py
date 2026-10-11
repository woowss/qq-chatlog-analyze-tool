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
"""群聊本地统计 —— 与私聊统计并存的一条独立轨

三条硬约束（写代码前先看这三条，改动时也别破坏它们）：
1. **不碰私聊口径**：analyzer/local_stats.py 的 13 个 calc_* 一个都不改。私聊的
   统计形状是模板逐字段引用的对外契约（tests/test_group_foundation.py 有冻结用例），
   群聊要新口径就写在这里，而不是给老函数加分支。
2. **口径唯一**：凡是"什么算同一段对话""什么算一次接话"，一律复用
   analyzer.local_stats.is_session_start（阈值 SESSION_GAP_MINUTES）。同一次统计里
   绝不出现第二个间隔阈值——否则报表说 12 段、模型看到 9 段，谁也不知道该信哪个。
3. **对账**：成员条数之和 + 未知条数 == 统计口径总条数。没有 sender_uid 的消息
   （导出器偶有）进"未知"桶而不是被悄悄丢掉：对不上账的统计没人敢用。

复用 local_stats._statistical 是有意为之：它把每条消息的 (日期, 小时, 星期, 月份)
算好并缓存在 chat 对象上，群聊几项统计都要用它。自己再算一遍时区转换 = 两处口径。
"""

from collections import Counter, deque
from datetime import datetime
from typing import Optional

import config
from parser.qq_parser import CST, ChatData
from parser.group_identity import Participant, is_placeholder_sender
from analyzer.local_stats import (
    _statistical,
    is_session_start,
    calc_daily_counts,
    calc_face_stats,
    calc_hourly_distribution,
    calc_milestones,
    calc_message_length_stats,
    calc_overview,
    calc_weekly_activity,
    calc_weekly_distribution,
)

#: 统计口径之外的成员（无 sender_uid 或占位 sender 的消息）在结果里的保留键
UNKNOWN_UID = "__unknown__"
UNKNOWN_NAME = "未知发送者"


def _members(chat: ChatData) -> list[Participant]:
    """参与者名单（已按条数降序、显示名唯一化），与统计口径同源"""
    return chat.participants()


def is_unknown_message(m) -> bool:
    """这条消息是否"没有归属"：无 sender_uid，或发送者是导出器的占位 sender。

    判定必须与 parser.group_identity.collect_participants 完全一致——一处把它算成员、
    另一处算未知，成员条数之和就对不上总条数（对账用例会立刻抓到）。
    这里做成公开函数，供页面与后续维度复用同一口径。
    """
    uid = getattr(m, "sender_uid", "") or ""
    if not uid:
        return True
    return is_placeholder_sender(uid, getattr(m, "sender_name", ""))


def _matrix_top_k() -> int:
    """矩阵成员上限：调用时读 config，便于测试与运行时调整（不在 import 期固化）"""
    return int(getattr(config, "GROUP_MATRIX_MEMBERS", 30) or 30)


INTERACTION_MATRIX_CACHE_LIMIT = 3


def _peak_window_ms() -> int:
    minutes = int(getattr(config, "GROUP_PEAK_WINDOW_MINUTES", 10) or 10)
    return max(1, minutes) * 60 * 1000


def calc_member_activity(chat: ChatData) -> list[dict]:
    """成员活跃度排行（条数降序，与 participants() 同序）。

    字段：uid / name / is_self / msg_count / char_count / avg_chars / active_days /
    avg_per_day（按记录跨度算，与总览的日均口径一致）/ share（占统计口径总条数）/
    first_day / last_day。
    """
    if chat._member_activity_cache is not None:
        return chat._member_activity_cache

    msgs, fields = _statistical(chat)
    total = len(msgs)
    span_days = chat.duration_days or 0
    if not span_days and fields:
        span_days = (
            datetime.strptime(max(f[0] for f in fields), "%Y-%m-%d")
            - datetime.strptime(min(f[0] for f in fields), "%Y-%m-%d")
        ).days + 1
    span_days = span_days or 1
    stat: dict[str, dict] = {}
    for i, m in enumerate(msgs):
        if is_unknown_message(m):
            continue
        uid = m.sender_uid
        entry = stat.get(uid)
        if entry is None:
            entry = stat[uid] = {"count": 0, "chars": 0, "days": set(), "first": None, "last": None}
        entry["count"] += 1
        entry["chars"] += len(m.text or "")
        day = fields[i][0]
        entry["days"].add(day)
        if entry["first"] is None or day < entry["first"]:
            entry["first"] = day
        if entry["last"] is None or day > entry["last"]:
            entry["last"] = day

    out = []
    for p in _members(chat):
        entry = stat.get(p.uid)
        if entry is None:  # 名单来自同一口径，正常不会发生；防御性跳过
            continue
        out.append(
            {
                "uid": p.uid,
                "name": p.name,
                "is_self": p.is_self,
                "msg_count": entry["count"],
                "char_count": entry["chars"],
                "avg_chars": round(entry["chars"] / entry["count"], 1) if entry["count"] else 0.0,
                "active_days": len(entry["days"]),
                "avg_per_day": round(entry["count"] / span_days, 1),
                "share": round(entry["count"] / total, 4) if total else 0.0,
                "first_day": entry["first"] or "",
                "last_day": entry["last"] or "",
            }
        )
    chat._member_activity_cache = out
    return out


def calc_interaction_matrix(chat: ChatData, top_k: Optional[int] = None) -> dict:
    """只在展示边界生成兼容用的密集矩阵。

    JSON 字段与方向保持不变：matrix[行][列] 表示列成员对行成员的动作。None 使用配置的
    展示上限，正数选择对应人数，0 表示请求全员。

    显式请求 0 仍会为兼容性分配多个成员数平方级数组。常规页面使用配置上限；成员画像走
    稀疏摘要接口，不再请求全员密集矩阵。
    """
    limit = _matrix_top_k() if top_k is None else top_k
    cache = chat._matrix_cache
    if cache is None:
        cache = chat._matrix_cache = {}
    cached = cache.get(limit)
    if cached is not None:
        return cached

    sparse = _interaction_counts(chat)
    members = sparse["members"]
    kept = members[:limit] if limit > 0 else members
    index = {p.uid: i for i, p in enumerate(kept)}
    n = len(kept)
    directed = [[0] * n for _ in range(n)]
    explicit = [[0] * n for _ in range(n)]
    mention = [[0] * n for _ in range(n)]

    def materialize(edges: Counter, matrix: list) -> int:
        included = 0
        for (target_uid, actor_uid), count in edges.items():
            i, j = index.get(target_uid), index.get(actor_uid)
            if i is not None and j is not None:
                matrix[i][j] += count
                included += count
        return included

    directed_in_matrix = materialize(sparse["directed"], directed)
    explicit_in_matrix = materialize(sparse["explicit"], explicit)
    mention_in_matrix = materialize(sparse["mention"], mention)
    dropped_replies = sum(sparse["directed"].values()) - directed_in_matrix

    def _symmetrize(matrix: list) -> list:
        """Merge opposite directions while keeping self-reply diagonal values unchanged."""
        return [[matrix[i][j] + matrix[j][i] if i != j else matrix[i][i] for j in range(n)] for i in range(n)]

    undirected = _symmetrize(directed)
    explicit_undirected = _symmetrize(explicit)
    self_replies = sum(explicit[i][i] for i in range(n))

    def _edges(matrix: list) -> list:
        return [
            {"source": i, "target": j, "value": matrix[i][j]}
            for i in range(n)
            for j in range(i + 1, n)
            if matrix[i][j]
        ]

    totals = [
        {
            "uid": kept[i].uid,
            "name": kept[i].name,
            "is_self": kept[i].is_self,
            "replies_to": sum(directed[j][i] for j in range(n)),
            "replied_by": sum(directed[i]),
            "explicit_replies_to": sum(explicit[j][i] for j in range(n)),
            "explicit_replied_by": sum(explicit[i]),
            "mentions_sent": sum(mention[j][i] for j in range(n)),
            "mentions_received": sum(mention[i]),
        }
        for i in range(n)
    ]
    result = {
        "members": [{"uid": p.uid, "name": p.name, "is_self": p.is_self} for p in kept],
        "directed": directed,
        "undirected": undirected,
        "edges": _edges(undirected),
        "explicit_directed": explicit,
        "explicit_undirected": explicit_undirected,
        "explicit_edges": _edges(explicit_undirected),
        "self_replies": self_replies,
        "mention_directed": mention,
        "mentions_all_count": sparse["mentions_all_count"],
        "totals": totals,
        "truncated": len(members) > n,
        "dropped": max(0, len(members) - n),
        "dropped_replies": dropped_replies,
        "matrix_limit": limit,
        "unknown_replies": sparse["unknown_replies"],
        "reply_total": sparse["reply_total"],
        "reply_located": sparse["reply_located"],
        "reply_no_target": sparse["reply_no_target"],
        "reply_resolved": sparse["reply_resolved"],
        "reply_unresolved": sparse["reply_unresolved"],
        "reply_unknown": sparse["reply_unknown"],
        "reply_outside": max(0, sparse["reply_resolved"] - sparse["reply_unknown"] - explicit_in_matrix),
        "mention_total": sparse["mention_total"],
        "mention_unknown": sparse["mention_unknown"],
        "mention_outside": max(0, sparse["mention_total"] - sparse["mention_unknown"] - mention_in_matrix),
    }
    # Dense outputs vary by display limit; retain only a few projections per chat object.
    while len(cache) >= INTERACTION_MATRIX_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    cache[limit] = result
    return result


def _interaction_counts(chat: ChatData) -> dict:
    """Build sparse interaction edges and all-member totals in one linear scan."""
    cached = chat._interaction_sparse_cache
    if cached is not None:
        return cached

    msgs, _fields = _statistical(chat)
    members = _members(chat)
    member_by_uid = {p.uid: p for p in members}
    directed: Counter = Counter()
    explicit: Counter = Counter()
    mention: Counter = Counter()
    unknown_replies = 0
    reply_total = reply_located = reply_no_target = 0
    reply_resolved = reply_unresolved = reply_unknown = 0
    mention_total = mention_unknown = 0
    mentions_all_count = 0

    prev = None
    for m in msgs:
        # Matrix convention: key is (target, actor); the later speaker acts on the previous one.
        if prev is not None and not (
            not is_unknown_message(m) and not is_unknown_message(prev) and m.sender_uid == prev.sender_uid
        ):
            if not is_session_start(prev.timestamp, m.timestamp):
                if is_unknown_message(m) or is_unknown_message(prev):
                    unknown_replies += 1
                else:
                    directed[(prev.sender_uid, m.sender_uid)] += 1
        prev = m

        if m.is_reply or m.reply_to_id:
            reply_total += 1
            if not m.reply_to_id:
                reply_no_target += 1
            else:
                reply_located += 1
                if not m.reply_to_uid:
                    reply_unresolved += 1
                else:
                    reply_resolved += 1
                    if is_unknown_message(m) or is_placeholder_sender(m.reply_to_uid, ""):
                        reply_unknown += 1
                    else:
                        explicit[(m.reply_to_uid, m.sender_uid)] += 1

        if m.mentions_all:
            mentions_all_count += 1
        for target in m.mentions:
            mention_total += 1
            if is_placeholder_sender(target, ""):
                mention_unknown += 1
            else:
                mention[(target, m.sender_uid)] += 1

    totals = {
        p.uid: {
            "uid": p.uid,
            "name": p.name,
            "is_self": p.is_self,
            "replies_to": 0,
            "replied_by": 0,
            "explicit_replies_to": 0,
            "explicit_replied_by": 0,
            "mentions_sent": 0,
            "mentions_received": 0,
        }
        for p in members
    }
    inferred_partners: dict[str, Counter] = {uid: Counter() for uid in totals}
    explicit_partners: dict[str, Counter] = {uid: Counter() for uid in totals}

    for (target_uid, actor_uid), count in directed.items():
        if target_uid in totals and actor_uid in totals:
            totals[actor_uid]["replies_to"] += count
            totals[target_uid]["replied_by"] += count
            if target_uid != actor_uid:
                inferred_partners[target_uid][actor_uid] += count
                inferred_partners[actor_uid][target_uid] += count
    for (target_uid, actor_uid), count in explicit.items():
        if target_uid in totals and actor_uid in totals:
            totals[actor_uid]["explicit_replies_to"] += count
            totals[target_uid]["explicit_replied_by"] += count
            if target_uid != actor_uid:
                explicit_partners[target_uid][actor_uid] += count
                explicit_partners[actor_uid][target_uid] += count
    for (target_uid, actor_uid), count in mention.items():
        if target_uid in totals and actor_uid in totals:
            totals[actor_uid]["mentions_sent"] += count
            totals[target_uid]["mentions_received"] += count

    cached = {
        "members": members,
        "directed": directed,
        "explicit": explicit,
        "mention": mention,
        "unknown_replies": unknown_replies,
        "reply_total": reply_total,
        "reply_located": reply_located,
        "reply_no_target": reply_no_target,
        "reply_resolved": reply_resolved,
        "reply_unresolved": reply_unresolved,
        "reply_unknown": reply_unknown,
        "mention_total": mention_total,
        "mention_unknown": mention_unknown,
        "mentions_all_count": mentions_all_count,
        "member_totals": totals,
        "inferred_partners": inferred_partners,
        "explicit_partners": explicit_partners,
        "member_by_uid": member_by_uid,
    }
    chat._interaction_sparse_cache = cached
    return cached


def calc_member_interaction_summary(chat: ChatData, uid: str) -> dict:
    """Return one member's whole-group counts and main partners without making a matrix."""
    sparse = _interaction_counts(chat)
    result = dict(sparse["member_totals"].get(uid, {}))
    if not result:
        return {
            "uid": uid,
            "replies_to": 0,
            "replied_by": 0,
            "explicit_replies_to": 0,
            "explicit_replied_by": 0,
            "mentions_sent": 0,
            "mentions_received": 0,
            "top_explicit": [],
            "top_inferred": [],
        }

    def top(partners: dict[str, Counter]) -> list[dict]:
        choices = [
            (count, sparse["member_by_uid"][partner_uid].name, partner_uid)
            for partner_uid, count in partners.get(uid, {}).items()
        ]
        choices.sort(reverse=True)
        return [
            {"uid": partner_uid, "name": name, "count": count} for count, name, partner_uid in choices[:3]
        ]

    result["top_explicit"] = top(sparse["explicit_partners"])
    result["top_inferred"] = top(sparse["inferred_partners"])
    return result


def calc_member_hourly(chat: ChatData) -> dict:
    """按成员拆分的 24 小时分布（堆叠面积图/热力图用）。

    返回 {"hours": [0..23], "series": [{uid, name, is_self, counts: [24]}], "unknown": [24]}
    """
    msgs, fields = _statistical(chat)
    series: dict[str, list] = {}
    unknown = [0] * 24
    for i, m in enumerate(msgs):
        hour = fields[i][1]
        if is_unknown_message(m):
            unknown[hour] += 1
            continue
        uid = m.sender_uid
        counts = series.get(uid)
        if counts is None:
            counts = series[uid] = [0] * 24
        counts[hour] += 1
    return {
        "hours": list(range(24)),
        "series": [
            {"uid": p.uid, "name": p.name, "is_self": p.is_self, "counts": series.get(p.uid) or [0] * 24}
            for p in _members(chat)
        ],
        "unknown": unknown,
    }


def _peak_concurrent(msgs: list) -> dict:
    """同时在线高峰：滑动窗口内出现过的不同发言者数的最大值。

    只是"同时有人说话"的近似（聊天不是实时的在线状态），窗口见
    config.GROUP_PEAK_WINDOW_MINUTES。用 deque + Counter 单次遍历，O(N)。
    """
    window = _peak_window_ms()
    q: deque = deque()
    live: Counter = Counter()
    best = 0
    best_ts = 0
    for m in msgs:
        if is_unknown_message(m):
            continue
        uid = m.sender_uid
        q.append((m.timestamp, uid))
        live[uid] += 1
        while q and m.timestamp - q[0][0] > window:
            old_ts, old_uid = q.popleft()
            live[old_uid] -= 1
            if live[old_uid] <= 0:
                del live[old_uid]
        if len(live) > best:
            best = len(live)
            best_ts = m.timestamp
    return {
        "count": best,
        "at": datetime.fromtimestamp(best_ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M") if best_ts else "",
        "window_minutes": window // 60000,
    }


def calc_group_overview(chat: ChatData, activity: list | None = None) -> dict:
    """群聊总览：在私聊总览的基础上补群聊字段。

    - self_count / other_count 语义变成"我 / 其他所有成员合计"（不再有单一"对方"），
      因此 other_name 置空、改给一个稳定的 other_label，避免模板把群名当成某个人；
    - exchange_rounds 在群里会被连珠炮放大，语义已变，群聊不给这个字段（置 None），
      需要轮次概念的地方用 daily_counts / member_activity 替代；
    - unknown_messages：没有 sender_uid 的统计口径消息数（对账用）。

    activity 可由调用方传入（compute_group_stats 已经算过一次），避免同一份消息
    为了一个字段多遍历一遍。
    """
    msgs, _fields = _statistical(chat)
    overview = calc_overview(chat)
    members = _members(chat)
    if activity is None:
        activity = calc_member_activity(chat)
    unknown = sum(1 for m in msgs if is_unknown_message(m))
    top = activity[0] if activity else None
    overview.update(
        {
            "is_group": True,
            "group_name": chat.chat_name,
            "other_name": "",
            "other_label": "其他成员",
            "member_count": len(members),
            "member_names": [p.name for p in members],
            "most_active_member": (
                {"uid": top["uid"], "name": top["name"], "msg_count": top["msg_count"]} if top else None
            ),
            "peak_concurrent": _peak_concurrent(msgs),
            "unknown_messages": unknown,
            "exchange_rounds": None,  # 群聊口径下不成立，明确置空而不是给个误导性的数字
        }
    )
    return overview


def calc_group_milestones(chat: ChatData) -> dict:
    """群聊里程碑：复用私聊里程碑，另加三条只有多人才成立的指标。

    mutual_nights 在群聊里的含义是"我与至少一位成员共同熬夜（凌晨 2-5 点）的天数"
    ——字段名不变、口径如注释所述；群聊专属指标单列，不改动私聊函数的返回值。
    """
    msgs, fields = _statistical(chat)
    milestones = calc_milestones(chat)
    if not msgs:
        return milestones
    day_members: dict[str, set] = {}
    for i, m in enumerate(msgs):
        if is_unknown_message(m):
            continue
        day_members.setdefault(fields[i][0], set()).add(m.sender_uid)
    peak_day, peak_members = ("", set())
    if day_members:
        peak_day, peak_members = max(day_members.items(), key=lambda kv: len(kv[1]))
    milestones.update(
        {
            "peak_day_members": len(peak_members),
            "peak_day_members_date": peak_day,
            "multi_member_days": sum(1 for v in day_members.values() if len(v) >= 2),
            "mutual_nights_note": "群聊口径：我与至少一位成员共同熬夜的天数",
        }
    )
    return milestones


def compute_group_stats(chat: ChatData) -> dict:
    """群聊本地统计的全部结果（与私聊 compute_stats 互斥，由 mode 决定走哪条）。

    刻意**不包含** response_time：私聊那套"对方发来→我回复"的间隔在群里没有对应
    语义（谁回复谁要成对看），群聊用互动矩阵与后续的回复网络表达，避免给出一个
    看着像结论、其实无从解释的平均值。

    daily_counts / hourly_dist / weekly_dist / length_stats / face_stats 复用私聊函数：
    它们的形状是"我 vs 其他所有人"，在多人群里仍然成立且口径不变，前端图表可直接复用；
    按成员拆分的版本由 member_activity / member_hourly / interaction 提供。
    """
    activity = calc_member_activity(chat)
    overview = calc_group_overview(chat, activity=activity)
    return {
        "mode": "group",
        "overview": overview,
        "member_activity": activity,
        "interaction": calc_interaction_matrix(chat),
        "member_hourly": calc_member_hourly(chat),
        "daily_counts": calc_daily_counts(chat),
        "hourly_dist": calc_hourly_distribution(chat),
        "weekly_dist": calc_weekly_distribution(chat),
        "weekly_activity": calc_weekly_activity(chat),
        "length_stats": calc_message_length_stats(chat),
        "face_stats": calc_face_stats(chat),
        "exchange_rounds": overview["exchange_rounds"],
        "milestones": calc_group_milestones(chat),
    }
