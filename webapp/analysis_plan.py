# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Read-only analysis planning using the same prompt and cache-key builders as execution."""

import hashlib
import json
import math

from analyzer import deepseek_client as dc
from analyzer import group_client as gc
from analyzer import recap_client
from analyzer.cache_policy import content_cache_policy
from analyzer.dialog import preview_vision_context
from analyzer.usage import _price_for
from config import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from webapp import store


def _labels(chat):
    return {
        "self": chat.self_name,
        "other": chat.other_name,
    }


def _person_template(dimension):
    return dc._person_analysis_spec(dimension)


def _member_template():
    return gc._member_prompt_template()


def _unit(
    key,
    dimension,
    label,
    input_chars,
    *,
    status="missing",
    vision_uncertain=False,
    thinking=False,
    refresh=False,
    identity="",
):
    cache_hit = status in {"缓存命中", "维度缓存命中"}
    no_work = status == "无可分析消息"
    calls = {"text_min": 0 if cache_hit or no_work else 1, "text_max": 0 if cache_hit or no_work else 1}
    if vision_uncertain and not cache_hit and not refresh:
        calls["text_min"] = 0
    max_tokens = dc.MAX_TOKENS_BY_DIM.get(dimension, 0)
    return {
        "key": key,
        "dimension": dimension,
        "label": label,
        "status": status,
        "cached": cache_hit,
        "input_chars": input_chars,
        "input_tokens_min": math.ceil(input_chars / 4) if input_chars else 0,
        "input_tokens_max": input_chars,
        "text_calls_min": calls["text_min"],
        "text_calls_max": calls["text_max"],
        "vision_calls_min": 0,
        "vision_calls_max": 1 if vision_uncertain and not cache_hit else 0,
        "vision_uncertain": vision_uncertain,
        "thinking": thinking,
        "max_output_tokens": max_tokens,
        "refresh": bool(refresh),
        "cache_identity": identity,
    }


def _month_cache_status(prompt, system_prompt, dimension, chat_hash, refresh):
    fp = getattr(prompt, "cache_fingerprint", None) or dc.fingerprint_for_dimension(dimension)
    keys = [dc._month_key(system_prompt, prompt, fp)]
    keys.extend(
        dc._month_key(system_prompt, prompt, old_fp)
        for old_fp in dc._legacy_chain_for_fingerprint_value(fp)
        if old_fp != fp
    )
    if refresh:
        return False, keys[0]
    thinking = dc.thinking_enabled(dimension)
    hit = any(
        dc._read_result_cache(key, dimension, expect_thinking=thinking, chat_hash=chat_hash, read_only=True)
        is not None
        for key in keys
    )
    return hit, keys[0]


