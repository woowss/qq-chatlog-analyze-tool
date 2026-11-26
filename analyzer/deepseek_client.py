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
"""DeepSeek API 调用封装与维度编排。

对话构建（`analyzer/dialog.py`）、月份增量缓存（`analyzer/month_cache.py`）、
限流重试（`analyzer/llm_transport.py`）与无状态运行时（`analyzer/llm_runtime.py`）已拆至独立模块，
并在此保留兼容重导出与旧测试打桩转发。
"""

import concurrent.futures
import hashlib
import json  # noqa: F401 - 保留模块别名供测试打桩 json.load
import os
import sys
import threading
import time  # noqa: F401 - 保留模块别名供测试打桩 time.sleep
import types
from typing import Any, Callable, Optional

from parser.qq_parser import ChatData, is_statistical, split_by_month
from analyzer.logger import get_logger, mask_name
from analyzer import purge_marks
from analyzer import person_context
from analyzer.shutdown import shutdown_requested
from analyzer.result_schema import (
    ResultValidationError,
    supports_dimension,
    validate_cached_result,
    validate_result,
)
from analyzer.local_stats import SESSION_GAP_MS
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
    SYSTEM_PROMPT_PROFILE,
)

# 兼容重导出（dialog 中的对话格式化函数）
from analyzer.dialog import (
    MAX_DIALOG_CHARS,
    RELATIVE_MARK_MINUTES,
    TIME_MARK_MINUTES,
    _build_dialog as _build_dialog,
    _build_dialog_with_meta,
    _conversation_stats as _conversation_stats,
    _dialog_needs_sampling,
    _fit_lines,
    _fit_message_context as _fit_message_context,
    _gap_mark as _gap_mark,
    _has_content,
    _message_line,
    _render_message_lines as _render_message_lines,
    _short_time as _short_time,
    _truncate_dialog_line as _truncate_dialog_line,
    _vision_digest,
)

# 兼容重导出月份缓存接口（状态变量如 _MONTH_CACHE_DIR 须在 analyzer.month_cache 上打桩）
from analyzer.month_cache import (
    MONTH_CACHE_GRACE_SECONDS as MONTH_CACHE_GRACE_SECONDS,
    _MONTH_CACHE_LOCK as _MONTH_CACHE_LOCK,
    _last_write_warning as _last_write_warning,
    _manifest_path as _manifest_path,
    _month_key,
    _read_month_cache,
    _record_month_usage,
    _referenced_keys_locked as _referenced_keys_locked,
    _write_month_cache,
    configure_month_cache as configure_month_cache,
    month_cache_path as month_cache_path,
    month_cache_enabled as month_cache_enabled,
    purge_month_cache as purge_month_cache,
    sweep_orphan_month_cache as sweep_orphan_month_cache,
)
from analyzer import llm_runtime
from analyzer import llm_stream
from analyzer.llm_runtime import AnalysisIncompleteError, QuotaExhaustedError

logger = get_logger("deepseek")


class _PromptText(str):
    """保留原始 prompt 文本，同时把输入格式代次传给月份缓存层。"""

    def __new__(cls, value: str, cache_fingerprint: str | None = None):
        obj = super().__new__(cls, value)
        obj.cache_fingerprint = cache_fingerprint
        return obj


#: 参与私聊提示词指纹的提示词**名单**，顺序即哈希顺序，不要动。
#: 取值与排列必须与旧实现 `sorted(dir(analyzer.prompts)) 里 SYSTEM_PROMPT_*` 完全一致
#: ——这样改成显式名单是"零值变更"，所有既有私聊缓存继续命中（有测试钉住这一点）。
#: 名单是**封闭**的：往 analyzer/prompts.py 里新加常量（例如群聊提示词）不会被算进来，
#: 因此不会作废私聊缓存。群聊提示词请放 analyzer/group_prompts.py 并使用自己的指纹。
#: 改名或删除名单里的常量会在 import 期直接报 AttributeError（响亮失败，好过静默换键）。
_PRIVATE_PROMPT_NAMES = (
    "SYSTEM_PROMPT_EMOTION",
    "SYSTEM_PROMPT_HABITS",
    "SYSTEM_PROMPT_PROFILE",
    "SYSTEM_PROMPT_RELATIONSHIP",
    "SYSTEM_PROMPT_TOPICS",
)


def _validate_before_month_cache(dimension: str, result: Any) -> None:
    """校验新模型结果；结构契约与是否保留缓存无关。"""
    if supports_dimension(dimension):
        validate_result(dimension, result)


