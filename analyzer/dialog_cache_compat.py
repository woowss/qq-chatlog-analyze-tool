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

"""Frozen pre-issue-11 source used only to rebuild historical cache keys.

These strings are parsed as AST and never executed. SHA-256 cannot recover the
configuration prefix from a fingerprint, so the changed functions' historical
syntax is needed to preserve unsampled caches under arbitrary budgets and salts.
Never update these snapshots with the current formatter. Callers must verify a
fixed digest of the current format before selecting this compatibility formula.
"""

import ast
from functools import lru_cache

from analyzer.fingerprint_utils import canonical_ast


PRIVATE_BUILD_DIALOG = r'''
def _build_dialog(
    messages: list,
    self_uid: str,
    self_name: str,
    other_name: str,
    max_chars: Optional[int] = MAX_DIALOG_CHARS,
    chat_hash: str = "",
    vision_label: str = "",
) -> str:
    """构建喂给模型的对话内容：统计头（含本地事实）+ 压缩后的对话行 + 图片摘要。

    统计头给模型全貌（即使抽样截断也能知道真实消息量），并附上本地精确算出的
    事实（条数、图片数、最活跃时段、回复中位数、谁更常开启话题、对话段数），
    避免模型凭样本"数数"。

    vision_label 非空时会尝试附上该批消息的图片摘要（见 analyzer/vision.py）：
    摘要按图片指纹缓存，同一批图只花一次视觉调用，5 个维度共用。
    """
    valid = [m for m in messages if _has_content(m) and is_statistical(m)]
    if not valid:
        return ""
    total = len(valid)
    self_n = sum(1 for m in valid if m.sender_uid == self_uid)
    other_n = total - self_n
    images = sum(1 for m in valid if m.has_image)

    lines: list[str] = []
    prev_uid: Optional[str] = None
    prev_ts: Optional[int] = None
    for m in valid:
        lines.append(
            _message_line(m, self_name if m.sender_uid == self_uid else other_name, prev_uid, prev_ts)
        )
        prev_uid, prev_ts = m.sender_uid, m.timestamp
    original_n = len(lines)
    if max_chars:
        lines = _fit_lines(lines, max_chars)

    parts = [f"共 {total} 条消息（我方 {self_n} 条 / 对方 {other_n} 条，图片 {images} 张）"]
    stats = _conversation_stats(valid, self_uid)
    if stats["peak_hour"] is not None:
        parts.append(f"最活跃时段约 {stats['peak_hour']} 时")
    if stats["median_gap"] is not None:
        parts.append(f"回复间隔中位数约 {int(stats['median_gap'])} 秒")
    if stats["sessions"]:
        parts.append(f"共 {stats['sessions']} 段对话（间隔超 {TIME_MARK_MINUTES} 分钟算新的一段）")
        if self_uid:
            ratio = round(stats["self_opened"] / stats["sessions"] * 100)
            parts.append(f"其中我方先开口 {ratio}%、对方 {100 - ratio}%")
    head = "统计：" + "，".join(parts)
    if len(lines) < original_n:
        head += f"，因篇幅限制展示其中 {len(lines)} 条（等间隔抽样，覆盖整月分布）"
    dialog = f"{head}。\n\n" + "\n".join(lines)

    digest = _vision_digest(valid, chat_hash, vision_label)
    if digest:
        dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
    return dialog
'''


GROUP_BUILD_DIALOG = r'''
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
        entries.append((idx, m.sender_uid or "__unknown__", line))
        prev_uid, prev_ts = m.sender_uid, m.timestamp

    original_n = len(entries)
    kept_lines = _fit_group_lines(entries, max_chars or GROUP_MAX_DIALOG_CHARS)

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
    if len(kept_lines) < original_n:
        head += (
            f"\n（因篇幅限制展示其中 {len(kept_lines)} 条：按成员配额抽样，"
            "每位成员都有保底条数，低频成员不会被整段丢掉）"
        )

    dialog = f"{head}。\n\n" + "\n".join(kept_lines)
    digest = _vision_digest(valid, chat_hash, vision_label)
    if digest:
        dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
    return dialog
'''


