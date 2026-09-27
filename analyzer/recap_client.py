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
recap 不修补那五支的键（动它们=全员重付费），而是新增一支**真的拿得到跨月事实**的
独立维度：输入是本地统计的逐月表（calc_trends/calc_milestones，确定性、chat_hash 不变
则输入不变），加时间均匀抽样对话，一次调用产出全周期复盘。

指纹隔离（本项目的最高约束）：
- 私聊指纹只哈希 analyzer/prompts.py 里显式列名的常量 + dialog.py 的 5 个格式化函数，
  本模块不在其列 → 既有私聊缓存逐字节不动。
- recap 有**自己的指纹**（哈希自己的 system prompt、自己的摘要构建源码、以及摘要会
  消费的 calc_trends/calc_milestones 源码）：这些任何一个变了，只有 recap 缓存换代。
  摘要数字来自本地统计函数——把它们源码进指纹正是为了"统计口径变了，复盘跟着重算"，
  而本地统计重算本来就免费，代价只有一次 recap 调用。
- 只进维度缓存、不进月份缓存（habits/profile 同样是单次调用维度，先例一致）：
  输入对同一 chat_hash 完全确定，维度缓存即够用，强制重分析在页面上有开关。
"""

import hashlib
import inspect
import os
from typing import Any, Callable, Optional

from analyzer.logger import get_logger
from analyzer.recap_prompts import SYSTEM_PROMPT_ASK, SYSTEM_PROMPT_RECAP

logger = get_logger("app")

RECAP_DIMENSION = "recap"

# 抽样预算：均匀抽样条数与字符上限。这两个数字进指纹（走 _recap_consts_part 显式列名，
# **不是**靠 _recap_digest 的源码——函数源码里它们只是 Name 节点，改值不改形状），
# 调整=recap 换代重算：只有这一族，付一次调用，代价可控、语义也真变了。
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
    """recap 的输出预算：从 API 层统一表取（LLM_MAX_TOKENS_RECAP 可覆盖）。

    延迟导入 deepseek_client 避免模块级环（那一层反过来 import 不到我们，但指纹
    分派在它内部完成，形成运行时互相引用，函数内 import 是既定手法）。
    """
    from analyzer import deepseek_client as dc

    return dc.MAX_TOKENS_BY_DIM.get(RECAP_DIMENSION, 16384)


def _recap_consts_part() -> str:
    """把"改变 recap 模型输入、但不出现在任何函数源码里"的模块级常量收成一段。

    为什么必须单列而不是靠 _recap_digest 的源码：指纹哈希的是 **AST 归一的函数源码**，
    函数体里的 RECAP_SAMPLE_LINES / RECAP_SAMPLE_CHARS 只是 Name 节点——把 800 改成 200
    不改变任何一个节点的形状，于是"喂给模型的对话样本少了一整半"这件事在指纹上完全
    隐形，而旧样本算出的旧复盘照旧以"新配置"的名义命中 30 天（缓存命中时 _recap_digest
    根本不会重跑，没有任何地方会发现输入变了）。私聊族早已用同一段（deepseek_client
    的 "consts:" 块）修过这个坑，并有用例 test_optimizations.
    test_input_budget_change_changes_fingerprint 钉着；recap 族诞生时漏了这段，
    等于把同一个 bug 重新引入一遍。

    列在这里的每一项都必须是**真的会进模型输入**的量：抽样条数、抽样字符预算。
    模型名与思考开关已各自在文件名里，不必重复。
    """
    return f"consts:{RECAP_SAMPLE_LINES},{RECAP_SAMPLE_CHARS}"


def recap_prompt_fingerprint(salt: Optional[str] = None, normalize: bool = True) -> str:
    """recap 独立指纹：system prompt + 摘要构建源码 + 它消费的全部上游函数源码 + 常量 + 预算 + 盐。

    统计函数（calc_overview/calc_trends/calc_milestones）进指纹是有意的：recap 的输入数字
    由它们产出，口径一变旧复盘就配不上新数字——换代重算只花一次调用，且这本来就意味着
    输入变了。calc_overview 此前漏在名单外，而 _recap_digest 的头行与"本地事实"段的每条
    数字都由它产出（见那里对 ov[...] 的取值），它一改旧键照样命中。
    对话格式化函数（_message_line/_has_content）同理：抽样对话原文逐条经它们成型，
    换格式=换模型输入。私聊族一直把这两个函数进指纹，recap 不能例外。
    normalize 语义与其他族一致（AST 归一 / 旧原文公式，读旧缓存迁移用）。
    """
    from analyzer import deepseek_client as dc
    from analyzer import local_stats as ls
    from analyzer.dialog import _has_content, _message_line

    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    funcs = (_recap_digest, ls.calc_overview, ls.calc_trends, ls.calc_milestones, _message_line, _has_content)
    try:
        hashed = [dc._hashed_source(f) if normalize else inspect.getsource(f) for f in funcs]
    except (OSError, TypeError):
        hashed = [f"<source-unavailable:{f.__name__}>" for f in funcs]
        logger.warning("recap 指纹源码不可读，降级为函数名级（改动需手动设盐换键）")
    parts = [
        SYSTEM_PROMPT_RECAP,
        f"budget:{_recap_max_tokens()}",
        _recap_consts_part(),
        *hashed,
    ]
    if salt:
        parts.append(f"salt:{salt}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


RECAP_PROMPT_FINGERPRINT = recap_prompt_fingerprint()
#: 与私聊/群聊两族同构的"旧公式"值。新族诞生即预置：读不到当前键时再试这一支并迁移，
#: 让未来的 recap 指纹演进也能走"用户不重复付费"的既有机制，而不是第三次换代时断层。
#:
#: 写死成**字面量**而不是 recap_prompt_fingerprint(normalize=False)：旧公式哈希的是当前
#: 源码原文，任何一次对 _recap_digest / build_ask_user / 那几个统计与格式化函数的改动
#: 都会让现算值漂移，而它偏偏是"记住过去那个键"的凭据——漂掉的后果是那条
#: "用户不重复付费"的迁移路径静默断掉。理由同 deepseek_client.PROMPT_FINGERPRINT_LEGACY。
#: 本族尚未发布，所以这里就取它诞生时（当前源码）的旧公式值。
RECAP_PROMPT_FINGERPRINT_LEGACY = "16439af05a71"


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
    from analyzer import deepseek_client as dc

    if on_progress:
        on_progress(0, 1)
    if dc.shutdown_requested() or (should_cancel and should_cancel()):
        logger.info("recap：已请求停止，跳过本次调用")
        return None
    digest = _recap_digest(chat)
    if not digest.strip():
        return None
    result = dc._call_api(
        SYSTEM_PROMPT_RECAP,
        digest,
        max_tokens=_recap_max_tokens(),
        tag=RECAP_DIMENSION,
        dim=RECAP_DIMENSION,
    )
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


def build_ask_user(question: str, digest: str) -> str:
    """提问的 user 侧文本：材料在前、问题在后（长材料里的"尾部指令"更不易被忽略）。"""
    return (
        f"{digest}\n\n"
        f"—— 关于以上记录，我的问题 ——\n{question}\n\n"
        '只输出 JSON：{"answer": "直接回答（可分点，别超过 300 字）", '
        '"confidence": "high | medium | low", "evidence": ["材料里的依据原句或数字，0-3 条"]}'
    )


def ask_fingerprint(salt: Optional[str] = None) -> str:
    """ask 独立指纹：ask 的 system prompt + 问题拼装方式 + 摘要会消费的全部上游源码 + 常量 + 预算 + 盐。

    复用 recap 的 _recap_digest 源码进指纹：摘要口径一变，旧答案配不上新材料，换代重算
    （每个问题各付一次，作用域是"问过的题"，不是全部历史）。
    抽样常量与上游统计/格式化函数与 recap 同口径（见 _recap_consts_part 的理由）：
    ask 与 recap 吃的是**同一份 digest**，所以让 recap 隐形的改动同样不能对 ask 隐形。
    """
    from analyzer import deepseek_client as dc
    from analyzer import local_stats as ls
    from analyzer.dialog import _has_content, _message_line

    if salt is None:
        salt = (os.getenv("PROMPT_CACHE_SALT", "") or "").strip()
    funcs = (
        build_ask_user,
        _recap_digest,
        ls.calc_overview,
        ls.calc_trends,
        ls.calc_milestones,
        _message_line,
        _has_content,
    )
    try:
        hashed = [dc._hashed_source(f) for f in funcs]
    except (OSError, TypeError):
        hashed = ["<source-unavailable:ask>"]
    parts = [
        SYSTEM_PROMPT_ASK,
        f"budget:{dc.MAX_TOKENS_BY_DIM.get(ASK_DIMENSION, 8192)}",
        _recap_consts_part(),
        *hashed,
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
    from analyzer import deepseek_client as dc

    if dc.shutdown_requested():
        return None
    digest = _recap_digest(chat)
    if not digest.strip():
        return None
    return dc._call_api(
        SYSTEM_PROMPT_ASK,
        build_ask_user(question, digest),
        max_tokens=dc.MAX_TOKENS_BY_DIM.get(ASK_DIMENSION, 8192),
        tag=ASK_DIMENSION,
        dim=ASK_DIMENSION,
    )
