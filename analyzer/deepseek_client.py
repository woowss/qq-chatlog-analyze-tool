# Copyright (C) 2026 Zhang Yangming. All Rights Reserved.
# Licensed under MIT License.
# SPDX-License-Identifier: MIT
#
"""DeepSeek API 调用封装"""
import json
import time
from typing import Any, Optional

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
from parser.qq_parser import ChatData, split_by_month
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
    SYSTEM_PROMPT_PROFILE,
)


def _get_client() -> Optional[OpenAI]:
    """获取 OpenAI 客户端；未配置 API Key 则返回 None"""
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "你的DeepSeek_API_Key":
        return None
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def _build_dialog(messages: list, self_uid: str, self_name: str, other_name: str) -> str:
    """将消息列表拼接成对话文本"""
    lines = []
    for m in messages:
        name = self_name if m.sender_uid == self_uid else other_name
        lines.append(f"[{m.time_str}] {name}: {m.text}")
    return "\n".join(lines)


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


def analyze_emotion(chat: ChatData) -> dict[str, Any]:
    """逐月情绪分析，返回 {"2025-09": {...}, ...}"""
    months = split_by_month(chat)
    results: dict[str, Any] = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)
        result = _call_api(
            SYSTEM_PROMPT_EMOTION,
            f"以下是 {period} 月的对话记录：\n\n{dialog}",
        )
        if result:
            result["period"] = period
            result["month"] = period
            results[period] = result
    return results


def analyze_topics(chat: ChatData) -> dict[str, Any]:
    """逐月话题分析"""
    months = split_by_month(chat)
    results: dict[str, Any] = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)
        result = _call_api(
            SYSTEM_PROMPT_TOPICS,
            f"以下是 {period} 月的对话记录：\n\n{dialog}",
        )
        if result:
            result["period"] = period
            result["month"] = period
            results[period] = result
    return results


def analyze_relationship(chat: ChatData) -> dict[str, Any]:
    """逐月人际关系分析"""
    months = split_by_month(chat)
    results: dict[str, Any] = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog(msgs, chat.self_uid, chat.self_name, chat.other_name)
        result = _call_api(
            SYSTEM_PROMPT_RELATIONSHIP,
            f"以下是 {period} 月的对话记录：\n\n{dialog}",
        )
        if result:
            result["period"] = period
            result["month"] = period
            results[period] = result
    return results


def analyze_habits(chat: ChatData) -> dict[str, Any]:
    """分析双方的语言习惯"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}

    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        sample = msgs[-200:]  # 取最近 200 条
        lines = [f"[{m.time_str}] {m.text}" for m in sample]
        dialog = "\n".join(lines)
        display_name = chat.self_name if person_key == "self" else chat.other_name

        result = _call_api(
            SYSTEM_PROMPT_HABITS,
            f"分析以下 {display_name} 的发言，总结其说话风格：\n\n{dialog}",
        )
        if result:
            result["name"] = display_name
            result["total_messages"] = len(msgs)
            results[person_key] = result

    return results


def analyze_profile(chat: ChatData) -> dict[str, Any]:
    """AI 人物锐评 — 分析双方的性格画像"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}

    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        # 取最近 300 条作为样本
        sample = msgs[-300:]
        lines = [f"[{m.time_str}] {m.text}" for m in sample if m.text]
        dialog = "\n".join(lines)
        display_name = chat.self_name if person_key == "self" else chat.other_name

        result = _call_api(
            SYSTEM_PROMPT_PROFILE,
            f"以下是 {display_name} 在私聊中的发言记录，请对其进行深度性格分析：\n\n{dialog}",
        )
        if result:
            result["name"] = display_name
            result["total_messages"] = len(msgs)
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
