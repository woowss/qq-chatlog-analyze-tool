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
"""DeepSeek API 调用封装"""

import concurrent.futures
import hashlib
import inspect
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from functools import partial
from typing import Any, Callable, Iterable, Optional

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
from parser.qq_parser import CST, MEDIA_KINDS, ChatData, is_statistical, split_by_month
from analyzer.logger import get_logger, mask_name
from analyzer.shutdown import shutdown_requested
from analyzer.usage import record_call
from analyzer.local_stats import SESSION_GAP_MS, is_session_start
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
    SYSTEM_PROMPT_PROFILE,
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


# 单月对话文本上限（字符数）。**准确性优先**：默认 60 万字符（约 25-30 万 tokens），
# 足以装下绝大多数月份的全部消息（实测：最长那个月会逼近这个上限），
# 因此正常情况下不会触发抽样；只有极端月份（几十万条）才会等间隔抽样并注明。
# 想省钱可在 .env 里调小 LLM_MAX_DIALOG_CHARS。
MAX_DIALOG_CHARS = int(env_number("LLM_MAX_DIALOG_CHARS", 600_000, 1000, 2_000_000))

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


def begin_run() -> None:
    """开始一次分析运行：调用计数清零。

    "一次运行" = 单个维度的分析，或"一键全量"的整个循环（后者只清零一次，
    所以上限覆盖全部维度）。生产路径由 webapp.jobs 的两个入口调用。

    计数是**进程级**的而不是线程局部的：月份调用发生在并发线程池里，线程局部计数
    跨不过去。代价是同一进程内两个并发分析共享这一个上限——单机工具里这种并发很
    少见，换来的是"无论怎么触发，总量都不会悄悄翻倍"。
    """
    global _run_calls, _run_active
    with _run_calls_lock:
        _run_calls = 0
        _run_active = True


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