def _read_result_cache(
    key: str,
    dimension: str,
    *,
    expect_thinking=None,
    chat_hash: str = "",
    read_only: bool = False,
) -> Optional[dict]:
    result = _read_month_cache(key, expect_thinking=expect_thinking, chat_hash=chat_hash, read_only=read_only)
    if not result:
        return None
    if supports_dimension(dimension):
        try:
            validate_cached_result(dimension, result)
        except ResultValidationError as e:
            # 错误类型不能随旧缓存放行；只补算该单元，不删除其他已付费的有效结果。
            logger.warning("%s 内容缓存结构无效（字段 %s），重新分析该单元", dimension, e.path)
            return None
    return result


PRIVATE_CACHE_VERSION = "v2"


def _private_fingerprint_inputs():
    import analyzer.prompts as _prompts
    from analyzer import vision

    return (
        tuple(getattr(_prompts, name) for name in _PRIVATE_PROMPT_NAMES),
        (
            MAX_DIALOG_CHARS,
            TIME_MARK_MINUTES,
            RELATIVE_MARK_MINUTES,
            llm_runtime.MAX_TOKENS_BY_DIM.get("emotion"),
            llm_runtime.MAX_TOKENS_BY_DIM.get("topics"),
            llm_runtime.MAX_TOKENS_BY_DIM.get("relationship"),
            llm_runtime.MAX_TOKENS_BY_DIM.get("habits"),
            llm_runtime.MAX_TOKENS_BY_DIM.get("profile"),
            int(SESSION_GAP_MS),
            vision.VISION_SYSTEM,
            vision.VISION_DETAIL,
            vision.VISION_MAX_PER_MONTH,
            vision.VISION_MIN_SIDE,
        ),
    )


def _prompt_fingerprint(salt: "str | None" = None) -> str:
    """私聊提示词 + 预算/版本号指纹，参与缓存键。

    **只哈希 _PRIVATE_PROMPT_NAMES 里显式列出的提示词**，保证新增其他维度常量
    （如群聊或总括提示词）不会影响私聊缓存键。
    """
    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    prompt_values, const_values = _private_fingerprint_inputs()
    parts = [f"version:{PRIVATE_CACHE_VERSION}", *prompt_values, f"consts:{repr(const_values)}"]
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _unsampled_prompt_fingerprint(salt: "str | None" = None) -> str:
    """未抽样输入复用同配置键；预算变化仍需隔离维度缓存。"""
    return _prompt_fingerprint(salt)


def sampled_prompt_fingerprint(salt: "str | None" = None) -> str:
    """私聊触发预算抽样时使用的格式代次，不污染未抽样缓存键。"""
    parts = [_prompt_fingerprint(salt), "private-sampled-dialog-v1"]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


PROMPT_FINGERPRINT = _prompt_fingerprint()


def fingerprint_for_dimension(dim: str) -> str:
    """该维度该用哪个提示词指纹：群聊维度走群聊指纹，recap 走 recap 指纹，其余（私聊/未知）走私聊指纹。"""
    from analyzer import group_client  # 延迟导入：group_client 在模块级 import 本模块

    if dim in group_client.GROUP_DIMENSIONS:
        return group_client.GROUP_PROMPT_FINGERPRINT
    from analyzer import recap_client

    if dim == recap_client.RECAP_DIMENSION:
        return recap_client.RECAP_PROMPT_FINGERPRINT
    return PROMPT_FINGERPRINT


def analysis_cache_fingerprint(dimension: str, chat: ChatData) -> str:
    """按这份聊天是否真的触发上下文抽样选择维度级缓存指纹。"""
    private_monthly = {"emotion", "topics", "relationship"}
    if dimension in private_monthly:
        for messages in chat.months().values():
            if _dialog_needs_sampling(
                messages, chat.self_uid, chat.self_name, chat.other_name, MAX_DIALOG_CHARS
            ):
                return scope_cache_fingerprint(chat, sampled_prompt_fingerprint())
        return scope_cache_fingerprint(chat, _unsampled_prompt_fingerprint())

    from analyzer import group_client

    if dimension in group_client.GROUP_DIMENSIONS:
        if dimension == "member_profiles":
            return scope_cache_fingerprint(
                chat,
                person_context.strategy_fingerprint(group_client.GROUP_PROMPT_FINGERPRINT, dimension),
                include_targets=True,
            )
        for messages in chat.months().values():
            if group_client._group_dialog_needs_sampling(chat, messages):
                return scope_cache_fingerprint(chat, group_client.group_sampled_prompt_fingerprint())
        return scope_cache_fingerprint(chat, group_client._unsampled_group_prompt_fingerprint())
    base_fingerprint = fingerprint_for_dimension(dimension)
    if dimension == "recap" and getattr(chat, "analysis_input_scope_id", ""):
        # Scoped recaps generated with the export's full duration used wrong
        # daily averages. Keep full-chat recap caches compatible.
        base_fingerprint = hashlib.sha256(
            f"{base_fingerprint}:scoped-duration-v2".encode("utf-8")
        ).hexdigest()[:12]
    return scope_cache_fingerprint(
        chat,
        person_context.strategy_fingerprint(base_fingerprint, dimension),
        include_targets=dimension in {"habits", "profile"},
    )


