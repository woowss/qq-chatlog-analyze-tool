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
from typing import Callable, Optional

from config import GROUP_AI_MAX_MEMBERS
from parser.qq_parser import ChatData, is_statistical
from parser.group_identity import Participant
from analyzer import group_prompts as gp
from analyzer.deepseek_client import (
    MAX_TOKENS_BY_DIM,
    QuotaExhaustedError,
    _analyze_periods,
    _call_api,
    _env_number,
    _fit_lines,
    _has_content,
    _message_line,
    _vision_digest,
    logger,
)
from analyzer.group_stats import calc_interaction_matrix, calc_member_activity, is_unknown_message

#: 单个群聊月的对话文本上限（字符）。默认与私聊同档（准确性优先），
#: 想省钱可在 .env 里调小 LLM_GROUP_MAX_DIALOG_CHARS。
GROUP_MAX_DIALOG_CHARS = int(_env_number("LLM_GROUP_MAX_DIALOG_CHARS", 600_000, 1000, 2_000_000))
#: 成员感知抽样时每位成员的保底条数（防止低频成员被整段丢掉）
GROUP_MEMBER_MIN_LINES = int(_env_number("LLM_GROUP_MEMBER_MIN_LINES", 20, 1, 500))
#: 成员画像的单人样本上限（与私聊锐评同档：按时间均匀抽样覆盖整个时段）
MEMBER_PROFILE_SAMPLES = 800
#: 单条 prompt 里展示的互动"Top 对"数量（三张矩阵各取前 N 对，太长反而淹没重点）
INTERACTION_DIGEST_PAIRS = 8


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

    def _quota_plan(min_lines: int) -> dict[str, int]:
        plan: dict[str, int] = {}
        for uid, items in by_member.items():
            share = sum(len(e[2]) + 1 for e in items) / total_chars
            plan[uid] = max(min_lines, int(round(share * (max_chars / 1.05) / 1.0)) if share else min_lines)
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


def _group_month_prompt(chat: ChatData, period: str, msgs: list, chat_hash: str = "") -> str:
    dialog = build_group_dialog(chat, msgs, chat_hash=chat_hash, vision_label=f"{period} 月")
    if not dialog.strip():
        return ""
    return f"以下是 {period} 月「{chat.chat_name}」的群聊数据：\n\n{dialog}"


def _member_context(chat: ChatData, member: Participant) -> str:
    """成员画像的群上下文：本地精确算出的互动数字（事实与推断分开标注）"""
    matrix = calc_interaction_matrix(chat, top_k=0)  # 不截断：这里要给每位成员准确数字
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
        result = dc._read_month_cache(key)
        if result is not None:
            logger.info("%s 命中成员缓存，跳过 API 调用", member.name)
        else:
            result = _call_api(system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag)
            if result:
                dc._write_month_cache(key, result)
        if result is not None and used_keys is not None:
            used_keys.add(key)
        if result:
            result["name"] = member.name
            result["uid"] = member.uid
            result["is_self"] = member.is_self
            result["total_messages"] = len(msgs)
            return result
    except QuotaExhaustedError:
        raise  # 配额耗尽要中止整个维度，不能被当作单人失败吞掉
    except Exception as e:
        logger.error("%s 的群内画像失败: %s", member.name, e)
    return None


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
            if results:  # 已有部分结果：保留已完成者，向上报告配额问题
                logger.error("配额耗尽，剩余成员未分析（已完成 %d/%d）", len(results), total)
                break
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


def group_prompt_fingerprint(salt: "str | None" = None) -> str:
    """群聊提示词与格式的指纹，参与群聊的月份缓存键与维度缓存文件名。

    与私聊指纹**完全独立**：私聊那份按名单哈希 analyzer/prompts.py 的常量，
    这里哈希 analyzer/group_prompts.py 的全部常量 + 群聊自己的格式化函数源码 + 群聊常量。
    两边互不影响，因此新增/修改群聊提示词不会作废任何私聊缓存（那会让用户重新付费）。

    源码不可读时（frozen/打包）降级为函数名占位，并提示用 PROMPT_CACHE_SALT 手动换键。
    """
    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    parts = [getattr(gp, n) for n in sorted(dir(gp)) if n.startswith("GROUP_SYSTEM_PROMPT_")]
    parts.append(gp.MEMBER_CONTEXT_NOTES)
    parts.append(
        "consts:%s"
        % repr(
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
            )
        )
    )
    fmt_funcs = (build_group_dialog, _fit_group_lines, _interaction_digest, _member_facts, _member_context)
    try:
        parts += [inspect.getsource(f) for f in fmt_funcs]
    except (OSError, TypeError):
        parts += [f"<source-unavailable:{f.__name__}>" for f in fmt_funcs]
        logger.warning(
            "无法读取群聊格式化函数源码（编译/打包环境），群聊指纹降级为函数名级——"
            "改动群聊对话格式不会自动失效旧缓存；改过格式后请设 PROMPT_CACHE_SALT 手动换键"
        )
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


GROUP_PROMPT_FINGERPRINT = group_prompt_fingerprint()
