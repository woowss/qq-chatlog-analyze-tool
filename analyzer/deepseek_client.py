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
"""DeepSeek API 调用封装

本模块负责**API 层**：客户端复用、调用闸门与限流冷却、错误分类与重试、思考模式、
各维度的执行体（逐月并发/取消/配额中止），以及进缓存的提示词指纹。

职责拆分到独立模块（调用入口仍在本模块兼容保留）：
- analyzer/dialog.py       对话构建（把消息压成喂模型的文本）与它自己那套格式化常量；
- analyzer/month_cache.py  月份级增量缓存（内容寻址键 + manifest 引用计数 + 孤儿回收）。
- analyzer/llm_transport.py 共享的调用计数、限流、用量记账与重试；
- analyzer/fingerprint_utils.py 稳定的 AST 源码归一化。

为什么拆：把跨维度共用的纯请求与指纹机械逻辑放在独立模块，方便单独核对与复用。
为什么**不能**顺手改名：dialog 里那 5 个函数的源码进 PROMPT_FINGERPRINT，
改名/改注释/被 ruff format 重排都会让所有既有用户的私聊缓存失效、重新付费——
tests/test_group_foundation.py 的 PINNED_PRIVATE_FINGERPRINT 会让这种改动在 CI 上变红。

两者仍从本模块**再导出**，既有调用点（app.py / webapp / tools / tests）不必改；
但模块级状态的打桩要打在 owns 它的模块上（见下方导入处的说明）。

命名约定（三条，都是被缓存键逼出来的）：
1. **进指纹的 5 个函数不能改名**，而且它们正文里引用到的全局名也一起被冻住
   （_has_content / _vision_digest / _fit_lines / _message_line 等）——理由见 dialog.py
   的文件说明。下划线在这里不表示"私有"，而是"这个名字参与缓存键"。
2. 跨模块使用的下划线名（`_analyze_periods` / `_call_api` / `_clamp_int` /
   `_normalize_topic_weights` / `store._chat_hash` 等）**刻意保留原名**：它们同时是
   测试的 patch 目标（约二十处，含 6 个测试文件），改名是纯外观收益却要全仓改动，
   而改漏一处的表现是"打桩没生效"（真调 API 或静默命中真实缓存），代价远大于收益。
   读代码时请按"约定俗成的内部 API"理解这些名字。
3. 拆模块时的再导出写成 `X as X`：静态检查据此不再把它当"导入了没用"，
   也让"哪些名字是对外契约"一眼可见。
"""

import concurrent.futures
import hashlib
import inspect
import json
import os
import re
import threading
import time
from functools import partial
from typing import Any, Callable, Optional

from openai import OpenAI

# 环境变量解析统一走 config 的公开助手：口径只有一份（留空取默认、非法/越界回退并出声）。
# 这里原先自己实现过一份 _env_number，与 config 的 _env_int 逻辑相同、失败通道却不同
# （一个写 logger、一个写 stderr），排查"我明明设了参数为什么没生效"时不知道该看哪里。
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
from analyzer import fingerprint_utils
from analyzer.shutdown import shutdown_requested
from analyzer.usage import record_call
from analyzer.local_stats import SESSION_GAP_MS
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
    SYSTEM_PROMPT_PROFILE,
)

# —— 下面两个模块是从本文件拆出去的。这里全部重导出：app.py / webapp / tools / tests
#    既有的 `from analyzer.deepseek_client import configure_month_cache` 这类调用点不必改。
#
#    两条必须知道的约定：
#    ① 进 PROMPT_FINGERPRINT 的 5 个函数（_build_dialog / _message_line / _fit_lines /
#       _conversation_stats / _short_time）**函数名不可改**，而且它们正文里引用到的全局名
#       也一起被冻住了（例如 _build_dialog 里调用的 _has_content / _vision_digest）——
#       getsource 的返回值参与哈希，改名字或改注释都会让所有既有用户的私聊缓存失效、重新付费。
#    ② **模块级状态**（month_cache._MONTH_CACHE_DIR 等）必须打桩在 owns 它的模块上：
#       这里的重导出只是搬迁瞬间的值拷贝，改它不影响那边的逻辑。
from analyzer.dialog import (
    MAX_DIALOG_CHARS,
    RELATIVE_MARK_MINUTES,
    TIME_MARK_MINUTES,
    _build_dialog,
    _conversation_stats,
    _fit_lines,
    _has_content,
    _message_line,
    _short_time,
    _vision_digest,
)