def scope_cache_fingerprint(chat: ChatData, fingerprint: str, *, include_targets: bool = False) -> str:
    """Put message scope in cache identity; target-only changes affect profile dimensions."""
    scope_id = getattr(
        chat,
        "analysis_scope_id" if include_targets else "analysis_input_scope_id",
        "",
    )
    if not scope_id:
        return fingerprint
    return hashlib.sha256(f"{fingerprint}:scope:{scope_id}".encode("utf-8")).hexdigest()[:16]


def _analyze_periods(
    months: dict[str, list],
    system_prompt: str,
    make_prompt: Callable[[str, list], str],
    max_tokens: int,
    tag: str = "unknown",
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    chat_hash: str = "",
    fingerprint: "str | None" = None,
) -> dict[str, Any]:
    """按有界并发窗口逐月调用 API；支持取消、月份级增量缓存及配额中止。"""
    results: dict[str, Any] = {}
    total = len(months)
    done = 0
    fatal: dict[str, str] = {}
    failed_periods: set[str] = set()
    invalid_periods: dict[str, str] = {}
    failure_lock = threading.Lock()
    used_keys: set[str] = set()

    def _mark_failed(period: str, path: str = "") -> None:
        with failure_lock:
            failed_periods.add(period)
            if path:
                invalid_periods[period] = path

    def _cancel_requested() -> bool:
        return shutdown_requested() or bool(should_cancel and should_cancel())

    def _keys_for(prompt: str, active_fingerprint: "str | None" = None) -> list:
        """返回该月提示词对应的当前月份缓存键。"""
        current_fp = active_fingerprint or fingerprint or PROMPT_FINGERPRINT
        return [_month_key(system_prompt, prompt, current_fp)]

    def _work(period: str, msgs: list) -> tuple[str, Optional[dict]]:
        if _cancel_requested():
            return period, None
        with llm_stream.stream_context(unit=f"{tag}:{period}", label=f"{period} 月", fresh_state=True):
            try:
                prompt = make_prompt(period, msgs)
                if not prompt.strip():
                    return period, None
                key = _keys_for(prompt, getattr(prompt, "cache_fingerprint", None))[0]
                # 思考模式须与 _call_api 口径一致并参与月份缓存校验
                want_thinking = llm_runtime.thinking_enabled(tag)
                result = _read_result_cache(key, tag, expect_thinking=want_thinking, chat_hash=chat_hash)
                if result is not None:
                    logger.info("%s 命中月份缓存，跳过 API 调用", period)
                    llm_stream.emit_event(
                        "cache",
                        f"{period} 月命中本地缓存（{len(msgs)} 条消息），跳过 API 调用",
                        data={"cached": True},
                    )
                else:
                    llm_stream.emit_event("stage", f"开始分析 {period} 月（{len(msgs)} 条消息）")
                    result = llm_runtime._call_api(
                        system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag
                    )
                    if not result:
                        _mark_failed(period)
                    elif supports_dimension(tag):
                        try:
                            _validate_before_month_cache(tag, result)
                        except ResultValidationError as e:
                            # 仅记录维度/月份/字段路径，避免原始消息或错误体泄露到日志
                            _mark_failed(period, e.path)
                            logger.warning("%s 月 AI 结果结构无效（字段 %s）", period, e.path)
                            llm_stream.emit_event("error", f"{period} 月 AI 结果结构无效（字段 {e.path}）")
                            result = None
                    if result:
                        llm_stream.emit_result_fallback(result)
                        llm_stream.emit_event("stage", f"{period} 月分析完成")
                    # 若分析期间聊天已被清理，仅返回内存结果而不回写月份磁盘缓存
                    if result and not purge_marks.is_marked(chat_hash):
                        _write_month_cache(key, result, thinking=want_thinking)
                if result:
                    result["period"] = period
                    result["month"] = period
                    used_keys.add(key)
                    return period, result
            except llm_stream.StreamCancelledError:
                llm_stream.emit_event("stage", f"{period} 月已取消")
            except QuotaExhaustedError as e:
                fatal.setdefault("error", str(e))
                logger.error("%s 月 AI 分析中止（配额耗尽）", period)
                llm_stream.emit_event("error", f"{period} 月分析中止：配额耗尽")
            except Exception as e:
                _mark_failed(period)
                logger.error("%s 月 AI 分析失败: %s", period, e)
                llm_stream.emit_event("error", f"{period} 月 AI 分析失败：{llm_runtime.scrub_secrets(e)}")
        return period, None

    pending = list(months.items())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=llm_runtime.CONCURRENCY) as pool:
            futures: dict[concurrent.futures.Future, str] = {}
            while pending or futures:
                while (
                    pending
                    and len(futures) < llm_runtime.CONCURRENCY
                    and not fatal
                    and not _cancel_requested()
                ):
                    period, msgs = pending.pop(0)
                    # ContextVars do not propagate into pool workers automatically.
                    from contextvars import copy_context

                    futures[pool.submit(copy_context().run, _work, period, msgs)] = period
                if not futures:
                    break
                finished, _ = concurrent.futures.wait(
                    list(futures), return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in finished:
                    futures.pop(fut, None)
                    period, result = fut.result()
                    if result:
                        results[period] = result
                    done += 1
                    if on_progress:
                        try:
                            on_progress(done, total)
                        except Exception as e:
                            logger.debug("进度回调异常（忽略）: %s", e)
                if fatal:
                    for f in futures:
                        f.cancel()
                    break
    finally:
        # 无论正常完成、取消或配额中止，均批量登记已引用月份键，避免已完成月份变为孤儿
        _record_month_usage(chat_hash, used_keys)

    if fatal:
        # 配额中止须上抛，避免残缺月份汇总写入维度缓存；已完成的月份已在月份缓存中保留
        raise QuotaExhaustedError(fatal["error"])

    if failed_periods and not _cancel_requested():
        if invalid_periods:
            first_path = next(iter(invalid_periods.values()))
            raise AnalysisIncompleteError(
                f"{len(failed_periods)} 个月结果结构无效（字段 {first_path}）；"
                "已完成月份已缓存，可重试失败月份"
            )
        raise AnalysisIncompleteError(
            f"{len(failed_periods)} 个月没有得到有效结果；已完成月份已缓存，可重试补齐"
        )

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
    """把各话题 weight 归一化，保证总和恒为 1.0（防御模型权重不收敛到 1）

    逐个 round(w/total, 2) 之后求和的**和**不保证是 1.0（如 0.333/0.333/0.334 各自
    舍入成 0.33 → 和 0.99），前端按百分比展示时会出现"加起来 99%/101%"这种对不上
    的细节。所以先各自舍入，再把舍入误差补给占比最大的那个话题。两个坑：
    - 补给"最后一个"：话题多、末位又极小时会算出负权重（10 个话题里末位只占 0.1%，
      漂移可达 -0.04）；最大项 ≥ 1/N，吃掉 ±0.005×N 的漂移既不会变负也不改排序。
    - 舍入后并列时得用真实占比打破平手：不然 0.333/0.333/0.334 三项都舍成 0.33，
      误差会落到真实占比最小的那一项上。
    """
    topics = obj.get("topics")
    if not isinstance(topics, list):
        return
    valid = [t for t in topics if isinstance(t, dict)]
    if not valid:
        return
    total = 0.0
    weights: list[float] = []
    for t in valid:
        try:
            w = float(t.get("weight", 0))
        except (TypeError, ValueError):
            w = 0.0
            t["weight"] = 0.0  # 非数值项就地归零，便于排查
        weights.append(w)
        total += w
    if total <= 0:
        return
    rounded = [round(w / total, 2) for w in weights]
    drift = round(1.0 - sum(rounded), 2)
    if drift:
        # 并列时用真实占比打破平手，保证误差落在真正最大的话题上
        biggest = max(range(len(rounded)), key=lambda i: (rounded[i], weights[i]))
        rounded[biggest] = round(rounded[biggest] + drift, 2)
    for t, w in zip(valid, rounded, strict=True):
        t["weight"] = w


def _month_prompt(chat: ChatData, period: str, msgs: list, chat_hash: str = "") -> str:
    """构建单月 prompt；该月经过滤（系统/撤回/转发/空文本）后无有效消息时返回空串，
    由 _analyze_periods 跳过，避免为空月份白白消耗一次 API 调用。"""
    dialog, sampled = _build_dialog_with_meta(
        msgs,
        chat.self_uid,
        chat.self_name,
        chat.other_name,
        max_chars=MAX_DIALOG_CHARS,
        chat_hash=chat_hash,
        vision_label=f"{period} 月",
    )
    if not dialog.strip():
        return ""
    cache_fp = scope_cache_fingerprint(
        chat, sampled_prompt_fingerprint() if sampled else _unsampled_prompt_fingerprint()
    )
    return _PromptText(f"以下是 {period} 月的对话数据：\n\n{dialog}", cache_fp)


def analyze_emotion(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:
    """逐月情绪分析，返回 {"2024-01": {...}, ...}"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_EMOTION,
        lambda p, msgs: _month_prompt(chat, p, msgs, chat_hash=chat_hash),
        max_tokens=llm_runtime.MAX_TOKENS_BY_DIM["emotion"],
        tag="emotion",
        on_progress=on_progress,
        should_cancel=should_cancel,
        chat_hash=chat_hash,
    )
    # 强度夹紧到 0-10，防御越界/非法值
    for r in results.values():
        _clamp_int(r, "self_intensity", 0, 10)
        _clamp_int(r, "other_intensity", 0, 10)
    return results


def analyze_topics(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:
    """逐月话题分析"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_TOPICS,
        lambda p, msgs: _month_prompt(chat, p, msgs, chat_hash=chat_hash),
        max_tokens=llm_runtime.MAX_TOKENS_BY_DIM["topics"],
        tag="topics",
        on_progress=on_progress,
        should_cancel=should_cancel,
        chat_hash=chat_hash,
    )
    # 权重归一化，保证各月话题占比之和恒为 1.0
    for r in results.values():
        _normalize_topic_weights(r)
    return results


def analyze_relationship(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:
    """逐月人际关系分析"""
    months = split_by_month(chat)
    results = _analyze_periods(
        months,
        SYSTEM_PROMPT_RELATIONSHIP,
        lambda p, msgs: _month_prompt(chat, p, msgs, chat_hash=chat_hash),
        max_tokens=llm_runtime.MAX_TOKENS_BY_DIM["relationship"],
        tag="relationship",
        on_progress=on_progress,
        should_cancel=should_cancel,
        chat_hash=chat_hash,
    )
    for r in results.values():
        _clamp_int(r, "closeness_score", 1, 10)
        _clamp_float(r, "initiator_ratio_self", 0.0, 1.0, 0.5)
    return results


def _prepare_person_request(
    system_prompt: str,
    sample_size: int,
    msgs: list,
    display_name: str,
    prompt_template: str,
    tag: str = "unknown",
    stratified: bool = False,
    chat_hash: str = "",
    context_messages: list | None = None,
    speaker_name_of: Callable[[Any], str] | None = None,
    scope_id: str = "",
) -> Optional[dict]:
    """Build the exact person prompt and content-cache identity used by execution."""
    valid_all = [m for m in msgs if _has_content(m) and is_statistical(m)]
    use_person_context = tag == "profile" and person_context.PERSON_CONTEXT_ENABLED
    if use_person_context:
        source_messages = msgs if context_messages is None else context_messages
        speaker_name_of = speaker_name_of or (lambda _message: display_name)
        selection = person_context.select_person_context(
            source_messages,
            valid_all,
            speaker_name_of,
            sample_size,
            MAX_DIALOG_CHARS,
            stratified=stratified,
        )
        sample = selection.target_messages
        valid = sample
        lines = []
        span_note = "，按时间均匀抽样覆盖整个时段" if len(valid_all) > sample_size else ""
    elif stratified and len(valid_all) > sample_size:
        # 向上取整步长：保证抽样后条数 <= sample_size 且覆盖整个时间轴
        stride = (len(valid_all) + sample_size - 1) // sample_size
        sample = valid_all[::stride]
        span_note = "，按时间均匀抽样覆盖整个时段"
        valid = sample
        lines = [_message_line(m, display_name) for m in valid]
    else:
        sample = valid_all[-sample_size:]
        span_note = ""
        valid = sample
        lines = [_message_line(m, display_name) for m in valid]
    if not valid:
        return None
    if use_person_context:
        context_note = (
            f"；另含 {selection.context_count} 条仅供理解的上下文消息" if selection.context_count else ""
        )
        head = (
            f"统计：{display_name} 共发言 {len(valid_all)} 条（目标样本 {len(sample)} 条{span_note}，"
            f"图片 {sum(1 for m in valid if m.has_image)} 张{context_note}）"
        )
        if selection.sampled:
            head += "，其余上下文受篇幅限制未展示"
        dialog = f"{head}。\n\n{selection.text}"
        # The digest is labeled as the target's images; neighbors keep their
        # speaker labels in the text but must not contribute images here.
        vision_messages = selection.target_messages
    else:
        original_n = len(lines)
        lines = _fit_lines(lines, MAX_DIALOG_CHARS)
        head = f"统计：{display_name} 共发言 {len(valid_all)} 条（样本 {len(valid)} 条{span_note}，图片 "
        # Keep this legacy count wording stable: it is part of the prompt/cache contract.
        head += f"{sum(1 for m in valid if m.has_image)} 张）"
        if len(lines) < original_n:
            head += f"，因篇幅限制展示其中 {len(lines)} 条"
        dialog = f"{head}。\n\n" + "\n".join(lines)
        vision_messages = valid_all
    digest = _vision_digest(vision_messages, chat_hash, f"{display_name} 的发言中")
    if digest:
        dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
    if not dialog.strip():
        return None
    user_content = prompt_template.format(display_name=display_name, dialog=dialog)
    cache_version = f":{person_context.STRATEGY_VERSION}" if use_person_context else ""
    cache_prompt = f"{system_prompt}\n[private-person:{tag}:{display_name}{cache_version}]"
    cache_key = _month_key(
        cache_prompt,
        user_content,
        hashlib.sha256(f"{fingerprint_for_dimension(tag)}:scope:{scope_id}".encode("utf-8")).hexdigest()[:16]
        if scope_id
        else fingerprint_for_dimension(tag),
    )
    return {
        "valid_all": valid_all,
        "sample": sample,
        "user_content": user_content,
        "cache_key": cache_key,
        "thinking": llm_runtime.thinking_enabled(tag),
    }


def _analyze_person(
    system_prompt: str,
    sample_size: int,
    msgs: list,
    display_name: str,
    prompt_template: str,
    max_tokens: int,
    tag: str = "unknown",
    stratified: bool = False,
    chat_hash: str = "",
    context_messages: list | None = None,
    speaker_name_of: Callable[[Any], str] | None = None,
    scope_id: str = "",
) -> Optional[dict]:
    """单人的习惯/锐评分析：先过滤再取样本，失败仅记日志。

    stratified=False（习惯）：取最近 sample_size 条 —— 语言习惯看当下。
    stratified=True（锐评）：按时间均匀抽样覆盖整个时段 —— 否则 growth_observation
    要求的"这段时间的变化"根本不在样本里，模型只能编或写数据不足。
    """
    unit_id = f"{tag}:{display_name}"
    with llm_stream.stream_context(unit=unit_id, label=display_name, fresh_state=True):
        try:
            request_data = _prepare_person_request(
                system_prompt,
                sample_size,
                msgs,
                display_name,
                prompt_template,
                tag=tag,
                stratified=stratified,
                chat_hash=chat_hash,
                context_messages=context_messages,
                speaker_name_of=speaker_name_of,
                scope_id=scope_id,
            )
            if request_data is None:
                return None
            valid_all = request_data["valid_all"]
            user_content = request_data["user_content"]
            cache_key = request_data["cache_key"]
            want_thinking = request_data["thinking"]
            result = _read_result_cache(cache_key, tag, expect_thinking=want_thinking, chat_hash=chat_hash)
            if result is not None:
                logger.info("%s 命中个人分析缓存，跳过 API 调用", mask_name(display_name))
                llm_stream.emit_event(
                    "cache",
                    f"{display_name} 命中个人缓存（{len(valid_all)} 条消息），跳过 API 调用",
                    data={"cached": True},
                )
            else:
                llm_stream.emit_event("stage", f"开始分析 {display_name}（{len(valid_all)} 条有效消息）")
                result = llm_runtime._call_api(
                    system_prompt,
                    user_content,
                    max_tokens=max_tokens,
                    tag=tag,
                    dim=tag,
                )
                if result:
                    try:
                        _validate_before_month_cache(tag, result)
                    except ResultValidationError as e:
                        logger.warning(
                            "%s 的 %s 结果结构无效（字段 %s）",
                            mask_name(display_name),
                            tag,
                            e.path,
                        )
                        llm_stream.emit_event(
                            "error",
                            f"{display_name} 的 {tag} 结果结构无效（字段 {e.path}）",
                        )
                        raise AnalysisIncompleteError(
                            f"{mask_name(display_name)} 的 {tag} 结果结构无效（字段 {e.path}）；可重试该成员"
                        ) from e
                    llm_stream.emit_result_fallback(result)
                    llm_stream.emit_event("stage", f"{display_name} 分析完成")
                if result and not purge_marks.is_marked(chat_hash):
                    _write_month_cache(cache_key, result, thinking=want_thinking)
            if result and chat_hash:
                _record_month_usage(chat_hash, {cache_key})
            if result:
                result["name"] = display_name
                result["total_messages"] = len(valid_all)
                return result
        except llm_stream.StreamCancelledError:
            llm_stream.emit_event("stage", f"{display_name} 已取消")
            raise
        except QuotaExhaustedError:
            llm_stream.emit_event("error", f"{display_name} 分析中止：配额耗尽")
            raise  # 配额耗尽需中止整个维度，不能被当作单人失败吞掉
        except AnalysisIncompleteError:
            raise
        except Exception as e:
            logger.error("%s 的 AI 分析失败: %s", mask_name(display_name), llm_runtime.scrub_secrets(e))
            llm_stream.emit_event("error", f"{display_name} 的 AI 分析失败：{llm_runtime.scrub_secrets(e)}")
        return None


def _person_analysis_spec(dimension: str) -> tuple[str, str, int, bool]:
    """Return the model prompt and sample policy shared by preview and execution."""
    if dimension == "habits":
        return (
            SYSTEM_PROMPT_HABITS,
            "分析以下 {display_name} 的发言，总结其说话风格：\n\n{dialog}",
            500,
            False,
        )
    if dimension == "profile":
        template = "以下是 {display_name} 在私聊中的发言记录，请对其进行深度性格分析：\n"
        if person_context.PERSON_CONTEXT_ENABLED:
            template += (
                "样本行以[目标人物]标记其本人发言，[仅供上下文]行只用于理解对话背景；"
                "画像、用词特征与发言统计只根据[目标人物]消息。\n"
            )
        return SYSTEM_PROMPT_PROFILE, template + "\n{dialog}", 800, True
    raise ValueError(f"unsupported person-analysis dimension: {dimension}")


def analyze_habits(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """分析双方的语言习惯"""
    target_uids = getattr(chat, "analysis_target_uids", None)
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]
    if target_uids is not None:
        if chat.self_uid not in target_uids:
            self_msgs = []
        other_msgs = [m for m in other_msgs if m.sender_uid in target_uids]

    results: dict[str, Any] = {}
    incomplete = False
    system_prompt, template, sample_size, stratified = _person_analysis_spec("habits")
    people = [("self", self_msgs), ("other", other_msgs)]
    if target_uids is not None:
        people = [
            (key, msgs)
            for key, msgs in people
            if (chat.self_uid in target_uids if key == "self" else bool(msgs))
        ]
    total, done = len(people), 0
    for person_key, msgs in people:
        if should_cancel and should_cancel():
            break
        display_name = chat.self_name if person_key == "self" else chat.other_name
        try:
            result = _analyze_person(
                system_prompt,
                sample_size,
                msgs,
                display_name,
                template,
                max_tokens=llm_runtime.MAX_TOKENS_BY_DIM["habits"],
                tag="habits",
                stratified=stratified,
                chat_hash=chat_hash,
                scope_id=getattr(chat, "analysis_scope_id", ""),
            )
        except llm_stream.StreamCancelledError:
            break
        except QuotaExhaustedError:
            raise
        except AnalysisIncompleteError:
            # 双方独立分析；先保留另一方的有效缓存，再上报整个维度未完成。
            incomplete = True
            result = None
        if result:
            results[person_key] = result
        elif any(_has_content(m) and is_statistical(m) for m in msgs):
            incomplete = True
        done += 1
        if on_progress:
            on_progress(done, total)
    if incomplete and not (should_cancel and should_cancel()):
        error = AnalysisIncompleteError("habits 分析未能取得双方的完整结果；已完成的一方可从缓存复用")
        if results:
            error.partial_result = results
        raise error
    return results


def analyze_profile(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """AI 人物锐评 — 分析双方的性格画像"""
    target_uids = getattr(chat, "analysis_target_uids", None)
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]
    if target_uids is not None:
        if chat.self_uid not in target_uids:
            self_msgs = []
        other_msgs = [m for m in other_msgs if m.sender_uid in target_uids]

    results: dict[str, Any] = {}
    incomplete = False
    system_prompt, template, sample_size, stratified = _person_analysis_spec("profile")
    people = [("self", self_msgs), ("other", other_msgs)]
    if target_uids is not None:
        people = [
            (key, msgs)
            for key, msgs in people
            if (chat.self_uid in target_uids if key == "self" else bool(msgs))
        ]
    total, done = len(people), 0
    for person_key, msgs in people:
        if should_cancel and should_cancel():
            break
        display_name = chat.self_name if person_key == "self" else chat.other_name
        try:
            result = _analyze_person(
                system_prompt,
                sample_size,
                msgs,
                display_name,
                template,
                max_tokens=llm_runtime.MAX_TOKENS_BY_DIM["profile"],
                tag="profile",
                stratified=stratified,
                chat_hash=chat_hash,
                context_messages=chat.messages,
                speaker_name_of=lambda message: (
                    chat.self_name if message.sender_uid == chat.self_uid else chat.other_name
                ),
                scope_id=getattr(chat, "analysis_scope_id", ""),
            )
        except llm_stream.StreamCancelledError:
            break
        except QuotaExhaustedError:
            raise
        except AnalysisIncompleteError:
            incomplete = True
            result = None
        if result:
            results[person_key] = result
        elif any(_has_content(m) and is_statistical(m) for m in msgs):
            incomplete = True
        done += 1
        if on_progress:
            on_progress(done, total)
    if incomplete and not (should_cancel and should_cancel()):
        error = AnalysisIncompleteError("profile 分析未能取得双方的完整结果；已完成的一方可从缓存复用")
        if results:
            error.partial_result = results
        raise error
    return results


llm_runtime.register_prompt_fingerprint_provider(lambda: PROMPT_FINGERPRINT)

# 兼容重导出 / 测试打桩转发：读写这些运行期状态或调用入口时，直接同步到 analyzer.llm_runtime
_RUNTIME_REEXPORTS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_BASE_URL",
        "CONCURRENCY",
        "CALL_MIN_INTERVAL",
        "REQUEST_TIMEOUT",
        "TPM_MAX_ATTEMPTS",
        "TPM_WAIT_SECONDS",
        "_OFFICIAL",
        "_DEFAULT_INTERVAL",
        "_DEFAULT_CONCURRENCY",
        "_DEFAULT_MAX_TOKENS",
        "_max_tokens",
        "MAX_TOKENS_BY_DIM",
        "_THINKING_ENV",
        "_THINKING_NORMALIZED",
        "THINKING_DEFAULT",
        "THINKING_DIMS",
        "_SEND_THINKING_PARAM",
        "THINKING_MIN_TOKENS",
        "thinking_enabled",
        "thinking_budget_warnings",
        "_call_gate",
        "_next_call_at",
        "_cooldown_until",
        "_pace",
        "_set_cooldown",
        "MAX_CALLS_PER_RUN",
        "_run_calls",
        "_run_calls_lock",
        "_run_active",
        "_run_depth",
        "begin_run",
        "end_run",
        "calls_used",
        "_count_call",
        "_is_plan_exhausted",
        "_is_tpm_throttle",
        "_PLACEHOLDER_KEYS",
        "_KEY_SHAPED",
        "_KEYED_VALUE",
        "scrub_secrets",
        "_CLIENT",
        "_CLIENT_LOCK",
        "_get_client",
        "_request_with_retry",
        "_json_request_params",
        "_call_api",
        "_call_vision",
        "is_api_configured",
        "is_insecure_base_url",
        "test_connection",
    }
)


class _RuntimeFacadeModule(types.ModuleType):
    def __getattribute__(self, name: str) -> Any:
        if name in _RUNTIME_REEXPORTS:
            return getattr(llm_runtime, name)
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _RUNTIME_REEXPORTS:
            setattr(llm_runtime, name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _RUNTIME_REEXPORTS:
            delattr(llm_runtime, name)
            return
        super().__delattr__(name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | _RUNTIME_REEXPORTS)


sys.modules[__name__].__class__ = _RuntimeFacadeModule
