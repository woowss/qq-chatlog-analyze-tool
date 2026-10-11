#!/usr/bin/env python3
"""Validation and human-review tools for the synthetic analysis benchmark.

Ordinary commands are offline. Real model calls require the dedicated run-real-plan
command, an explicit --execute-real flag, and a separately configured benchmark key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "fixtures" / "analysis_benchmark_v1.json"
ALLOWED_CLAIM_TYPES = {"fact", "interpretation", "uncertainty", "opinion"}
MAX_SCENARIOS = 30
MAX_CLAIMS_PER_RECORD = 200
REVIEW_RATING_FIELDS = (
    "evidence_support",
    "key_fact_coverage",
    "interpretation_calibration",
    "uncertainty_calibration",
)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _dataset_errors(dataset: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(dataset, dict):
        return ["dataset 根节点必须是对象"]
    for key in ("dataset_id", "dataset_version", "description"):
        if not _is_text(dataset.get(key)):
            errors.append(f"dataset.{key} 必须是非空字符串")
    categories = dataset.get("categories")
    scenarios = dataset.get("scenarios")
    if not isinstance(categories, list) or not categories or any(not _is_text(item) for item in categories):
        errors.append("dataset.categories 必须是非空字符串数组")
        categories = []
    if len(set(categories)) != len(categories):
        errors.append("dataset.categories 存在重复项")
    if not isinstance(scenarios, list) or not 20 <= len(scenarios) <= MAX_SCENARIOS:
        errors.append(f"dataset.scenarios 数量必须在 20 至 {MAX_SCENARIOS} 之间")
        scenarios = scenarios if isinstance(scenarios, list) else []

    scenario_ids: set[str] = set()
    category_counts: Counter[str] = Counter()
    all_message_ids: set[str] = set()
    for si, scenario in enumerate(scenarios):
        prefix = f"scenarios[{si}]"
        if not isinstance(scenario, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        sid = scenario.get("id")
        category = scenario.get("category")
        if not _is_text(sid):
            errors.append(f"{prefix}.id 必须是非空字符串")
        elif sid in scenario_ids:
            errors.append(f"{prefix}.id 重复")
        else:
            scenario_ids.add(sid)
        if category not in categories:
            errors.append(f"{prefix}.category 不在 categories 中")
        else:
            category_counts[category] += 1

        messages = scenario.get("messages")
        if not isinstance(messages, list) or not messages:
            errors.append(f"{prefix}.messages 必须是非空数组")
            messages = []
        local_ids: set[str] = set()
        for mi, message in enumerate(messages):
            mp = f"{prefix}.messages[{mi}]"
            if not isinstance(message, dict):
                errors.append(f"{mp} 必须是对象")
                continue
            for key in ("id", "time", "speaker", "text"):
                if not _is_text(message.get(key)):
                    errors.append(f"{mp}.{key} 必须是非空字符串")
            mid = message.get("id")
            if _is_text(mid):
                if mid in local_ids or mid in all_message_ids:
                    errors.append(f"{mp}.id 重复")
                local_ids.add(mid)
                all_message_ids.add(mid)
            stamp = message.get("time")
            if _is_text(stamp):
                try:
                    datetime.fromisoformat(stamp)
                except ValueError:
                    errors.append(f"{mp}.time 不是有效 ISO 日期时间")

        for list_key in ("facts", "acceptable_interpretations", "rejected_overclaims"):
            items = scenario.get(list_key)
            if not isinstance(items, list) or not items:
                errors.append(f"{prefix}.{list_key} 必须是非空数组")
                continue
            for ii, item in enumerate(items):
                if list_key == "facts":
                    if (
                        not isinstance(item, dict)
                        or not _is_text(item.get("id"))
                        or not _is_text(item.get("statement"))
                    ):
                        errors.append(f"{prefix}.{list_key}[{ii}] 缺少 id 或 statement")
                        continue
                    evidence = item.get("evidence_ids")
                    if not isinstance(evidence, list) or not evidence:
                        errors.append(f"{prefix}.{list_key}[{ii}].evidence_ids 必须是非空数组")
                    elif any(mid not in local_ids for mid in evidence):
                        errors.append(f"{prefix}.{list_key}[{ii}] 引用了场景外消息")
                elif not _is_text(item):
                    errors.append(f"{prefix}.{list_key}[{ii}] 必须是非空字符串")

    for category in categories:
        if category_counts[category] < 2:
            errors.append(f"类别 {category} 至少需要 2 个场景")
    return errors


def validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    dataset = _load_json(path)
    errors = _dataset_errors(dataset)
    counts = Counter(item["category"] for item in dataset.get("scenarios", []) if isinstance(item, dict))
    return {
        "valid": not errors,
        "dataset_id": dataset.get("dataset_id") if isinstance(dataset, dict) else None,
        "dataset_version": dataset.get("dataset_version") if isinstance(dataset, dict) else None,
        "scenario_count": len(dataset.get("scenarios", [])) if isinstance(dataset, dict) else 0,
        "category_counts": dict(sorted(counts.items())),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "errors": errors,
    }


def _scenario_indexes(dataset: dict) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    scenarios = {item["id"]: item for item in dataset["scenarios"]}
    messages = {
        sid: {message["id"]: message for message in scenario["messages"]}
        for sid, scenario in scenarios.items()
    }
    return scenarios, messages


def _opaque_message_id_maps(
    scenario: dict, input_message_ids: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return per-request aliases that reveal neither scenario nor fixture IDs."""
    selected = [message for message in scenario["messages"] if message["id"] in input_message_ids]
    original_to_alias = {message["id"]: f"m{index}" for index, message in enumerate(selected, 1)}
    alias_to_original = {alias: original for original, alias in original_to_alias.items()}
    return original_to_alias, alias_to_original