# 月份缓存的名字**为兼容而再导出**：app.py 用 configure_month_cache，webapp/cleanup 用
# sweep_orphan_month_cache，webapp/store 用 purge_month_cache，测试还直接读 _MONTH_CACHE_LOCK /
# MONTH_CACHE_GRACE_SECONDS 这些模块级状态。写成 `X as X` 是表达"这是有意的再导出"的
# 标准写法（静态检查据此不再把它当"导入了没用"）。
# 但**状态**的读写请打桩在 analyzer.month_cache 上：这里的 `_MONTH_CACHE_DIR` 之类只是
# 搬迁瞬间的值拷贝（字符串/数值），改它不会影响那边的逻辑。
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
    migrate_month_cache,
    month_cache_path as month_cache_path,
    purge_month_cache as purge_month_cache,
    sweep_orphan_month_cache as sweep_orphan_month_cache,
)

logger = get_logger("deepseek")

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


# 限流节奏默认值按服务商自适应：官方 DeepSeek（并发上限 2500）可以快得多，
# 而阿里云百炼的 TPM 是按主账号聚合的，必须保守。两个值都可用环境变量覆盖。
_OFFICIAL = "api.deepseek.com" in DEEPSEEK_BASE_URL.lower()
_DEFAULT_INTERVAL = 0.5 if _OFFICIAL else 3.0
_DEFAULT_CONCURRENCY = 6 if _OFFICIAL else 2
CONCURRENCY = int(env_number("LLM_CONCURRENCY", _DEFAULT_CONCURRENCY, 1, 64))
# 全局请求平滑：两次 API 调用之间的最小间隔（秒）。注意 _pace() 是串行闸门，
# N 次调用的排队下限是 (N-1)×该值，所以它直接决定多月份分析的墙钟时间。
CALL_MIN_INTERVAL = float(env_number("LLM_CALL_MIN_INTERVAL", _DEFAULT_INTERVAL, 0.0, 60.0))
# 单次 API 请求超时（秒）：准确性优先后单月 prompt 可达十几万 tokens、思考模式输出
# 也可能很长，120s 偏紧（大月份实测 14s，但留足余量更稳）
REQUEST_TIMEOUT = 300
# TPM 限流（429 Allocated quota exceeded）：等待后重试，通常 1 分钟内恢复
TPM_MAX_ATTEMPTS = 4
TPM_WAIT_SECONDS = 25.0

# 各维度输出 token 预算：准确性优先。官方 max output 为 384K，这里给足——
# max_tokens 只是上限，模型停下来就不计费，卡太紧才会真的出事：
# 实测最长那个月（prompt 可达数十万 tokens）开着思考模式时
# 8192 会被思维链吃满 → finish_reason=length → 整月结果被丢弃。
_DEFAULT_MAX_TOKENS = {
    "emotion": 32768,
    "topics": 32768,
    "relationship": 32768,
    "habits": 32768,
    "profile": 49152,
    # —— 群聊维度（analyzer/group_client.py）。加在这里是为了共用
    #    LLM_MAX_TOKENS_<维度> 的环境变量覆盖；它们**不在**下面的私聊指纹常量元组里，
    #    因此增删/调整不会让任何私聊缓存失效。成员画像与私聊锐评同档（每个成员一次调用）。
    "group_dynamics": 32768,
    "group_topics": 32768,
    "group_emotion": 32768,
    "member_profiles": 49152,
    # —— 「整体总括」维度（analyzer/recap_client.py）。加进这张表是为了共用
    #    LLM_MAX_TOKENS_<维度> 的环境变量覆盖；它**不在**私聊指纹的常量元组里
    #    （那里只 .get() 五维的名字），所以加这一行不会让任何既有缓存失效。
    "recap": 16384,
    # 自定义提问：单问单答；预算同样守"≥16384 给思维链留足空间"的全维度不变量
    # （max_tokens 只是上限，短答案不会因此多付一分钱）
    "ask": 16384,
}


