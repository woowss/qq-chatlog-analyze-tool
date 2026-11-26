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
"""「整体总括」维度：一次调用看完整条时间线。

为什么需要它：逐月的五个维度各自只看得到自己那一个月，prompt 里却被要求给出
"亲密趋势/话题迁移"——结构上无从看见上月，只能靠猜（docs/reviews 与调研都点了这条）。
recap 不修补那五支的键，而是新增一支**真的拿得到跨月事实**的独立维度：输入是本地统计的
逐月表（calc_trends/calc_milestones，确定性、chat_hash 不变则输入不变），加时间均匀抽样
对话，一次调用产出全周期复盘。
"""

import hashlib
import os
from typing import Any, Callable, Optional

from analyzer import llm_runtime
from analyzer import llm_stream
from analyzer import recap_prompts as rp
from analyzer.dialog import _has_content
from analyzer.logger import get_logger
from analyzer.recap_prompts import SYSTEM_PROMPT_ASK, SYSTEM_PROMPT_RECAP
from analyzer.shutdown import shutdown_requested

logger = get_logger("app")

RECAP_DIMENSION = "recap"
RECAP_CACHE_VERSION = "v2"

# 抽样预算：均匀抽样条数与字符上限。这两个数字进指纹（走 _recap_consts_part 显式列名），
# 调整=recap 换代重算。
RECAP_SAMPLE_LINES = 800
RECAP_SAMPLE_CHARS = 150_000


