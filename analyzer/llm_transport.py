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

"""LLM 请求的限流、调用计数、用量记账与错误退避。"""

from typing import Any, Callable, Optional

from . import llm_stream


class EmptyChoicesError(RuntimeError):
    """模型响应缺少有效 choices（可能触发内容审核或上游网关异常）。"""


def _is_stream_param_rejected(exc: Exception) -> bool:
    """判断错误是否由服务端或桩函数不支持 stream / stream_options 参数引起。"""
    if isinstance(exc, TypeError):
        return True
    if getattr(exc, "status_code", None) != 400:
        return False
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "stream_options",
            "stream",
            "unrecognized",
            "unknown parameter",
            "unsupported",
            "extra_forbidden",
        )
    )


def _call_completions(client, build_params: Callable[[], dict]):
    """在有活跃流上下文时优先启用流式输出，不支持时自动降级并透传响应。"""
    params = build_params()
    if not llm_stream.has_active_stream() or params.get("stream") is False:
        return client.chat.completions.create(**params)

    stream_with_usage = dict(params)
    stream_with_usage["stream"] = True
    stream_with_usage.setdefault("stream_options", {"include_usage": True})
    try:
        raw = client.chat.completions.create(**stream_with_usage)
    except Exception as e:
        if not _is_stream_param_rejected(e):
            raise
        stream_only = dict(params)
        stream_only["stream"] = True
        try:
            raw = client.chat.completions.create(**stream_only)
        except Exception as e2:
            if not _is_stream_param_rejected(e2):
                raise
            raw = client.chat.completions.create(**build_params())
    return llm_stream.consume_response_stream(raw)


def _emit_retry_warn(message: str) -> None:
    should_reset = llm_stream.has_active_stream() and llm_stream.current_delta_emitted()
    llm_stream.emit_event(
        "warn",
        message,
        data={"reset": True} if should_reset else None,
    )
    if should_reset:
        llm_stream.reset_delta_emitted()


def request_with_retry(
    client,
    build_params: Callable[[], dict],
    *,
    tag: str,
    max_attempts: int,
    generic_retries: int,
    tpm_wait: float,
    fatal_message: str,
    count_call,
    pace,
    is_plan_exhausted,
    is_tpm_throttle,
    set_cooldown,
    tpm_max_attempts: int,
    logger,
    record_call,
    model: str,
    scrub_secrets,
    quota_exhausted_error,
    sleep,
) -> tuple[Any, Optional[str]]:
    """执行带重试的 API 调用，返回响应或可恢复失败原因。"""
    tpm_hits = 0
    generic_failures = 0
    last_tpm = False
    for _attempt in range(max_attempts):
        if llm_stream.is_cancelled():
            raise llm_stream.StreamCancelledError("分析任务已取消")
        # 计费闸门在 try 之外，调用上限错误不能被当成网络波动重试。
        count_call()
        try:
            pace()
            if llm_stream.is_cancelled():
                raise llm_stream.StreamCancelledError("分析任务已取消")
            resp = _call_completions(client, build_params)
            choices = getattr(resp, "choices", None)
            if not choices:
                raise EmptyChoicesError("模型返回空 choices（可能触发内容审核或上游网关异常）")
            choice = choices[0]
            if resp.usage:
                logger.info(
                    "token 用量[%s]: prompt=%s completion=%s finish=%s",
                    tag,
                    resp.usage.prompt_tokens,
                    resp.usage.completion_tokens,
                    choice.finish_reason,
                )
                record_call(model, tag, resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
                if llm_stream.has_active_stream():
                    llm_stream.emit_event(
                        "usage",
                        (
                            f"token 用量：prompt={resp.usage.prompt_tokens or 0}, "
                            f"completion={resp.usage.completion_tokens or 0}, finish={choice.finish_reason}"
                        ),
                        data={
                            "prompt_tokens": int(resp.usage.prompt_tokens or 0),
                            "completion_tokens": int(resp.usage.completion_tokens or 0),
                            "finish_reason": str(choice.finish_reason or ""),
                        },
                    )
            return resp, None
        except (llm_stream.StreamCancelledError, EmptyChoicesError):
            raise
        except Exception as e:
            if is_plan_exhausted(e):
                raise quota_exhausted_error(fatal_message) from e
            if is_tpm_throttle(e):
                last_tpm = True
                tpm_hits += 1
                if tpm_hits < tpm_max_attempts:
                    logger.warning(
                        "触发每分钟限流（TPM/RPM），全局冷却 %.0fs 后重试（%d/%d）",
                        tpm_wait,
                        tpm_hits,
                        tpm_max_attempts,
                    )
                    _emit_retry_warn(
                        f"触发每分钟限流，全局冷却 {tpm_wait:.0f}s 后重试（{tpm_hits}/{tpm_max_attempts}）"
                    )
                    set_cooldown(tpm_wait)
                    sleep(tpm_wait)
                    continue
                return None, "tpm"
            last_tpm = False
            if generic_failures < generic_retries:
                generic_failures += 1
                delay = 2 ** (generic_failures - 1)
                logger.warning(
                    "API 调用失败（第 %s 次，%ss 后重试）: %s",
                    generic_failures,
                    delay,
                    scrub_secrets(e),
                )
                _emit_retry_warn(
                    f"API 调用失败（第 {generic_failures} 次，{delay}s 后重试）：{scrub_secrets(e)}"
                )
                sleep(delay)
                continue
            raise
    return None, ("tpm" if last_tpm else "error")