def _max_tokens(dim: str, default: int) -> int:
    """按维度读输出预算，可用 LLM_MAX_TOKENS_<维度> 覆盖（如 LLM_MAX_TOKENS_PROFILE=65536）。

    此前遇到截断时，启动横幅让用户"调大 MAX_TOKENS_BY_DIM（analyzer/deepseek_client.py）"，
    等于要求普通用户改源码；现在改 .env 即可。预算参与提示词指纹，
    所以调大之后旧缓存会自动失效、按新预算重算。
    """
    env_name = f"LLM_MAX_TOKENS_{dim.upper()}"
    raw = (os.getenv(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning("%s=%r 不是数字，已回退为 %d", env_name, raw, default)
        return default
    if not 256 <= value <= 384_000:
        logger.warning("%s=%d 超出 [256, 384000]，已回退为 %d", env_name, value, default)
        return default
    return value


MAX_TOKENS_BY_DIM = {dim: _max_tokens(dim, default) for dim, default in _DEFAULT_MAX_TOKENS.items()}

# 思考模式：准确性优先——官方 DeepSeek 端点默认**全维度开启**（思维链能显著提升
# 证据引用与推断质量），并且各维度输出预算已提到 8192 以上，不会再出现
# "思维链吃光预算 → finish_reason=length → 结果丢弃"的老问题。
# - LLM_THINKING=disabled 可整体关掉；LLM_THINKING_DIMS=a,b 可只给指定维度开；
# - 非官方网关（如百炼）不认识该字段：未显式配置时不发送，避免被严格网关判为非法参数。
#
# 这个开关历来还认 enabled/disabled（README 与 CHANGELOG 都这么写过），比共用的
# parse_bool 词表宽。所以先归一成 true/false 再交给它——统一解析口径不该顺手改掉
# 一个既有开关的语义（把 enabled 读成假，等于用户设了思考模式却静默失去它）。
_THINKING_ENV = (os.getenv("LLM_THINKING", "") or "").strip().lower()
_THINKING_NORMALIZED = {"enabled": "true", "disabled": "false"}.get(_THINKING_ENV, _THINKING_ENV)
THINKING_DEFAULT = parse_bool(_THINKING_NORMALIZED, _OFFICIAL)
THINKING_DIMS = frozenset(
    s.strip().lower() for s in (os.getenv("LLM_THINKING_DIMS", "") or "").split(",") if s.strip()
)
_SEND_THINKING_PARAM = bool(_THINKING_ENV or THINKING_DIMS) or "deepseek" in DEEPSEEK_BASE_URL.lower()


def thinking_enabled(dim: str) -> bool:
    """该维度是否使用思考模式：全局开关命中，或维度名在 LLM_THINKING_DIMS 白名单中。

    维度名与调用时传入的 tag 一致（emotion/topics/relationship/habits/profile）。
    """
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


def _hashed_source(func) -> str:
    # Stable compatibility hook; the frozen dialog functions are sourced and normalized
    # by fingerprint_utils without changing their names or implementations.
    return fingerprint_utils.hashed_source(func, canonical_ast=_canonical_ast, logger=logger)


def _canonical_ast(node) -> str:
    # Keep the historical patch point used by tests and tools.
    return fingerprint_utils.canonical_ast(node)


def _prompt_fingerprint(salt: "str | None" = None, normalize: bool = True) -> str:
    """提示词 + 对话格式的指纹，参与缓存键。

    以前靠手工维护 PROMPT_VERSION：改了 prompt 或抓取/抽样逻辑却忘了 bump，
    旧缓存就会顶着"新分析"的名义返回旧风格结果。现在把私聊的 SYSTEM_PROMPT_*
    与所有影响模型输入的格式化函数一起哈希，任何改动都会自动让旧缓存失效。

    **只哈希下面 _PRIVATE_PROMPT_NAMES 里显式列出的提示词**（不是"dir() 里所有
    SYSTEM_PROMPT_*"）：这个指纹同时进维度级缓存文件名与月份级缓存键，一旦多出
    一个常量（例如把群聊提示词放进 analyzer/prompts.py），全部私聊缓存与月份缓存
    会在 24 小时后被孤儿回收删除，用户下次分析要**全量重新付费**。群聊提示词因此
    必须放在独立模块（analyzer/group_prompts.py）并使用自己的指纹。
    改动名单的内容或顺序 = 故意换键，等同于让所有私聊缓存失效。

    取值仍然在调用时从 analyzer.prompts 现取（而不是 import 期把字符串绑进元组）：
    这样"改了提示词文本必须换指纹"这条既有保证不受影响（tests 里有用例靠打桩
    prompts 模块来验证它），也让运行时热改提示词同样能反映到指纹上。

    normalize=False 复现"按原始源码哈希"的旧公式，只用来算 PROMPT_FINGERPRINT_LEGACY
    （读旧缓存、读到就迁移，见 month_cache.month_keys 与 webapp.store._read_cache）。

    源码不可读时（frozen/编译打包，inspect.getsource 抛 OSError）降级为
    函数名占位——提示词改动仍然会失效缓存，但格式逻辑改动不会，
    因此打一条警告并支持 PROMPT_CACHE_SALT 手动换键。
    """
    import analyzer.prompts as _prompts

    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    parts = [getattr(_prompts, name) for name in _PRIVATE_PROMPT_NAMES]
    # 影响模型输入的模块级常量也要进指纹：getsource 只覆盖函数体，函数引用的
    # MAX_DIALOG_CHARS / 时间标记阈值 等常量改了源码也不变——只哈希函数会让
    # "调小了对话预算"或"改了时间标记口径"之后继续命中旧缓存（输入其实变了）。
    from analyzer import vision

    parts.append(
        "consts:%s"
        % repr(
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
                # 视觉参数同样改变输入（图片摘要是 prompt 的一部分）
                vision.VISION_SYSTEM,
                vision.VISION_DETAIL,
                vision.VISION_MAX_PER_MONTH,
                vision.VISION_MIN_SIDE,
            )
        )
    )
    fmt_funcs = (_build_dialog, _message_line, _fit_lines, _conversation_stats, _short_time)
    try:
        parts += [_hashed_source(f) if normalize else inspect.getsource(f) for f in fmt_funcs]
    except (OSError, TypeError):
        parts += [f"<source-unavailable:{f.__name__}>" for f in fmt_funcs]
        logger.warning(
            "无法读取格式化函数源码（编译/打包环境），指纹降级为函数名级——"
            "改动对话格式不会自动失效旧缓存；改过格式后请设 PROMPT_CACHE_SALT"
            " 为任意新值手动换键"
        )
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


PROMPT_FINGERPRINT = _prompt_fingerprint()
#: 旧公式（按 getsource 原文哈希）的取值。只用于"读旧缓存并迁移"：
#: 用 AST 归一之后，所有既有用户的缓存都是以这个值命名的，直接换键等于让他们重新付费。
#:
#: **这里必须写死成字面量，不可以再改成 _prompt_fingerprint(normalize=False)。**
#: 旧公式的哈希对象是**当前源码原文**，所以只要动过任何一个进指纹的函数
#: （哪怕只是重排一行、或有意改一次口径），现算值就跟着漂——而它是
#: "读取 AST 归一之前写下的缓存并免费迁移"的唯一凭据。漂掉的后果不是测试变红那么简单：
#: 那批老用户的旧缓存永远读不到，本该免费的迁移退化成一次真实重付费，
#: 而且症状与"我们改了什么"毫无关联，排查时会完全想不到这条路径断了。
#: 历史值本身就是**事实**，事实不该由活的源码算出来。它由
#: tests/test_group_foundation.py 的 PINNED_LEGACY_PRIVATE_FINGERPRINT 钉住，
#: 并由 tests/test_privacy_guards.py::TestFingerprintMigration 端到端验证仍然读得通。
PROMPT_FINGERPRINT_LEGACY = "26bf952fe772"


def fingerprint_for_dimension(dim: str) -> str:
    """该维度该用哪个提示词指纹：群聊维度走群聊指纹，recap 走 recap 指纹，其余（私聊/未知）走私聊指纹。

    维度级缓存的文件名里嵌着这个值，所以群聊提示词一改，群聊维度的旧缓存自动失效，
    而私聊维度的文件名**一个字都不变**（用户不会因为新增群聊功能而重新付费）。
    recap 独立成族的理由同 group_client：它有自己的 system prompt 与摘要构建逻辑，
    既不能被私聊指纹的变动无谓刷掉，也不能因为不在 `_PRIVATE_PROMPT_NAMES` 里而
    改了自身提示词却不失效（那会让旧复盘永远命中）。
    """
    from analyzer import group_client  # 延迟导入：group_client 在模块级 import 本模块

    if dim in group_client.GROUP_DIMENSIONS:
        return group_client.GROUP_PROMPT_FINGERPRINT
    from analyzer import recap_client

    if dim == recap_client.RECAP_DIMENSION:
        return recap_client.RECAP_PROMPT_FINGERPRINT
    return PROMPT_FINGERPRINT


#: 私聊族的历史代链（最新一代在前）。设计见 legacy_fingerprints_for_dimension：
#: 每次有意换代，把"当时的当前值"压进这里，读侧就会挨代找到旧缓存并搬到新键。
#:
#: f4bd6aa06d52 —— 中位数口径换代时的值。_conversation_stats 的 median_gap 从
#: 就地 `s[n//2]`（上中位）改为复用 local_stats._median（教科书定义，偶数取中间两数
#: 平均）。这句改动真的改变喂给模型的那行"回复间隔中位数约 N 秒"，所以必须换键；
#: 换键让既有私聊用户的五维缓存不再命中当前键，**默认就是全员重付费**（已确认接受）。
#: 把它留在链里，是为了让"重付费一次"不会变成"每次换代都重付一次"：
#: 下一代键的用户仍能沿链搬回这一代已付费的结果。
PRIVATE_FINGERPRINT_GENERATIONS = ("f4bd6aa06d52",)


def _legacy_chain_for_fingerprint_value(fp: str) -> tuple:
    """某个族指纹**值**对应的历史代链（认不出该值 = 这族没有历史包袱，返回空链）。"""
    if fp == PROMPT_FINGERPRINT:
        chain = (*PRIVATE_FINGERPRINT_GENERATIONS, PROMPT_FINGERPRINT_LEGACY)
        # 顺序：先按"上一代的当前键"找（AST 归一时代写的），再按更早的原文公式找；
        # 与当前键相同的值剔掉，否则会白读一次自己
        return tuple(dict.fromkeys(x for x in chain if x != fp))
    from analyzer import group_client, recap_client

    if fp == group_client.GROUP_PROMPT_FINGERPRINT:
        return (
            (group_client.GROUP_PROMPT_FINGERPRINT_LEGACY,)
            if group_client.GROUP_PROMPT_FINGERPRINT_LEGACY != fp
            else ()
        )
    if fp == recap_client.RECAP_PROMPT_FINGERPRINT:
        return (
            (recap_client.RECAP_PROMPT_FINGERPRINT_LEGACY,)
            if recap_client.RECAP_PROMPT_FINGERPRINT_LEGACY != fp
            else ()
        )
    return ()


def legacy_fingerprints_for_dimension(dim: str) -> tuple:
    """该维度"历史代"指纹值的链（读旧缓存时的尝试顺序，最新一代在前）。

    此前每族只认**一代**旧键（AST 归一之前的原文公式），意味着第三次指纹换代时，
    第二代键写下的缓存再也读不到——用户为同一段对话再付一次钱，而这次改动与内容
    无关。泛化成链：以后每次换代，把"当时的当前值"压进对应族的链头，读侧逻辑一字不改。
    现在每族仍只有既有的一代，行为与升级前逐字节一致（纯机制扩展，零现值变化）。
    """
    return _legacy_chain_for_fingerprint_value(fingerprint_for_dimension(dim))


def legacy_fingerprint_for_dimension(dim: str) -> str:
    """该维度"旧公式"的指纹：只用来读 AST 归一之前写下的缓存（读到即迁移）。

    保留单值入口（`_cache_path(legacy=True)` 与既有用例依赖它）；链式读取请用
    legacy_fingerprints_for_dimension。链空时回当前指纹（等价"没有历史包袱"）。
    """
    chain = legacy_fingerprints_for_dimension(dim)
    return chain[0] if chain else fingerprint_for_dimension(dim)


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
    max_attempts = max(retry, TPM_MAX_ATTEMPTS - 1) + 1

    while True:
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
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            # 思考模式下思维链也占 max_tokens：大月份偶发被吃满。
            # 结果被截断=该月白跑，所以先降级为"关思考"重试一次——宁可精度略降，
            # 也不让这个月从结果里消失（真实数据踩过：最大月份整月丢失）。
            if think:
                logger.warning(
                    "输出被 max_tokens=%s 截断，改用非思考模式重试一次（保住这个月的结果）", max_tokens
                )
                think = False
                continue
            logger.error("模型输出被 max_tokens=%s 截断，放弃本次结果（不重试）", max_tokens)
            return None
        try:
            # strict=False：容忍字符串里未转义的控制字符。模型偶尔把多行文本写成**裸换行**
            # （锐评、群动态里常见），严格模式会直接判非法 JSON，从而丢掉整月/整位成员的结果。
            # 真实数据实测：三次失败里有两次属于这种"内容合法、转义偷懒"，放宽解析即可救回；
            # 它只影响解析容忍度，不会把合法 JSON 解析错。
            return json.loads(choice.message.content, strict=False)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("模型返回非法 JSON（不重试）: %s", e)
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
    choice = resp.choices[0]
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
    """并发逐月调用 API，返回 {period: result}。

    单月失败仅记录日志并跳过，不中断整体分析。
    on_progress(done, total) 每完成一个月回调一次；
    should_cancel() 返回 True 时不再启动新任务并尽快返回已完成部分。

    提交策略是"有界窗口"（最多 CONCURRENCY 个月在跑）：早先一次性 submit
    全部月份时，线程池队列里的月份已经排队，用户点取消也拦不住，剩余月份
    照常调用计费——与"可随时取消"的承诺相反。

    fingerprint：月份缓存的指纹，默认私聊指纹（既有行为、键值不变）。
    群聊维度传 group_prompt_fingerprint()，两类月份的缓存互不干扰。
    """
    results: dict[str, Any] = {}
    total = len(months)
    done = 0
    fatal: dict[str, str] = {}  # 配额耗尽等致命错误：中止剩余月份
    failed_periods: set[str] = set()  # 普通请求失败或模型输出无效的月份
    used_keys: set[str] = set()  # 本维度命中的月份缓存键，收尾时一次性写 manifest

    def _cancel_requested() -> bool:
        # 用户点了取消，或进程正在关闭（Ctrl+C）：都不该再往外发新请求
        return shutdown_requested() or bool(should_cancel and should_cancel())

    def _keys_for(prompt: str) -> list:
        """该月 prompt 对应的缓存键：[当前键, 各历史代键…]（尝试顺序即链序）。

        历史代是为了读取"AST 归一之前"（以及未来任何一次换代之前）写下的月份缓存。
        不这么做的话，指纹公式一改，所有既有用户的月份缓存全部不再命中——他们会为
        **同一段对话**重新付一次钱，而这次改动本身与提示词、与对话内容都无关。
        链按**指纹值**映射（调用方传进来的就是这个族的值），换代时只需把上一代
        当前值压进对应族的链头，这里不再逐族写 if。
        """
        current_fp = fingerprint or PROMPT_FINGERPRINT
        keys = [_month_key(system_prompt, prompt, fingerprint)]
        for legacy_fp in _legacy_chain_for_fingerprint_value(current_fp):
            if legacy_fp != current_fp:
                keys.append(_month_key(system_prompt, prompt, legacy_fp))
        return keys

    def _work(period: str, msgs: list) -> tuple[str, Optional[dict]]:
        # 已被排入线程池但尚未开始执行时取消：直接跳过，不产生 API 调用
        if _cancel_requested():
            return period, None
        try:
            prompt = make_prompt(period, msgs)
            if not prompt.strip():
                return period, None
            keys = _keys_for(prompt)
            key = keys[0]
            # 思考模式必须参与口径核对：思维链开与关产出的结果差别很大，让一次
            # "关了思考"的分析吃到"开着思考"算出来的月份，用户看到的就是配置静默失效
            # （维度缓存那侧早就用 `_think` 后缀把两者分开了，月份缓存这一侧一直漏着）。
            # 取值与 _call_api 实际用的那一次完全一致（它按 dim=tag 决定发不发 thinking）。
            want_thinking = thinking_enabled(tag)
            result = _read_month_cache(key, expect_thinking=want_thinking, chat_hash=chat_hash)
            if result is None:
                # 必须**挨代**试，不能只看 keys[1]：链里现在可能有两代以上
                # （私聊在"原文公式"之外又多了一代 AST 归一期的键，见
                # PRIVATE_FINGERPRINT_GENERATIONS）。写死下标的版本只会试到链头那一代，
                # 于是"更早一代"写下的月份缓存永远读不到——用户为同一段对话再付一次钱，
                # 而这正是 legacy_fingerprints_for_dimension 当初泛化成链要防的事，
                # 只是读侧漏跟着改。顺序即链序：最近的换代在前。
                for legacy_key in keys[1:]:
                    result = _read_month_cache(legacy_key, expect_thinking=want_thinking, chat_hash=chat_hash)
                    if result is None:
                        continue
                    # 旧键里的结果照常可用：改名到当前键，并按**当前键**记账（下面 used_keys），
                    # 否则新文件会成为"无引用"，宽限期后被孤儿回收删掉。
                    if migrate_month_cache(legacy_key, key):
                        logger.info("%s 命中旧指纹的月份缓存，已迁移到当前键", period)
                    else:
                        logger.info("%s 命中旧指纹的月份缓存", period)
                    break
            if result is not None:
                logger.info("%s 命中月份缓存，跳过 API 调用", period)
            else:
                result = _call_api(system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag)
                if not result:
                    failed_periods.add(period)
                # 清理守卫：用户在这几分钟里换了文件，这一轮就不该把含聊天原句引用
                # 的结果写回盘上（否则级联清理报称"已删除"的数据会原地复活）。
                # 已经付费拿到的结果本身照常返回给这一轮，只是不缓存。
                if result and not purge_marks.is_marked(chat_hash):
                    _write_month_cache(key, result, thinking=want_thinking)
            if result:
                result["period"] = period
                result["month"] = period
                used_keys.add(key)
                return period, result
        except QuotaExhaustedError as e:
            fatal.setdefault("error", str(e))
            logger.error("%s 月 AI 分析中止（配额耗尽）", period)
        except Exception as e:
            failed_periods.add(period)
            logger.error("%s 月 AI 分析失败: %s", period, e)
        return period, None

    pending = list(months.items())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures: dict[concurrent.futures.Future, str] = {}
            while pending or futures:
                # 只补足到并发上限：取消后窗口内的任务跑完即止，剩余月份不再启动
                while pending and len(futures) < CONCURRENCY and not fatal and not _cancel_requested():
                    period, msgs = pending.pop(0)
                    futures[pool.submit(_work, period, msgs)] = period
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
                            # 进度回调只管界面显示，出错不该中断分析；留一条 debug 便于排查"进度不动"
                            logger.debug("进度回调异常（忽略）: %s", e)
                if fatal:
                    # 配额耗尽：取消排队中的月份，尽快收尾
                    for f in futures:
                        f.cancel()
                    break
    finally:
        # manifest 一次写入：不管正常结束、取消还是配额中止，已经用到的月份都要记账
        # （否则这些月份文件会变成"无引用"，宽限期后被孤儿回收删掉，增量分析白跑）
        _record_month_usage(chat_hash, used_keys)

    if fatal:
        # 配额中止一律上报，哪怕已经拿到几个月的结果。
        # 原先"有一个月成功就当成正常结果返回"的做法，会让调用方把**缺了后面所有月**的
        # 残缺结果写进维度缓存并置 done：用户之后每次点这个维度都直接命中这份残缺缓存，
        # 缺掉的那几个月永远不会再补——界面显示"分析完成"，数据却少了一半，正是最难发现
        # 的那类错。这里抛出去，jobs 侧会记为失败、不写维度缓存；而已成功的那几个月
        # **已经落在月份缓存里**，充值或调高上限后重跑只为缺的月份付费。
        # 与 _stop_requested 对"取消/关闭"立的规矩同一性质（见 webapp/jobs.py 那段注释）。
        raise QuotaExhaustedError(fatal["error"])

    if failed_periods and not _cancel_requested():
        # 成功月份已独立写入内容缓存。不要把缺月的汇总结果交给 jobs.py 写成完整维度缓存，
        # 否则之后的普通请求会永久命中这份残缺汇总，错过月份也不会再被补算。
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
    dialog = _build_dialog(
        msgs, chat.self_uid, chat.self_name, chat.other_name, chat_hash=chat_hash, vision_label=f"{period} 月"
    )
    if not dialog.strip():
        return ""
    return f"以下是 {period} 月的对话数据：\n\n{dialog}"


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
) -> Optional[dict]:
    """单人的习惯/锐评分析：先过滤再取样本，失败仅记日志。

    stratified=False（习惯）：取最近 sample_size 条 —— 语言习惯看当下。
    stratified=True（锐评）：按时间均匀抽样覆盖整个时段 —— 否则 growth_observation
    要求的"这段时间的变化"根本不在样本里，模型只能编或写数据不足。
    """
    try:
        valid_all = [m for m in msgs if _has_content(m) and is_statistical(m)]
        if stratified and len(valid_all) > sample_size:
            # 向上取整步长：保证抽样后条数 <= sample_size 且覆盖整个时间轴
            stride = (len(valid_all) + sample_size - 1) // sample_size
            sample = valid_all[::stride]
            span_note = "，按时间均匀抽样覆盖整个时段"
        else:
            sample = valid_all[-sample_size:]
            span_note = ""
        valid = sample
        if not valid:
            return None
        lines = [_message_line(m, display_name) for m in valid]
        original_n = len(lines)
        lines = _fit_lines(lines, MAX_DIALOG_CHARS)
        head = f"统计：{display_name} 共发言 {len(valid_all)} 条（样本 {len(valid)} 条{span_note}，图片 "
        # 已知口径分歧（故意保留，与 analyzer.dialog / group_client._member_facts 同批）：
        # 这里按"含图消息条数"，仪表盘按张。本函数虽不在指纹集合里，但它生成的文本进
        # 月份缓存的 user_content 哈希——单改这里会让私聊五个维度内部又分两种口径，
        # 而三处一起改要等指纹换代的多代迁移链。统一方案见
        # docs/reviews/2026-09-26-round6-precommit-review.md P1-3。
        head += f"{sum(1 for m in valid if m.has_image)} 张）"
        if len(lines) < original_n:
            head += f"，因篇幅限制展示其中 {len(lines)} 条"
        dialog = f"{head}。\n\n" + "\n".join(lines)
        # 这一方的图片摘要（同一批图在所有维度间复用，只花一次视觉调用）
        digest = _vision_digest(valid_all, chat_hash, f"{display_name} 的发言中")
        if digest:
            dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
        if not dialog.strip():
            return None
        user_content = prompt_template.format(display_name=display_name, dialog=dialog)
        # 双方分析各自独立计费。按人物内容缓存成功的一方，额度或临时错误后重试时
        # 只补失败的一方；命名空间与逐月缓存分开，提示词指纹变动仍会自动换键。
        cache_prompt = f"{system_prompt}\n[private-person:{tag}:{display_name}]"
        cache_key = _month_key(cache_prompt, user_content, fingerprint_for_dimension(tag))
        want_thinking = thinking_enabled(tag)
        result = _read_month_cache(cache_key, expect_thinking=want_thinking, chat_hash=chat_hash)
        if result is not None:
            logger.info("%s 命中个人分析缓存，跳过 API 调用", mask_name(display_name))
        else:
            result = _call_api(
                system_prompt,
                user_content,
                max_tokens=max_tokens,
                tag=tag,
                dim=tag,
            )
            if result and not purge_marks.is_marked(chat_hash):
                _write_month_cache(cache_key, result, thinking=want_thinking)
        if result and chat_hash:
            _record_month_usage(chat_hash, {cache_key})
        if result:
            result["name"] = display_name
            result["total_messages"] = len(valid_all)
            return result
    except QuotaExhaustedError:
        raise  # 配额耗尽需中止整个维度，不能被当作单人失败吞掉
    except Exception as e:
        logger.error("%s 的 AI 分析失败: %s", mask_name(display_name), scrub_secrets(e))
    return None