def _recap_digest(chat) -> str:
    """构建跨月输入：统计头 + 逐月事实表 + 里程碑 + 时间均匀抽样对话。

    确定性是本模块的立身之本：维度缓存按 chat_hash 寻址，同文件必得同输入，
    所以缓存永远不错配。名字/备注出现在头行与对话行里——改名会产生新的
    chat_hash（文件字节变了），语义上正是要重算的。
    """
    from analyzer import local_stats as ls
    from analyzer.dialog import _has_content, _message_line

    ov = ls.calc_overview(chat)
    tr = ls.calc_trends(chat)
    ms = ls.calc_milestones(chat)

    head = [
        f"参与者：{chat.self_name}（我）与 {chat.other_name}；"
        f"期间 {ms.get('first_day', '?')} ~ {ms.get('last_day', '?')}，"
        f"共 {ov['total_messages']} 条消息（日均 {ov['avg_daily']}，活跃 {ov['active_days']} 天）。",
    ]
    facts = [
        f"消息对比：我 {ov['self_count']} 条 / {ov['self_chars']} 字；"
        f"对方 {ov['other_count']} 条 / {ov['other_chars']} 字。",
    ]
    if ov.get("total_recalls"):
        facts.append(
            f"撤回：共 {ov['total_recalls']} 条"
            f"（我 {ov.get('self_recalls', 0)}，对方 {ov.get('other_recalls', 0)}）。"
        )
    streak = ms.get("longest_streak") or {}
    if streak.get("days"):
        facts.append(f"连续聊天纪录：{streak['days']} 天（{streak.get('start')}~{streak.get('end')}）。")
    silence = ms.get("longest_silence") or {}
    if silence.get("days"):
        facts.append(f"最长沉默：{silence['days']} 天（{silence.get('before')} → {silence.get('after')}）。")
    restarts = tr.get("restarts") or {}
    if restarts.get("count"):
        facts.append(
            f"冷场 ≥{restarts['threshold_days']} 天后的重启：{restarts['count']} 次"
            f"（我开口 {restarts['self']} 次 / 对方 {restarts['other']} 次）。"
        )
    if ms.get("peak_day"):
        facts.append(f"单日峰值：{ms['peak_day']['date']} 共 {ms['peak_day']['count']} 条。")

    monthly = []
    for row in tr.get("months") or []:
        monthly.append(
            f"{row['month']}：我 {row['self']} 条 / 对方 {row['other']} 条；"
            f"平均句长 我 {row['avg_len_self']} 字 / 对方 {row['avg_len_other']} 字"
        )

    # 时间均匀抽样对话（同 profile 的 stratified 思路：不然大跨度里只剩最后一两个月）
    valid = [m for m in chat.statistical() if _has_content(m)]
    lines = []
    if valid:
        stride = max(1, (len(valid) + RECAP_SAMPLE_LINES - 1) // RECAP_SAMPLE_LINES)
        for m in valid[::stride]:
            name = chat.self_name if m.sender_uid == chat.self_uid else chat.other_name
            lines.append(_message_line(m, name))
        budget, kept = RECAP_SAMPLE_CHARS, []
        for line in lines:
            budget -= len(line) + 1
            if budget < 0:
                kept.append("……（后略，样本因篇幅截断）")
                break
            kept.append(line)
        lines = kept

    parts = head + ["本地事实：\n" + "\n".join(facts)]
    if monthly:
        parts.append("逐月事实（跨月对比的唯一可靠依据）：\n" + "\n".join(monthly))
    if lines:
        parts.append("按时间均匀抽样的对话原文：\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _recap_max_tokens() -> int:
    """recap 的输出预算：从统一表取（LLM_MAX_TOKENS_RECAP 可覆盖）。"""
    return llm_runtime.MAX_TOKENS_BY_DIM.get(RECAP_DIMENSION, 16384)


def _recap_consts_part() -> str:
    """把改变 recap/ask 模型输入的抽样预算常量收成一段参与指纹计算。"""
    return f"consts:{RECAP_SAMPLE_LINES},{RECAP_SAMPLE_CHARS}"


def recap_prompt_fingerprint(salt: Optional[str] = None) -> str:
    """recap 独立指纹：版本号 + system prompt + 抽样预算常量 + 输出预算 + 盐。"""
    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    parts = [
        f"version:{RECAP_CACHE_VERSION}",
        rp.SYSTEM_PROMPT_RECAP,
        SYSTEM_PROMPT_RECAP,
        f"budget:{_recap_max_tokens()}",
        _recap_consts_part(),
    ]
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


RECAP_PROMPT_FINGERPRINT = recap_prompt_fingerprint()


def analyze_recap(
    chat,
    on_progress: Optional[Callable] = None,
    should_cancel: Optional[Callable] = None,
    chat_hash: str = "",
) -> Optional[dict[str, Any]]:
    """整体总括：一次调用，输入是跨月事实 + 均匀抽样对话。返回单份 JSON 结果 dict。

    与单次调用维度（habits/profile）同形：不发月份并发、不写月份缓存；
    取消/关闭在派发前检查（一次调用没有"剩余月份"，检查点只有派发前这一处）。
    """
    if on_progress:
        on_progress(0, 1)
    if shutdown_requested() or (should_cancel and should_cancel()):
        logger.info("recap：已请求停止，跳过本次调用")
        return None
    if not any(_has_content(m) for m in chat.statistical()):
        return None
    digest = _recap_digest(chat)
    if not digest.strip():
        return None
    with llm_stream.stream_context(unit=RECAP_DIMENSION, label="整体总括", fresh_state=True):
        llm_stream.emit_event("stage", "开始生成整体总括（跨月趋势与关键节点）")
        try:
            result = llm_runtime._call_api(
                SYSTEM_PROMPT_RECAP,
                digest,
                max_tokens=_recap_max_tokens(),
                tag=RECAP_DIMENSION,
                dim=RECAP_DIMENSION,
            )
        except llm_stream.StreamCancelledError:
            llm_stream.emit_event("stage", "整体总括已取消")
            result = None
        if result:
            llm_stream.emit_result_fallback(result)
            llm_stream.emit_event("stage", "整体总括生成完成")
    if on_progress:
        on_progress(1, 1)
    return result


#: 注册表（与 GROUP_DIMENSIONS 同构：dim → (中文名, 执行函数, 进度单位)）。
#: jobs.py 的 analyze_func_for / dimension_unit 据此查询；
#: 刻意**不进** dimensions_for_mode 的全量列表——"一键全量"的成本承诺不因新维度悄悄变。
RECAP_DIMENSIONS: dict = {RECAP_DIMENSION: ("整体总括", analyze_recap, "次")}


# ---------------------------------------------------------------------------
# 自定义提问（ask）：与 recap 同一份跨月摘要作上下文，用户问什么答什么。
# 独立缓存族：文件名含 ask 指纹 + 问题文本哈希——同一问题重问免费（命中缓存），
# 换个问题就是另一份键；与 recap/私聊/群聊各族互不影响。
# ---------------------------------------------------------------------------

ASK_DIMENSION = "ask"
ASK_CACHE_VERSION = "v2"


def build_ask_user(question: str, digest: str) -> str:
    """提问的 user 侧文本：材料在前、问题在后（长材料里的"尾部指令"更不易被忽略）。"""
    return (
        f"{digest}\n\n"
        f"—— 关于以上记录，我的问题 ——\n{question}\n\n"
        '只输出 JSON：{"answer": "直接回答（可分点，别超过 300 字）", '
        '"confidence": "high | medium | low", "evidence": ["材料里的依据原句或数字，0-3 条"]}'
    )


def ask_fingerprint(salt: Optional[str] = None) -> str:
    """ask 独立指纹：版本号 + system prompt + 抽样预算常量 + 输出预算 + 盐。"""
    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    parts = [
        f"version:{ASK_CACHE_VERSION}",
        rp.SYSTEM_PROMPT_ASK,
        SYSTEM_PROMPT_ASK,
        f"budget:{llm_runtime.MAX_TOKENS_BY_DIM.get(ASK_DIMENSION, 8192)}",
        _recap_consts_part(),
    ]
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


ASK_PROMPT_FINGERPRINT = ask_fingerprint()


def answer_question(chat, question: str, chat_hash: str = "") -> Optional[dict]:
    """单次调用回答一个关于这份聊天的问题。返回 {"answer","confidence","evidence"} 或 None。

    与 recap 同形（一次调用、无月份并发、无月份缓存）；调用计数与限流由 _call_api 统一处理。
    不进 begin_run/end_run 的"运行"配额记账：一次提问本身就是一次调用，
    "一次点击不因为配错参数而花超"由 LLM_MAX_CALLS_PER_RUN 在分析任务那侧兜住。
    """
    if shutdown_requested():
        return None
    digest = _recap_digest(chat)
    if not digest.strip():
        return None
    with llm_stream.stream_context(unit=ASK_DIMENSION, label="自定义提问", fresh_state=True):
        llm_stream.emit_event("stage", "开始思考自定义提问")
        try:
            result = llm_runtime._call_api(
                SYSTEM_PROMPT_ASK,
                build_ask_user(question, digest),
                max_tokens=llm_runtime.MAX_TOKENS_BY_DIM.get(ASK_DIMENSION, 8192),
                tag=ASK_DIMENSION,
                dim=ASK_DIMENSION,
            )
        except llm_stream.StreamCancelledError:
            llm_stream.emit_event("stage", "自定义提问已取消")
            return None
        if result:
            llm_stream.emit_result_fallback(result)
            llm_stream.emit_event("stage", "自定义提问回答生成完成")
        return result
