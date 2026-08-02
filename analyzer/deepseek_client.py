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
"""DeepSeek API 调用封装"""
import concurrent.futures
import json
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Optional

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
from parser.qq_parser import CST, ChatData, split_by_month
from analyzer.logger import get_logger
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
    SYSTEM_PROMPT_PROFILE,
)

logger = get_logger("deepseek")

# 单月对话文本上限（字符数）。超出上限的月份会做等间隔抽样，
# 保证整月分布仍在模型上下文窗口内，避免 "context length exceeded" 导致整体失败。
MAX_DIALOG_CHARS = 50000
# 并发分析的月份数（兼顾速度与 API 限流）
CONCURRENCY = 3
# 单次 API 请求超时（秒）
REQUEST_TIMEOUT = 120

# 不应进入 AI 分析的 msg_type：系统消息 / 转发 / 商城表情等
_SKIP_MSG_TYPES = {"type_11", "type_17", "type_23"}


def _get_client() -> Optional[OpenAI]:
    """获取 OpenAI 客户端；未配置 API Key 则返回 None"""
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "你的DeepSeek_API_Key":
        return None
    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=REQUEST_TIMEOUT,
        max_retries=0,  # 重试由 _call_api 自行实现（指数退避）
    )


def _fit_lines(lines: list[str], max_chars: int) -> list[str]:
    """把消息行压到 max_chars 以内：先等间隔抽样，再按需从尾部截断。

    等间隔抽样能尽量保留整月的对话分布，而不是只留最新的消息。
    """
    if not lines:
        return []
    total = sum(len(line) + 1 for line in lines)
    if total <= max_chars:
        return lines

    # 1) 等间隔抽样
    step = max(1, (total + max_chars - 1) // max_chars)
    sampled = lines[::step]
    if len(sampled) < 3 and len(lines) > 3:
        sampled = lines[-30:]
    total = sum(len(line) + 1 for line in sampled)
    if total <= max_chars:
        return sampled

    # 2) 仍超限（如存在单条超长消息）：从尾部逐条截断
    kept: list[str] = []
    used = 0
    for line in reversed(sampled):
        if used + len(line) + 1 > max_chars:
            if not kept and line:
                kept.append(line[:max_chars])
            break
        used += len(line) + 1
        kept.append(line)
    return list(reversed(kept))


def _has_content(m) -> bool:
    """消息是否有可喂给模型的内容（正文或图片/表情/回复等信号）"""
    return bool(m.text) or m.has_image or m.is_reply or bool(m.face_names) or bool(m.face_ids)


def _short_time(time_str: str) -> str:
    """把 '2025-09-16 21:56:49' 压缩为 '09-16 21:56'，省 token 且同月内信息无损失"""
    return time_str[5:16] if len(time_str) >= 16 else time_str


def _conversation_stats(messages: list) -> tuple:
    """精简统计：最活跃小时 + 回复间隔中位数（秒），喂给模型作参考数据，提升针对性"""
    hours: Counter = Counter()
    gaps: list[float] = []
    last = None
    for m in messages:
        hours[datetime.fromtimestamp(m.timestamp / 1000, tz=CST).hour] += 1
        if last is not None and last.sender_uid != m.sender_uid:
            gap = (m.timestamp - last.timestamp) / 1000
            if 0 < gap <= 3600 * 6:
                gaps.append(gap)
        last = m
    peak = hours.most_common(1)[0][0] if hours else None
    median = sorted(gaps)[len(gaps) // 2] if gaps else None
    return peak, median


def _message_line(m, name: str) -> str:
    """单条消息 → 对话行，附带图片/表情/回复标注，便于模型理解非文本内容"""
    body = m.text or ""
    marks = []
    if m.has_image:
        marks.append("图片")
    if m.is_reply:
        marks.append("回复")
    if m.face_names:
        marks.append("表情:" + "、".join(m.face_names[:4]))
    prefix = f"[{_short_time(m.time_str)}] {name}:"
    if marks:
        mark = "[" + ", ".join(marks) + "]"
        return f"{prefix} {body} {mark}".strip() if body else f"{prefix} {mark}"
    return f"{prefix} {body}".strip() if body else prefix


def _build_dialog(messages: list, self_uid: str, self_name: str, other_name: str,
                  max_chars: Optional[int] = MAX_DIALOG_CHARS) -> str:
    """构建喂给模型的对话内容：统计头 + 带标注的对话行。

    返回形如 "统计：共 N 条消息（我方 a 条 / 对方 b 条，图片 c 张）。\n\n[时间] 昵称: 内容 [标注]\n..."。
    统计头给模型全貌（即使抽样截断也能知道真实消息量），避免被样本误导。
    """
    valid = [m for m in messages if _has_content(m) and m.msg_type not in _SKIP_MSG_TYPES]
    if not valid:
        return ""
    total = len(valid)
    self_n = sum(1 for m in valid if m.sender_uid == self_uid)
    other_n = total - self_n
    images = sum(1 for m in valid if m.has_image)
    lines = [
        _message_line(m, self_name if m.sender_uid == self_uid else other_name)
        for m in valid
    ]
    original_n = len(lines)
    if max_chars:
        lines = _fit_lines(lines, max_chars)
    parts = [f"共 {total} 条消息（我方 {self_n} 条 / 对方 {other_n} 条，图片 {images} 张）"]
    peak, median = _conversation_stats(valid)
    if peak is not None:
        parts.append(f"最活跃时段约 {peak} 时")
    if median is not None:
        parts.append(f"回复间隔中位数约 {int(median)} 秒")
    head = "统计：" + "，".join(parts)
    if len(lines) < original_n:
        head += f"，因篇幅限制展示其中 {len(lines)} 条（等间隔抽样，覆盖整月分布）"
    return f"{head}。\n\n" + "\n".join(lines)


def _call_api(system_prompt: str, user_content: str, retry: int = 2) -> Optional[dict]:
    """调用 DeepSeek API，返回解析后的 JSON"""
    client = _get_client()
    if client is None:
        return None

    for attempt in range(retry + 1):
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            if attempt < retry:
                time.sleep(2 ** attempt)
                continue
            raise  # 最后仍失败则抛出


def _analyze_periods(months: dict[str, list], system_prompt: str,
                     make_prompt: Callable[[str, list], str]) -> dict[str, Any]:
    """并发逐月调用 API，返回 {period: result}。

    单月失败仅记录日志并跳过，不中断整体分析。
    """
    results: dict[str, Any] = {}

    def _work(period: str, msgs: list) -> tuple[str, Optional[dict]]:
        try:
            prompt = make_prompt(period, msgs)
            if not prompt.strip():
                return period, None
            result = _call_api(system_prompt, prompt)
            if result:
                result["period"] = period
                result["month"] = period
                return period, result
        except Exception as e:
            logger.error("%s 月 AI 分析失败: %s", period, e)
        return period, None

    items = list(months.items())
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(_work, period, msgs) for period, msgs in items]
        for fut in concurrent.futures.as_completed(futures):
            period, result = fut.result()
            if result:
                results[period] = result

    # 按月份自然序返回
    return {period: results[period] for period in months if period in results}


# ---------------------------------------------------------------------------
# 输出校验与修正（防御模型不按约束输出，保证数值正确性）
# ---------------------------------------------------------------------------


def _clamp_int(obj: dict, key: str, lo: int, hi: int) -> None:
    """把数值字段夹紧到 [lo, hi] 的整数；非法值归零"""
    try:
        obj[key] = int(min(max(float(obj.get(key, 0)), lo), hi))
    except (TypeError, ValueError):
        obj[key] = 0


def _clamp_float(obj: dict, key: str, lo: float, hi: float, default: float) -> None:
    """把数值字段夹紧到 [lo, hi] 的小数，保留两位；非法值用 default"""
    try:
        obj[key] = round(min(max(float(obj.get(key, default)), lo), hi), 2)
    except (TypeError, ValueError):
        obj[key] = default


def _normalize_topic_weights(obj: dict) -> None:
    """把各话题 weight 归一化，保证总和恒为 1.0（防御模型权重不收敛到 1）"""
    topics = obj.get("topics")
    if not isinstance(topics, list):
        return
    total = 0.0
    for t in topics:
        if not isinstance(t, dict):
            continue
        try:
            total += float(t.get("weight", 0))
        except (TypeError, ValueError):
            t["weight"] = 0.0
    if total <= 0:
        return
    for t in topics:
        if not isinstance(t, dict):
            continue
        try:
            t["weight"] = round(float(t.get("weight", 0)) / total, 2)
        except (TypeError, ValueError):
            t["weight"] = 0.0


def analyze_emotion(chat: ChatData) -> dict[str, Any]:
    """逐月情绪分析，返回 {"2025-09": {...}, ...}"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_EMOTION,
        lambda p, msgs: f"以下是 {p} 月的对话数据：\n\n"
                        f"{_build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)}",
    )
    # 强度夹紧到 0-10，防御越界/非法值
    for r in results.values():
        _clamp_int(r, "self_intensity", 0, 10)
        _clamp_int(r, "other_intensity", 0, 10)
    return results


def analyze_topics(chat: ChatData) -> dict[str, Any]:
    """逐月话题分析"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_TOPICS,
        lambda p, msgs: f"以下是 {p} 月的对话数据：\n\n"
                        f"{_build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)}",
    )
    # 权重归一化，保证各月话题占比之和恒为 1.0
    for r in results.values():
        _normalize_topic_weights(r)
    return results