def analyze_habits(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """分析双方的语言习惯"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
    incomplete = False
    template = "分析以下 {display_name} 的发言，总结其说话风格：\n\n{dialog}"
    total, done = 2, 0
    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        if should_cancel and should_cancel():
            break
        display_name = chat.self_name if person_key == "self" else chat.other_name
        try:
            result = _analyze_person(
                SYSTEM_PROMPT_HABITS,
                500,
                msgs,
                display_name,
                template,
                max_tokens=MAX_TOKENS_BY_DIM["habits"],
                tag="habits",
                chat_hash=chat_hash,
            )
        except QuotaExhaustedError:
            raise
        if result:
            results[person_key] = result
        elif any(_has_content(m) and is_statistical(m) for m in msgs):
            incomplete = True
        done += 1
        if on_progress:
            on_progress(done, total)
    if incomplete and results and not (should_cancel and should_cancel()):
        raise AnalysisIncompleteError("habits 分析未能取得双方的完整结果；已完成的一方可从缓存复用")
    return results


def analyze_profile(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """AI 人物锐评 — 分析双方的性格画像"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
    incomplete = False
    template = "以下是 {display_name} 在私聊中的发言记录，请对其进行深度性格分析：\n\n{dialog}"
    total, done = 2, 0
    for person_key, msgs in [("self", self_msgs), ("other", other_msgs)]:
        if should_cancel and should_cancel():
            break
        display_name = chat.self_name if person_key == "self" else chat.other_name
        try:
            result = _analyze_person(
                SYSTEM_PROMPT_PROFILE,
                800,
                msgs,
                display_name,
                template,
                max_tokens=MAX_TOKENS_BY_DIM["profile"],
                tag="profile",
                stratified=True,
                chat_hash=chat_hash,
            )
        except QuotaExhaustedError:
            raise
        if result:
            results[person_key] = result
        elif any(_has_content(m) and is_statistical(m) for m in msgs):
            incomplete = True
        done += 1
        if on_progress:
            on_progress(done, total)
    if incomplete and results and not (should_cancel and should_cancel()):
        raise AnalysisIncompleteError("profile 分析未能取得双方的完整结果；已完成的一方可从缓存复用")
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