def _build_units(chat, chat_hash, dimension, dimension_cache, refresh):
    units = []
    dimension_cached = dimension_cache is not None and not refresh
    with preview_vision_context() as vision_state:
        if dimension in {
            "emotion",
            "topics",
            "relationship",
            "group_dynamics",
            "group_topics",
            "group_emotion",
        }:
            months = chat.months()
            if dimension in {"emotion", "topics", "relationship"}:
                systems = {
                    "emotion": dc.SYSTEM_PROMPT_EMOTION,
                    "topics": dc.SYSTEM_PROMPT_TOPICS,
                    "relationship": dc.SYSTEM_PROMPT_RELATIONSHIP,
                }
                system_prompt = systems[dimension]

                def make_prompt(month, messages):
                    return dc._month_prompt(chat, month, messages, chat_hash)
            else:
                import analyzer.group_prompts as gp

                systems = {
                    "group_dynamics": gp.GROUP_SYSTEM_PROMPT_DYNAMICS,
                    "group_topics": gp.GROUP_SYSTEM_PROMPT_TOPICS,
                    "group_emotion": gp.GROUP_SYSTEM_PROMPT_EMOTION,
                }
                system_prompt = systems[dimension]

                def make_prompt(month, messages):
                    return gc._group_month_prompt(chat, month, messages, chat_hash)

            for month, messages in months.items():
                before = len(vision_state["uncertain"])
                with content_cache_policy(refresh):
                    prompt = make_prompt(month, messages)
                if not prompt.strip():
                    continue
                uncertain = len(vision_state["uncertain"]) > before
                hit, cache_key = _month_cache_status(prompt, system_prompt, dimension, chat_hash, refresh)
                units.append(
                    _unit(
                        f"{dimension}:month:{month}",
                        dimension,
                        month,
                        len(system_prompt) + len(prompt),
                        status="维度缓存命中"
                        if dimension_cached
                        else "缓存命中"
                        if hit
                        else "暂时无法确定"
                        if uncertain
                        else "待调用",
                        vision_uncertain=uncertain,
                        thinking=dc.thinking_enabled(dimension),
                        refresh=refresh,
                        identity=hashlib.sha256(cache_key.encode()).hexdigest(),
                    )
                )
        elif dimension in {"habits", "profile"}:
            system_prompt, template, sample_size, stratified = _person_template(dimension)
            labels = _labels(chat)
            for person_key, messages in (
                ("self", [m for m in chat.messages if m.sender_uid == chat.self_uid]),
                ("other", [m for m in chat.messages if m.sender_uid != chat.self_uid]),
            ):
                target_uids = getattr(chat, "analysis_target_uids", None)
                if target_uids is not None:
                    if person_key == "self" and chat.self_uid not in target_uids:
                        continue
                    if person_key == "other":
                        messages = [message for message in messages if message.sender_uid in target_uids]
                        if not messages:
                            continue
                before = len(vision_state["uncertain"])
                with content_cache_policy(refresh):
                    request = dc._prepare_person_request(
                        system_prompt,
                        sample_size,
                        messages,
                        labels[person_key],
                        template,
                        tag=dimension,
                        stratified=stratified,
                        chat_hash=chat_hash,
                        context_messages=chat.messages if dimension == "profile" else None,
                        speaker_name_of=(
                            lambda message: (
                                chat.self_name if message.sender_uid == chat.self_uid else chat.other_name
                            )
                        )
                        if dimension == "profile"
                        else None,
                        scope_id=getattr(chat, "analysis_scope_id", ""),
                    )
                uncertain = len(vision_state["uncertain"]) > before
                if request is None:
                    units.append(
                        _unit(
                            f"{dimension}:person:{person_key}",
                            dimension,
                            labels[person_key],
                            0,
                            status="无可分析消息",
                        )
                    )
                    continue
                hit = (
                    False
                    if refresh
                    else dc._read_result_cache(
                        request["cache_key"],
                        dimension,
                        expect_thinking=request["thinking"],
                        chat_hash=chat_hash,
                        read_only=True,
                    )
                    is not None
                )
                units.append(
                    _unit(
                        f"{dimension}:person:{person_key}",
                        dimension,
                        labels[person_key],
                        len(system_prompt) + len(request["user_content"]),
                        status="维度缓存命中"
                        if dimension_cached
                        else "缓存命中"
                        if hit
                        else "暂时无法确定"
                        if uncertain
                        else "待调用",
                        vision_uncertain=uncertain,
                        thinking=request["thinking"],
                        refresh=refresh,
                        identity=hashlib.sha256(request["cache_key"].encode()).hexdigest(),
                    )
                )
        elif dimension == "member_profiles":
            import analyzer.group_prompts as gp

            template = _member_template()
            system_prompt = gp.GROUP_SYSTEM_PROMPT_MEMBER_PROFILE
            for member in gc.select_ai_members(chat):
                before = len(vision_state["uncertain"])
                with content_cache_policy(refresh):
                    request = gc._prepare_member_request(chat, member, template, dimension, chat_hash)
                uncertain = len(vision_state["uncertain"]) > before
                if request is None:
                    continue
                hit = (
                    False
                    if refresh
                    else dc._read_result_cache(
                        request["cache_key"],
                        dimension,
                        expect_thinking=request["thinking"],
                        chat_hash=chat_hash,
                        read_only=True,
                    )
                    is not None
                )
                units.append(
                    _unit(
                        f"{dimension}:member:{member.uid}",
                        dimension,
                        member.name,
                        len(system_prompt) + len(request["prompt"]),
                        status="维度缓存命中"
                        if dimension_cached
                        else "缓存命中"
                        if hit
                        else "暂时无法确定"
                        if uncertain
                        else "待调用",
                        vision_uncertain=uncertain,
                        thinking=request["thinking"],
                        refresh=refresh,
                        identity=hashlib.sha256(request["cache_key"].encode()).hexdigest(),
                    )
                )
        elif dimension == "recap":
            digest = recap_client._recap_digest(chat)
            units.append(
                _unit(
                    "recap:all",
                    dimension,
                    "完整对话",
                    len(recap_client.SYSTEM_PROMPT_RECAP) + len(digest),
                    status="维度缓存命中" if dimension_cached else "待调用",
                    thinking=dc.thinking_enabled(dimension),
                    refresh=refresh,
                    identity=hashlib.sha256(
                        (recap_client.RECAP_PROMPT_FINGERPRINT + digest).encode("utf-8")
                    ).hexdigest(),
                )
            )
    return units