def _fit_lines(lines: list[str], max_chars: int) -> list[str]:
    """把消息行压到 max_chars 以内：先等间隔抽样，再按需从尾部截断。

    等间隔抽样能尽量保留整月的对话分布，而不是只留最新的消息。
    """
    if not lines:
        return []
    total = sum(len(line) + 1 for line in lines)
    if total <= max_chars:
        return lines

    # 1) 等间隔抽样
    step = max(1, (total + max_chars - 1) // max_chars)
    sampled = lines[::step]
    if len(sampled) < 3 and len(lines) > 3:
        sampled = lines[-30:]
    total = sum(len(line) + 1 for line in sampled)
    if total <= max_chars:
        return sampled

    # 2) 仍超限（如存在单条超长消息）：从尾部逐条截断
    kept: list[str] = []
    used = 0
    for line in reversed(sampled):
        if used + len(line) + 1 > max_chars:
            if not kept and line:
                kept.append(line[:max_chars])
            break
        used += len(line) + 1
        kept.append(line)
    return list(reversed(kept))


def _has_content(m) -> bool:
    """消息是否有可喂给模型的内容（正文或图片/表情/文件/转发/回复等信号）"""
    return (
        bool(m.text)
        or m.has_image
        or m.is_reply
        or bool(m.media_kind)
        or bool(m.face_names)
        or bool(m.face_ids)
    )


def _short_time(time_str: str) -> str:
    """把 '2024-01-01 08:00:00' 压缩为 '01-01 08:00'，省 token 且同月内信息无损失"""
    return time_str[5:16] if len(time_str) >= 16 else time_str


def _conversation_stats(messages: list, self_uid: str = "") -> dict:
    """喂给模型作参考的本地事实：最活跃小时、回复间隔中位数、谁更常开启话题。

    这些都能在本地精确算出，直接写进统计头，模型就不必"猜"（也减少幻觉）。
    """
    hours: Counter = Counter()
    gaps: list[float] = []
    last = None
    sessions = 0
    self_opened = 0
    for m in messages:
        hours[datetime.fromtimestamp(m.timestamp / 1000, tz=CST).hour] += 1
        if last is None or is_session_start(last.timestamp, m.timestamp):
            sessions += 1
            if self_uid and m.sender_uid == self_uid:
                self_opened += 1
        elif last.sender_uid != m.sender_uid:
            gap = (m.timestamp - last.timestamp) / 1000
            if 0 < gap <= 3600 * 6:
                gaps.append(gap)
        last = m
    return {
        "peak_hour": hours.most_common(1)[0][0] if hours else None,
        "median_gap": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "sessions": sessions,
        "self_opened": self_opened,
    }


# 时间戳只在"新的一段"（间隔超过该分钟数）或换人时打印；段内小间隔用 (+3m) 紧凑标注。
# 原格式每行固定 18 字符（时间 + 昵称），短句为主的聊天里能占到 60–73% 的字符预算。
TIME_MARK_MINUTES = 30
RELATIVE_MARK_MINUTES = 2
TIME_MARK_MS = TIME_MARK_MINUTES * 60 * 1000


def _gap_mark(gap_ms: int) -> str:
    """把间隔压成 (+3m)/(+2h)/(+1d) 这类紧凑标记"""
    minutes = gap_ms // 60000
    if minutes < 60:
        return f"(+{minutes}m)"
    if minutes < 60 * 24:
        return f"(+{minutes // 60}h)"
    return f"(+{minutes // (60 * 24)}d)"


def _message_line(m, name: str, prev_uid: Optional[str] = None, prev_ts: Optional[int] = None) -> str:
    """单条消息 → 对话行。

    打印规则（相同信息量、更省字符）：
    - 首条 / 与上一条间隔 ≥ TIME_MARK_MINUTES / 换人 → 打印 `[01-01 08:00] 昵称:`
    - 段内同人连发 → 只打印正文；段内换人 → 只打印 `昵称(+间隔):`
    """
    body = m.text or ""
    marks = []
    if m.has_image:
        marks.append("图片")
    if m.is_reply:
        marks.append("回复")
    if m.media_kind:
        # 文件/视频/转发/红包/表情气泡/Markdown：带短标签，让模型知道这里发生过什么
        kind = MEDIA_KINDS.get(m.media_kind, m.media_kind)
        marks.append(f"{kind}:{m.media_label}" if m.media_label else kind)
    if m.face_names:
        marks.append("表情:" + "、".join(m.face_names[:4]))

    first = prev_uid is None or prev_ts is None
    gap_ms = 0 if first else max(0, m.timestamp - prev_ts)
    changed = first or m.sender_uid != prev_uid
    # 段内小间隔不值得标注（阈值以下留空）
    mark = _gap_mark(gap_ms) if gap_ms >= RELATIVE_MARK_MINUTES * 60000 else ""

    # 三种打印形态（信息量相同，但字符数递减）：
    #   跨段/间隔够大 → `[01-01 08:00] 昵称:`   换人时靠时间戳认出"这是新的一段"
    #   段内换人      → `昵称(+3m):`            省掉时间，只留"谁说的、隔了多久"
    #   段内同人连发  → `(+3m):`                连昵称都省掉，间隔太小则什么都不印
    if first or gap_ms >= TIME_MARK_MS:
        prefix = f"[{_short_time(m.time_str)}] {name}:"
    elif changed:
        prefix = f"{name}{mark}:"
    else:
        prefix = f"{mark}:" if mark else ""

    if marks:
        mark = "[" + ", ".join(marks) + "]"
        text = f"{body} {mark}".strip() if body else mark
    else:
        text = body
    return f"{prefix} {text}".strip() if prefix and text else (prefix or text)


def _build_dialog(
    messages: list,
    self_uid: str,
    self_name: str,
    other_name: str,
    max_chars: Optional[int] = MAX_DIALOG_CHARS,
    chat_hash: str = "",
    vision_label: str = "",
) -> str:
    """构建喂给模型的对话内容：统计头（含本地事实）+ 压缩后的对话行 + 图片摘要。

    统计头给模型全貌（即使抽样截断也能知道真实消息量），并附上本地精确算出的
    事实（条数、图片数、最活跃时段、回复中位数、谁更常开启话题、对话段数），
    避免模型凭样本"数数"。

    vision_label 非空时会尝试附上该批消息的图片摘要（见 analyzer/vision.py）：
    摘要按图片指纹缓存，同一批图只花一次视觉调用，5 个维度共用。
    """
    valid = [m for m in messages if _has_content(m) and is_statistical(m)]
    if not valid:
        return ""
    total = len(valid)
    self_n = sum(1 for m in valid if m.sender_uid == self_uid)
    other_n = total - self_n
    images = sum(1 for m in valid if m.has_image)

    lines: list[str] = []
    prev_uid: Optional[str] = None
    prev_ts: Optional[int] = None
    for m in valid:
        lines.append(
            _message_line(m, self_name if m.sender_uid == self_uid else other_name, prev_uid, prev_ts)
        )
        prev_uid, prev_ts = m.sender_uid, m.timestamp
    original_n = len(lines)
    if max_chars:
        lines = _fit_lines(lines, max_chars)

    parts = [f"共 {total} 条消息（我方 {self_n} 条 / 对方 {other_n} 条，图片 {images} 张）"]
    stats = _conversation_stats(valid, self_uid)
    if stats["peak_hour"] is not None:
        parts.append(f"最活跃时段约 {stats['peak_hour']} 时")
    if stats["median_gap"] is not None:
        parts.append(f"回复间隔中位数约 {int(stats['median_gap'])} 秒")
    if stats["sessions"]:
        parts.append(f"共 {stats['sessions']} 段对话（间隔超 {TIME_MARK_MINUTES} 分钟算新的一段）")
        if self_uid:
            ratio = round(stats["self_opened"] / stats["sessions"] * 100)
            parts.append(f"其中我方先开口 {ratio}%、对方 {100 - ratio}%")
    head = "统计：" + "，".join(parts)
    if len(lines) < original_n:
        head += f"，因篇幅限制展示其中 {len(lines)} 条（等间隔抽样，覆盖整月分布）"
    dialog = f"{head}。\n\n" + "\n".join(lines)

    digest = _vision_digest(valid, chat_hash, vision_label)
    if digest:
        dialog += f"\n\n图片内容摘要（由视觉模型识别，供参考）：\n{digest}"
    return dialog


def _vision_digest(messages: list, chat_hash: str, label: str) -> str:
    """图片摘要：未开启/无图/失败都返回空串，绝不影响文本分析主流程"""
    if not label:
        return ""
    try:
        from analyzer import vision

        return vision.digest(messages, chat_hash=chat_hash, label=label)
    except QuotaExhaustedError:
        raise  # 额度耗尽要中止整体任务，不能悄悄吞掉
    except Exception as e:
        logger.warning("图片摘要失败（继续纯文本分析）: %s", e)
        return ""


def _prompt_fingerprint(salt: "str | None" = None) -> str:
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
        parts += [inspect.getsource(f) for f in fmt_funcs]
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


def fingerprint_for_dimension(dim: str) -> str:
    """该维度该用哪个提示词指纹：群聊维度走群聊指纹，其余（私聊/未知）走私聊指纹。

    维度级缓存的文件名里嵌着这个值，所以群聊提示词一改，群聊维度的旧缓存自动失效，
    而私聊维度的文件名**一个字都不变**（用户不会因为新增群聊功能而重新付费）。
    """
    from analyzer import group_client  # 延迟导入：group_client 在模块级 import 本模块

    if dim in group_client.GROUP_DIMENSIONS:
        return group_client.GROUP_PROMPT_FINGERPRINT
    return PROMPT_FINGERPRINT


# 思考模式的最低输出预算：思维链 token 也计入 max_tokens，低于这个值必然截断
THINKING_MIN_TOKENS = 4096


# ---------------------------------------------------------------------------
# 月份级缓存（增量分析）
# ---------------------------------------------------------------------------
# 维度级缓存以"整份文件哈希"为键：导出文件只要多一个月，历史月份会全部重跑。
# 月份级缓存改用内容寻址键（模型 + 提示词指纹 + 系统提示词 + 该月对话文本），
# 于是重新导出同一段对话时历史月份直接命中，只为新增月份付费。
#
# 清理：每个聊天文件对应 manifest_{chat_hash}.json，记录它用过哪些月份文件；
# 该聊天被替换/删除时删掉 manifest，并只回收"没有其他 manifest 引用"的月份文件，
# 从而保留"不留孤儿敏感数据"的隐私属性。
_MONTH_CACHE_DIR = ""
_MONTH_CACHE_LOCK = threading.Lock()
# manifest 已引用月份的进程级缓存：{manifest 文件名: (mtime, keys)}。
# _referenced_keys_locked 会被 purge（每次上传）与 sweep（定期清理）调用，原实现
# 每次都要把目录下所有 manifest 完整读一遍再做 json.loads——分析过的聊天越多越慢。
# 这里缓存结果并用 mtime 校验：文件没被改过（stat 比"读文件 + 解析"便宜一个量级）
# 就直接复用。写 manifest 的那一处会同步更新缓存，不依赖 mtime 精度。
# 受 _MONTH_CACHE_LOCK 保护。
_MANIFEST_KEYS: dict[str, tuple[float, set]] = {}
# 缓存写失败的告警去抖（磁盘满时每次调用都会失败，不能每次刷一行）
_WRITE_WARN_INTERVAL = 300.0
_last_write_warning = [0.0]
# 无引用的月份缓存先留一段宽限期：上传新文件时的级联清理不能顺手删掉
# "同一段对话的历史月份"，否则增量分析就失去意义。孤儿文件由定期清理回收。
MONTH_CACHE_GRACE_SECONDS = env_number("LLM_MONTH_CACHE_GRACE_HOURS", 24, 0, 24 * 30) * 3600


def configure_month_cache(directory: str) -> None:
    """由应用层注入缓存目录；传空字符串即关闭月份级缓存"""
    global _MONTH_CACHE_DIR
    _MONTH_CACHE_DIR = directory or ""
    # 目录换了，上一个目录的 manifest 缓存必须丢弃（键只是文件名，会张冠李戴）
    with _MONTH_CACHE_LOCK:
        _MANIFEST_KEYS.clear()


def _month_key(system_prompt: str, user_content: str, fingerprint: "str | None" = None) -> str:
    """月份缓存的键：任何影响该月输出的因素（模型/提示词/格式/对话文本）都进哈希。

    fingerprint 默认取私聊指纹（既有行为，键值与升级前完全一致）；群聊维度传
    group_prompt_fingerprint()，两类月份的缓存互不干扰——私聊月份也不会因为
    新增群聊提示词而变成"无引用"被回收。
    """
    digest = hashlib.sha256()
    for part in (DEEPSEEK_MODEL, fingerprint or PROMPT_FINGERPRINT, system_prompt, user_content):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:20]