def analyze_relationship(chat: ChatData) -> dict[str, Any]:
    """逐月人际关系分析"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_RELATIONSHIP,
        lambda p, msgs: f"以下是 {p} 月的对话数据：\n\n"
                        f"{_build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)}",
    )
    for r in results.values():
        _clamp_int(r, "closeness_score", 1, 10)
        _clamp_float(r, "initiator_ratio_self", 0.0, 1.0, 0.5)
    return results


def _analyze_person(system_prompt: str, sample_size: int, msgs: list,
                    display_name: str, prompt_template: str) -> Optional[dict]:
    """单人的习惯/锐评分析：取最近 sample_size 条样本，失败仅记日志。"""
    try:
        sample = msgs[-sample_size:]
        valid = [m for m in sample if _has_content(m) and m.msg_type not in _SKIP_MSG_TYPES]
        if not valid:
            return None
        lines = [_message_line(m, display_name) for m in valid]
        original_n = len(lines)
        lines = _fit_lines(lines, MAX_DIALOG_CHARS)
        head = f"统计：{display_name} 共发言 {len(msgs)} 条（样本 {len(valid)} 条，图片 "
        head += f"{sum(1 for m in valid if m.has_image)} 张）"
        if len(lines) < original_n:
            head += f"，因篇幅限制展示其中 {len(lines)} 条"
        dialog = f"{head}。\n\n" + "\n".join(lines)
        if not dialog.strip():
            return None
        result = _call_api(system_prompt, prompt_template.format(display_name=display_name, dialog=dialog))
        if result:
            result["name"] = display_name
            result["total_messages"] = len(msgs)
            return result
    except Exception as e:
        logger.error("%s 的 AI 分析失败: %s", display_name, e)
    return None


def analyze_habits(chat: ChatData) -> dict[str, Any]:
    """分析双方的语言习惯"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
    template = "分析以下 {display_name} 的发言，总结其说话风格：\n\n{dialog}"
    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        display_name = chat.self_name if person_key == "self" else chat.other_name
        result = _analyze_person(SYSTEM_PROMPT_HABITS, 200, msgs, display_name, template)
        if result:
            results[person_key] = result
    return results


def analyze_profile(chat: ChatData) -> dict[str, Any]:
    """AI 人物锐评 — 分析双方的性格画像"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
    template = "以下是 {display_name} 在私聊中的发言记录，请对其进行深度性格分析：\n\n{dialog}"
    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        display_name = chat.self_name if person_key == "self" else chat.other_name
        result = _analyze_person(SYSTEM_PROMPT_PROFILE, 300, msgs, display_name, template)
        if result:
            results[person_key] = result
    return results


def analyze_all(chat: ChatData) -> dict:
    """一次运行所有分析"""
    return {
        "emotion": analyze_emotion(chat),
        "topics": analyze_topics(chat),
        "relationship": analyze_relationship(chat),
        "habits": analyze_habits(chat),
        "profile": analyze_profile(chat),
    }


def is_api_configured() -> bool:
    """检查 API Key 是否已配置"""
    return bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "你的DeepSeek_API_Key"