def build_analysis_plan(chat, chat_hash, dimensions, *, refresh=False):
    """Return a stable, read-only plan. No cache mtime, migration, writes or model calls."""
    dimensions = list(dict.fromkeys(dimensions))
    entries = []
    for dimension in dimensions:
        fingerprint = store.analysis_cache_fingerprint(dimension, chat)
        with content_cache_policy(refresh):
            dimension_cache = (
                None
                if refresh
                else store._read_cache(dimension, chat_hash, fingerprint=fingerprint, read_only=True)
            )
            units = _build_units(chat, chat_hash, dimension, dimension_cache, refresh)
        entries.extend(units)

    price_in, price_out = _price_for(DEEPSEEK_MODEL)
    prices_configured = bool(price_in or price_out)
    all_units = [u for u in entries if u["status"] != "无可分析消息"]
    new_units = [u for u in all_units if not u["cached"]]
    input_min = sum(u["input_tokens_min"] for u in new_units if u["text_calls_min"] > 0)
    input_max = sum(
        u["input_tokens_max"] for u in new_units if u["text_calls_max"] > 0 and not u["vision_uncertain"]
    )
    text_min = sum(u["text_calls_min"] for u in all_units)
    text_max = sum(u["text_calls_max"] for u in all_units)
    vision_max = sum(u["vision_calls_max"] for u in all_units)
    vision_min = int(bool(refresh and any(u["vision_calls_max"] for u in all_units)))
    output_max = sum(u["max_output_tokens"] for u in new_units)
    cost = None
    if prices_configured:
        low = input_min / 1_000_000 * price_in
        uncertain_input = any(unit["vision_uncertain"] and not unit["cached"] for unit in entries)
        high = None if uncertain_input else (input_max * price_in + output_max * price_out) / 1_000_000
        cost = {
            "min": round(low, 4),
            "max": round(high, 4) if high is not None else None,
            "currency": "CNY",
            "incomplete": uncertain_input,
        }

    config = {
        "model": DEEPSEEK_MODEL,
        "base_url": DEEPSEEK_BASE_URL,
        "chat_hash": chat_hash,
        "scope_id": getattr(chat, "analysis_scope_id", ""),
        "prices": [price_in, price_out],
        "refresh": bool(refresh),
        "dimensions": dimensions,
        "units": [
            {
                "key": u["key"],
                "status": u["status"],
                "input_chars": u["input_chars"],
                "vision_uncertain": u["vision_uncertain"],
                "thinking": u["thinking"],
                "max_output_tokens": u["max_output_tokens"],
                "cache_identity": u["cache_identity"],
            }
            for u in entries
        ],
    }
    plan_id = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {
        "plan_id": plan_id,
        "dimensions": dimensions,
        "refresh": bool(refresh),
        "scope_id": getattr(chat, "analysis_scope_id", ""),
        "model": DEEPSEEK_MODEL,
        "units": entries,
        "summary": {
            "unit_count": len(entries),
            "cache_hits": sum(1 for u in entries if u["cached"]),
            "text_calls_min": text_min,
            "text_calls_max": text_max,
            "vision_calls_min": vision_min,
            "vision_calls_max": vision_max,
            "input_tokens_min": input_min,
            "input_tokens_max": input_max,
            "output_tokens_max": output_max,
            "price_input_per_million": price_in,
            "price_output_per_million": price_out,
            "cost": cost,
            "vision_note": (
                "图片摘要调用单列为上限估算；不同单元可能复用同一摘要，"
                "失败重试与图片 token 不计入文本 token 区间。"
            ),
            "token_note": "输入 token 按字符数的 1/4 到 1 倍估算；输出上限按配置预算计，实际输出通常更少。",
            "cost_note": (
                "费用为价格配置下的估算区间，不含图片 token、失败重试及供应商附加费用；"
                "待生成图片摘要时只给已知文本的费用下限。"
            )
            if prices_configured
            else "未配置可识别的模型价格，仅显示调用数和 token 估算。",
        },
    }
