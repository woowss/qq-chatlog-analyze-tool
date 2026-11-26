# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""LLM 流式输出上下文与分片聚合工具。

通过 contextvars 在不改动既有提示词指纹和单元测试接口的前提下，
将底层流式 thinking/content 增量与阶段事件安全传递到 Web 任务日志。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional


class StreamCancelledError(RuntimeError):
    """流式输出过程中检测到任务取消。"""


_STREAM_SINK: ContextVar[Optional[Callable[[dict[str, Any]], None]]] = ContextVar(
    "qqchat_llm_stream_sink", default=None
)
_STREAM_UNIT: ContextVar[str] = ContextVar("qqchat_llm_stream_unit", default="")
_STREAM_LABEL: ContextVar[str] = ContextVar("qqchat_llm_stream_label", default="")
_STREAM_CANCEL: ContextVar[Optional[Callable[[], bool]]] = ContextVar(
    "qqchat_llm_stream_cancel", default=None
)
_STREAM_STATE: ContextVar[Optional[dict[str, Any]]] = ContextVar("qqchat_llm_stream_state", default=None)


def has_active_stream() -> bool:
    """当前执行上下文是否挂载了实时输出接收器。"""
    return _STREAM_SINK.get() is not None


def current_unit() -> str:
    """返回当前分析单元标识（如月度 key、成员标识或总括标签）。"""
    return _STREAM_UNIT.get()


def current_label() -> str:
    """返回当前分析单元的可读标题。"""
    return _STREAM_LABEL.get()


def is_cancelled() -> bool:
    """检查当前流式上下文是否收到取消请求。"""
    fn = _STREAM_CANCEL.get()
    return bool(fn and fn())


def current_delta_emitted() -> bool:
    """当前上下文自最近一次重置后是否已产生 thinking/content 文本增量。"""
    state = _STREAM_STATE.get()
    return bool(state and state.get("delta_emitted"))


def reset_delta_emitted() -> None:
    """清空当前状态的 delta 标记。"""
    state = _STREAM_STATE.get()
    if state is not None:
        state["delta_emitted"] = False


@contextmanager
def stream_context(
    *,
    sink: Optional[Callable[[dict[str, Any]], None]] = None,
    unit: Optional[str] = None,
    label: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    fresh_state: bool = False,
):
    """绑定或覆写当前上下文中的流式事件目标、单元元信息与取消回调。"""
    tokens = []
    if sink is not None:
        tokens.append((_STREAM_SINK, _STREAM_SINK.set(sink)))
    if unit is not None:
        tokens.append((_STREAM_UNIT, _STREAM_UNIT.set(unit)))
    if label is not None:
        tokens.append((_STREAM_LABEL, _STREAM_LABEL.set(label)))
    if cancel_check is not None:
        tokens.append((_STREAM_CANCEL, _STREAM_CANCEL.set(cancel_check)))
    if fresh_state or _STREAM_STATE.get() is None:
        tokens.append((_STREAM_STATE, _STREAM_STATE.set({"delta_emitted": False})))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def emit_event(
    kind: str,
    text: str = "",
    *,
    unit: Optional[str] = None,
    label: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
) -> None:
    """向当前活跃流写入一条结构化输出事件；无活跃流时为空操作。"""
    sink = _STREAM_SINK.get()
    if sink is None:
        return
    u = _STREAM_UNIT.get() if unit is None else unit
    lbl = _STREAM_LABEL.get() if label is None else label
    txt = "" if text is None else str(text)
    if kind in ("thinking", "content") and txt:
        state = _STREAM_STATE.get()
        if state is not None:
            state["delta_emitted"] = True
    payload: dict[str, Any] = {
        "kind": str(kind or "stage"),
        "unit": str(u or ""),
        "label": str(lbl or ""),
        "text": txt,
    }
    if data is not None:
        payload["data"] = dict(data)
    sink(payload)


def emit_result_fallback(result: Any) -> None:
    """当调用链未产生流式增量（例如测试直接 mock 了 _call_api）时，输出格式化结果。"""
    if not has_active_stream() or result is None or current_delta_emitted():
        return
    try:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(result)
    if text:
        emit_event("result", text)


def _extract_reasoning_text(obj: Any) -> str:
    """从 delta 或 message 中提取思维链文本（仅接受真实字符串）。"""
    for attr in ("reasoning_content", "reasoning"):
        val = getattr(obj, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


def consume_response_stream(
    resp: Any,
    *,
    flush_interval: float = 0.08,
    flush_chars: int = 80,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Any:
    """消费 OpenAI 流式响应或透传非流式 ChatCompletion，返回标准 choices 对象。"""
    choices = getattr(resp, "choices", None)
    if isinstance(choices, (list, tuple)):
        if choices:
            msg = getattr(choices[0], "message", None)
            if msg is not None:
                r_text = _extract_reasoning_text(msg)
                if r_text:
                    emit_event("thinking", r_text)
                c_text = getattr(msg, "content", None)
                if isinstance(c_text, str) and c_text:
                    emit_event("content", c_text)
        return resp

    try:
        iterator = iter(resp)
    except TypeError:
        return resp

    cancel = cancel_check or _STREAM_CANCEL.get()
    thinking_chunks: list[str] = []
    content_chunks: list[str] = []
    finish_reason: Optional[str] = None
    usage: Any = None

    buf_kind: Optional[str] = None
    buf_parts: list[str] = []
    buf_len = 0
    last_flush = time.monotonic()

    def flush_buf() -> None:
        nonlocal buf_kind, buf_parts, buf_len, last_flush
        if buf_kind and buf_parts:
            emit_event(buf_kind, "".join(buf_parts))
        buf_kind = None
        buf_parts = []
        buf_len = 0
        last_flush = time.monotonic()

    def push_delta(kind: str, part: str) -> None:
        nonlocal buf_kind, buf_len
        if buf_kind and buf_kind != kind:
            flush_buf()
        buf_kind = kind
        buf_parts.append(part)
        buf_len += len(part)
        if buf_len >= flush_chars or (time.monotonic() - last_flush) >= flush_interval:
            flush_buf()

    try:
        for chunk in iterator:
            if cancel and cancel():
                flush_buf()
                raise StreamCancelledError("分析任务已取消")
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            chunk_choices = getattr(chunk, "choices", None)
            if not isinstance(chunk_choices, (list, tuple)) or not chunk_choices:
                continue
            c0 = chunk_choices[0]
            fr = getattr(c0, "finish_reason", None)
            if isinstance(fr, str) and fr:
                finish_reason = fr
            delta = getattr(c0, "delta", None)
            if delta is None:
                continue
            r_part = _extract_reasoning_text(delta)
            if r_part:
                thinking_chunks.append(r_part)
                push_delta("thinking", r_part)
            c_part = getattr(delta, "content", None)
            if isinstance(c_part, str) and c_part:
                content_chunks.append(c_part)
                push_delta("content", c_part)
        if cancel and cancel():
            flush_buf()
            raise StreamCancelledError("分析任务已取消")
        flush_buf()
    finally:
        close_fn = getattr(resp, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    msg = SimpleNamespace(
        content="".join(content_chunks),
        reasoning_content="".join(thinking_chunks),
    )
    choice = SimpleNamespace(
        message=msg,
        finish_reason=finish_reason or "stop",
    )
    return SimpleNamespace(choices=[choice], usage=usage)
