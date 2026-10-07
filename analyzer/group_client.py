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
"""群聊 AI 分析：对话构建、成员感知抽样、4 个维度的执行体

与私聊的分工（**不要越界**）：
- 复用 deepseek_client 的既有零件：_analyze_periods（逐月并发/取消/月份缓存）、
  _call_api（限流/配额/思考模式）、_message_line（对话行格式）、_fit_lines（兜底截断）、
  _vision_digest（图片摘要）。这些都是纯函数或参数化的执行器，复用它们才不会出现
  "私聊一套节奏、群聊另一套节奏"。
- **不改**私聊提示词、不改 _build_dialog / _message_line 等进私聊指纹的函数源码：
  群聊自己的对话构建与抽样写在这里，并只进 group_prompt_fingerprint。
- 群聊的月份缓存与维度缓存都走群聊自己的指纹（见 group_prompt_fingerprint），
  因此新增/修改群聊提示词**不会**让任何私聊缓存失效。

群聊特有的两处设计（都是被真实数据逼出来的）：
1. **成员感知抽样**：等间隔抽样在 50 人以上的群里会把低频成员整段丢掉，模型于是把
   他们读成"缺席/潜水"——恰好污染 lurker_ratio 这类结论。这里先给每位成员保底条数，
   再按发言占比分配预算，最后在原时间顺序上还原。
2. **成员画像必须带群上下文**：只喂一个人自己的发言，模型无法判断他是"捧哏王"还是
   "话题主导者"（两者的定义都依赖看到别人）。所以每个成员的样本前会附上本地精确算出的
   互动数字（被回复/被@/回复了谁/主要互动对象），并明确区分"事实"与"推断"。
"""

import hashlib
import inspect
import os
from bisect import bisect_left
from collections import deque
from dataclasses import replace
from typing import Callable, Optional

from config import GROUP_AI_MAX_MEMBERS, env_number
from parser.qq_parser import ChatData, is_statistical
from parser.group_identity import Participant
from analyzer import group_prompts as gp
from analyzer.deepseek_client import (
    AnalysisIncompleteError,
    MAX_TOKENS_BY_DIM,
    _PromptText,
    QuotaExhaustedError,
    ResultValidationError,
    _analyze_periods,
    _call_api,
    _fit_lines,
    _gap_mark,
    _has_content,
    _hashed_source,
    _message_line,
    month_cache_enabled,
    validate_result,
    _short_time,
    _truncate_dialog_line,
    _vision_digest,
    logger,
)
from analyzer.group_stats import calc_interaction_matrix, calc_member_activity, is_unknown_message
from analyzer.logger import mask_name

#: 单个群聊月的对话文本上限（字符）。默认与私聊同档（准确性优先），
#: 想省钱可在 .env 里调小 LLM_GROUP_MAX_DIALOG_CHARS。
GROUP_MAX_DIALOG_CHARS = int(env_number("LLM_GROUP_MAX_DIALOG_CHARS", 600_000, 1000, 2_000_000))
#: 成员感知抽样时每位成员的保底条数（防止低频成员被整段丢掉）
GROUP_MEMBER_MIN_LINES = int(env_number("LLM_GROUP_MEMBER_MIN_LINES", 20, 1, 500))
#: 成员画像的单人样本上限（与私聊锐评同档：按时间均匀抽样覆盖整个时段）
MEMBER_PROFILE_SAMPLES = 800
#: 单条 prompt 里展示的互动"Top 对"数量（三张矩阵各取前 N 对，太长反而淹没重点）
INTERACTION_DIGEST_PAIRS = 8

_GROUP_COMPAT_FINGERPRINT = "30c7d6356e3a"
# Keep the historical formula behind a literal source check.  Recomputing
# this digest at import time would make a formatter change look compatible again.
_GROUP_COMPAT_FORMAT_DIGEST = "91ebeee1c18355a095ebf21eef21b9ab0083fa441dac87c1e4bcde9e1ee2fd3e"


def select_ai_members(chat: ChatData, limit: int = 0) -> list[Participant]:
    """成员画像要分析哪些人：按发言量取前 K 位，**自己一定在里面**。

    "我"如果这个月发言很少，纯按条数排序会把我挤出名单——而用户最想看的往往就是
    "我在群里是什么角色"。所以自己始终入选（名额满时替换掉末位那位）。
    """
    k = limit or GROUP_AI_MAX_MEMBERS
    people = chat.participants()
    if not people:
        return []
    chosen = people[:k]
    me = next((p for p in people if p.is_self), None)
    if me is not None and me not in chosen:
        if len(chosen) >= k:
            chosen = chosen[:-1]
        chosen = chosen + [me]
    return chosen


def _month_messages(chat: ChatData, msgs: list) -> list:
    """该月的有效消息（有内容 + 统计口径）"""
    return [m for m in msgs if _has_content(m) and is_statistical(m)]


def _peak_concurrent(msgs: list, window_ms: int) -> int:
    """本月同时在聊的高峰人数（与 group_stats 同口径：滑动窗口内不同发言者数）"""
    from collections import Counter, deque

    q: deque = deque()
    live: Counter = Counter()
    best = 0
    for m in msgs:
        if is_unknown_message(m):
            continue
        q.append((m.timestamp, m.sender_uid))
        live[m.sender_uid] += 1
        while q and m.timestamp - q[0][0] > window_ms:
            _ts, uid = q.popleft()
            live[uid] -= 1
            if live[uid] <= 0:
                del live[uid]
        best = max(best, len(live))
    return best


