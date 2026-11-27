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
"""无状态 LLM 运行时公共底座：客户端缓存、节流冷却、预算控制、文本与视觉请求。"""

import json
import os
import re
import threading
import time
import uuid
from functools import partial
from importlib.metadata import PackageNotFoundError, version as _package_version
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

from openai import OpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    env_choice,
    env_int,
    env_number,
    parse_bool,
)
from analyzer import llm_stream
from analyzer import llm_transport
from analyzer.logger import get_logger
from analyzer.usage import record_call

logger = get_logger("deepseek")


def _is_opencode_url(base_url: str) -> bool:
    """判断 base_url 是否指向 OpenCode Inference 网关。"""
    parsed = urlparse((base_url or "").strip())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    return host in ("opencode.ai", "www.opencode.ai") and (
        path == "/inference" or path.startswith("/inference/")
    )


def _normalize_base_url(base_url: str) -> str:
    """将 OpenCode Inference 入口地址规整为 OpenAI Chat Completions 所需的 base_url。"""
    raw = (base_url or "").strip()
    if not _is_opencode_url(raw):
        return raw
    trimmed = raw.rstrip("/")
    if trimmed.lower().endswith("/chat/completions"):
        trimmed = trimmed[: -len("/chat/completions")]
    parsed = urlparse(trimmed)
    path = parsed.path.rstrip("/")
    lower_path = path.lower()
    if lower_path in ("/inference", "/inference/go"):
        path = f"{path}/openai/v1"
    elif lower_path in ("/inference/openai", "/inference/go/openai"):
        path = f"{path}/v1"
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def _client_user_agent() -> str:
    """生成用于网关识别的客户端 User-Agent。"""
    try:
        ver = _package_version("qqchatlog")
    except PackageNotFoundError:
        ver = "dev"
    return f"qqchatlog/{ver}"


_OPENCODE_SESSION_ID = (os.getenv("OPENCODE_SESSION_ID", "") or "").strip() or f"ses_{uuid.uuid4().hex[:24]}"


def _client_default_headers(base_url: str) -> Optional[dict[str, str]]:
    """为 OpenCode Inference/Go 附加必需的会话与客户端标识请求头。"""
    if not _is_opencode_url(base_url):
        return None
    session_id = (os.getenv("OPENCODE_SESSION_ID", "") or "").strip() or _OPENCODE_SESSION_ID
    return {
        "x-opencode-session": session_id,
        "User-Agent": _client_user_agent(),
    }


# 按服务商预设请求并发与节流间隔（阿里云百炼按主账号聚合 TPM，默认值更保守）
_OFFICIAL = "api.deepseek.com" in DEEPSEEK_BASE_URL.lower()
_IS_OPENCODE_DEEPSEEK = _is_opencode_url(DEEPSEEK_BASE_URL) and "deepseek" in DEEPSEEK_MODEL.lower()
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

# 思考模式开关：官方端点与 OpenCode DeepSeek 默认全开，其余未显式设置时不透传；兼容 enabled/disabled
_REASONING_EFFORT_CHOICES = ("minimal", "low", "medium", "high", "xhigh", "max")
REASONING_EFFORT = env_choice("LLM_REASONING_EFFORT", "", _REASONING_EFFORT_CHOICES)
_THINKING_ENV = (os.getenv("LLM_THINKING", "") or "").strip().lower()
_THINKING_NORMALIZED = {"enabled": "true", "disabled": "false"}.get(_THINKING_ENV, _THINKING_ENV)
THINKING_DEFAULT = parse_bool(
    _THINKING_NORMALIZED, _OFFICIAL or _IS_OPENCODE_DEEPSEEK or bool(REASONING_EFFORT)
)
THINKING_DIMS = frozenset(
    s.strip().lower() for s in (os.getenv("LLM_THINKING_DIMS", "") or "").split(",") if s.strip()
)
_SEND_THINKING_PARAM = (
    bool(_THINKING_ENV or THINKING_DIMS) or "deepseek" in DEEPSEEK_BASE_URL.lower() or _IS_OPENCODE_DEEPSEEK
)


