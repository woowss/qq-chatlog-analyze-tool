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

"""对话构建：把消息列表压成喂给模型的文本（私聊轨）

从 analyzer/deepseek_client.py 拆出（那个文件曾扛八件事、1500 多行）。这是**纯搬迁**：
下面几个函数的源码进 PROMPT_FINGERPRINT（它的哈希对象里有 inspect.getsource 的返回值），
所以函数名、docstring、注释、引号风格都是缓存契约的一部分——改名或改注释会让所有既有
用户的私聊分析缓存失效、重新付费。

搬迁本身安全：getsource 只返回函数自身那几行，不含它所在模块的路径，
tests/test_group_foundation.py 有用例钉住这一点（test_source_moves_do_not_change_the_fingerprint）。

本模块只依赖 config / parser / logger，不反向依赖 API 层（唯一例外见 _vision_digest）。
"""

from collections import Counter
from datetime import datetime
from typing import Optional

from config import env_number
from parser.qq_parser import CST, MEDIA_KINDS, is_statistical
from analyzer.local_stats import _median, is_session_start
from analyzer.logger import get_logger

logger = get_logger("deepseek")


# 单月对话文本上限（字符数）。**准确性优先**：默认 60 万字符（约 25-30 万 tokens），
# 足以装下绝大多数月份的全部消息（实测：最长那个月会逼近这个上限），
# 因此正常情况下不会触发抽样；只有极端月份（几十万条）才会等间隔抽样并注明。
# 想省钱可在 .env 里调小 LLM_MAX_DIALOG_CHARS。
MAX_DIALOG_CHARS = int(env_number("LLM_MAX_DIALOG_CHARS", 600_000, 1000, 2_000_000))


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
        # 中位数必须走 local_stats._median：就地 `sorted(gaps)[len(gaps)//2]` 取的是
        # **上中位**，偶数样本上它不是中位数（[10,20,30,40] 报成 30，真值 25；
        # [1,9] 报成 9，真值 5）。那正是本轮 local_stats 为句长/媒体长度专门
        # 顶了一次 STATS_SCHEMA_VERSION 修掉的同一个毛病——同一个词"中位数"在
        # 界面卡上算了 25、在给模型这句"回复间隔中位数约 N 秒"里算了 30，
        # 用户只会以为其中一处错了。口径唯一是本地统计的立身之本。
        # _median 与 is_session_start 同源于 local_stats（模块级已 import，不成环），
        # 复用而不是就地再写一遍：两处各算一遍就是下一次漂移的种子。
        "median_gap": _median(sorted(gaps)) if gaps else None,
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
    # 延迟导入：QuotaExhaustedError 由 API 层（deepseek_client）拥有，而它反过来要 import
    # 本模块的对话构建函数，模块级互相导入会成环。这个错误类型只在真的要走图片摘要时
    # 才需要，放在函数里最省事也最不容易出环。
    from analyzer.deepseek_client import QuotaExhaustedError

    try:
        from analyzer import vision

        return vision.digest(messages, chat_hash=chat_hash, label=label)
    except QuotaExhaustedError:
        raise  # 额度耗尽要中止整体任务，不能悄悄吞掉
    except Exception as e:
        logger.warning("图片摘要失败（继续纯文本分析）: %s", e)
        return ""
