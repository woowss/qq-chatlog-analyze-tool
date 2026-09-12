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


def _peak_window_ms() -> int:
    minutes = int(getattr(config, "GROUP_PEAK_WINDOW_MINUTES", 10) or 10)
    return max(1, minutes) * 60 * 1000


def calc_member_activity(chat: ChatData) -> list[dict]:
    """成员活跃度排行（条数降序，与 participants() 同序）。

    字段：uid / name / is_self / msg_count / char_count / avg_chars / active_days /
    avg_per_day（按记录跨度算，与总览的日均口径一致）/ share（占统计口径总条数）/
    first_day / last_day。
    """
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
    return out


def calc_interaction_matrix(chat: ChatData, top_k: int = 0) -> dict:
    """互动矩阵：谁回应了谁。**推断的"接话"与精确的"回复/@点名"分开统计**。

    三条信号的可信度完全不同，混在一起会得出似是而非的结论，所以各占一个矩阵：
    1. directed（接话，推断）：相邻两条消息、换人、且在同一段对话内。口径是
       is_session_start（间隔超过 SESSION_GAP_MINUTES 算新的一段）——新段的第一条不是
       "接话"而是新话题的开口，算成回复会让所有跨时段发言都变成一次互动。
    2. explicit_directed（回复，精确）：新版导出器的 reply 元素带被回复消息的 id，
       回查得到发言人（实测绝大多数都能回查到发言人）。这是"真的按了回复"，比相邻推断硬。
    3. mention_directed（@点名，精确）：at 元素带 uid（实测导出里全部带）。
       @全体成员（atType=1）不是点名某个成员，只计入 mentions_all，绝不摊到人头上。

    **三个矩阵使用同一约定**：`X[i][j]` 表示"j 对 i 的一次动作"（j 接 i 的话 /
    j 回复 i / j 点名 i）。于是第 i 列之和 = i 对别人动作的次数，第 i 行之和 = 别人对
    i 动作的次数——热力图的行列读法、totals 的字段都由这条约定决定，不能各写各的。

    返回结构（前端热力图与力导向图直接可用）：
    - members: [{uid, name, is_self}]，矩阵的轴（已按发言量截断到 top_k）
    - directed / undirected / edges：接话（推断）
    - explicit_directed / explicit_undirected / explicit_edges：回复（精确）
    - mention_directed / mentions_all_count：@点名（精确）
    - totals: [{uid, replies_to, replied_by, explicit_replies_to, explicit_replied_by,
      mentions_sent, mentions_received}]
    - dropped / dropped_replies：因 top_k 截断而未进矩阵的成员数与互动数
    - unknown_replies：一侧无归属（无 uid 或占位 sender）的接话次数
    - reply_total / reply_located / reply_no_target / reply_resolved / reply_unresolved /
      reply_unknown / reply_outside：回复信号的账目（举例：120 条回复标记 =
      118 条可定位 + 2 条原消息已删除）；mention_total / mention_unknown / mention_outside
      同理。任何一条没被计入的互动都是"悄悄消失的数据"，宁可单列也不让它凭空不见。
    """
    msgs, _fields = _statistical(chat)
    limit = top_k or _matrix_top_k()
    members = _members(chat)
    kept = members[:limit] if limit > 0 else members
    index = {p.uid: i for i, p in enumerate(kept)}
    n = len(kept)
    directed = [[0] * n for _ in range(n)]
    explicit = [[0] * n for _ in range(n)]
    mention = [[0] * n for _ in range(n)]
    unknown_replies = 0
    dropped_replies = 0
    reply_total = reply_located = reply_no_target = 0
    reply_resolved = reply_unresolved = reply_unknown = reply_outside = 0
    mention_total = mention_unknown = mention_outside = 0
    mentions_all_count = 0

    prev = None
    for m in msgs:
        # —— 1) 接话（推断）——
        if prev is not None and not (
            not is_unknown_message(m) and not is_unknown_message(prev) and m.sender_uid == prev.sender_uid
        ):
            if not is_session_start(prev.timestamp, m.timestamp):
                if is_unknown_message(m) or is_unknown_message(prev):
                    unknown_replies += 1
                else:
                    i, j = index.get(prev.sender_uid), index.get(m.sender_uid)
                    if i is not None and j is not None:
                        directed[i][j] += 1
                    else:
                        # 至少一侧在矩阵之外（被 top_k 截断）：不硬塞进矩阵，但要记账
                        dropped_replies += 1
        prev = m

        # —— 2) 回复（精确）——
        # 账目必须闭合（举例：120 条回复标记 = 118 条有引用 id + 2 条原消息已删除）：
        #   reply_total = reply_located + reply_no_target
        #   reply_located = reply_resolved + reply_unresolved
        #   reply_resolved = 进矩阵的 + reply_unknown + reply_outside
        # 任何一条没被计入的回复都是"悄悄消失的数据"，宁可单列也不让它凭空不见。
        # 判定取或：解析器在有 reply 元素时总会置 is_reply（"引用已删除"那条走的正是
        # is_reply=True + reply_to_id 为空的分支）；反过来，若将来有调用方只填了
        # reply_to_id 而没置 is_reply，也不该被漏掉——少算一条回复就是悄悄丢数据。
        if m.is_reply or m.reply_to_id:
            reply_total += 1
            if not m.reply_to_id:
                # 有回复标记但没有可定位的引用（原消息已删除，导出器给 null）
                reply_no_target += 1
            else:
                reply_located += 1
                if not m.reply_to_uid:
                    # 引用的消息不在本次导出里：无法归属，如实单列
                    reply_unresolved += 1
                else:
                    reply_resolved += 1
                    if is_unknown_message(m) or is_placeholder_sender(m.reply_to_uid, ""):
                        reply_unknown += 1
                    else:
                        i, j = index.get(m.reply_to_uid), index.get(m.sender_uid)
                        if i is not None and j is not None:
                            explicit[i][j] += 1
                        else:
                            reply_outside += 1

        # —— 3) @点名（精确）——
        if m.mentions_all:
            mentions_all_count += 1
        for target in m.mentions:
            mention_total += 1
            if is_placeholder_sender(target, ""):
                mention_unknown += 1
                continue
            i, j = index.get(target), index.get(m.sender_uid)
            if i is not None and j is not None:
                mention[i][j] += 1
            else:
                # 点名对象在矩阵之外（被 top_k 截断）：不硬塞，但要记账
                mention_outside += 1

    undirected = [[directed[i][j] + directed[j][i] for j in range(n)] for i in range(n)]
    explicit_undirected = [[explicit[i][j] + explicit[j][i] for j in range(n)] for i in range(n)]

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
            # 列和 = 我对别人动作的次数；行和 = 别人对我动作的次数
            "replies_to": sum(directed[j][i] for j in range(n)),
            "replied_by": sum(directed[i]),
            "explicit_replies_to": sum(explicit[j][i] for j in range(n)),
            "explicit_replied_by": sum(explicit[i]),
            "mentions_sent": sum(mention[j][i] for j in range(n)),
            "mentions_received": sum(mention[i]),
        }
        for i in range(n)
    ]
    return {
        "members": [{"uid": p.uid, "name": p.name, "is_self": p.is_self} for p in kept],
        "directed": directed,
        "undirected": undirected,
        "edges": _edges(undirected),
        "explicit_directed": explicit,
        "explicit_undirected": explicit_undirected,
        "explicit_edges": _edges(explicit_undirected),
        "mention_directed": mention,
        "mentions_all_count": mentions_all_count,
        "totals": totals,
        "truncated": len(members) > n,
        "dropped": max(0, len(members) - n),
        "dropped_replies": dropped_replies,
        "matrix_limit": limit,
        "unknown_replies": unknown_replies,
        # 回复信号的覆盖率与归属情况：无法归属的部分如实单列，不猜也不丢
        # reply_total = reply_located + reply_no_target
        # reply_located = reply_resolved + reply_unresolved
        # reply_resolved = （进矩阵的）+ reply_unknown + reply_outside
        "reply_total": reply_total,
        "reply_located": reply_located,
        "reply_no_target": reply_no_target,
        "reply_resolved": reply_resolved,
        "reply_unresolved": reply_unresolved,
        "reply_unknown": reply_unknown,
        "reply_outside": reply_outside,
        # @点名的账目同样闭合：mention_total = （进矩阵的）+ mention_unknown + mention_outside
        "mention_total": mention_total,
        "mention_unknown": mention_unknown,
        "mention_outside": mention_outside,
    }


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