def thinking_enabled(dim: str) -> bool:
    """判断维度是否启用思考模式（由全局开关或 LLM_THINKING_DIMS 控制）。"""
    return THINKING_DEFAULT or (dim or "").strip().lower() in THINKING_DIMS


_prompt_fingerprint_provider: Optional[Callable[[], str]] = None


def register_prompt_fingerprint_provider(provider: Callable[[], str]) -> None:
    """由提示词指纹拥有方注册默认指纹提供器，避免子模块反向导入 deepseek_client 成环。"""
    global _prompt_fingerprint_provider
    _prompt_fingerprint_provider = provider


def default_prompt_fingerprint() -> str:
    """返回当前活跃的默认私聊提示词指纹（未注册时延迟导入拥有方，仍未注册则抛错）。"""
    provider = _prompt_fingerprint_provider
    if provider is None:
        # 独立脚本/测试可能先导入 month_cache 或 vision，按需延迟触发 deepseek_client 注册
        import analyzer.deepseek_client  # noqa: F401

        provider = _prompt_fingerprint_provider
    if provider is None:
        raise RuntimeError("默认私聊提示词指纹提供器尚未注册")
    return provider()


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
    所以上限覆盖全部维度）。生产路径由 webapp.jobs 的入口调用，并必须与
    end_run() 配对（放在 finally 里）。
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
    if status in (401, 403) and (
        "quota" in s
        or "entitlementerror" in s
        or "subscription required" in s
        or "only available from within opencode" in s
    ):
        return True
    if status == 402 or "insufficient balance" in s:
        return True  # DeepSeek 官方 / OpenCode：余额不足（402 Insufficient Balance）
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
#: ① 各家网关的 `sk-` 与 OpenCode 的 `oc_sk_` 前缀；② 键名后的长值（保留第 1 组键名，抹掉第 2 组值）。
_KEY_SHAPED = re.compile(r"\b(?:oc_sk_|sk-)[A-Za-z0-9_\-]{8,}\b")
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
                base_url = _normalize_base_url(DEEPSEEK_BASE_URL)
                kwargs: dict[str, Any] = {
                    "api_key": DEEPSEEK_API_KEY,
                    "base_url": base_url,
                    "timeout": REQUEST_TIMEOUT,
                    "max_retries": 0,  # 重试由 _call_api 自行实现（指数退避）
                }
                default_headers = _client_default_headers(base_url)
                if default_headers:
                    kwargs["default_headers"] = default_headers
                _CLIENT = OpenAI(**kwargs)
    return _CLIENT


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
    """JSON 模式的请求参数（思考模式决定 extra_body、reasoning_effort 与是否下发 temperature）"""
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
        if _SEND_THINKING_PARAM or not REASONING_EFFORT:
            params["extra_body"] = {"thinking": {"type": "enabled"}}
        if REASONING_EFFORT:
            params["reasoning_effort"] = REASONING_EFFORT
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


def is_api_configured() -> bool:
    """检查 API Key 是否已配置（占位符视为未配置）"""
    return DEEPSEEK_API_KEY.strip() not in _PLACEHOLDER_KEYS


def is_insecure_base_url() -> bool:
    """base_url 是否为"明文 http 且指向非本机"——这种配置下 Key 与聊天内容会明文过网"""
    url = (DEEPSEEK_BASE_URL or "").strip()
    if not url.lower().startswith("http://"):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return True
    return host not in ("localhost", "127.0.0.1", "::1")


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
        kwargs: dict[str, Any] = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        if _SEND_THINKING_PARAM:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        client.chat.completions.create(**kwargs)
    except Exception as e:  # noqa: BLE001 —— 错误文本就是产品，这里不吞、原样带回
        # 原样带回，但先洗一遍：第三方网关常把收到的 Key 原样写进错误正文，
        # 而这段文本会直接显示给用户、并进日志（见 scrub_secrets）。
        return False, scrub_secrets(f"{type(e).__name__}: {str(e)[:200]}")
    return True, f"模型 {DEEPSEEK_MODEL} 应答成功，延迟 {int((time.time() - t0) * 1000)} ms"
