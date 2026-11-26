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

对话构建（`analyzer/dialog.py`）、月份增量缓存（`analyzer/month_cache.py`）与
限流重试（`analyzer/llm_transport.py`）已拆至独立模块，并在此保留兼容重导出；
测试打桩请直接作用于对应子模块的全局状态。
"""

import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from functools import partial
from typing import Any, Callable, Optional

from openai import OpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_BASE_URL,
    env_int,
    env_number,
    parse_bool,
)
from parser.qq_parser import ChatData, is_statistical, split_by_month
from analyzer.logger import get_logger, mask_name
from analyzer import purge_marks
from analyzer import llm_transport
from analyzer import person_context
from analyzer.shutdown import shutdown_requested
from analyzer.usage import record_call
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
from analyzer import llm_stream

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


# 按服务商预设请求并发与节流间隔（阿里云百炼按主账号聚合 TPM，默认值更保守）
_OFFICIAL = "api.deepseek.com" in DEEPSEEK_BASE_URL.lower()
_DEFAULT_INTERVAL = 0.5 if _OFFICIAL else 3.0
_DEFAULT_CONCURRENCY = 6 if _OFFICIAL else 2
CONCURRENCY = int(env_number("LLM_CONCURRENCY", _DEFAULT_CONCURRENCY, 1, 64))
CALL_MIN_INTERVAL = float(env_number("LLM_CALL_MIN_INTERVAL", _DEFAULT_INTERVAL, 0.0, 60.0))
REQUEST_TIMEOUT = 300
TPM_MAX_ATTEMPTS = 4
TPM_WAIT_SECONDS = 25.0

# 各维度输出 token 上限预算（供 LLM_MAX_TOKENS_<DIM> 覆盖；思考模式需预留思维链空间）
_DEFAULT_MAX_TOKENS = {
    "emotion": 32768,
    "topics": 32768,
    "relationship": 32768,
    "habits": 32768,
    "profile": 49152,
    # 群聊、总括与提问共用 LLM_MAX_TOKENS_<维度> 覆盖机制，不计入私聊提示词指纹
    "group_dynamics": 32768,
    "group_topics": 32768,
    "group_emotion": 32768,
    "member_profiles": 49152,
    "recap": 16384,
    "ask": 16384,
}


def _max_tokens(dim: str, default: int) -> int:
    """读取维度输出 token 上限（支持环境变量 LLM_MAX_TOKENS_<DIM> 覆盖，影响指纹键）。"""
    env_name = f"LLM_MAX_TOKENS_{dim.upper()}"
    raw = (os.getenv(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        logger.warning("%s=%r 不是数字，已回退为 %d", env_name, raw, default)
        return default
    if not 256 <= value <= 384_000:
        logger.warning("%s=%d 超出 [256, 384000]，已回退为 %d", env_name, value, default)
        return default
    return value


MAX_TOKENS_BY_DIM = {dim: _max_tokens(dim, default) for dim, default in _DEFAULT_MAX_TOKENS.items()}

# 思考模式开关：官方端点默认全开，非官方网关未显式设置时不透传参数；兼容 enabled/disabled 取值
_THINKING_ENV = (os.getenv("LLM_THINKING", "") or "").strip().lower()
_THINKING_NORMALIZED = {"enabled": "true", "disabled": "false"}.get(_THINKING_ENV, _THINKING_ENV)
THINKING_DEFAULT = parse_bool(_THINKING_NORMALIZED, _OFFICIAL)
THINKING_DIMS = frozenset(
    s.strip().lower() for s in (os.getenv("LLM_THINKING_DIMS", "") or "").split(",") if s.strip()
)
_SEND_THINKING_PARAM = bool(_THINKING_ENV or THINKING_DIMS) or "deepseek" in DEEPSEEK_BASE_URL.lower()


def thinking_enabled(dim: str) -> bool:
    """判断维度是否启用思考模式（由全局开关或 LLM_THINKING_DIMS 控制）。"""
    return THINKING_DEFAULT or (dim or "").strip().lower() in THINKING_DIMS


_call_gate = threading.Lock()
_next_call_at = [0.0]
_cooldown_until = [0.0]  # 全局冷却：任一请求吃到 429 后，所有线程共同退避


def _pace() -> None:
    """全局调用闸门：均匀排队 + 遵守 429 触发的全局冷却，避免并发线程各自撞墙"""
    with _call_gate:
        now = time.monotonic()
        start = max(now, _next_call_at[0], _cooldown_until[0])
        _next_call_at[0] = start + CALL_MIN_INTERVAL
        delay = start - now
    if delay > 0:
        time.sleep(delay)


def _set_cooldown(seconds: float) -> None:
    with _call_gate:
        _cooldown_until[0] = max(_cooldown_until[0], time.monotonic() + seconds)


class QuotaExhaustedError(RuntimeError):
    """配额类致命错误：套餐耗尽/欠费，或 TPM 限流多次等待后仍未恢复。
    重试已无意义，上层应中止剩余任务并保留部分结果。"""


class AnalysisIncompleteError(RuntimeError):
    """至少一个有可分析内容的单元未能生成有效结果，汇总结果不可作为完成态缓存。"""


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


# ---------------------------------------------------------------------------
# 本次运行的调用次数硬上限（默认 0 = 不限）
# ---------------------------------------------------------------------------
# 存在理由是"意外花费"：月份级并发 + 成员画像按人计费，一旦参数配错（月数算错、
# 群成员上限调得过大），一次点击就可能发出远超预期的付费请求，而界面上当时看不出异常。
# 超限走既有的 QuotaExhaustedError 路径——中止剩余任务、已完成的部分照常保留
# （月份级与成员级结果都已落盘，重跑不会重复付费）。
#
# 默认 0（不限）是刻意的：几年私聊 × 5 个维度本来就能上千次调用，给一个默认上限
# 等于把正常用法掐断。想要护栏就在 .env 里设一个自己算得清的值。
MAX_CALLS_PER_RUN = env_int("LLM_MAX_CALLS_PER_RUN", 0, 0, 1_000_000)

_run_calls = 0
_run_calls_lock = threading.Lock()
#: 是否处在"一次运行"之内。上限只约束有明确起点与终点的运行；
#: 直接调用 analyze_* 的脚本与工具没有这个边界，不该被它掐断（也没有"这次"可言）。
_run_active = False
#: 正在进行的运行数（>0 表示还有 run 边界没结束）。重叠运行**共享**同一份额度：
#: 第二个 begin_run 不清零，否则两次点击各拿一份额度、总量可达上限的 N 倍。
_run_depth = 0


def begin_run() -> None:
    """开始一次分析运行：**只有当前没有运行在跑时**才把计数清零。

    "一次运行" = 单个维度的分析，或"一键全量"的整个循环（后者只清零一次，
    所以上限覆盖全部维度）。生产路径由 webapp.jobs 的两个入口调用，并必须与
    end_run() 配对（那两个入口放在 finally 里）。

    为什么要数深度而不是每次清零：上限的承诺是"一次点击不会悄悄花超"。两个运行
    重叠时（同进程里点了两次、或全量任务没结束又发起单维度）各自清零等于把额度
    放大 N 倍，而且先开始的那个会因为计数被重置而失去约束——它的花费再也不会
    被卡住。现在后开始的那个只是**加入**正在进行的额度：谁先开始谁定义这份预算，
    总发出请求数始终 ≤ 上限。

    计数是**进程级**的而不是线程局部的：月份调用发生在并发线程池里，线程局部计数
    跨不过去。代价是同一进程内两个并发分析共享这一个上限——单机工具里这种并发很
    少见，换来的是"无论怎么触发，总量都不会悄悄翻倍"。
    """
    global _run_calls, _run_active, _run_depth
    with _run_calls_lock:
        if _run_depth == 0:
            _run_calls = 0
        _run_depth += 1
        _run_active = True


def end_run() -> None:
    """结束一次运行；最后一个结束的调用把"运行中"收回 False。

    必须与 begin_run() 成对（webapp.jobs 用 finally 保证）。漏掉一次的后果不是
    "少减一个数"：_run_depth 会永远 >0，此后每次 begin_run 都不再清零，上限会
    悄悄变成"进程启动以来的累计"。多调一次则由下面的夹取兜住，不会变成负数。
    """
    global _run_active, _run_depth
    with _run_calls_lock:
        if _run_depth > 0:
            _run_depth -= 1
        if _run_depth == 0:
            _run_active = False


def calls_used() -> int:
    """本次运行已发出的请求数（供界面与排查读取）"""
    with _run_calls_lock:
        return _run_calls


def _count_call() -> int:
    """记一次即将发出的请求；超过 LLM_MAX_CALLS_PER_RUN 时抛出致命错误。

    每次**真正发出的 HTTP 请求**都算一次（含限流/网络错误的重试）：那些重试同样
    占用服务商配额，按"请求数"而不是"成功调用数"来卡才是花费上限该有的口径。
    """
    global _run_calls
    with _run_calls_lock:
        _run_calls += 1
        used = _run_calls
        active = _run_active
    if active and MAX_CALLS_PER_RUN and used > MAX_CALLS_PER_RUN:
        raise QuotaExhaustedError(
            f"本次分析已发出 {used} 次请求，超过 LLM_MAX_CALLS_PER_RUN={MAX_CALLS_PER_RUN} 的上限，"
            "剩余任务已中止。已完成的部分照常保留（月份级与成员级结果都已落盘，重跑不会重复付费）。"
            "要跑完就把该值调大，或设为 0 关闭这道上限。"
        )
    return used


def _is_plan_exhausted(e: Exception) -> bool:
    """真正的额度耗尽/账号异常（非限流，重试无意义）"""
    status = getattr(e, "status_code", None)
    s = str(e).lower()
    if status in (401, 403) and "quota" in s:
        return True
    if status == 402 or "insufficient balance" in s:
        return True  # DeepSeek 官方：余额不足（402 Insufficient Balance）
    return "arrearage" in s or "free allocated quota exceeded" in s


def _is_tpm_throttle(e: Exception) -> bool:
    """百炼 429 = TPM/RPM 每分钟限流（官方文档：通常 1 分钟内自动恢复），可重试。
    注意其报错文案 'Allocated quota exceeded'/'insufficient_quota' 有误导性，
    与套餐额度（7 天池）无关。"""
    if getattr(e, "status_code", None) == 429:
        return True
    s = str(e).lower()
    return any(
        m in s
        for m in (
            "allocated quota exceeded",
            "you exceeded your current quota",
            "insufficient_quota",
            "rate limit",
        )
    )


# .env 模板中的占位符值，视为"未配置"
_PLACEHOLDER_KEYS = {"", "你的DeepSeek_API_Key", "你的API_Key"}

#: 长得像 API Key 的字串。刻意**窄**：宽泛的"长字串"规则会把
#: `session_id=…`、`request-id: …` 这类有用的排障线索一起抹掉。
#: ① 各家网关通用的 `sk-` 前缀；② 键名后面跟的长值（第 1 组是键名，保留；第 2 组是值，抹掉）。
_KEY_SHAPED = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_KEYED_VALUE = re.compile(
    r"(\b(?:api[_-]?key|apikey|access[_-]?token|authorization)\b\s*[:=]\s*[\"']?)([A-Za-z0-9_\-\.]{12,})",
    re.IGNORECASE,
)


def scrub_secrets(text) -> str:
    """把可能夹带 API Key 的文本洗一遍，供**对外可见**的路径使用（回显给浏览器、写日志）。

    为什么需要：本项目把上游错误原文当"产品"如实带回（429/401/404 的文案差异就是排障
    线索），Key 本身是以 Authorization 头发的、不出现在 URL 里，官方端点还会把它打码
    ——但"OpenAI 兼容"的第三方网关/反代把**收到的 Key 原样写进错误正文**是常见做法。
    而这些文本会走到两处对外的地方：`/api/status/test` 的响应（用户会截图贴出来求助）
    与 `logs/app.log`（README 承诺日志可以整目录留存/贴给别人看）。
    所以抹两类东西：① 本机配置的那个 Key 本身（无论上游怎么拼装、切片它）；
    ② 任何"长得像 Key"的字串。是抹掉而不是截断：错误文本其余部分仍要可读。
    """
    out = str(text or "")
    configured = (DEEPSEEK_API_KEY or "").strip()
    if configured and configured not in _PLACEHOLDER_KEYS:
        out = out.replace(configured, "<API_KEY>")
    out = _KEY_SHAPED.sub("<redacted>", out)
    return _KEYED_VALUE.sub(r"\1<redacted>", out)


_CLIENT: Optional[OpenAI] = None
_CLIENT_LOCK = threading.Lock()


def _get_client() -> Optional[OpenAI]:
    """获取 OpenAI 客户端（进程内复用）。

    每次调用都新建 client 会让连接池无法复用：一次全量分析几十次调用就是几十次
    TLS 握手，而且并发月份各自建池。key/base_url 都是 import 期常量，缓存无风险。
    """
    global _CLIENT
    if DEEPSEEK_API_KEY.strip() in _PLACEHOLDER_KEYS:
        return None
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = OpenAI(
                    api_key=DEEPSEEK_API_KEY,
                    base_url=DEEPSEEK_BASE_URL,
                    timeout=REQUEST_TIMEOUT,
                    max_retries=0,  # 重试由 _call_api 自行实现（指数退避）
                )
    return _CLIENT


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
            MAX_TOKENS_BY_DIM.get("emotion"),
            MAX_TOKENS_BY_DIM.get("topics"),
            MAX_TOKENS_BY_DIM.get("relationship"),
            MAX_TOKENS_BY_DIM.get("habits"),
            MAX_TOKENS_BY_DIM.get("profile"),
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


# 思考模式的最低输出预算：思维链 token 也计入 max_tokens，低于这个值必然截断
THINKING_MIN_TOKENS = 4096


def thinking_budget_warnings() -> list[str]:
    """启动自检：列出"开了思考模式但预算不足"的维度（这类组合会 100% 截断丢结果）"""
    return [
        f"{dim}（预算 {budget} < {THINKING_MIN_TOKENS}）"
        for dim, budget in MAX_TOKENS_BY_DIM.items()
        if thinking_enabled(dim) and budget < THINKING_MIN_TOKENS
    ]


def _request_with_retry(
    client: OpenAI,
    build_params: Callable[[], dict],
    *,
    tag: str,
    max_attempts: int,
    generic_retries: int,
    tpm_wait: float,
    fatal_message: str,
) -> tuple[Any, Optional[str]]:
    # Stable facade: tests and callers patch this module-level name.
    return llm_transport.request_with_retry(
        client,
        build_params,
        tag=tag,
        max_attempts=max_attempts,
        generic_retries=generic_retries,
        tpm_wait=tpm_wait,
        fatal_message=fatal_message,
        count_call=_count_call,
        pace=_pace,
        is_plan_exhausted=_is_plan_exhausted,
        is_tpm_throttle=_is_tpm_throttle,
        set_cooldown=_set_cooldown,
        tpm_max_attempts=TPM_MAX_ATTEMPTS,
        logger=logger,
        record_call=record_call,
        model=DEEPSEEK_MODEL,
        scrub_secrets=scrub_secrets,
        quota_exhausted_error=QuotaExhaustedError,
        sleep=time.sleep,
    )


def _json_request_params(system_prompt: str, user_content: str, max_tokens: int, think: bool) -> dict:
    """JSON 模式的请求参数（思考模式决定 extra_body 与是否下发 temperature）"""
    params: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if think:
        params["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        # 非思考模式：temperature 生效，保持原有低随机性以保证 JSON 稳定
        params["temperature"] = 0.3
        if _SEND_THINKING_PARAM:
            params["extra_body"] = {"thinking": {"type": "disabled"}}
    return params


def _call_api(
    system_prompt: str,
    user_content: str,
    max_tokens: int = 2048,
    retry: int = 2,
    tpm_wait: Optional[float] = None,
    tag: str = "unknown",
    dim: Optional[str] = None,
) -> Optional[dict]:
    """调用 LLM API，返回解析后的 JSON。

    dim：维度名，决定是否使用思考模式（默认取 tag；两者取值同为
    emotion/topics/relationship/habits/profile）。

    错误分类策略：
    - 429（TPM/RPM 每分钟限流，含误导性的 "Allocated quota exceeded/insufficient_quota"
      文案）：等待约 25s 重试，最多 4 次——官方文档确认通常 1 分钟内自动恢复；
    - 403/欠费等真正的额度耗尽：立即抛 QuotaExhaustedError，中止上层任务（重试无意义）；
    - 输出被 max_tokens 截断或非法 JSON：记日志返回 None（输入相同则结果确定，重试浪费钱）；
    - 其他网络错误：指数退避重试。
    """
    client = _get_client()
    if client is None:
        return None

    think = thinking_enabled(dim or tag)
    if think and max_tokens < THINKING_MIN_TOKENS:
        logger.warning(
            "%s 开启了思考模式，但输出预算只有 %d tokens（思维链同样占用），"
            "结果很可能被截断丢弃；建议把预算提到 %d 以上或关闭该维度的思考模式",
            dim or tag,
            max_tokens,
            THINKING_MIN_TOKENS,
        )
    tpm_wait = TPM_WAIT_SECONDS if tpm_wait is None else tpm_wait
    max_attempts = retry + TPM_MAX_ATTEMPTS

    while True:
        llm_stream.reset_delta_emitted()
        # partial 而不是闭包：把当前的 think 值**绑定**进参数里（闭包会跟着
        # 后面的 think = False 一起变，被截断降级后的重试就不是同一个请求了）
        resp, reason = _request_with_retry(
            client,
            partial(_json_request_params, system_prompt, user_content, max_tokens, think),
            tag=tag,
            max_attempts=max_attempts,
            generic_retries=retry,
            tpm_wait=tpm_wait,
            fatal_message=(
                "套餐额度耗尽/账号异常（401/403 配额、402 余额不足、欠费）：请到服务商控制台"
                "充值或等待配额周期重置后重试。剩余任务已中止，已完成部分已保留。"
            ),
        )
        if resp is None:
            # 走到这里只可能是限流重试已用尽（其他失败在骨架里已抛出）
            raise QuotaExhaustedError(
                "每分钟限流（TPM/RPM）多次等待后仍未恢复：可能同账号其他程序正在占用配额。"
                "可稍后再试，或在 .env 中调低 LLM_CONCURRENCY / LLM_CALL_MIN_INTERVAL，"
                "也可到服务商控制台提升该模型的 TPM 限额。"
            )
        choices = getattr(resp, "choices", None)
        if not choices:
            logger.error("模型返回空 choices，放弃本次结果（不重试）")
            llm_stream.emit_event("error", "模型返回空 choices，放弃本次结果")
            return None
        choice = choices[0]
        if choice.finish_reason == "length":
            # 思考模式下思维链也占 max_tokens：大月份偶发被吃满。
            # 结果被截断=该月白跑，所以先降级为"关思考"重试一次——宁可精度略降，
            # 也不让这个月从结果里消失（真实数据踩过：最大月份整月丢失）。
            if think:
                logger.warning(
                    "输出被 max_tokens=%s 截断，改用非思考模式重试一次（保住这个月的结果）", max_tokens
                )
                llm_stream.emit_event(
                    "warn",
                    f"输出被 max_tokens={max_tokens} 截断，改用非思考模式重试一次",
                    data={"reset": True} if llm_stream.current_delta_emitted() else None,
                )
                think = False
                continue
            logger.error("模型输出被 max_tokens=%s 截断，放弃本次结果（不重试）", max_tokens)
            llm_stream.emit_event("error", f"模型输出被 max_tokens={max_tokens} 截断，放弃本次结果")
            return None
        try:
            # strict=False：容忍字符串里未转义的控制字符。模型偶尔把多行文本写成**裸换行**
            # （锐评、群动态里常见），严格模式会直接判非法 JSON，从而丢掉整月/整位成员的结果。
            # 真实数据实测：三次失败里有两次属于这种"内容合法、转义偷懒"，放宽解析即可救回；
            # 它只影响解析容忍度，不会把合法 JSON 解析错。
            parsed = json.loads(choice.message.content, strict=False)
            llm_stream.emit_result_fallback(parsed)
            return parsed
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("模型返回非法 JSON（不重试）: %s", e)
            llm_stream.emit_event("error", f"模型返回非法 JSON：{e}")
            return None


def _call_vision(system_prompt: str, user_text: str, images: list) -> str:
    """多模态调用：图片 + 文本一起发给模型，返回纯文本（失败返回空串）。

    与 _call_api 的关系：共用 _request_with_retry 的闸门、429/额度错误分类与用量统计，
    区别是输出为自由文本（不要 JSON），且失败**不致命**——图片看不懂不该拖垮文本分析。
    图片只能放在 user 消息里（放 system/assistant 会被官方判 400）。
    """
    client = _get_client()
    if client is None:
        return ""
    from analyzer import vision

    content: list[dict] = [{"type": "text", "text": user_text}]
    for img in images:
        try:
            url = vision.load_image_b64(img["path"], img["mime"])
        except OSError as e:
            logger.warning("读取图片失败，跳过该图: %s", e)
            continue
        content.append({"type": "image_url", "image_url": {"url": url, "detail": vision.VISION_DETAIL}})
    if len(content) == 1:
        return ""

    def _build_params() -> dict:
        params: dict = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "max_tokens": 512,
            # 图片摘要**固定走非思考模式**：思维链同样计入 max_tokens，而这里的预算是
            # 512（摘要本身要求不超过 240 字，够用），开思考会稳定撞上 finish_reason=length
            # 把摘要截成半截话——与"思考模式吃满预算就丢结果"是同一个坑。
            # 摘要要的是"看到什么写什么"，本来也不需要推理链。
            "temperature": 0.3,
        }
        if _SEND_THINKING_PARAM:
            params["extra_body"] = {"thinking": {"type": "disabled"}}
        return params

    try:
        resp, reason = _request_with_retry(
            client,
            _build_params,
            tag="vision",
            max_attempts=TPM_MAX_ATTEMPTS,
            generic_retries=2,
            tpm_wait=TPM_WAIT_SECONDS,
            fatal_message="套餐额度耗尽/账号异常：图片理解已中止（文本分析同样无法继续）。",
        )
    except QuotaExhaustedError:
        raise  # 额度/账号问题必须上抛：文本分析同样跑不下去，静默降级会让用户以为"只是没图"
    except Exception as e:
        logger.warning("图片摘要失败（文本分析继续）: %s", e)
        return ""
    if resp is None:
        logger.warning("图片摘要因限流放弃（文本分析继续）")
        return ""
    choices = getattr(resp, "choices", None)
    if not choices:
        logger.warning("图片摘要返回空 choices（文本分析继续）")
        return ""
    choice = choices[0]
    if choice.finish_reason == "length":
        logger.warning("图片摘要被 max_tokens 截断，已按截断内容使用")
    return (choice.message.content or "").strip()


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
                want_thinking = thinking_enabled(tag)
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
                    result = _call_api(system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag)
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
                llm_stream.emit_event("error", f"{period} 月 AI 分析失败：{scrub_secrets(e)}")
        return period, None

    pending = list(months.items())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures: dict[concurrent.futures.Future, str] = {}
            while pending or futures:
                while pending and len(futures) < CONCURRENCY and not fatal and not _cancel_requested():
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
        max_tokens=MAX_TOKENS_BY_DIM["emotion"],
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
        max_tokens=MAX_TOKENS_BY_DIM["topics"],
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
        max_tokens=MAX_TOKENS_BY_DIM["relationship"],
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
        "thinking": thinking_enabled(tag),
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
                result = _call_api(
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
            logger.error("%s 的 AI 分析失败: %s", mask_name(display_name), scrub_secrets(e))
            llm_stream.emit_event("error", f"{display_name} 的 AI 分析失败：{scrub_secrets(e)}")
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
                max_tokens=MAX_TOKENS_BY_DIM["habits"],
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
                max_tokens=MAX_TOKENS_BY_DIM["profile"],
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


def is_api_configured() -> bool:
    """检查 API Key 是否已配置（占位符视为未配置）"""
    return DEEPSEEK_API_KEY.strip() not in _PLACEHOLDER_KEYS


def is_insecure_base_url() -> bool:
    """base_url 是否为"明文 http 且指向非本机"——这种配置下 Key 与聊天内容会明文过网"""
    url = (DEEPSEEK_BASE_URL or "").strip().lower()
    if not url.startswith("http://"):
        return False
    host = url[len("http://") :].split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # 去掉端口
    return host not in ("localhost", "127.0.0.1", "::1", "[::1]")


# 启动即检查一次：明文 http 的非本机端点会让 API Key 与聊天内容裸奔，值得每次都提醒
if is_insecure_base_url():
    logger.warning(
        "DEEPSEEK_BASE_URL 使用明文 http 且不是本机地址：API Key 与聊天内容会以明文过网，请改用 https 端点"
    )


def test_connection() -> tuple:
    """连通性自检：发一个真实的最小请求（输出 1 token），如实回报延迟或错误。

    is_api_configured 只查"Key 存在"，答不了"端点通不通、模型名对不对、Key 有没有额度"——
    用户第一次配置时最想要的就是这一声回话，而不是点全量分析才发现 401。
    错误文本原样带回（截断）：429/401/404 的文案差异本身就是排障线索。
    不记入 token 用量表：探测不是分析，混进用量报表会污染"每天花了多少"的账。
    """
    if not is_api_configured():
        return False, "API Key 未配置（在 .env 里填 DEEPSEEK_API_KEY）"
    client = _get_client()
    if client is None:
        return False, "客户端创建失败：检查 DEEPSEEK_BASE_URL 与 DEEPSEEK_API_KEY"
    t0 = time.time()
    try:
        client.chat.completions.create(
            model=DEEPSEEK_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=1
        )
    except Exception as e:  # noqa: BLE001 —— 错误文本就是产品，这里不吞、原样带回
        # 原样带回，但先洗一遍：第三方网关常把收到的 Key 原样写进错误正文，
        # 而这段文本会直接显示给用户、并进日志（见 scrub_secrets）。
        return False, scrub_secrets(f"{type(e).__name__}: {str(e)[:200]}")
    return True, f"模型 {DEEPSEEK_MODEL} 应答成功，延迟 {int((time.time() - t0) * 1000)} ms"