def _restore_evidence_ids(value: Any, alias_to_original: dict[str, str]) -> tuple[Any, list[str]]:
    """Restore model-visible aliases recursively and return unknown evidence aliases."""
    unknown: list[str] = []
    if isinstance(value, dict):
        restored = {}
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                mapped = []
                for alias in item:
                    if isinstance(alias, str) and alias in alias_to_original:
                        mapped.append(alias_to_original[alias])
                    else:
                        mapped.append(alias)
                        if isinstance(alias, str):
                            unknown.append(alias)
                restored[key] = mapped
            else:
                restored[key], nested_unknown = _restore_evidence_ids(item, alias_to_original)
                unknown.extend(nested_unknown)
        return restored, unknown
    if isinstance(value, list):
        restored = []
        for item in value:
            mapped, nested_unknown = _restore_evidence_ids(item, alias_to_original)
            restored.append(mapped)
            unknown.extend(nested_unknown)
        return restored, unknown
    return value, unknown


def _build_prompt_messages(plan: dict, scenario: dict, record: dict) -> tuple[list[dict], int, int, int]:
    """Build the exact synthetic-only request and conservative tokenizer-independent budget estimate."""
    message_index = {message["id"]: message for message in scenario["messages"]}
    input_ids = set(record["input_message_ids"])
    selected = [message for message in scenario["messages"] if message["id"] in input_ids]
    if len(selected) != len(input_ids) or any(mid not in message_index for mid in input_ids):
        raise ValueError("input_message_ids 必须只引用场景内消息")

    original_to_alias, _ = _opaque_message_id_maps(scenario, input_ids)
    rendered_messages = [
        {
            "id": original_to_alias[message["id"]],
            **{key: message[key] for key in ("time", "speaker", "text")},
        }
        for message in selected
    ]

    system_content = plan["system_prompt"].strip() + (
        "\n\n只输出一个 JSON 对象，包含 answer 字符串和 claims 数组。"
        "每条 claim 包含 text、type（fact/interpretation/uncertainty/opinion）、"
        "可选 speaker、evidence_ids 和可选 quote。引用只能使用输入消息中的中性 ID；"
        "证据不足时应明确表达不确定性。"
    )
    user_content = (
        plan["user_prompt"].strip()
        + "\n输入消息（JSON 数组；按时间顺序）：\n"
        + json.dumps(rendered_messages, ensure_ascii=False, separators=(",", ":"))
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    prompt_chars = sum(len(item["content"]) for item in messages)
    message_chars = sum(len(message["text"]) for message in selected)
    # Provider tokenizers differ. UTF-8 bytes bound the text payload conservatively;
    # the fixed allowance covers chat roles and message framing. Actual usage is checked too.
    estimated_prompt_tokens = (
        sum(len(item["content"].encode("utf-8")) for item in messages) + 16 * len(messages) + 8
    )
    return messages, prompt_chars, message_chars, estimated_prompt_tokens


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _create_benchmark_client(api_key: str, base_url: str):
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=0)


def _review_metrics(records: list[dict], scenarios: dict[str, dict]) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {"records": 0, "claims": 0, "cited_claims": 0}
    )
    claim_count = 0
    cited_claim_count = 0
    for record in records:
        scenario = scenarios.get(record.get("scenario_id"), {})
        category = scenario.get("category", "unknown")
        metrics = by_category[category]
        metrics["records"] += 1
        claims = record.get("claims", [])
        if isinstance(claims, list):
            for claim in claims:
                if isinstance(claim, dict):
                    claim_count += 1
                    metrics["claims"] += 1
                    if claim.get("evidence_ids"):
                        cited_claim_count += 1
                        metrics["cited_claims"] += 1
    return {
        "claim_count": claim_count,
        "claims_with_citations": cited_claim_count,
        "citation_coverage": round(cited_claim_count / claim_count, 4) if claim_count else None,
        "by_category": dict(sorted(by_category.items())),
    }