def month_cache_path(key: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"month_{key}.json")


def _manifest_path(chat_hash: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"manifest_{chat_hash}.json")


def _read_month_cache(key: str) -> Optional[dict]:
    if not _MONTH_CACHE_DIR:
        return None
    path = month_cache_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data.pop("_created", None)  # 元数据不进调用方拿到的结果
    try:
        os.utime(path, None)  # 命中即续期，避免常用缓存被 30 天 TTL 回收
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _warn_write_failure(what: str, path: str, err: OSError) -> None:
    """缓存落盘失败必须出声。

    静默吞掉 OSError 的后果不是"少一个文件"，而是月份缓存与 manifest 从此写不进去：
    用户以为命中了缓存，实际上每个月都在重复付费，且界面上完全看不出来。
    磁盘满时会高频失败，所以按 5 分钟去抖，避免刷爆日志。
    """
    now = time.monotonic()
    if now - _last_write_warning[0] < _WRITE_WARN_INTERVAL:
        return
    _last_write_warning[0] = now
    logger.warning("%s写入失败（缓存不生效，可能重复调用 API）: %s (%s)", what, path, err)


def _write_month_cache(key: str, result: dict) -> None:
    if not _MONTH_CACHE_DIR:
        return
    path = month_cache_path(key)
    tmp = f"{path}.tmp"
    try:
        # _created 是"绝对 90 天"硬上限的依据（cleanup 读它）。缺了它就只能按 mtime 判，
        # 而 mtime 在每次命中时被续期（见 _read_month_cache）——含聊天原句引用的这族
        # 缓存会因此无限期留存。读侧会把它 pop 掉，调用方拿到的结果不变。
        # 放在**最前面**写：清理任务只扫文件头就能取到，不必整份解析这些最敏感的月份文件
        # （见 webapp.store.read_created_at）。
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        payload.pop("_created", None)
        # 目录可能被用户按 README 的指引删掉来"彻底清除数据"，而服务还开着：
        # 这里不补目录，月份缓存从此再也写不进去，增量分析静默失效（每月重复付费）。
        os.makedirs(_MONTH_CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_created": time.time(), **payload}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        _warn_write_failure("月份缓存", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _record_month_usage(chat_hash: str, keys: "Iterable[str]") -> None:
    """把这一批用到的月份缓存记进该聊天的 manifest，供级联清理做引用计数。

    调用方按"一次分析"批量传入（见 _analyze_periods）：原先每完成一个月就
    「读 manifest → 改 → 写回」，24 个月就是 48 次文件 I/O，而写进去的内容
    只是同一个集合在变大。现在整个维度只读一次、写一次。
    """
    if not _MONTH_CACHE_DIR or not chat_hash:
        return
    new_keys = set(keys)
    if not new_keys:
        return
    path = _manifest_path(chat_hash)
    name = os.path.basename(path)
    with _MONTH_CACHE_LOCK:
        data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
        merged = set(data.get("months") or [])
        merged |= new_keys
        data["months"] = sorted(merged)
        data["updated"] = time.time()
        # setdefault 语义：绝对上限看的是"首次创建"，重写 manifest 不该把它续期。
        # 重排到最前面写，让清理任务只扫文件头就能取到（见 webapp.store.read_created_at）。
        data["_created"] = data.get("_created") or time.time()
        payload = {"_created": data.pop("_created"), **data}
        tmp = f"{path}.tmp"
        try:
            # 同 _write_month_cache：manifest 写不进去 = 这些月份文件会变成"无引用"，
            # 宽限期后被孤儿回收删掉，增量分析白跑。
            os.makedirs(_MONTH_CACHE_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            # manifest 写不进去同样只影响"重新导出时能否复用历史月份"，
            # 但会让增量分析静默失效（每次都全量付费），所以也要出声
            _warn_write_failure("月份缓存 manifest", path, e)
            _MANIFEST_KEYS.pop(name, None)
        else:
            _MANIFEST_KEYS[name] = (os.path.getmtime(path), set(data["months"]))


def purge_month_cache(chat_hash: str) -> int:
    """删除该聊天的 manifest，并回收不再被任何 manifest 引用的月份缓存。

    这里必须带 **宽限期**：上传新文件时会立刻触发本函数，而"新文件其实是同一段
    对话又多了几个月"恰恰是最需要复用月份缓存的场景——立刻删除会让增量分析失效。
    因此只回收"无引用 **且** 已超过 MONTH_CACHE_GRACE_SECONDS 未被动过"的文件；
    换成完全不同的对话时，旧的月份文件也会在宽限期后被 sweep_orphan_month_cache 收走。
    """
    if not _MONTH_CACHE_DIR or not chat_hash:
        return 0
    path = _manifest_path(chat_hash)
    name = os.path.basename(path)

    removed = 0
    with _MONTH_CACHE_LOCK:
        mine = _manifest_keys_locked(name)
        others = _referenced_keys_locked(exclude=name)
        now = time.time()
        for key in mine - others:
            target = month_cache_path(key)
            try:
                if now - os.path.getmtime(target) < MONTH_CACHE_GRACE_SECONDS:
                    continue  # 宽限期内：留给增量分析复用
                os.remove(target)
                removed += 1
            except OSError:
                pass
    try:
        os.remove(path)
    except OSError:
        pass
    with _MONTH_CACHE_LOCK:
        _MANIFEST_KEYS.pop(name, None)
    return removed


def _manifest_keys_locked(name: str) -> set:
    """（调用方须持有 _MONTH_CACHE_LOCK）单个 manifest 引用的月份 key 集合

    带 mtime 缓存：manifest 只由本模块写，写路径会同步刷缓存，所以命中时直接用。
    """
    path = os.path.join(_MONTH_CACHE_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _MANIFEST_KEYS.pop(name, None)
        return set()
    cached = _MANIFEST_KEYS.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            keys = set(json.load(f).get("months") or [])
    except (OSError, json.JSONDecodeError):
        keys = set()
    _MANIFEST_KEYS[name] = (mtime, keys)
    return keys


def _referenced_keys_locked(exclude: str = "") -> set:
    """（调用方须持有 _MONTH_CACHE_LOCK）所有 manifest 引用到的月份 key"""
    keys: set = set()
    try:
        names = os.listdir(_MONTH_CACHE_DIR)
    except OSError:
        return keys
    live = {n for n in names if n.startswith("manifest_")}
    for name in live:
        if name == exclude:
            continue
        keys |= _manifest_keys_locked(name)
    # 目录里已经没有的 manifest，其缓存条目顺手清掉，避免随历史会话无限增长
    if len(_MANIFEST_KEYS) > len(live):
        for stale in [n for n in _MANIFEST_KEYS if n not in live]:
            _MANIFEST_KEYS.pop(stale, None)
    return keys


def sweep_orphan_month_cache() -> int:
    """回收"已无 manifest 引用且超过宽限期"的月份缓存（定期清理时调用）"""
    if not _MONTH_CACHE_DIR:
        return 0
    removed = 0
    with _MONTH_CACHE_LOCK:
        referenced = _referenced_keys_locked()
        now = time.time()
        try:
            names = os.listdir(_MONTH_CACHE_DIR)
        except OSError:
            return 0
        for name in names:
            if not name.startswith("month_") or not name.endswith(".json"):
                continue
            key = name[len("month_") : -len(".json")]
            if key in referenced:
                continue
            target = os.path.join(_MONTH_CACHE_DIR, name)
            try:
                if now - os.path.getmtime(target) >= MONTH_CACHE_GRACE_SECONDS:
                    os.remove(target)
                    removed += 1
            except OSError:
                continue
    return removed


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
    """调用 API 的公共骨架：调用闸门 → 请求 → 用量记账 → 错误分类与退避。

    _call_api 与 _call_vision 原先各抄了一份近 100 行的同样逻辑（_pace、限流重试、
    额度耗尽判定、指数退避、用量统计），改一处必须记得改两处——这里收成一份，
    两个调用方只管各自的参数构建与结果解析。

    错误分类（两份调用必须一致，否则"文本分析会退避、看图不会"这种差异会
    在最需要稳定的时候暴露出来）：
    - 429（TPM/RPM 每分钟限流，含误导性的 "Allocated quota exceeded/insufficient_quota"
      文案）：全局冷却后重试，最多 TPM_MAX_ATTEMPTS 次；
    - 401/403 配额、402 余额、欠费等真正的额度耗尽：立即抛 QuotaExhaustedError
      （重试无意义，且要让上层中止剩余任务而不是烧钱）；
    - 其他错误：指数退避，最多 generic_retries 次，仍失败则原样抛出。

    返回值：(resp, None) 表示成功；(None, "tpm") 表示限流重试已用尽，由调用方
    决定是抛 QuotaExhaustedError（文本分析）还是降级返回空串（看图，不致命）。
    """
    tpm_hits = 0
    for attempt in range(max_attempts):
        # 计费闸门放在 try **之外**：上限错误不能被下面的"错误分类与退避"当成网络抖动
        # 去重试（那会白白多等几秒，还会把上限再撞几次）。重试循环里的每一次尝试
        # 都算一次请求——限流重试同样占用服务商配额。
        _count_call()
        try:
            _pace()
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
                record_call(
                    DEEPSEEK_MODEL, tag, resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0
                )
            return resp, None
        except Exception as e:
            if _is_plan_exhausted(e):
                raise QuotaExhaustedError(fatal_message) from e
            if _is_tpm_throttle(e):
                tpm_hits += 1
                if tpm_hits < TPM_MAX_ATTEMPTS:
                    logger.warning(
                        "触发每分钟限流（TPM/RPM），全局冷却 %.0fs 后重试（%d/%d）",
                        tpm_wait,
                        tpm_hits,
                        TPM_MAX_ATTEMPTS,
                    )
                    _set_cooldown(tpm_wait)  # 让所有并发线程一起退避，而非各自撞
                    time.sleep(tpm_wait)
                    continue
                return None, "tpm"
            if attempt < generic_retries:
                delay = 2**attempt
                logger.warning("API 调用失败（第 %s 次，%ss 后重试）: %s", attempt + 1, delay, e)
                time.sleep(delay)
                continue
            raise  # 最后仍失败则抛出，由调用方决定"致命"还是"降级"
    return None, "error"


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
    used_keys: set[str] = set()  # 本维度命中的月份缓存键，收尾时一次性写 manifest

    def _cancel_requested() -> bool:
        # 用户点了取消，或进程正在关闭（Ctrl+C）：都不该再往外发新请求
        return shutdown_requested() or bool(should_cancel and should_cancel())

    def _work(period: str, msgs: list) -> tuple[str, Optional[dict]]:
        # 已被排入线程池但尚未开始执行时取消：直接跳过，不产生 API 调用
        if _cancel_requested():
            return period, None
        try:
            prompt = make_prompt(period, msgs)
            if not prompt.strip():
                return period, None
            key = _month_key(system_prompt, prompt, fingerprint)
            result = _read_month_cache(key)
            if result is not None:
                logger.info("%s 命中月份缓存，跳过 API 调用", period)
            else:
                result = _call_api(system_prompt, prompt, max_tokens=max_tokens, tag=tag, dim=tag)
                if result:
                    _write_month_cache(key, result)
            if result:
                result["period"] = period
                result["month"] = period
                used_keys.add(key)
                return period, result
        except QuotaExhaustedError as e:
            fatal.setdefault("error", str(e))
            logger.error("%s 月 AI 分析中止（配额耗尽）", period)
        except Exception as e:
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
        if not results:
            raise QuotaExhaustedError(fatal["error"])
        logger.error("部分月份因配额耗尽未完成: %s", fatal["error"])

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
        result = _call_api(
            system_prompt,
            prompt_template.format(display_name=display_name, dialog=dialog),
            max_tokens=max_tokens,
            tag=tag,
            dim=tag,
        )
        if result:
            result["name"] = display_name
            result["total_messages"] = len(valid_all)
            return result
    except QuotaExhaustedError:
        raise  # 配额耗尽需中止整个维度，不能被当作单人失败吞掉
    except Exception as e:
        logger.error("%s 的 AI 分析失败: %s", mask_name(display_name), e)
    return None


def analyze_habits(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """分析双方的语言习惯"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
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
            if results:  # 已有部分结果：保留已完成者，向上报告配额问题
                logger.error("配额耗尽，剩余对象未分析（已完成 %d/2）", len(results))
                break
            raise
        if result:
            results[person_key] = result
        done += 1
        if on_progress:
            on_progress(done, total)
    return results


def analyze_profile(
    chat: ChatData, on_progress=None, should_cancel=None, chat_hash: str = ""
) -> dict[str, Any]:  # chat_hash 保留以统一调用签名
    """AI 人物锐评 — 分析双方的性格画像"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results: dict[str, Any] = {}
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
            if results:
                logger.error("配额耗尽，剩余对象未分析（已完成 %d/2）", len(results))
                break
            raise
        if result:
            results[person_key] = result
        done += 1
        if on_progress:
            on_progress(done, total)
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