def _interaction_digest(msgs: list, name_of: Callable[[str], str]) -> str:
    """本月互动的"Top 对"摘要：三张矩阵（推断接话 / 精确回复 / @点名）各取前几对。

    为什么不把整张矩阵塞进 prompt：几十人的矩阵就是数千个数字，既贵又淹重点。
    这里只给最强的几对，并在文字里标明哪些是事实、哪些是推断。
    """
    from collections import Counter
    from analyzer.local_stats import is_session_start

    inferred: Counter = Counter()
    explicit: Counter = Counter()
    mention: Counter = Counter()
    prev = None
    for m in msgs:
        if prev is not None and not (
            not is_unknown_message(m) and not is_unknown_message(prev) and m.sender_uid == prev.sender_uid
        ):
            if not is_session_start(prev.timestamp, m.timestamp) and not (
                is_unknown_message(m) or is_unknown_message(prev)
            ):
                inferred[(prev.sender_uid, m.sender_uid)] += 1  # (被接话的人, 接话的人)
        prev = m
        if m.reply_to_uid and not is_unknown_message(m):
            explicit[(m.reply_to_uid, m.sender_uid)] += 1
        for target in m.mentions:
            mention[(target, m.sender_uid)] += 1

    def _fmt(counter: Counter, sep: str, resolve) -> str:
        pairs = [
            f"{resolve(a)} {sep} {resolve(b)} 共 {n} 次"
            for (a, b), n in counter.most_common(INTERACTION_DIGEST_PAIRS)
        ]
        return "、".join(pairs) if pairs else "无"

    return (
        f"- 精确回复（事实）：{_fmt(explicit, '被', name_of)}\n"
        f"- @点名（事实）：{_fmt(mention, '被', name_of)}\n"
        f"- 接话（推断，相邻换人且间隔<30 分钟）：{_fmt(inferred, '被', name_of)}"
    )


# 注意：_member_facts / build_group_dialog 在 GROUP_PROMPT_FINGERPRINT 的哈希集合里，
# 而 *_LEGACY 指纹按**原始源码**哈希——函数体内连注释都不许动（提交前复核在这里加过
# 说明注释，把 GROUP_PROMPT_FINGERPRINT_LEGACY 挤离钉值、被 CI 拦下；已知口径分歧的
# 说明因此只能挂在这里——函数体外）。
# 已知口径分歧（故意保留）：_member_facts 的图片数按"含图消息条数"，仪表盘按张。
# 一条消息连发多张图时两个数字不一致。不在函数内改成 image_count 的原因：动它 = 群聊
# 指纹换代，而迁移链只认"当前 + 一代旧"两代键，换代即让所有既有群聊月份/维度缓存
# 不可达（LEGACY 是历史值、无法跟着重算，全体群聊用户重新付费）。想统一必须先扩
# 多代迁移链，见 docs/reviews/2026-09-26-round6-precommit-review.md P1-3。
def _member_facts(msgs: list, chat: ChatData) -> tuple[str, list[tuple[str, int]]]:
    """本月群级事实（统计头用）与成员条数排行"""
    from collections import Counter
    from datetime import datetime

    from parser.qq_parser import CST

    counts: Counter = Counter()
    hours: Counter = Counter()
    images = faces = 0
    replies = mentions = mentions_all = 0
    for m in msgs:
        if not is_unknown_message(m):
            counts[m.sender_uid] += 1
        hours[datetime.fromtimestamp(m.timestamp / 1000, tz=CST).hour] += 1
        images += 1 if m.has_image else 0
        faces += len(m.face_names) or len(m.face_ids)
        replies += 1 if m.reply_to_uid else 0
        mentions += len(m.mentions)
        mentions_all += 1 if m.mentions_all else 0
    ranked = counts.most_common()
    return (
        f"共 {len(msgs)} 条消息（发言 {len(ranked)} 人 / 群成员 {len(chat.participants())} 位，"
        f"图片 {images} 张、表情 {faces} 个，精确回复 {replies} 条、@点名 {mentions} 次"
        f"（含 @全体 {mentions_all} 条））",
        ranked,
    )


def _entry_line(entry) -> str:
    """取成员配额计算用的基准行（兼容旧的三元组测试夹具）。"""
    if len(entry) > 3 and isinstance(entry[3], str):
        return entry[3]
    return entry[2] if isinstance(entry[2], str) else ""


def _trim_group_entries(entries: list, render, max_chars: int) -> tuple[list, list[str]]:
    """最后一道预算闸门：删消息或截一条消息，但始终用完整上下文重绘。"""
    if max_chars <= 0:
        return [], []
    current = list(entries)
    while current:
        lines = render(current)
        if sum(len(line) + 1 for line in lines) <= max_chars:
            return current, lines
        if len(current) == 1:
            return current, [_truncate_dialog_line(lines[0], max(0, max_chars - 1))]
        kept: list = []
        used = 0
        for entry, line in reversed(list(zip(current, lines, strict=True))):
            if used + len(line) + 1 <= max_chars:
                kept.append(entry)
                used += len(line) + 1
            elif not kept:
                kept.append(entry)
                break
        current = list(reversed(kept))
    return [], []


