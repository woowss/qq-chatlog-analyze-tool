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
    for attempt in range(max_attempts):
        # 计费闸门在 try 之外，调用上限错误不能被当成网络波动重试。
        count_call()
        try:
            pace()
            resp = client.chat.completions.create(**build_params())
            choice = resp.choices[0]
            if resp.usage:
                logger.info(
                    "token 用量[%s]: prompt=%s completion=%s finish=%s",
                    tag,
                    resp.usage.prompt_tokens,
                    resp.usage.completion_tokens,
                    choice.finish_reason,
                )
                record_call(model, tag, resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            return resp, None
        except Exception as e:
            if is_plan_exhausted(e):
                raise quota_exhausted_error(fatal_message) from e
            if is_tpm_throttle(e):
                tpm_hits += 1
                if tpm_hits < tpm_max_attempts:
                    logger.warning(
                        "触发每分钟限流（TPM/RPM），全局冷却 %.0fs 后重试（%d/%d）",
                        tpm_wait,
                        tpm_hits,
                        tpm_max_attempts,
                    )
                    set_cooldown(tpm_wait)
                    sleep(tpm_wait)
                    continue
                return None, "tpm"
            if attempt < generic_retries:
                delay = 2**attempt
                logger.warning(
                    "API 调用失败（第 %s 次，%ss 后重试）: %s",
                    attempt + 1,
                    delay,
                    scrub_secrets(e),
                )
                sleep(delay)
                continue
            raise
    return None, "error"