GROUP_FIT_LINES = r'''
def _fit_group_lines(entries: list, max_chars: int) -> list:
    """成员感知抽样：entries 是 [(原始序号, uid, 文本行)]，返回保序的文本行列表。

    先给每位成员 GROUP_MEMBER_MIN_LINES 条保底，再按"该成员字符占比"分配预算，
    每个成员内部用等间隔步长取（保证覆盖整段时间）；仍然超预算时按兜底截断收敛。
    """
    if not entries:
        return []
    lines = [e[2] for e in entries]
    total_chars = sum(len(line) + 1 for line in lines)
    if total_chars <= max_chars:
        return lines

    by_member: dict[str, list] = {}
    for e in entries:
        by_member.setdefault(e[1], []).append(e)

    def _avg_line_len(items: list) -> float:
        """该成员平均每条多少字符（含换行）。字符预算换算成行数要用它。"""
        return (sum(len(e[2]) + 1 for e in items) / len(items)) if items else 1.0

    def _quota_plan(min_lines: int) -> dict[str, int]:
        """把"该成员的字符占比"换算成"该成员保留多少**行**"。

        单位必须换算清楚：max_chars 是**字符**预算，而 plan 的值是**行数**。
        早先写的是 `share * (max_chars / 1.05)` —— 直接把字符数当行数用，比正确值
        大了一个平均行长（实测约 40 倍）。后果不是"稍微多留一点"，而是整段逻辑失效：
        keep = min(plan[uid], len(items)) 恒等于 len(items)（人人都全留）→ _estimate
        必然超预算 → 降级循环又被下面的 max(min_lines, ...) 掩住（min_lines 只是下限）
        → 每次落到 :244 的 _fit_lines 均匀截断。也就是说"成员感知抽样"从未生效过，
        而 build_group_dialog 已经对着模型写了"每位成员都有保底条数，低频成员不会被
        整段丢掉"——那句话在这个 bug 下是假的，低频成员恰恰被均匀抽样丢掉了。
        """
        plan: dict[str, int] = {}
        for uid, items in by_member.items():
            share = sum(len(e[2]) + 1 for e in items) / total_chars
            # 该成员分到的字符预算 ÷ 他的平均行长 = 他能保留的行数
            line_budget = int(round(share * max_chars / max(1.0, _avg_line_len(items) * 1.05)))
            plan[uid] = min(len(items), max(min_lines, line_budget))
        return plan

    def _estimate(plan: dict[str, int]) -> int:
        total = 0
        for uid, items in by_member.items():
            keep = min(plan[uid], len(items))
            stride = max(1, (len(items) + keep - 1) // keep)
            total += sum(len(e[2]) + 1 for e in items[::stride])
        return total

    plan = _quota_plan(GROUP_MEMBER_MIN_LINES)
    if _estimate(plan) > max_chars:
        # 保底太高（人特别多）：逐步降到 1 条，仍超预算就交给兜底截断
        for min_lines in (10, 5, 3, 1):
            plan = _quota_plan(min_lines)
            if _estimate(plan) <= max_chars:
                break
    kept: list = []
    for uid, items in by_member.items():
        keep = max(1, min(plan[uid], len(items)))
        stride = max(1, (len(items) + keep - 1) // keep)
        kept.extend(items[::stride])
    kept.sort(key=lambda e: e[0])
    out = [e[2] for e in kept]
    if sum(len(line) + 1 for line in out) > max_chars:
        out = _fit_lines(out, max_chars)  # 兜底：与私聊同一套截断，保证绝不超预算
    return out
'''


@lru_cache(maxsize=3)
def historical_source(name: str) -> str:
    """Return a frozen canonical AST without relying on inspect or source files."""
    sources = {
        "_build_dialog": PRIVATE_BUILD_DIALOG,
        "build_group_dialog": GROUP_BUILD_DIALOG,
        "_fit_group_lines": GROUP_FIT_LINES,
    }
    return canonical_ast(ast.parse(sources[name]))