def _fit_group_entries(entries: list, max_chars: int, render) -> tuple[list, list[str], bool]:
    """按成员配额选择消息，再根据最终保留消息重建发言人上下文。"""
    if not entries:
        return [], [], False
    if max_chars <= 0:
        return [], [], True
    full_lines = render(entries)
    total_chars = sum(len(line) + 1 for line in full_lines)
    if total_chars <= max_chars:
        return entries, full_lines, False

    by_member: dict[str, list] = {}
    for entry in entries:
        by_member.setdefault(entry[1], []).append(entry)
    # A stride always selects a member's first row. If that row alone exceeds
    # the budget, shrinking quotas cannot make it fit and eventually loses the
    # entire member. Prefer its ordinary rows; retain oversized rows only when
    # the member has no alternative so the final gate can truncate one.
    for uid, items in by_member.items():
        fitting = [entry for entry in items if len(_entry_line(entry)) + 1 <= max_chars]
        if fitting:
            by_member[uid] = fitting
    base_chars = sum(len(_entry_line(entry)) + 1 for items in by_member.values() for entry in items)

    def _avg_line_len(items: list) -> float:
        return sum(len(_entry_line(entry)) + 1 for entry in items) / len(items)

    def _quota_plan(min_lines: int) -> dict[str, int]:
        plan: dict[str, int] = {}
        for uid, items in by_member.items():
            share = sum(len(_entry_line(entry)) + 1 for entry in items) / max(1, base_chars)
            line_budget = int(round(share * max_chars / max(1.0, _avg_line_len(items) * 1.05)))
            plan[uid] = min(len(items), max(min_lines, line_budget))
        return plan

    def _representative_cost(entry) -> int:
        if isinstance(entry[2], str):
            return len(_entry_line(entry))
        # Original rows can omit a speaker/time prefix. Compare independently
        # rendered rows instead; the resolved name adds the same cost to every
        # candidate for a given member and is unnecessary for this comparison.
        return len(_message_line(entry[2], ""))

    def _pick(plan: dict[str, int]) -> list:
        picked: list = []
        for uid, items in by_member.items():
            keep = max(1, min(plan[uid], len(items)))
            if keep == 1:
                # A member's expensive first row can prevent everyone fitting
                # even when a shorter row from that member would fit easily.
                picked.append(min(items, key=_representative_cost))
                continue
            stride = max(1, (len(items) + keep - 1) // keep)
            picked.extend(items[::stride])
        return sorted(picked, key=lambda entry: entry[0])

    def _fit_representatives() -> tuple[list, list[str]]:
        cheapest = _pick(dict.fromkeys(by_member, 1))
        lines = render(cheapest)
        cost = sum(len(line) + 1 for line in lines)
        if cost <= max_chars:
            return cheapest, lines
        # Choosing shorter bodies can spread messages farther apart and add
        # full time markers. Try the earliest rows too before dropping members.
        earliest = sorted((items[0] for items in by_member.values()), key=lambda entry: entry[0])
        early_lines = render(earliest)
        early_cost = sum(len(line) + 1 for line in early_lines)
        if early_cost <= max_chars:
            return earliest, early_lines
        best, best_cost = (earliest, early_cost) if early_cost < cost else (cheapest, cost)
        # Keep cheap candidates in a moving window instead of overwriting them
        # with each member's latest row. Once the window covers every member,
        # advance its start too: cheap but distant old rows must not prevent a
        # compact middle cohort from fitting. Per-member monotone queues retain
        # the cheapest row until it expires, including rows followed by noise.
        candidates = sorted((entry for items in by_member.values() for entry in items), key=lambda e: e[0])
        indexed = {entry[0]: entry for entry in candidates}
        queues: dict[str, deque] = {uid: deque() for uid in by_member}
        chosen: dict[str, int] = {}
        ordered: list[int] = []
        costs: dict[int, int] = {}
        running_cost = 0

        def row_cost(index: int, previous: int | None) -> int:
            pair = [indexed[previous], indexed[index]] if previous is not None else [indexed[index]]
            return len(render(pair)[-1]) + 1

        def choose(uid: str, index: int | None) -> None:
            nonlocal running_cost
            if chosen.get(uid) == index:
                return
            # Only the replaced row and its immediate successors change cost.
            # Recompute their prefixes, including when a successor becomes the
            # first row. Always validate a fitting set with the full renderer.
            if uid in chosen:
                old = chosen.pop(uid)
                position = bisect_left(ordered, old)
                running_cost -= costs.pop(old)
                ordered.pop(position)
                if position < len(ordered):
                    successor = ordered[position]
                    running_cost -= costs[successor]
                    costs[successor] = row_cost(successor, ordered[position - 1] if position else None)
                    running_cost += costs[successor]
            if index is None:
                return
            position = bisect_left(ordered, index)
            costs[index] = row_cost(index, ordered[position - 1] if position else None)
            running_cost += costs[index]
            ordered.insert(position, index)
            chosen[uid] = index
            if position + 1 < len(ordered):
                successor = ordered[position + 1]
                running_cost -= costs[successor]
                costs[successor] = row_cost(successor, index)
                running_cost += costs[successor]

        left = 0
        for entry in candidates:
            index, uid = entry[:2]
            queue = queues[uid]
            price = _representative_cost(entry)
            while queue and queue[-1][1] > price:
                queue.pop()
            queue.append((index, price))
            choose(uid, queue[0][0])
            while len(chosen) == len(by_member):
                if running_cost < best_cost:
                    selected = [indexed[i] for i in ordered]
                    selected_lines = render(selected)
                    selected_cost = sum(len(line) + 1 for line in selected_lines)
                    if selected_cost <= max_chars:
                        return selected, selected_lines
                    if selected_cost < best_cost:
                        best, best_cost = selected, selected_cost
                expired = candidates[left]
                left += 1
                queue = queues[expired[1]]
                if queue[0][0] == expired[0]:
                    queue.popleft()
                    choose(expired[1], queue[0][0] if queue else None)
        # Preserve every speaker if their identity/time markers fit. Searching
        # representatives is a heuristic; failure to find complete bodies does
        # not justify discarding a member. Truncate only the body, and never a
        # speaker marker. The string-only compatibility fixtures have no message
        # metadata with which to render these markers.
        if not isinstance(best[0][2], str):
            prefix_options = []
            for selected in (best, cheapest, earliest):
                bare = [
                    (
                        entry[0],
                        entry[1],
                        replace(
                            entry[2],
                            text="",
                            has_image=False,
                            is_reply=False,
                            media_kind="",
                            media_label="",
                            face_names=[],
                        ),
                        "",
                    )
                    for entry in selected
                ]
                prefixes = render(bare)
                prefix_cost = sum(len(line) + 1 for line in prefixes)
                prefix_options.append((prefix_cost, selected, prefixes))
            prefix_cost, selected, prefixes = min(prefix_options, key=lambda option: option[0])
            if prefix_cost <= max_chars:
                selected_lines = render(selected)
                if all(
                    line.startswith(prefix) for line, prefix in zip(selected_lines, prefixes, strict=True)
                ):
                    needs = [
                        len(line) - len(prefix) for line, prefix in zip(selected_lines, prefixes, strict=True)
                    ]
                    allowance = [0] * len(needs)
                    remaining = max_chars - prefix_cost
                    count = len(needs)
                    for i in sorted(range(count), key=needs.__getitem__):
                        allowance[i] = min(needs[i], remaining // count)
                        remaining -= allowance[i]
                        count -= 1
                    clipped = []
                    for line, prefix, need, take in zip(
                        selected_lines, prefixes, needs, allowance, strict=True
                    ):
                        if take >= need:
                            clipped.append(line)
                        elif take:
                            clipped.append(line[: len(prefix) + take - 1] + "…")
                        else:
                            clipped.append(prefix)
                    return selected, clipped
        return _trim_group_entries(best, render, max_chars)

    plan = _quota_plan(GROUP_MEMBER_MIN_LINES)
    # Keep a member's floor intact while another member still has excess rows.
    # Scaling every allocation together can erase a low-frequency member even
    # when its complete set of messages would still fit after reducing a noisy
    # member. Only when all allocations are at their floors may the floors be
    # lowered to make an unusually large group fit.
    floor_plan = {uid: min(GROUP_MEMBER_MIN_LINES, len(items)) for uid, items in by_member.items()}
    for _ in range(16):
        picked = _pick(plan)
        rendered = render(picked)
        rendered_chars = sum(len(line) + 1 for line in rendered)
        if rendered_chars <= max_chars:
            return picked, rendered, True
        reducible = [uid for uid, count in plan.items() if count > floor_plan[uid]]
        if not reducible:
            reducible = [uid for uid, count in plan.items() if count > 1]
            if reducible:
                # Members with fewer messages than the configured floor keep
                # all their rows until the high-frequency members have reached
                # one row each, even when their rows exceed the fair share.
                frequent = [uid for uid in reducible if len(by_member[uid]) > GROUP_MEMBER_MIN_LINES]
                reducible = frequent or reducible
                # Reduce all members above a fair character share together.
                # Reducing just one member per round exhausts the iteration
                # limit in large groups, after which tail trimming loses entire
                # members. Cheap, low-frequency members keep their full floor.
                costs = dict.fromkeys(by_member, 0)
                for entry, line in zip(picked, rendered, strict=True):
                    costs[entry[1]] += len(line) + 1
                fair_share = max_chars / len(by_member)
                costly = [uid for uid in reducible if costs[uid] > fair_share]
                reducible = costly or reducible
        if not reducible:
            picked, rendered = _fit_representatives()
            return picked, rendered, True
        changed = False
        for uid in reducible:
            current = plan[uid]
            # Leave a little headroom for prefixes introduced by re-rendering.
            minimum = floor_plan[uid] if current > floor_plan[uid] else 1
            target = max(minimum, (current * max_chars * 9) // (rendered_chars * 10))
            if target >= current:
                target = current - 1
            if target < current:
                plan[uid] = target
                changed = True
        if not changed:
            break

    # Even pathological prefix changes must not send a partially reduced plan
    # to tail trimming: first try one representative from every member. Only
    # when that complete set exceeds the budget may the final gate drop members.
    picked, rendered = _fit_representatives()
    return picked, rendered, True


def _fit_group_lines(entries: list, max_chars: int) -> list:
    """成员感知抽样的兼容接口，返回保序文本行。"""
    _kept, lines, _sampled = _fit_group_entries(
        entries, max_chars, lambda selected: [_entry_line(entry) for entry in selected]
    )
    return lines


def _render_group_entries(entries: list, resolve) -> list[str]:
    """只用最终保留的消息计算首条、换人和相对时间标记。"""
    lines: list[str] = []
    prev_uid = prev_ts = None
    for entry in entries:
        message = entry[2]
        line = _message_line(message, resolve(message.sender_uid), prev_uid, prev_ts)
        lines.append(line)
        prev_uid, prev_ts = message.sender_uid, message.timestamp
    return lines


def _group_dialog_needs_sampling(chat: ChatData, msgs: list, max_chars: int = 0) -> bool:
    """判断群聊月份是否会触发成员感知抽样。"""
    valid = _month_messages(chat, msgs)
    if not valid:
        return False
    name_map = {participant.uid: participant.name for participant in chat.participants()}

    def resolve(uid: str) -> str:
        return name_map.get(uid, "未知发送者")

    entries = [(index, m.sender_uid or "__unknown__", m, "") for index, m in enumerate(valid)]
    lines = _render_group_entries(entries, resolve)
    return sum(len(line) + 1 for line in lines) > (max_chars or GROUP_MAX_DIALOG_CHARS)


# build_group_dialog 的隐私面：函数体内的注释同样受上方"LEGACY 按原始源码哈希"约束，
# 因此"重名成员显示名带 #完整uid 会随 prompt 外发名单内成员的号"这笔隐私记账只能写在这里
# （见 privacy-audit-report.md 与 CHANGELOG「未发布」节），不进去改函数体内那行注释。
def build_group_dialog(
    chat: ChatData, msgs: list, chat_hash: str = "", vision_label: str = "", max_chars: int = 0
) -> str:
    """群聊对话内容：统计头（含本月三张互动摘要）+ 成员感知抽样后的对话行 + 图片摘要。"""
    valid = _month_messages(chat, msgs)
    if not valid:
        return ""
    facts, ranked = _member_facts(valid, chat)
    # 不在成员名单里的 uid（占位 sender 等）统一显示成"未知发送者"，绝不显示原始 uid
    name_map = {p.uid: p.name for p in chat.participants()}

    def resolve(uid: str) -> str:
        return name_map.get(uid, "未知发送者")

    entries = []
    prev_uid = prev_ts = None
    for idx, m in enumerate(valid):
        line = _message_line(m, resolve(m.sender_uid), prev_uid, prev_ts)
        entries.append((idx, m.sender_uid or "__unknown__", m, line))
        prev_uid, prev_ts = m.sender_uid, m.timestamp

    _, kept_lines, sampled = _fit_group_entries(
        entries,
        max_chars or GROUP_MAX_DIALOG_CHARS,
        lambda selected: _render_group_entries(selected, resolve),
    )

    top = "、".join(f"{resolve(uid)} {n} 条" for uid, n in ranked[:5]) or "无"
    from datetime import datetime

    from parser.qq_parser import CST

    hours: dict[int, int] = {}
    for m in valid:
        hour = datetime.fromtimestamp(m.timestamp / 1000, tz=CST).hour
        hours[hour] = hours.get(hour, 0) + 1
    peak_hour = max(hours, key=lambda h: hours[h]) if hours else None
    from config import GROUP_PEAK_WINDOW_MINUTES

    peak_live = _peak_concurrent(valid, GROUP_PEAK_WINDOW_MINUTES * 60 * 1000)

    parts = [f"统计：{facts}"]
    if peak_hour is not None:
        parts.append(f"最活跃时段约 {peak_hour} 时")
    parts.append(f"同时在聊高峰 {peak_live} 人（{GROUP_PEAK_WINDOW_MINUTES} 分钟窗口内）")
    parts.append(f"本月发言最多：{top}")
    # 明确标出"我"是哪个昵称：群聊里没有"对方"，模型只能靠这一行把我从几十个昵称里认出来。
    # 真实数据实测：不标这一行时 group_dynamics 的 self_role 会写成
    # "样本未标注 self 发言，无法定位导出者本人的角色"——一个本可避免的"数据不足"。
    me = next((p for p in chat.participants() if p.is_self), None)
    if me is not None:
        parts.append(f"「我」= 导出者本人，在群里的显示名是 {me.name}")
    head = "，".join(parts)
    head += "\n本月互动摘要：\n" + _interaction_digest(valid, resolve)
    if sampled:
        head += (
            f"\n（因篇幅限制展示其中 {len(kept_lines)} 条：按成员配额抽样，"
            "优先覆盖所有成员，预算不足时截断正文或减少展示成员）"
        )

    dialog = f"{head}。\n\n" + "\n".join(kept_lines)
    digest = _vision_digest(valid, chat_hash, vision_label)
    if digest:
        dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
    if sampled:
        return _PromptText(dialog, group_sampled_prompt_fingerprint())
    return _PromptText(dialog, _unsampled_group_prompt_fingerprint())


def _group_month_prompt(chat: ChatData, period: str, msgs: list, chat_hash: str = "") -> str:
    dialog = build_group_dialog(chat, msgs, chat_hash=chat_hash, vision_label=f"{period} 月")
    if not dialog.strip():
        return ""
    return _PromptText(
        f"以下是 {period} 月「{chat.chat_name}」的群聊数据：\n\n{dialog}",
        getattr(dialog, "cache_fingerprint", _unsampled_group_prompt_fingerprint()),
    )


def _member_context(chat: ChatData, member: Participant) -> str:
    """成员画像的群上下文：本地精确算出的互动数字（事实与推断分开标注）"""
    # top_k=0 在这里的含义是**不截断**，而这不是"顺手传个 0"：下面要按 uid 取每位
    # 成员的互动计数，矩阵默认只保留发言量前 30 名（GROUP_MATRIX_MEMBERS）。
    # 榜外成员在 totals 里根本没有条目 → totals.get(uid, {}) 拿到空 dict → 下面的
    # 数字全成 0 → 提示词写成"被精确回复 0 次、主动回复别人 0 次（事实）"，
    # 而 select_ai_members 恰恰会专门把不在前列的"我"选进来（见那里的注释），
    # 也就是说最容易踩到的正是"用户本人被编成零互动"。这是最贵的一档维度
    # （每位成员一次付费调用）拿到的假数据。
    # 早先同样写的是 top_k=0，但那时 group_stats 用 `top_k or 默认上限` 解释参数，
    # 0 被当成"没传"→ 照样截断 → 这句注释承诺的事从未发生（见该函数里的说明）。
    matrix = calc_interaction_matrix(chat, top_k=0)
    totals = {t["uid"]: t for t in matrix["totals"]}
    t = totals.get(member.uid, {})
    activity = {a["uid"]: a for a in calc_member_activity(chat)}
    a = activity.get(member.uid, {})

    def _top_partners(key: str) -> str:
        """该成员互动最多的对象（取矩阵行/列最强的前 3 位）"""
        names = {p["uid"]: p["name"] for p in matrix["members"]}
        idx = {p["uid"]: i for i, p in enumerate(matrix["members"])}
        i = idx.get(member.uid)
        if i is None:
            return "无"
        picks = []
        for j in range(len(matrix["members"])):
            if j == i:
                continue
            value = matrix["explicit_undirected"][i][j] if key == "explicit" else matrix["undirected"][i][j]
            if value:
                picks.append((value, names[matrix["members"][j]["uid"]]))
        picks.sort(reverse=True)
        return "、".join(f"{n}（{v} 次）" for v, n in picks[:3]) or "无"

    lines = [
        f"- 发言 {a.get('msg_count', 0)} 条（占全群 {a.get('share', 0) * 100:.1f}%），"
        f"活跃 {a.get('active_days', 0)} 天，平均每条 {a.get('avg_chars', 0)} 字",
        f"- 被精确回复 {t.get('explicit_replied_by', 0)} 次、"
        f"主动回复别人 {t.get('explicit_replies_to', 0)} 次（事实）",
        f"- 被 @ {t.get('mentions_received', 0)} 次、主动 @ 别人 {t.get('mentions_sent', 0)} 次（事实）",
        f"- 被接话 {t.get('replied_by', 0)} 次、接别人话 {t.get('replies_to', 0)} 次（推断）",
        f"- 主要互动对象（精确回复/@）：{_top_partners('explicit')}",
    ]
    if member.is_self:
        lines.append("- 注意：这个人就是导出者本人（我），评价同样要客观")
    return "\n".join(lines)


def _member_cache_key(member: Participant, user_content: str) -> str:
    """成员画像的缓存键：内容寻址（模型 + 群聊指纹 + 该成员的提示词正文）

    复用月份缓存那套文件（同一目录、同一引用计数），只是键里多带了成员 uid 与提示词正文。
    这样"某位成员那次调用失败了"只需补他一个人，而不是把 10 位成员全部重跑一遍——
    真实数据实测过：一次 10 人的成员画像里有 2 位因模型输出非法 JSON 失败，
    没有按人缓存时补这 2 位要重付 10 次的钱。
    """
    import analyzer.deepseek_client as dc

    return dc._month_key(f"member_profiles:{member.uid}", user_content, group_prompt_fingerprint())


def _analyze_member(
    chat: ChatData,
    member: Participant,
    system_prompt: str,
    prompt_template: str,
    max_tokens: int,
    tag: str,
    chat_hash: str = "",
    used_keys: "set | None" = None,
) -> Optional[dict]:
    """单个成员的群内画像：按时间均匀抽样 + 群上下文头部（结果按成员内容寻址缓存）。

    抽样口径与私聊锐评一致（stratified）：growth_observation 要的是"这段时间的变化"，
    只取最近 800 条会让变化根本不在样本里。
    """
    try:
        msgs = [
            m for m in chat.messages if m.sender_uid == member.uid and _has_content(m) and is_statistical(m)
        ]
        if not msgs:
            return None
        if len(msgs) > MEMBER_PROFILE_SAMPLES:
            stride = (len(msgs) + MEMBER_PROFILE_SAMPLES - 1) // MEMBER_PROFILE_SAMPLES
            sample = msgs[::stride]
            span_note = "，按时间均匀抽样覆盖整个时段"
        else:
            sample = msgs
            span_note = ""
        lines = [_message_line(m, member.name) for m in sample]
        original_n = len(lines)
        lines = _fit_lines(lines, GROUP_MAX_DIALOG_CHARS)
        head = f"统计：{member.name} 共发言 {len(msgs)} 条（样本 {len(sample)} 条{span_note}，"
        head += f"图片 {sum(1 for m in sample if m.has_image)} 张）"
        if len(lines) < original_n:
            head += f"，因篇幅限制展示其中 {len(lines)} 条"
        dialog = f"{head}。\n\n" + "\n".join(lines)
        digest = _vision_digest(msgs, chat_hash, f"{member.name} 的发言中")
        if digest:
            dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
        context = _member_context(chat, member)
        prompt = prompt_template.format(
            display_name=member.name, group_name=chat.chat_name, context=context, dialog=dialog
        )
        import analyzer.deepseek_client as dc

        key = _member_cache_key(member, prompt)
        # 思考模式要进口径核对，与私聊月份缓存那条规则一致（见 month_cache._read_month_cache）：
        # 成员画像是最贵的一个维度，让"关了思考"的重跑吃到"开着思考"算出来的画像，
        # 用户看到的同样是配置静默失效。
        want_thinking = dc.thinking_enabled(tag)
        result = dc._read_month_cache(key, expect_thinking=want_thinking, chat_hash=chat_hash)
        if result is not None:
            logger.info("%s 命中成员缓存，跳过 API 调用", mask_name(member.name))
        else:
            result = _call_api(system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag)
            if result and month_cache_enabled():
                try:
                    validate_result(tag, result)
                except ResultValidationError as e:
                    logger.warning("%s 的成员画像结果结构无效（字段 %s）", mask_name(member.name), e.path)
                    raise AnalysisIncompleteError(
                        f"{mask_name(member.name)} 的成员画像结果结构无效（字段 {e.path}）；可重试该成员"
                    ) from e
            # 与私聊同一道清理守卫：这个聊天刚被级联清掉就不要把成员画像写回盘上
            if result and not dc.purge_marks.is_marked(chat_hash):
                dc._write_month_cache(key, result, thinking=want_thinking)
        if result is not None and used_keys is not None:
            used_keys.add(key)
        if result:
            result["name"] = member.name
            result["uid"] = member.uid
            result["is_self"] = member.is_self
            result["total_messages"] = len(msgs)
            return result
        raise AnalysisIncompleteError(f"{mask_name(member.name)} 的成员画像未得到有效结果")
    except QuotaExhaustedError:
        raise  # 配额耗尽要中止整个维度，不能被当作单人失败吞掉
    except AnalysisIncompleteError:
        raise
    except Exception as e:
        logger.error("%s 的群内画像失败: %s", mask_name(member.name), e)
        raise AnalysisIncompleteError(f"{mask_name(member.name)} 的成员画像分析失败") from e


def analyze_member_profiles(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict:
    """群内成员画像：每位入选成员一次调用（Top-K，自己必定入选）。"""
    members = select_ai_members(chat)
    if not members:
        return {}
    results: dict = {}
    template = (
        "以下是 {display_name} 在群「{group_name}」里的发言样本与互动数字，"
        "请分析他在这个群里扮演什么角色、是个什么样的人：\n\n"
        + gp.MEMBER_CONTEXT_NOTES
        + "\n\n## 发言样本\n{dialog}"
    )
    total, done = len(members), 0
    used_keys: set = set()
    for member in members:
        if should_cancel and should_cancel():
            break
        try:
            result = _analyze_member(
                chat,
                member,
                gp.GROUP_SYSTEM_PROMPT_MEMBER_PROFILE,
                template,
                max_tokens=MAX_TOKENS_BY_DIM["member_profiles"],
                tag="member_profiles",
                chat_hash=chat_hash,
                used_keys=used_keys,
            )
        except QuotaExhaustedError:
            # 前面已完成的成员画像各自有内容缓存；重试时命中它们，只补尚未完成的成员。
            _record_member_usage(chat_hash, used_keys)
            raise
        except AnalysisIncompleteError:
            _record_member_usage(chat_hash, used_keys)
            raise
        if result:
            results[member.uid] = result
        done += 1
        if on_progress:
            on_progress(done, total)
    _record_member_usage(chat_hash, used_keys)
    return results


def _record_member_usage(chat_hash: str, keys: "set") -> None:
    """把本维度用到的成员缓存记进 manifest（与月份缓存共用引用计数与清理规则）"""
    if not keys:
        return
    import analyzer.deepseek_client as dc

    dc._record_month_usage(chat_hash, keys)


def _monthly_group_dimension(
    system_prompt: str, tag: str, chat: ChatData, on_progress, should_cancel, chat_hash
):
    """三个群级维度共用的执行体：逐月分析 + 群聊自己的月份缓存指纹"""
    months = chat.months()
    return _analyze_periods(
        months,
        system_prompt,
        lambda p, msgs: _group_month_prompt(chat, p, msgs, chat_hash=chat_hash),
        max_tokens=MAX_TOKENS_BY_DIM[tag],
        tag=tag,
        on_progress=on_progress,
        should_cancel=should_cancel,
        chat_hash=chat_hash,
        fingerprint=group_prompt_fingerprint(),  # 群聊自己的键：私聊月份缓存不受影响
    )


def analyze_group_dynamics(chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = "") -> dict:
    """群整体动态（群氛围 / 核心成员 / 小圈子 / 权力结构 / 潜水比例）"""
    return _monthly_group_dimension(
        gp.GROUP_SYSTEM_PROMPT_DYNAMICS, "group_dynamics", chat, on_progress, should_cancel, chat_hash
    )


def analyze_group_topics(chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = "") -> dict:
    """群聊话题（话题分布 + 每个话题是谁在聊）"""
    results = _monthly_group_dimension(
        gp.GROUP_SYSTEM_PROMPT_TOPICS, "group_topics", chat, on_progress, should_cancel, chat_hash
    )
    from analyzer.deepseek_client import _normalize_topic_weights

    for r in results.values():
        _normalize_topic_weights(r)  # 权重归一化：各月占比之和恒为 1.0
    return results


def analyze_group_emotion(chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = "") -> dict:
    """群整体情绪 + 成员情绪对比"""
    results = _monthly_group_dimension(
        gp.GROUP_SYSTEM_PROMPT_EMOTION, "group_emotion", chat, on_progress, should_cancel, chat_hash
    )
    from analyzer.deepseek_client import _clamp_int

    for r in results.values():
        _clamp_int(r, "group_intensity", 0, 10)
        for item in r.get("member_emotions") or []:
            if isinstance(item, dict):
                _clamp_int(item, "intensity", 0, 10)
    return results


#: 群聊维度注册表：dim → (中文名, 执行函数, 进度单位)
#: 顺序即"一键全量"的执行顺序：最贵的成员画像放最后（先让用户拿到便宜的结果）
GROUP_DIMENSIONS: dict = {
    "group_dynamics": ("群聊动态", analyze_group_dynamics, "月"),
    "group_topics": ("群聊话题", analyze_group_topics, "月"),
    "group_emotion": ("群聊情绪", analyze_group_emotion, "月"),
    "member_profiles": ("成员画像", analyze_member_profiles, "人"),
}


_GROUP_FORMAT_FUNCS = (
    build_group_dialog,
    _fit_group_entries,
    _render_group_entries,
    _fit_group_lines,
    _fit_lines,
    _interaction_digest,
    _member_facts,
    _member_context,
    _message_line,
    _gap_mark,
    _short_time,
    _truncate_dialog_line,
)
_GROUP_COMPAT_FORMAT_FUNCS = (
    build_group_dialog,
    _render_group_entries,
    _fit_lines,
    _interaction_digest,
    _member_facts,
    _member_context,
    _message_line,
    _gap_mark,
    _short_time,
)


def _group_fingerprint_inputs():
    prompt_values = tuple(
        getattr(gp, name) for name in sorted(dir(gp)) if name.startswith("GROUP_SYSTEM_PROMPT_")
    )
    return (
        prompt_values,
        gp.MEMBER_CONTEXT_NOTES,
        (
            GROUP_MAX_DIALOG_CHARS,
            GROUP_MEMBER_MIN_LINES,
            MEMBER_PROFILE_SAMPLES,
            INTERACTION_DIGEST_PAIRS,
            GROUP_AI_MAX_MEMBERS,
            MAX_TOKENS_BY_DIM.get("group_dynamics"),
            MAX_TOKENS_BY_DIM.get("group_topics"),
            MAX_TOKENS_BY_DIM.get("group_emotion"),
            MAX_TOKENS_BY_DIM.get("member_profiles"),
        ),
    )


def _group_compat_format_matches() -> bool:
    try:
        payload = "\n".join(_hashed_source(func) for func in _GROUP_COMPAT_FORMAT_FUNCS)
    except (OSError, TypeError):
        return False
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == _GROUP_COMPAT_FORMAT_DIGEST


def _group_source_parts(funcs: tuple, normalize: bool = True) -> list[str]:
    try:
        return [_hashed_source(func) if normalize else inspect.getsource(func) for func in funcs]
    except (OSError, TypeError):
        logger.warning(
            "无法读取群聊格式化函数源码（编译/打包环境），群聊指纹降级为函数名级——"
            "改动群聊对话格式不会自动失效旧缓存；改过格式后请设 PROMPT_CACHE_SALT 手动换键"
        )
        return [f"<source-unavailable:{func.__name__}>" for func in funcs]


def _group_fingerprint_from_inputs(current, salt: str, compatible: bool = False) -> str:
    prompt_values, context_notes, const_values = current
    parts = list(prompt_values)
    parts.append(context_notes)
    parts.append("consts:%s" % repr(const_values))
    if compatible:
        from analyzer.dialog_cache_compat import historical_source

        parts += [historical_source("build_group_dialog"), historical_source("_fit_group_lines")]
        parts += _group_source_parts((_interaction_digest, _member_facts, _member_context))
    else:
        parts += _group_source_parts(_GROUP_FORMAT_FUNCS)
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _group_cache_salt(salt: "str | None") -> str:
    if salt is not None:
        return salt
    value = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    group_salt = (os.getenv("QQCHAT_GROUP_CACHE_SALT", "") or "").strip()
    if group_salt:
        value = f"{value}+{group_salt}" if value else group_salt
    return value


def group_prompt_fingerprint(salt: "str | None" = None, normalize: bool = True) -> str:
    """群聊提示词与格式的指纹，参与群聊的月份缓存键与维度缓存文件名。

    与私聊指纹**完全独立**：私聊那份按名单哈希 analyzer/prompts.py 的常量，
    这里哈希 analyzer/group_prompts.py 的全部常量 + 群聊自己的格式化函数源码 + 群聊常量。
    两边互不影响，因此新增/修改群聊提示词不会作废任何私聊缓存（那会让用户重新付费）。

    normalize 的含义与私聊那份一致（见 deepseek_client._hashed_source）：
    True = 源码归一到 AST（注释/空白/格式重排不再换键），False = 复现旧的原文公式，
    只用于算 GROUP_PROMPT_FINGERPRINT_LEGACY 以读取旧缓存。

    源码不可读时（frozen/打包）降级为函数名占位，并提示用 PROMPT_CACHE_SALT 手动换键。

    群聊独立盐 QQCHAT_GROUP_CACHE_SALT：PROMPT_CACHE_SALT 是一颗盐同时换私聊/群聊
    两族键——想只重刷群聊缓存做不到。新增的这颗盐**只在非空时**并入群聊指纹，
    默认（空）时群聊指纹与升级前逐字节一致，不影响任何既有缓存；
    PROMPT_CACHE_SALT 对群聊继续生效（既有测试与用户配置都依赖它）。
    """
    salt = _group_cache_salt(salt)
    current = _group_fingerprint_inputs()
    if normalize:
        return _group_fingerprint_from_inputs(current, salt, compatible=_group_compat_format_matches())
    prompt_values, context_notes, const_values = current
    parts = list(prompt_values)
    parts.append(context_notes)
    parts.append("consts:%s" % repr(const_values))
    parts += _group_source_parts(
        (build_group_dialog, _fit_group_lines, _interaction_digest, _member_facts, _member_context),
        normalize=False,
    )
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


GROUP_PROMPT_FINGERPRINT = group_prompt_fingerprint()


def _unsampled_group_prompt_fingerprint(salt: "str | None" = None) -> str:
    """未抽样输入复用同配置的历史键；预算变化仍需隔离维度缓存。"""
    return group_prompt_fingerprint(salt)


def group_sampled_prompt_fingerprint(salt: "str | None" = None) -> str:
    """群聊触发预算抽样时的格式代次，保留未抽样缓存键。"""
    funcs = (
        _entry_line,
        _fit_group_entries,
        _trim_group_entries,
        _render_group_entries,
        _truncate_dialog_line,
        _message_line,
        _gap_mark,
        _short_time,
    )
    parts = [
        group_prompt_fingerprint(salt),
        "group-sampled-dialog-v1",
        *_group_source_parts(funcs),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


#: 旧公式（按 getsource 原文哈希）的取值：只为读取 AST 归一之前写下的群聊缓存。
#: **必须是字面量，不能改回 group_prompt_fingerprint(normalize=False)**：旧公式哈希的是
#: 当前源码原文，任何一次对进指纹函数（build_group_dialog / _fit_group_lines /
#: _interaction_digest / _member_facts / _member_context）的改动都会让现算值漂移，
#: 而它是"读旧群聊缓存并免费迁移"的唯一凭据——漂掉之后那批用户的旧缓存再也读不到，
#: 本该免费的迁移静默退化成重新付费。理由与私聊那条完全一致，
#: 详见 deepseek_client.PROMPT_FINGERPRINT_LEGACY 上方的说明。
#: 取值由 tests/test_group_foundation.py::PINNED_LEGACY_GROUP_FINGERPRINT 钉住。
GROUP_PROMPT_FINGERPRINT_LEGACY = "5f1f7bb3c0ce"