def validate_run(path: Path, dataset_path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    dataset = _load_json(dataset_path)
    dataset_errors = _dataset_errors(dataset)
    if dataset_errors:
        return {"valid": False, "errors": ["benchmark dataset 无效", *dataset_errors]}
    run = _load_json(path)
    errors: list[str] = []
    if not isinstance(run, dict):
        return {"valid": False, "errors": ["run 根节点必须是对象"]}

    for key in ("run_id", "condition", "model", "prompt_version"):
        if not _is_text(run.get(key)):
            errors.append(f"run.{key} 必须是非空字符串")
    strategy = run.get("input_strategy")
    if (
        not isinstance(strategy, dict)
        or not _is_text(strategy.get("name"))
        or not _is_text(strategy.get("version"))
    ):
        errors.append("run.input_strategy 必须包含非空 name 和 version")
    if run.get("dataset_id") != dataset.get("dataset_id"):
        errors.append("run.dataset_id 与所选 benchmark 不一致")
    current_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if run.get("dataset_sha256") != current_hash:
        errors.append("run.dataset_sha256 与所选 benchmark 版本不一致")
    if run.get("evaluation_type") not in ("simulated", "real"):
        errors.append("run.evaluation_type 必须是 simulated 或 real")
    records = run.get("records")
    if not isinstance(records, list):
        errors.append("run.records 必须是数组")
        records = []
    if "execution_status" in run and run.get("execution_status") != "complete":
        errors.append("真实运行未完整执行")

    scenarios, message_indexes = _scenario_indexes(dataset)
    seen_scenarios: set[str] = set()
    token_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    latency_total = 0
    has_all_usage = bool(records)
    for ri, record in enumerate(records):
        rp = f"records[{ri}]"
        if not isinstance(record, dict):
            errors.append(f"{rp} 必须是对象")
            continue
        if record.get("execution_error"):
            errors.append(f"{rp} 模型输出或用量记录无效")
        sid = record.get("scenario_id")
        if sid not in scenarios:
            errors.append(f"{rp}.scenario_id 不存在于 benchmark")
            continue
        if sid in seen_scenarios:
            errors.append(f"{rp}.scenario_id 重复")
        seen_scenarios.add(sid)
        input_ids = record.get("input_message_ids")
        if not isinstance(input_ids, list) or any(not isinstance(mid, str) for mid in input_ids):
            errors.append(f"{rp}.input_message_ids 必须是字符串数组")
            input_ids = []
        message_index = message_indexes[sid]
        if len(input_ids) != len(set(input_ids)):
            errors.append(f"{rp}.input_message_ids 存在重复项")
        if any(mid not in message_index for mid in input_ids):
            errors.append(f"{rp}.input_message_ids 引用了场景外消息")
        input_set = set(input_ids)

        claims = record.get("claims", [])
        if not isinstance(claims, list) or len(claims) > MAX_CLAIMS_PER_RECORD:
            errors.append(f"{rp}.claims 必须是最多 {MAX_CLAIMS_PER_RECORD} 项的数组")
            claims = []
        for ci, claim in enumerate(claims):
            cp = f"{rp}.claims[{ci}]"
            if not isinstance(claim, dict) or not _is_text(claim.get("text")):
                errors.append(f"{cp} 必须包含非空 text")
                continue
            if claim.get("type", "fact") not in ALLOWED_CLAIM_TYPES:
                errors.append(f"{cp}.type 不受支持")
            speaker = claim.get("speaker")
            known_speakers = {message["speaker"] for message in scenarios[sid]["messages"]}
            if speaker is not None and (not isinstance(speaker, str) or speaker not in known_speakers):
                errors.append(f"{cp}.speaker 不属于该场景的说话者")
            evidence_ids = claim.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or any(not isinstance(mid, str) for mid in evidence_ids):
                errors.append(f"{cp}.evidence_ids 必须是字符串数组")
                evidence_ids = []
            if len(evidence_ids) != len(set(evidence_ids)):
                errors.append(f"{cp}.evidence_ids 存在重复项")
            if any(mid not in message_index for mid in evidence_ids):
                errors.append(f"{cp}.evidence_ids 引用了场景外消息")
            if any(mid not in input_set for mid in evidence_ids):
                errors.append(f"{cp}.evidence_ids 引用了模型输入中不存在的消息")
            quote = claim.get("quote")
            if quote is not None:
                if not _is_text(quote):
                    errors.append(f"{cp}.quote 必须是非空字符串")
                elif not any(
                    quote in message_index[mid]["text"] for mid in evidence_ids if mid in message_index
                ):
                    errors.append(f"{cp}.quote 不存在于声明的证据消息中")

        dimension = record.get("dimension")
        if dimension is not None:
            if not _is_text(dimension) or not isinstance(record.get("model_result"), (dict, list)):
                errors.append(f"{rp} 指定 dimension 时必须包含 JSON model_result")
            else:
                try:
                    from analyzer.result_schema import validate_dimension_result

                    validate_dimension_result(dimension, record["model_result"])
                except (ValueError, TypeError) as exc:
                    safe = exc.safe_message() if hasattr(exc, "safe_message") else str(exc)
                    errors.append(f"{rp}.model_result: {safe}")

        for key in token_totals:
            value = record.get(key)
            if value is None:
                has_all_usage = False
            elif not _is_nonnegative_int(value):
                errors.append(f"{rp}.{key} 必须是非负整数")
            else:
                token_totals[key] += value
        latency = record.get("latency_ms")
        if latency is None:
            has_all_usage = False
        elif not _is_nonnegative_int(latency):
            errors.append(f"{rp}.latency_ms 必须是非负整数")
        else:
            latency_total += latency

    if run.get("evaluation_type") == "real":
        limits = run.get("limits")
        required_limits = (
            "max_calls",
            "max_input_chars",
            "max_total_prompt_tokens",
            "max_output_tokens",
        )
        if not isinstance(limits, dict) or any(
            not _is_nonnegative_int(limits.get(key)) or limits[key] <= 0 for key in required_limits
        ):
            errors.append("真实评测必须记录正数 max_calls、max_input_chars、max_output_tokens 限额")
        else:
            if len(records) > limits["max_calls"]:
                errors.append("实际样本数超过 max_calls 限额")
            total_input_chars = 0
            for record in records:
                if not isinstance(record, dict) or record.get("scenario_id") not in message_indexes:
                    continue
                if "input_chars" in record:
                    if not _is_nonnegative_int(record["input_chars"]):
                        errors.append("真实运行记录 input_chars 必须是非负整数")
                    else:
                        total_input_chars += record["input_chars"]
                else:
                    ids = record.get("input_message_ids", [])
                    total_input_chars += sum(
                        len(message_indexes[record["scenario_id"]][mid]["text"])
                        for mid in ids
                        if mid in message_indexes[record["scenario_id"]]
                    )
            if total_input_chars > limits["max_input_chars"]:
                errors.append("实际输入字符数超过 max_input_chars 限额")
            execution = run.get("execution")
            if isinstance(execution, dict):
                attempted_chars = execution.get("input_chars_attempted")
                if attempted_chars is not None:
                    if not _is_nonnegative_int(attempted_chars):
                        errors.append("真实运行 execution.input_chars_attempted 必须是非负整数")
                    elif attempted_chars > limits["max_input_chars"]:
                        errors.append("真实运行尝试输入字符数超过 max_input_chars 限额")
                attempted_prompt_tokens = execution.get("prompt_tokens")
                if attempted_prompt_tokens is not None:
                    if not _is_nonnegative_int(attempted_prompt_tokens):
                        errors.append("真实运行 execution.prompt_tokens 必须是非负整数")
                    elif attempted_prompt_tokens > limits["max_total_prompt_tokens"]:
                        errors.append("真实运行输入 token 总数超过 max_total_prompt_tokens 限额")
            if not has_all_usage:
                errors.append("真实评测必须记录每条样本的输入/输出 token 与耗时")
            elif token_totals["prompt_tokens"] > limits["max_total_prompt_tokens"]:
                errors.append("输入 token 总数超过 max_total_prompt_tokens 限额")
            elif any(
                isinstance(record, dict)
                and isinstance(record.get("completion_tokens"), int)
                and record["completion_tokens"] > limits["max_output_tokens"]
                for record in records
            ):
                errors.append("单次调用的输出 token 超过 max_output_tokens 限额")
            if isinstance(execution, dict) and "calls_attempted" in execution:
                calls_attempted = execution["calls_attempted"]
                if not _is_nonnegative_int(calls_attempted):
                    errors.append("真实运行 execution.calls_attempted 必须是非负整数")
                elif calls_attempted > limits["max_calls"]:
                    errors.append("真实运行实际尝试调用数超过 max_calls 限额")
                elif run.get("execution_status") == "complete" and calls_attempted != len(records):
                    errors.append("完整运行的调用数与结果记录数不一致")

    prices = run.get("prices")
    estimated_cost = None
    if prices is not None:
        if (
            not isinstance(prices, dict)
            or not _is_text(prices.get("currency"))
            or not _is_finite_nonnegative_number(prices.get("input_per_million"))
            or not _is_finite_nonnegative_number(prices.get("output_per_million"))
        ):
            errors.append("prices 必须包含非负输入/输出百万 token 单价和 currency")
        elif has_all_usage:
            estimated_cost = round(
                token_totals["prompt_tokens"] * prices["input_per_million"] / 1_000_000
                + token_totals["completion_tokens"] * prices["output_per_million"] / 1_000_000,
                6,
            )
            limits = run.get("limits", {})
            budget = limits.get("cost_budget") if isinstance(limits, dict) else None
            if run.get("evaluation_type") == "real" and budget is None:
                errors.append("配置价格的真实评测必须记录 limits.cost_budget")
            if budget is not None and not _is_finite_nonnegative_number(budget):
                errors.append("limits.cost_budget 必须是非负数字")
            elif budget is not None and estimated_cost > budget:
                errors.append("估算费用超过 limits.cost_budget")

    coverage = len(seen_scenarios) / len(scenarios) if scenarios else 0
    return {
        "valid": not errors,
        "run_id": run.get("run_id"),
        "evaluation_type": run.get("evaluation_type"),
        "scenario_count": len(scenarios),
        "record_count": len(records),
        "sample_coverage": round(coverage, 4),
        "usage": {
            "complete": has_all_usage,
            "prompt_tokens": token_totals["prompt_tokens"] if has_all_usage else None,
            "completion_tokens": token_totals["completion_tokens"] if has_all_usage else None,
            "latency_ms": latency_total if has_all_usage else None,
            "estimated_cost": estimated_cost,
            "currency": prices.get("currency") if isinstance(prices, dict) else None,
        },
        "mechanical_metrics": _review_metrics(records, scenarios),
        "errors": errors,
    }


def validate_plan(path: Path, dataset_path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    plan = _load_json(path)
    dataset = _load_json(dataset_path)
    dataset_report = validate_dataset(dataset_path)
    errors: list[str] = []
    if not isinstance(plan, dict):
        return {"valid": False, "errors": ["plan 根节点必须是对象"]}
    if not dataset_report["valid"]:
        errors.append("benchmark dataset 无效")
    for key in ("run_id", "condition", "model", "prompt_version"):
        if not _is_text(plan.get(key)):
            errors.append(f"plan.{key} 必须是非空字符串")
    for key in ("system_prompt", "user_prompt"):
        if not _is_text(plan.get(key)):
            errors.append(f"plan.{key} 必须是非空字符串；真实执行需固化实际提示词")
    strategy = plan.get("input_strategy")
    if (
        not isinstance(strategy, dict)
        or not _is_text(strategy.get("name"))
        or not _is_text(strategy.get("version"))
    ):
        errors.append("plan.input_strategy 必须包含非空 name 和 version")
    if plan.get("evaluation_type") != "real":
        errors.append("validate-plan 仅接受显式 evaluation_type: real")
    current_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if plan.get("dataset_id") != dataset.get("dataset_id"):
        errors.append("plan.dataset_id 与所选 benchmark 不一致")
    if plan.get("dataset_sha256") != current_hash:
        errors.append("plan.dataset_sha256 与所选 benchmark 版本不一致")

    scenarios, _ = _scenario_indexes(dataset)
    records = plan.get("records")
    if not isinstance(records, list) or not records:
        errors.append("plan.records 必须是非空数组")
        records = []
    seen: set[str] = set()
    input_prompt_chars = 0
    input_message_chars = 0
    estimated_prompt_tokens = 0
    for index, record in enumerate(records):
        prefix = f"records[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        sid = record.get("scenario_id")
        if sid not in scenarios:
            errors.append(f"{prefix}.scenario_id 不存在于 benchmark")
            continue
        if sid in seen:
            errors.append(f"{prefix}.scenario_id 重复")
        seen.add(sid)
        input_ids = record.get("input_message_ids")
        if not isinstance(input_ids, list) or any(not isinstance(mid, str) for mid in input_ids):
            errors.append(f"{prefix}.input_message_ids 必须是字符串数组")
            continue
        if len(input_ids) != len(set(input_ids)):
            errors.append(f"{prefix}.input_message_ids 存在重复项")
        message_index = {message["id"]: message for message in scenarios[sid]["messages"]}
        if any(mid not in message_index for mid in input_ids):
            errors.append(f"{prefix}.input_message_ids 引用了场景外消息")
        input_message_chars += sum(
            len(message_index[mid]["text"]) for mid in input_ids if mid in message_index
        )
        if _is_text(plan.get("system_prompt")) and _is_text(plan.get("user_prompt")):
            try:
                _, prompt_chars, _, token_estimate = _build_prompt_messages(plan, scenarios[sid], record)
                input_prompt_chars += prompt_chars
                estimated_prompt_tokens += token_estimate
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{prefix} 无法构建请求：{type(exc).__name__}")

    limits = plan.get("limits")
    required_limits = ("max_calls", "max_input_chars", "max_total_prompt_tokens", "max_output_tokens")
    if not isinstance(limits, dict) or any(
        not _is_nonnegative_int(limits.get(key)) or limits[key] <= 0 for key in required_limits
    ):
        errors.append("真实评测计划必须为调用数、输入字符、总输入 token 和单次输出 token 设置正数限额")
    else:
        if len(records) > limits["max_calls"]:
            errors.append("计划调用数超过 max_calls 限额")
        if input_prompt_chars > limits["max_input_chars"]:
            errors.append("完整请求提示词字符数超过 max_input_chars 限额")
        if estimated_prompt_tokens > limits["max_total_prompt_tokens"]:
            errors.append("按 UTF-8 字节估算的提示词 token 数超过 max_total_prompt_tokens 限额")

    prices = plan.get("prices")
    worst_case_cost = None
    currency = None
    if prices is not None:
        if (
            not isinstance(prices, dict)
            or not _is_text(prices.get("currency"))
            or not _is_finite_nonnegative_number(prices.get("input_per_million"))
            or not _is_finite_nonnegative_number(prices.get("output_per_million"))
        ):
            errors.append("prices 必须包含非负输入/输出百万 token 单价和 currency")
        elif isinstance(limits, dict) and all(
            _is_nonnegative_int(limits.get(key)) for key in required_limits
        ):
            budget = limits.get("cost_budget")
            if not _is_finite_nonnegative_number(budget):
                errors.append("配置价格的真实计划必须设置非负 limits.cost_budget")
            else:
                currency = prices["currency"]
                worst_case_cost = round(
                    limits["max_total_prompt_tokens"] * prices["input_per_million"] / 1_000_000
                    + limits["max_calls"]
                    * limits["max_output_tokens"]
                    * prices["output_per_million"]
                    / 1_000_000,
                    6,
                )
                if worst_case_cost > budget:
                    errors.append("按 token 限额计算的最坏费用超过 limits.cost_budget")

    return {
        "valid": not errors,
        "run_id": plan.get("run_id"),
        "evaluation_type": plan.get("evaluation_type"),
        "planned_call_count": len(records),
        "scenario_count": len(scenarios),
        "scenario_coverage": round(len(seen) / len(scenarios), 4) if scenarios else 0,
        "input_message_chars": input_message_chars,
        "input_prompt_chars": input_prompt_chars,
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "worst_case_cost": worst_case_cost,
        "currency": currency,
        "errors": errors,
    }


def execute_real_plan(
    plan_path: Path,
    output_path: Path,
    *,
    dataset_path: Path = DEFAULT_DATASET,
    execute_real: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
    client_factory=None,
) -> dict[str, Any]:
    """Execute a validated synthetic plan sequentially with explicit opt-in and budgets."""
    if not execute_real:
        raise ValueError("真实模型调用必须显式传入 execute_real=True")

    plan_path = Path(plan_path)
    output_path = Path(output_path)
    dataset_path = Path(dataset_path)
    if output_path.exists():
        raise FileExistsError("输出文件已存在；请使用新的运行输出路径")
    resolved_output = output_path.resolve()
    if resolved_output in {plan_path.resolve(), dataset_path.resolve()}:
        raise ValueError("输出文件不能覆盖评测计划或固定样本")

    plan_report = validate_plan(plan_path, dataset_path)
    if not plan_report["valid"]:
        raise ValueError("真实评测计划无效：" + "；".join(plan_report["errors"]))
    plan = _load_json(plan_path)
    dataset = _load_json(dataset_path)
    scenarios, _ = _scenario_indexes(dataset)
    first_record = plan["records"][0]
    first_messages, _, _, _ = _build_prompt_messages(
        plan, scenarios[first_record["scenario_id"]], first_record
    )

    key = (api_key or os.environ.get("QQCHAT_BENCHMARK_API_KEY", "")).strip()
    if not key:
        raise ValueError("请通过 QQCHAT_BENCHMARK_API_KEY 配置专用评测 API Key")
    endpoint = (
        base_url or os.environ.get("QQCHAT_BENCHMARK_BASE_URL") or "https://api.deepseek.com/v1"
    ).strip()
    parsed_endpoint = urlparse(endpoint)
    local_http = parsed_endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed_endpoint.scheme not in ({"https", "http"} if local_http else {"https"})
        or not parsed_endpoint.hostname
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise ValueError("评测 API 地址必须使用 HTTPS，或使用无凭据的本机 HTTP 地址")

    run = {
        "run_id": plan["run_id"],
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "condition": plan["condition"],
        "evaluation_type": "real",
        "model": plan["model"],
        "prompt_version": plan["prompt_version"],
        "input_strategy": plan["input_strategy"],
        "limits": plan["limits"],
        "prices": plan.get("prices"),
        "system_prompt_sha256": hashlib.sha256(first_messages[0]["content"].encode("utf-8")).hexdigest(),
        "user_instructions_sha256": hashlib.sha256(plan["user_prompt"].encode("utf-8")).hexdigest(),
        "records": [],
        "execution_status": "running",
        "execution": {
            "calls_attempted": 0,
            "input_chars_attempted": 0,
            "estimated_prompt_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated_cost": 0.0 if plan.get("prices") else None,
            "stop_reason": None,
        },
    }
    _write_json_atomic(output_path, run)

    if client_factory is None:
        client_factory = _create_benchmark_client
    try:
        client = client_factory(api_key=key, base_url=endpoint)
    except Exception as exc:
        run["execution_status"] = "incomplete"
        run["execution"]["stop_reason"] = f"client_error:{type(exc).__name__}"
        _write_json_atomic(output_path, run)
        return {
            "valid": False,
            "execution_status": run["execution_status"],
            "completed_records": 0,
            "calls_attempted": 0,
            "stop_reason": run["execution"]["stop_reason"],
            "output": str(output_path),
        }

    limits = plan["limits"]
    prompt_cap = limits["max_total_prompt_tokens"]
    input_char_cap = limits["max_input_chars"]
    call_cap = limits["max_calls"]
    output_cap = limits["max_output_tokens"]
    prices = plan.get("prices")

    for record in plan["records"]:
        scenario = scenarios[record["scenario_id"]]
        messages, prompt_chars, _, token_estimate = _build_prompt_messages(plan, scenario, record)
        execution = run["execution"]
        if execution["calls_attempted"] >= call_cap:
            execution["stop_reason"] = "max_calls_reached"
            break
        if execution["input_chars_attempted"] + prompt_chars > input_char_cap:
            execution["stop_reason"] = "max_input_chars_reached"
            break
        if execution["estimated_prompt_tokens"] + token_estimate > prompt_cap:
            execution["stop_reason"] = "max_total_prompt_tokens_preflight"
            break

        execution["calls_attempted"] += 1
        execution["input_chars_attempted"] += prompt_chars
        execution["estimated_prompt_tokens"] += token_estimate
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=plan["model"],
                messages=messages,
                max_tokens=output_cap,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            execution["stop_reason"] = f"api_error:{type(exc).__name__}"
            break

        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        choice = next(iter(getattr(response, "choices", []) or []), None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        record_error = None
        model_result = None
        claims = []
        if not _is_nonnegative_int(prompt_tokens) or not _is_nonnegative_int(completion_tokens):
            record_error = "API 未返回有效 token 用量"
        if not isinstance(content, str):
            record_error = record_error or "API 未返回文本 JSON 结果"
        else:
            try:
                model_result = json.loads(content)
            except json.JSONDecodeError:
                record_error = record_error or "模型输出不是有效 JSON"
            if not isinstance(model_result, dict) or not isinstance(model_result.get("answer"), str):
                record_error = record_error or "模型 JSON 缺少 answer 字符串"
            elif (
                not isinstance(model_result.get("claims"), list)
                or len(model_result["claims"]) > MAX_CLAIMS_PER_RECORD
            ):
                record_error = record_error or "模型 JSON claims 必须是受限长度的数组"
            else:
                _, alias_to_original = _opaque_message_id_maps(scenario, set(record["input_message_ids"]))
                model_result, unknown_aliases = _restore_evidence_ids(model_result, alias_to_original)
                if unknown_aliases:
                    unknown_summary = ", ".join(sorted(set(unknown_aliases))[:5])
                    alias_error = f"模型引用了未知输入消息别名: {unknown_summary}"
                    record_error = f"{record_error}; {alias_error}" if record_error else alias_error
                claims = model_result["claims"]

        selected_ids = set(record["input_message_ids"])
        actual_ids = [message["id"] for message in scenario["messages"] if message["id"] in selected_ids]
        output_record = {
            "scenario_id": record["scenario_id"],
            "input_message_ids": actual_ids,
            "input_chars": prompt_chars,
            "claims": claims,
            "model_result": model_result if isinstance(model_result, dict) else {},
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
        }
        if record_error:
            output_record["execution_error"] = record_error
        run["records"].append(output_record)

        if _is_nonnegative_int(prompt_tokens):
            execution["prompt_tokens"] += prompt_tokens
        if _is_nonnegative_int(completion_tokens):
            execution["completion_tokens"] += completion_tokens
        if prices and _is_nonnegative_int(prompt_tokens) and _is_nonnegative_int(completion_tokens):
            actual_cost = (
                execution["prompt_tokens"] * prices["input_per_million"]
                + execution["completion_tokens"] * prices["output_per_million"]
            ) / 1_000_000
            execution["estimated_cost"] = round(actual_cost, 6)

        if record_error:
            execution["stop_reason"] = "invalid_response_or_usage"
        elif execution["prompt_tokens"] > prompt_cap:
            execution["stop_reason"] = "max_total_prompt_tokens_exceeded"
        elif completion_tokens > output_cap:
            execution["stop_reason"] = "max_output_tokens_exceeded"
        elif prices and execution["estimated_cost"] > plan["limits"].get("cost_budget", float("inf")):
            execution["stop_reason"] = "cost_budget_exceeded"
        _write_json_atomic(output_path, run)
        if execution["stop_reason"]:
            break

    complete = len(run["records"]) == len(plan["records"]) and not run["execution"]["stop_reason"]
    run["execution_status"] = "complete" if complete else "incomplete"
    if complete:
        run["execution"]["stop_reason"] = "completed"
    _write_json_atomic(output_path, run)
    report = validate_run(output_path, dataset_path)
    return {
        "valid": bool(complete and report["valid"]),
        "execution_status": run["execution_status"],
        "completed_records": len(run["records"]),
        "calls_attempted": run["execution"]["calls_attempted"],
        "stop_reason": run["execution"]["stop_reason"],
        "validation": report,
        "output": str(output_path),
    }


def make_review_template(run_path: Path, output_path: Path, dataset_path: Path = DEFAULT_DATASET) -> None:
    run = _load_json(run_path)
    dataset = _load_json(dataset_path)
    scenarios, _ = _scenario_indexes(dataset)
    template = {
        "format_version": 1,
        "dataset_id": run.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "run_id": run.get("run_id"),
        "condition": run.get("condition"),
        "model": run.get("model"),
        "prompt_version": run.get("prompt_version"),
        "input_strategy": run.get("input_strategy"),
        "reviewer": "",
        "review_rubric": "docs/analysis_benchmark.md",
        "reviews": [
            {
                "scenario_id": record.get("scenario_id"),
                "category": scenarios.get(record.get("scenario_id"), {}).get("category"),
                "condition": run.get("condition"),
                "reference": {
                    key: scenarios.get(record.get("scenario_id"), {}).get(key, [])
                    for key in ("facts", "acceptable_interpretations", "rejected_overclaims")
                },
                "messages": [
                    {
                        **message,
                        "included_in_model_input": message["id"] in set(record.get("input_message_ids", [])),
                    }
                    for message in scenarios.get(record.get("scenario_id"), {}).get("messages", [])
                ],
                "input_message_ids": record.get("input_message_ids", []),
                "claims": record.get("claims", []),
                "model_result": record.get("model_result"),
                "evidence_support": None,
                "key_fact_coverage": None,
                "interpretation_calibration": None,
                "uncertainty_calibration": None,
                "missed_fact_ids": [],
                "unsupported_claims": [],
                "notes": "",
            }
            for record in run.get("records", [])
            if isinstance(record, dict)
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_review(
    path: Path,
    run_path: Path,
    dataset_path: Path = DEFAULT_DATASET,
) -> dict[str, Any]:
    run = _load_json(run_path)
    review = _load_json(path)
    dataset = _load_json(dataset_path)
    errors: list[str] = []
    if not isinstance(review, dict):
        return {"valid": False, "errors": ["review 根节点必须是对象"]}
    expected_values = {
        "dataset_id": run.get("dataset_id"),
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "dataset_version": dataset.get("dataset_version"),
        "run_id": run.get("run_id"),
    }
    for key, expected in expected_values.items():
        if review.get(key) != expected:
            errors.append(f"review.{key} 与对应运行或数据集不一致")
    if not _is_text(review.get("reviewer")):
        errors.append("review.reviewer 必须填写评审人标识")
    records = run.get("records", [])
    expected_scenarios = {
        record.get("scenario_id")
        for record in records
        if isinstance(record, dict) and isinstance(record.get("scenario_id"), str)
    }
    entries = review.get("reviews")
    if not isinstance(entries, list):
        errors.append("review.reviews 必须是数组")
        entries = []
    seen: set[str] = set()
    valid_ratings = 0
    for index, item in enumerate(entries):
        prefix = f"reviews[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        scenario_id = item.get("scenario_id")
        if scenario_id not in expected_scenarios:
            errors.append(f"{prefix}.scenario_id 不属于运行样本")
        if scenario_id in seen:
            errors.append(f"{prefix}.scenario_id 重复")
        if isinstance(scenario_id, str):
            seen.add(scenario_id)
        ratings_valid = True
        for field in REVIEW_RATING_FIELDS:
            value = item.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
                errors.append(f"{prefix}.{field} 必须是 1 至 5 的整数")
                ratings_valid = False
        if ratings_valid:
            valid_ratings += 1
        for field in ("missed_fact_ids", "unsupported_claims"):
            if not isinstance(item.get(field, []), list):
                errors.append(f"{prefix}.{field} 必须是数组")
            elif any(not _is_text(value) for value in item.get(field, [])):
                errors.append(f"{prefix}.{field} 只能包含非空字符串")
        known_fact_ids = {
            fact["id"]
            for fact in next(
                (
                    scenario.get("facts", [])
                    for scenario in dataset.get("scenarios", [])
                    if scenario.get("id") == scenario_id
                ),
                [],
            )
            if isinstance(fact, dict) and _is_text(fact.get("id"))
        }
        if any(fact_id not in known_fact_ids for fact_id in item.get("missed_fact_ids", [])):
            errors.append(f"{prefix}.missed_fact_ids 包含场景外事实")
        if not isinstance(item.get("notes", ""), str):
            errors.append(f"{prefix}.notes 必须是字符串")
    if seen != expected_scenarios:
        errors.append("review.reviews 必须恰好覆盖对应运行的全部场景")
    return {
        "valid": not errors,
        "run_id": review.get("run_id"),
        "reviewed_scenario_count": valid_ratings,
        "errors": errors,
    }


def _score_comparison(baseline_review: dict[str, Any], candidate_review: dict[str, Any]) -> dict[str, Any]:
    baseline_items = {item["scenario_id"]: item for item in baseline_review.get("reviews", [])}
    candidate_items = {item["scenario_id"]: item for item in candidate_review.get("reviews", [])}
    paired_ids = sorted(baseline_items.keys() & candidate_items.keys())
    result = {}
    for field in REVIEW_RATING_FIELDS:
        pairs = [
            (baseline_items[sid].get(field), candidate_items[sid].get(field))
            for sid in paired_ids
            if isinstance(baseline_items[sid].get(field), int)
            and not isinstance(baseline_items[sid].get(field), bool)
            and isinstance(candidate_items[sid].get(field), int)
            and not isinstance(candidate_items[sid].get(field), bool)
        ]
        baseline_values = [left for left, _ in pairs]
        candidate_values = [right for _, right in pairs]
        result[field] = {
            "paired_count": len(pairs),
            "baseline_mean": round(sum(baseline_values) / len(pairs), 4) if pairs else None,
            "candidate_mean": round(sum(candidate_values) / len(pairs), 4) if pairs else None,
            "mean_delta_candidate_minus_baseline": (
                round((sum(candidate_values) - sum(baseline_values)) / len(pairs), 4) if pairs else None
            ),
            "paired_wins": sum(right > left for left, right in pairs),
            "paired_ties": sum(right == left for left, right in pairs),
            "paired_losses": sum(right < left for left, right in pairs),
        }
    return result


def compare_runs(
    baseline_path: Path,
    candidate_path: Path,
    *,
    baseline_review_path: Path | None = None,
    candidate_review_path: Path | None = None,
    dataset_path: Path = DEFAULT_DATASET,
) -> dict[str, Any]:
    baseline = _load_json(baseline_path)
    candidate = _load_json(candidate_path)
    dataset = _load_json(dataset_path)
    dataset_report = validate_dataset(dataset_path)
    baseline_report = validate_run(baseline_path, dataset_path)
    candidate_report = validate_run(candidate_path, dataset_path)
    errors: list[str] = []
    if not baseline_report["valid"]:
        errors.append("baseline 运行记录无效")
    if not candidate_report["valid"]:
        errors.append("candidate 运行记录无效")
    if not dataset_report["valid"]:
        errors.append("benchmark dataset 无效")
    for key in ("dataset_id", "dataset_sha256", "evaluation_type"):
        if baseline.get(key) != candidate.get(key):
            errors.append(f"baseline 与 candidate 的 {key} 不一致")
    if baseline.get("run_id") == candidate.get("run_id"):
        errors.append("baseline 与 candidate 必须是不同运行")

    baseline_records = {
        item.get("scenario_id"): item for item in baseline.get("records", []) if isinstance(item, dict)
    }
    candidate_records = {
        item.get("scenario_id"): item for item in candidate.get("records", []) if isinstance(item, dict)
    }
    baseline_ids, candidate_ids = set(baseline_records), set(candidate_records)
    if not baseline_ids or baseline_ids != candidate_ids:
        errors.append("baseline 与 candidate 必须评测相同且非空的场景集合")
    scenarios, _ = _scenario_indexes(dataset)
    paired_records = sorted(baseline_ids & candidate_ids)
    paired_scenarios = [scenarios[sid] for sid in paired_records if sid in scenarios]
    denominator = len(scenarios)
    usage_delta = {}
    for field in ("prompt_tokens", "completion_tokens", "latency_ms", "estimated_cost"):
        left = baseline_report.get("usage", {}).get(field)
        right = candidate_report.get("usage", {}).get(field)
        if field == "estimated_cost" and baseline_report.get("usage", {}).get(
            "currency"
        ) != candidate_report.get("usage", {}).get("currency"):
            left = right = None
        usage_delta[field] = {
            "baseline": left,
            "candidate": right,
            "delta_candidate_minus_baseline": (
                round(right - left, 6)
                if isinstance(left, (int, float)) and isinstance(right, (int, float))
                else None
            ),
        }

    report: dict[str, Any] = {
        "valid": not errors,
        "evaluation_type": baseline.get("evaluation_type"),
        "quality_evidence": False,
        "dataset": {
            "dataset_id": dataset.get("dataset_id"),
            "dataset_version": dataset.get("dataset_version"),
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "paired_scenario_count": len(paired_records),
            "scenario_coverage": round(len(paired_records) / denominator, 4) if denominator else 0,
            "scenario_ids": paired_records,
        },
        "conditions": {
            "baseline": {
                key: baseline.get(key)
                for key in ("run_id", "condition", "model", "prompt_version", "input_strategy")
            },
            "candidate": {
                key: candidate.get(key)
                for key in ("run_id", "condition", "model", "prompt_version", "input_strategy")
            },
        },
        "mechanical_metrics": {
            "baseline": _review_metrics(
                [baseline_records[sid] for sid in paired_records],
                {item["id"]: item for item in paired_scenarios},
            ),
            "candidate": _review_metrics(
                [candidate_records[sid] for sid in paired_records],
                {item["id"]: item for item in paired_scenarios},
            ),
        },
        "usage": usage_delta,
        "human_review": {"status": "not_provided", "metrics": None},
        "errors": errors,
    }
    if bool(baseline_review_path) != bool(candidate_review_path):
        report["valid"] = False
        report["errors"].append("人工评审必须同时提供 baseline 和 candidate 文件")
    elif baseline_review_path and candidate_review_path:
        baseline_review = _load_json(baseline_review_path)
        candidate_review = _load_json(candidate_review_path)
        baseline_validation = validate_review(baseline_review_path, baseline_path, dataset_path)
        candidate_validation = validate_review(candidate_review_path, candidate_path, dataset_path)
        review_errors = []
        if not baseline_validation["valid"]:
            review_errors.append("baseline 人工评审无效")
        if not candidate_validation["valid"]:
            review_errors.append("candidate 人工评审无效")
        review_metrics = _score_comparison(baseline_review, candidate_review)
        review_valid = baseline_validation["valid"] and candidate_validation["valid"]
        report["human_review"] = {
            "status": "complete" if review_valid else "incomplete_or_invalid",
            "metrics": review_metrics,
            "baseline_validation": baseline_validation,
            "candidate_validation": candidate_validation,
            "errors": review_errors,
            "reviewers": {
                "baseline": baseline_review.get("reviewer"),
                "candidate": candidate_review.get("reviewer"),
                "same_reviewer": baseline_review.get("reviewer") == candidate_review.get("reviewer"),
            },
        }
        if review_errors:
            report["valid"] = False
            report["errors"].extend(review_errors)
        report["quality_evidence"] = (
            review_valid
            and baseline.get("evaluation_type") == "real"
            and not errors
            and len(paired_records) == denominator
        )
    if baseline.get("evaluation_type") != "real":
        report["quality_note"] = "模拟或离线结果只验证比较工具，不构成真实模型质量提升证据。"
    elif not report["quality_evidence"]:
        report["quality_note"] = "完成相同样本的真实运行和完整人工评审后，才可报告质量变化。"
    else:
        report["quality_note"] = "指标按维度分别报告；人工评分不合并为单一质量分。"
    return report


def _print_report(report: dict[str, Any]) -> int:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("valid") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    dataset_parser = subparsers.add_parser("validate-dataset", help="检查固定合成场景库")
    dataset_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run_parser = subparsers.add_parser("validate-run", help="离线检查模型运行记录")
    run_parser.add_argument("run", type=Path)
    run_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    plan_parser = subparsers.add_parser("validate-plan", help="发起真实模型调用前检查评测限额")
    plan_parser.add_argument("plan", type=Path)
    plan_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    execute_parser = subparsers.add_parser("run-real-plan", help="在显式确认后执行受限的真实模型评测")
    execute_parser.add_argument("plan", type=Path)
    execute_parser.add_argument("output", type=Path)
    execute_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    execute_parser.add_argument("--base-url", help="覆盖 QQCHAT_BENCHMARK_BASE_URL")
    execute_parser.add_argument(
        "--execute-real",
        action="store_true",
        help="明确允许产生真实模型调用和费用；不传时拒绝执行",
    )
    template_parser = subparsers.add_parser("review-template", help="生成统一人工评审 JSON 表")
    template_parser.add_argument("run", type=Path)
    template_parser.add_argument("--output", type=Path, required=True)
    template_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    review_parser = subparsers.add_parser("validate-review", help="检查完成的人工评审表")
    review_parser.add_argument("run", type=Path)
    review_parser.add_argument("review", type=Path)
    review_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    compare_parser = subparsers.add_parser("compare-runs", help="对齐基线与候选运行并汇总差异")
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    compare_parser.add_argument("--baseline-review", type=Path)
    compare_parser.add_argument("--candidate-review", type=Path)
    compare_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args(argv)

    try:
        if args.command == "validate-dataset":
            return _print_report(validate_dataset(args.dataset))
        if args.command == "validate-run":
            return _print_report(validate_run(args.run, args.dataset))
        if args.command == "validate-plan":
            return _print_report(validate_plan(args.plan, args.dataset))
        if args.command == "run-real-plan":
            return _print_report(
                execute_real_plan(
                    args.plan,
                    args.output,
                    dataset_path=args.dataset,
                    execute_real=args.execute_real,
                    base_url=args.base_url,
                )
            )
        if args.command == "review-template":
            make_review_template(args.run, args.output, args.dataset)
            print(f"人工评审表已写入 {args.output}")
            return 0
        if args.command == "validate-review":
            return _print_report(validate_review(args.review, args.run, args.dataset))
        return _print_report(
            compare_runs(
                args.baseline,
                args.candidate,
                baseline_review_path=args.baseline_review,
                candidate_review_path=args.candidate_review,
                dataset_path=args.dataset,
            )
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"benchmark 工具无法继续：{message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
