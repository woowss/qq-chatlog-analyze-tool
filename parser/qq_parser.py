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
"""QQ JSON 聊天记录解析器 — 支持 QQChatExporter V5 格式"""

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import env_bool
from parser.group_identity import (
    Participant,
    collect_participants,
    is_placeholder_sender,
    unique_display_names,
)

# 北京时间固定偏移，供月份分组与本地统计共用，避免口径不一致
CST = timezone(timedelta(hours=8))

# 不应进入统计与 AI 分析的消息类型：合并转发（旧格式 type_11，内容是一整段外部聊天，
# 不是两人的对话）、豆腐记录等小程序卡片（旧格式 type_23，无正文可分析）。
# 新版导出器改用语义化类型名（text/reply/system/file/forward/json/video…），因此这里
# 补上 "system"：系统提示消息在群里占 5% 上下（实测导出就是这个量级），绝大多数带
# system 标记、由 is_statistical 拦下，但"标记不齐全"是实测踩过的坑，多一道类型防线更稳。
# 注意 type_17（商城大表情）**不在**这里：它是表情信号，按表情统计与展示。
# 也**故意不**跳过 forward/json：新版导出器给它们的是结构化短标签（标题+条数、卡片摘要），
# 由 MEDIA_KINDS 的媒体口径承载，长度有上限（MEDIA_LABEL_MAX），不会灌进 prompt。
SKIP_MSG_TYPES = {"type_11", "type_23", "system"}

# 非文本媒体元素 → 中文标签（用于给模型与统计一个可读的占位说明）
MEDIA_KINDS = {
    "file": "文件",
    "video": "视频",
    "forward": "转发",
    "wallet": "红包",
    "face_bubble": "表情气泡",
    "markdown": "Markdown消息",
    "json": "卡片消息",  # QQ 小程序/分享卡片（导出器只给到 "[JSON消息]"）
    "av_record": "通话",  # 语音/视频通话记录（"通话 - 未接听" 之类）
    # 语音消息本体（不是通话记录）。此前它不在任何分派分支里：一条语音会以
    # 零正文、零标记的形态混进消息计数，词频与句长按空串处理、模型则完全看不见
    # 它——"语音多的人整段内容消失"。补进媒体口径后它至少**可见且可数**
    # （有 duration/summary 就给标签）。ASR 转写是另一件事，不在本地承诺范围内。
    "voice": "语音",
}
# 媒体标签的长度上限：文件名/转发标题可能很长，截断保留可读性
MEDIA_LABEL_MAX = 40

# 回退解析 time 字符串时支持的格式（导出器为 "%Y-%m-%d %H:%M:%S"）
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _to_int(value) -> int:
    """导出器把体积写成字符串，解析失败按 0 处理（统计不该被脏字段打断）

    OverflowError 也要接住：json.loads 默认接受 `Infinity` 字面量，
    `int(float(inf))` 抛的是 OverflowError，不在 (TypeError, ValueError) 之列。
    """
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _clean_face_name(name: str) -> str:
    """商城大表情的名字形如 "[[叉腰]]"，去掉方括号只留可读名"""
    return (name or "").strip().strip("[]").strip()


def _shorten(text: str, limit: int = MEDIA_LABEL_MAX) -> str:
    """媒体短标签：压平空白并截断，避免长文件名/长标题灌进 prompt"""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _media_label(el_type: str, el_data: dict, raw_text: str = "") -> str:
    """非文本媒体元素 → 人类可读短标签（文件名、转发标题与条数…）"""
    if el_type == "file":
        return _shorten(el_data.get("filename") or el_data.get("name") or "")
    if el_type == "video":
        return _shorten(el_data.get("filename") or "")
    if el_type == "forward":
        title = _shorten(el_data.get("title") or "")
        count = str(el_data.get("messageCount") or "").strip()
        if title and count:
            return f"{title}（{count}条）"
        return title or (f"{count}条转发" if count else "")
    if el_type == "wallet":
        return _shorten(el_data.get("summary") or "")
    if el_type == "face_bubble":
        return _shorten(el_data.get("faceSummary") or el_data.get("summary") or "")
    if el_type == "markdown":
        return _shorten(el_data.get("summary") or "")
    if el_type == "av_record":
        # 通话记录：导出器把"通话 - 未接听，点击回拨"放在消息文本里
        return _shorten(el_data.get("summary") or el_data.get("details") or raw_text or "")
    if el_type == "voice":
        # 语音消息本体：有秒数就带上（"语音12秒"比光"语音"信息量大，模型能分清
        # 一句话语音和一段长语音）。字段名两种写法都实测存在于各家导出器。
        seconds = _to_int(el_data.get("duration") or el_data.get("time"))
        if seconds:
            return f"{seconds}秒"
        return _shorten(el_data.get("summary") or "")
    return ""


def _allow_multi_party() -> bool:
    """旧的 QQCHAT_ALLOW_MULTI_PARTY 开关（调用时读 env，方便测试与临时放行）。

    新口径统一由 group_chat_mode() 表达；本函数只保留"旧变量怎么解析"这唯一一处，
    作为 two_party 模式的别名来源。旧文档与旧用户配置都指向这个变量，不能删。

    解析走 config.env_bool：这个变量原先自己写了一份词表（1/true/yes/on），
    与别处的布尔解析各写各的，正是"口径漂移"的起点。词表没有任何放宽——
    同一批写法仍然是真、其余仍然是假，所以既有配置的行为逐字不变。
    """
    return env_bool("QQCHAT_ALLOW_MULTI_PARTY", False)


#: 群聊轨是否已经可以对外服务。**这是"多人导出不再拒收"的唯一闸门**。
#: 2026-09-12 随 M3（群聊页面、互动矩阵、成员画像、成本预提示）一起打开；
#: 打开前它一直是 False：群聊的数据层/统计/AI/页面没就绪时，默认模式下多人导出仍被拒收，
#: 用户看不到半成品。想回到升级前的行为（多人导出直接拒收）把 QQCHAT_GROUP_CHAT 设为 off，
#: 或设为 two_party 继续用旧的「我 vs 其他人」两分类归并。
GROUP_TRACK_READY = True

#: QQCHAT_GROUP_CHAT 的合法取值。auto=识别为群聊（需 GROUP_TRACK_READY）；
#: off=维持拒收；two_party=按「我 vs 其他人」两分类归并（旧 QQCHAT_ALLOW_MULTI_PARTY=1 语义）。
_GROUP_MODES = ("auto", "off", "two_party")


def group_chat_mode() -> str:
    """多人（群聊）导出的处置模式，调用时读环境变量。

    口径唯一来源：解析层、应用层与文档都以本函数为准，避免"配置读两遍、取值不一致"。
    旧变量 QQCHAT_ALLOW_MULTI_PARTY 继续作为 two_party 的别名生效（README 与既有
    测试都依赖它）；新变量优先，非法值回退默认并告警。
    """
    raw = (os.getenv("QQCHAT_GROUP_CHAT", "") or "").strip().lower()
    if raw in _GROUP_MODES:
        return raw
    if raw:
        print(
            f"[WARN] QQCHAT_GROUP_CHAT={raw!r} 不是 {'/'.join(_GROUP_MODES)} 之一，已回退为 auto",
            file=sys.stderr,
        )
    return "two_party" if _allow_multi_party() else "auto"


def multi_party_action(chat: "ChatData") -> str:
    """多人防线对这份导出的处置：none | reject | merge | group。

    判定顺序即优先级：
    1. 有"有实质发言的第三方"（按门槛，见 _multi_party_offenders）时：
       - two_party → merge（旧逃生阀：所有人并进"对方"，用户知情）；
       - auto 且群聊轨已就绪 → group（走群聊分析）；
       - 其余（off，或 auto 但群聊轨未就绪）→ reject（与升级前逐字节一致）。
    2. 没有达到门槛的第三方，但**导出器自报这是群聊**（新版导出器写 chatInfo.type=group）
       且 auto 且群聊轨已就绪 → group。

    第 2 条是新增能力，只在"按老口径会判成私聊"的缝隙里生效，因此**不会改变任何既有
    分支的结果**：老分支该 merge 的仍 merge、该 reject 的仍 reject。它解决的是这类失真：
    3 人小群里两位长期潜水时，按发言门槛会被当成私聊，除我之外的所有人被并进"对方"。
    注意导出器自报类型**不**触发拒收（off 模式下它的行为与升级前完全一样）。
    """
    mode = group_chat_mode()
    if not _multi_party_offenders(chat):
        if mode == "auto" and GROUP_TRACK_READY and (chat.chat_type or "") == "group":
            return "group"
        return "none"
    if mode == "two_party":
        return "merge"
    if mode == "auto" and GROUP_TRACK_READY:
        return "group"
    return "reject"


# 第三方要"有实质发言"才认定成群聊：QQChatExporter 会给系统类消息安排占位
# sender（name="系统消息"、uid 形如"未知…"），而这类条目的 system 标记并不齐全——
# 实测较大规模的私聊导出里，若干占位消息中就有 1 条 type_23（商城表情）没有
# system 标记。若按"出现过就算一位"，正常私聊会被判成群聊直接拒收（这正是把
# 判定口径收紧到 is_statistical + 设门槛的原因）。
MULTI_PARTY_MIN_MESSAGES = 3
MULTI_PARTY_MIN_SHARE = 0.005


def _statistical_sender_counts(chat: "ChatData") -> dict:
    """统计口径下的发言者 → 条数（只算真正会进分析与统计的消息）。

    走 ChatData.sender_counts() 的实例级缓存：多人防线、拒收文案里的占比、以及
    "谁是对方"三处都要这份计数，原先各遍历一遍——数万条消息下是两到四遍全量。
    返回的是缓存对象本身，调用方只读。
    """
    return chat.sender_counts()


def _multi_party_offenders(chat: "ChatData") -> list[tuple[str, int]]:
    """返回「除自己与主要对话方之外、且有实质发言」的第三方（条数降序）。

    私聊返回空列表；群聊会返回其余参与者，由调用方决定是否拒收。

    占位 sender 必须先摘掉（is_placeholder_sender），否则这里数出来的"第三方"里
    混的是导出器给系统类消息安排的假发言人：门槛只有 3 条，而一份几百条的私聊导出
    只要漏标 3 条，就会被整条流水线判成群聊（other_uid 置空、response_time 不再算），
    在 QQCHAT_GROUP_CHAT=off 下更是直接拒收。身份层早就按名字/uid 前缀排除它们
    （collect_participants 里那一手），门槛这侧此前一直漏了同一个判据 ——
    两处口径不一致，出问题的是更难发现的那一侧。
    """
    counts = _statistical_sender_counts(chat)
    names = chat.sender_names()
    others = {
        uid: n
        for uid, n in counts.items()
        if uid != chat.self_uid and not is_placeholder_sender(uid, names.get(uid, ""))
    }
    if len(others) <= 1:
        return []
    total = sum(counts.values()) or 1
    threshold = max(MULTI_PARTY_MIN_MESSAGES, int(total * MULTI_PARTY_MIN_SHARE))
    ranked = sorted(others.items(), key=lambda kv: kv[1], reverse=True)
    return [(uid, n) for uid, n in ranked[1:] if n >= threshold]


#: 毫秒时间戳的合法区间：2001-01-01 ~ 2033-05-18（1e12 ~ 2e12 ms）。
#: 落在这个区间之外的数值不是"晚一点/早一点"，而是**量级错了**：导出器写秒、
#: 微秒或纳秒时，数值会整倍地偏出千倍。下游每个消费点都假定这个字段是毫秒
#: （`datetime.fromtimestamp(timestamp / 1000)`、按月分组的键、时间跨度差值），
#: 所以量级错的值有两种后果，都很糟：
#:   ① 微秒/纳秒 → /1000 后是一个荒谬的年 → datetime 直接抛
#:      `OSError: [Errno 22] Invalid argument`，**整份文件传不上去**，
#:      而用户看到的症状是一个与"时间戳格式"毫不相干的 errno；
#:   ② 秒 → 被当成毫秒解释成 1970-01，凭空多出一个"月份"，而 _analyze_periods
#:      会把它当作真实月份**发一次付费调用**——正是本函数丢弃 ts<=0 想防的事。
#: 因此这里做一次量级归一；归一不了的（例如落在合法区间但明显是错值）不猜，
#: 落回 time 字符串，两条路都不通则返回 None 由调用方丢弃并计入 dropped_messages。
_MS_MIN = 1_000_000_000_000  # 1e12  ≈ 2001-09
_SEC_MIN, _SEC_MAX = 1_000_000_000, 2_000_000_000  # 1e9 ~ 2e9 秒 ≈ 2001 ~ 2033
_USEC_MIN, _USEC_MAX = 1_000_000_000_000_000, 2_000_000_000_000_000  # ×1e3 of sec
_NSEC_MIN, _NSEC_MAX = 1_000_000_000_000_000_000, 2_000_000_000_000_000_000  # ×1e6


def _normalise_epoch_ms(ts: int) -> Optional[int]:
    """把"量级明显不是毫秒"的 epoch 值归一成毫秒；认不出来返回 None。

    只认三种整倍错位（秒 / 微秒 / 纳秒），且都要求归一后落回合法毫秒区间——
    宁可返回 None 让调用方回退 time 字符串，也不把一个仍然荒谬的值喂给下游。
    """
    if _MS_MIN <= ts <= 2_000_000_000_000:
        return ts  # 本来就是毫秒
    if _SEC_MIN <= ts <= _SEC_MAX:
        return ts * 1000
    if _USEC_MIN <= ts <= _USEC_MAX:
        return ts // 1000
    if _NSEC_MIN <= ts <= _NSEC_MAX:
        return ts // 1_000_000
    return None


def _parse_timestamp(value, time_str: str = "") -> Optional[int]:
    """把导出文件里的时间戳统一成毫秒整数；无法解析时返回 None。

    导出文件可能存在缺失/为 null/是字符串的时间戳。缺键时会 get 到 0，
    若照单全收，该消息会被归入 1970-01 并作为一个"月份"送去 AI 分析；
    为 null 或字符串时则会让排序/统计直接抛异常。这里统一兜底：
    先按数值解析并做量级归一（见 _normalise_epoch_ms），失败再用 time 字符串回退，
    两者都不可用则返回 None（调用方丢弃该条）。
    """
    try:
        ts = int(float(value))
        if ts > 0:
            normalised = _normalise_epoch_ms(ts)
            if normalised is not None:
                return normalised
            # 数值为正但量级救不回来：不许把荒谬值喂给下游，落回 time 字符串
    except (TypeError, ValueError, OverflowError):
        # OverflowError 来自 Infinity 时间戳（json.loads 默认接受）——按"数值不可用"
        # 处理，落回 time 字符串，两者都不行就返回 None 丢弃该条，不许崩整份文件
        pass
    text = (time_str or "").strip()
    if text:
        for fmt in _TIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=CST)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        try:
            # ISO 8601（新版导出器写的是 UTC：2024-01-01T00:00:00.000Z）
            iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if iso.tzinfo is None:
                iso = iso.replace(tzinfo=CST)
            return int(iso.timestamp() * 1000)
        except ValueError:
            pass
    return None


@dataclass
class Message:
    """单条消息"""

    id: str
    timestamp: int  # 毫秒时间戳
    time_str: str  # "2024-01-01 08:00:00"
    sender_name: str  # 发送者显示名
    sender_uid: str  # 发送者 UID
    text: str  # 纯文本（不含图片/表情/媒体标记）
    raw_text: str  # 原始文本（含占位符）
    msg_type: str  # type_1 / type_3 / type_17 …
    has_image: bool
    is_reply: bool
    face_ids: list[int] = field(default_factory=list)
    face_names: list[str] = field(default_factory=list)  # 表情名称（含商城大表情）
    # 非文本媒体：kind 取 file/video/forward/wallet/face_bubble/markdown，label 是短标签
    # （文件名、转发标题…）。这些内容不进 text——否则文件名/占位符会污染词频与句长，
    # 但它们要参与统计与 AI 分析，由 media_kind/label 承载。
    media_kind: str = ""
    media_label: str = ""
    media_bytes: int = 0  # 媒体体积（导出器给的 size，未知为 0）
    media_id: str = ""  # 媒体指纹（md5，可用于去重统计；视频等没有则为空）
    media_path: str = ""  # 资源相对路径（导出器的 url，如 resources/images/xx.jpg）
    media_w: int = 0  # 图片宽（用于挑图：几百 px 的多半是表情包，不是截图）
    media_h: int = 0
    face_url: str = ""  # 商城表情的 CDN 地址（可选功能"表情原图"用它取图）
    recalled: bool = False  # 已被撤回（导出器仍会保留该条目）
    system: bool = False  # 系统提示消息（"对方撤回了一条消息"等）
    # —— 下面四个字段来自新版导出器的结构化元素，全部有默认值，既有构造点不受影响。
    # 它们是群聊互动的**精确信号**：reply 指向被回复的那条消息（可解析出发言人），
    # at 是明确的点名。相比之下"相邻两条换人"只是推断，两者必须分开统计
    # （见 analyzer/group_stats.calc_interaction_matrix 的 explicit_* / mention_*）。
    reply_to_id: str = ""  # reply 元素里被回复消息的 id（解析后用于回填 reply_to_uid）
    reply_to_uid: str = ""  # 被回复者的 uid（由 reply_to_id 在全量消息里回查得到）
    mentions: list[str] = field(default_factory=list)  # @ 到的成员 uid（不含 @全体成员）
    mentions_all: bool = False  # 是否 @了全体成员（atType=1，uid="all"）
    # —— 图片专用口径（必须**加在最后**：前面的字段有按位置构造的用例）。
    # media_bytes 是这条消息里**所有**媒体元素的字节合计（图片逐个 + 非图片媒体逐个），
    # 一条既有图片又有文件的消息
    # 如果把它整体记进任何一边，另一边就漏算、这一边多算（实测：图片 2048 + 文件 4096
    # 被记成"图片 6144 + 文件/视频 6144"，仪表盘并排显示两个 6144，而真实发送量只有
    # 6144）。所以按元素归家：图片的那份单独记，张数与 md5 也单独记。
    image_bytes: int = 0  # 只属于图片元素的字节
    image_count: int = 0  # 这条消息里有几个图片元素（一条消息连发 3 张图就是 3 张）
    image_ids: list[str] = field(default_factory=list)  # 各图片元素的 md5（去重口径用）


def is_statistical(m: "Message") -> bool:
    """是否应进入统计与 AI 分析：排除系统消息、撤回消息与不可分析的卡片类消息"""
    return not m.system and not m.recalled and m.msg_type not in SKIP_MSG_TYPES


@dataclass
class ChatData:
    """解析后的完整聊天数据"""

    chat_name: str
    self_name: str
    other_name: str
    self_uid: str
    other_uid: str
    messages: list[Message] = field(default_factory=list)
    total_count: int = 0
    time_start: str = ""
    time_end: str = ""
    duration_days: int = 0
    dropped_messages: int = 0  # 解析期被丢弃的消息数（时间戳不可用、整条形状非法；0=全部可用）
    # 派生缓存：由 messages 算出、可重复计算、只在本对象生命周期内复用。
    # 声明成字段而不是 setattr 动态挂载——读代码的人能一眼看到"谁往这个对象上挂了什么"，
    # 类型检查也不必再靠 # type: ignore 绕过。repr/compare 排除：它们可能很大，
    # 且不该影响"两份聊天数据是否相等"的判断。
    _stats_cache: Optional[tuple] = field(default=None, repr=False, compare=False)
    _months_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    # —— 群聊（多人）支持：全部是**尾部带默认值**的新字段，因此既有构造点
    #    （tests 里 13 处 ChatData(...)）不需要任何改动，相等性语义也不变。
    #    默认 False / "private" 就是"两人私聊"，即这些字段出现之前的行为。
    is_group_chat: bool = False
    #: private（两人私聊）| group（群聊）| two_party（旧逃生阀：所有人并进"对方"）
    mode: str = "private"
    #: 导出器自报的会话类型（新版导出器给群聊写 "group"，私聊可能是 friend/private 或缺失）。
    #: 这是比"数发言者"更可靠的群聊信号：一个 3 人小群里两位潜水时，按发言门槛会误判成
    #: 私聊，而导出器早就写明这是群。判定顺序见 multi_party_action。
    chat_type: str = ""
    _participants_cache: Optional[list] = field(default=None, repr=False, compare=False)
    #: 统计口径下的 发言者 uid → 条数。多人防线、拒收文案里的占比、"谁是对方"三处共用。
    _sender_counts_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    #: 同一次遍历顺手记下的 发言者 uid → 最常用的显示名。判群门槛与"谁是对方"都要按
    #: 名字识别占位 sender（见 group_identity.is_placeholder_sender），只有条数就不够。
    _sender_names_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    #: 解析时未命中任何分派分支的元素类型 → 出现次数（导出器格式漂移的探测器）。
    #: 此前未知 el_type 会被 elif 链**静默吞掉**：哪天导出器把 "text" 改名，
    #: 正文会丢光而统计照常出数，用户与开发者都看不见事故发生了。
    #: 现在每个未知类型都记在这里，仪表盘给出提示、inspect_chat 逐类对账。
    #: 尾部带默认值的新字段：既有构造点与相等性语义不受影响。
    unknown_element_types: dict = field(default_factory=dict, repr=False, compare=False)
    #: 互动矩阵的结果缓存：**生效上限 → 矩阵 dict**（见 group_stats.calc_interaction_matrix）。
    #: 为什么必须缓存：成员画像那条路是"每位成员一次调用"，每次都要按 uid 取互动数字，
    #: 而它要的是**不截断**的矩阵（top_k=0）。不缓存的话，一个几千人的群每分析一位成员
    #: 就把 n×n 矩阵重算一遍——实测 n=3000 单次 4.4 秒 / 376 MB，×10 位成员就是 44 秒
    #: 与上 GB 的反复分配。键取**已解析的生效上限**而不是入参 top_k：`_matrix_top_k()`
    #: 是调用时读配置的（有用例专门钉这个语义，见 test_matrix_limit_reads_config_at_call_time），
    #: 用 top_k 当键会让"改配置后拿回旧上限的矩阵"。
    _matrix_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    # Browse/evidence indexes follow the parsed object's replacement/deletion lifecycle.
    _message_index: Optional[object] = field(default=None, repr=False, compare=False)

    def statistical(self) -> list["Message"]:
        """参与统计与分析的消息子集（过滤系统/撤回/转发）"""
        return [m for m in self.messages if is_statistical(m)]

    def sender_counts(self) -> dict:
        """统计口径下的 发言者 uid → 条数（结果缓存在本对象上）。

        这份计数有三处消费点：多人防线的门槛判定（_multi_party_offenders）、
        拒收报错的占比文案、以及"谁是对方"（取发言最多的一方）。原先各调一次
        _statistical_sender_counts，数万条消息就是两到四遍全量遍历。
        与 months()/participants() 同样的安全前提：解析完成后 messages 不再变动。

        返回的是缓存对象**本身**——调用方只读，不要就地改（那会污染后续判定）。
        """
        if self._sender_counts_cache is None:
            counts: dict[str, int] = {}
            name_tally: dict[str, dict[str, int]] = {}
            for m in self.messages:
                if m.sender_uid and is_statistical(m):
                    counts[m.sender_uid] = counts.get(m.sender_uid, 0) + 1
                    shown = (m.sender_name or "").strip()
                    if shown:
                        by_name = name_tally.setdefault(m.sender_uid, {})
                        by_name[shown] = by_name.get(shown, 0) + 1
            # 与 group_identity._Acc.display_name 同一条规则：**strip 后的名字**记票，
            # 选名按次数降序、同数按名字升序。strip 这一步不能省：带尾空格的昵称若不归一，
            # 会被拆成两个桶，同一份数据里 other_name 可能选出"小明 "而成员表/互动矩阵
            # 是"小明"（身份层 _Acc.add 正是按 strip 后记票的）——"同一条规则"要的是
            # 两边真用同一条规则，不是注释里写着就算数。
            self._sender_names_cache = {
                uid: min(by.items(), key=lambda kv: (-kv[1], kv[0]))[0] for uid, by in name_tally.items()
            }
            self._sender_counts_cache = counts
        return self._sender_counts_cache

    def sender_names(self) -> dict:
        """统计口径下的 发言者 uid → 最常用的显示名（与 sender_counts 同一趟遍历算出）。

        返回的是缓存对象**本身**，调用方只读。
        """
        self.sender_counts()  # 保证两份缓存一起算出来（同一趟遍历）
        return self._sender_names_cache or {}

    def participants(self) -> list[Participant]:
        """参与者身份列表（按发言条数降序，显示名已唯一化），结果缓存在本对象上。

        与 months() 同样的安全前提：解析完成后 messages 不再变动，所以缓存只算一次。
        注意这里**不设门槛**——有一条统计口径消息就算一位；"是不是群聊"由
        multi_party_action() 按门槛判定。门槛用于判定，名单用于归属，二者不能混。
        """
        if self._participants_cache is None:
            found = collect_participants(self.statistical(), self.self_uid)
            self._participants_cache = unique_display_names(found)
        return self._participants_cache

    def months(self) -> dict[str, list["Message"]]:
        """按月分组消息（结果缓存在本对象上）。

        一次分析里月份分组会被多个维度各算一遍，每遍都要做全量时区转换 + strftime：
        数万条消息每次约几十到一百多毫秒，几个维度叠加就是白烧几百毫秒。消息列表在解析
        完成后不再变动，所以这里的缓存与 _stats_cache 同样安全。
        """
        if self._months_cache is None:
            self._months_cache = _group_by_month(self.messages)
        return self._months_cache


def load_chat(filepath: str) -> ChatData:
    """加载并解析 QQChatExporter JSON 文件"""
    import json

    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # 整份文件不是对象（null / 数字 / 数组）时，下面的 `in` 会抛
    # TypeError: argument of type 'NoneType' is not a container —— 又是一句与
    # "文件格式"毫不相干的 Python 内部错误。这里先如实拒绝。
    if not isinstance(raw, dict):
        raise ValueError("无效的 QQChatExporter JSON 格式：顶层不是一个对象")
    if "chatInfo" not in raw or "messages" not in raw:
        raise ValueError("无效的 QQChatExporter JSON 格式")

    # 下面这几处 `or {}` / `or []` / isinstance 检查不是防御性过剩：导出文件里
    # "键存在但值是 null"与"整个元素是 null"都实测出现过（撤回消息、被删的引用、
    # 手工编辑过的样例文件），而 .get("k", {}) 只挡得住**缺键**，null 会照样返回给
    # 调用方 —— 症状是上传返回 400，正文写着 'NoneType' object has no attribute 'get'，
    # 一句 Python 内部错误，用户完全不知道该改文件还是该骂工具。
    #
    # messages 必须**报错**而不是静默跳过：它是 dict/str 时 `for msg in ...` 会逐 key
    # 迭代、每个 key 都"不是对象"被计入 dropped —— 整份文件被静默吞掉（症状是
    # "共 N 条消息、0 条可分析"，total_count 还照抄 statistics），比崩更难发现。
    if not isinstance(raw.get("messages"), list):
        raise ValueError("无效的 QQChatExporter JSON 格式：messages 不是一个数组")
    chat_info = raw.get("chatInfo") or {}
    if not isinstance(chat_info, dict):
        raise ValueError("无效的 QQChatExporter JSON 格式：chatInfo 不是一个对象")
    # uid 一律强转成 str：数字型 uid（异版导出器/手工编辑写成 JSON number）若不归一，
    # 稍后 is_placeholder_sender 的 .strip() 会把整份文件崩成
    # 'int' object has no attribute 'strip'（解析坏形状不抛异常正是本函数的承诺）。
    self_uid = str(chat_info.get("selfUid") or "")
    self_name = str(chat_info.get("selfName") or "")
    stats_raw = raw.get("statistics") or {}
    if not isinstance(stats_raw, dict):
        stats_raw = {}  # 整段汇总不可用，按缺省处理（函数尾部同口径）
    senders = stats_raw.get("senders") or []
    if not isinstance(senders, list):
        senders = []

    # 缺少 selfUid 时，先按显示名从 senders 里找回自己的 UID。
    # 顺序很关键：必须先确定自己是谁，否则下面挑"对方"时会把
    # senders 里的第一条（有可能就是自己）当成对方。
    # isinstance 守卫不许省（这个循环里漏过一次的 AttributeError 是实测复现过的）。
    if not self_uid and self_name:
        for s in senders:
            if isinstance(s, dict) and s.get("name") == self_name and s.get("uid"):
                self_uid = str(s["uid"])
                break

    # 双方身份无法确定时明确报错：继续跑下去会把所有消息静默判给"对方"，
    # 统计与 AI 分析全盘失真（且用户看不出问题）。
    if not self_uid:
        raise ValueError(
            "导出文件缺少 chatInfo.selfUid/selfName，无法区分自己与对方；"
            "请用 QQChatExporter 重新导出，或在文件中补齐 selfUid"
        )

    # 确定对方的显示名（这只是**兜底值**：真正定名在下面按 other_uid 同口径重算，
    # 这里保留文件顺序的取法是为了"一条消息都没有"这类退化场景仍有名字可用。
    # 占位 sender 同样不许当这个兜底值——"对方：系统消息"在零消息文件里照样会发生）
    other_name = chat_info.get("name") or "对方"
    for s in senders:
        if (
            isinstance(s, dict)
            and str(s.get("uid") or "") != self_uid
            and s.get("name")
            and not is_placeholder_sender(str(s.get("uid") or ""), str(s["name"]))
        ):
            other_name = str(s["name"])
            break

    chat = ChatData(
        chat_name=str(chat_info.get("name") or ""),  # 键在值 null 时 .get 默认值不生效，会印成 "None"
        self_name=self_name,
        other_name=other_name,
        self_uid=self_uid,
        other_uid="",
        # 导出器自报类型（群聊为新版导出器的 "group"）；缺失时留空，判定退回"数发言者"。
        # 强转 str：异版导出器把 type 写成 JSON 数字/对象时，裸 .strip() 会把整份文件崩成
        # 'int' object has no attribute 'strip'（与上面 uid/selfUid 同一类形状问题）。
        chat_type=str(chat_info.get("type") or "").strip().lower(),
    )

    for msg in raw.get("messages") or []:
        # 整条不是对象（null / 字符串）：跳过并计入 dropped_messages，而不是让一个
        # 坏条目把几万条的导出整体判死
        if not isinstance(msg, dict):
            chat.dropped_messages += 1
            continue
        sender = msg.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        sender_uid = str(sender.get("uid") or "")
        sender_name = str(sender.get("name") or sender.get("nickname") or "")

        # 时间戳不可用（缺失/null/非数值/<=0）时丢弃该条：
        # 留着会让排序崩溃或把消息塞进 1970-01，进而多出一次无意义的 AI 调用。
        timestamp = _parse_timestamp(msg.get("timestamp"), msg.get("time", ""))
        if timestamp is None:
            chat.dropped_messages += 1
            continue

        content = msg.get("content") or {}
        if isinstance(content, str):
            # 部分导出器把 content 直接写成纯文本
            raw_text = content
            elements = []
        elif isinstance(content, dict):
            raw_text = content.get("text") or ""
            elements = content.get("elements") or []
        else:
            raw_text = ""
            elements = []
        if not isinstance(elements, list):
            elements = []

        text_parts = []
        face_ids = []
        face_names = []
        has_image = False
        is_reply = False
        image_bytes = 0
        image_count = 0
        image_ids: list[str] = []
        media_kind = ""
        media_label = ""
        media_bytes = 0
        media_id = ""
        media_path = ""
        media_w = 0
        media_h = 0
        face_url = ""
        reply_to_id = ""
        mentions: list[str] = []
        mentions_all = False
        unknown_in_msg = 0  # 本条里没命中任何分支（或整个元素非对象）的元素个数，正文兜底用

        for el in elements:
            if not isinstance(el, dict):
                unknown_in_msg += 1  # 单个元素是 null：跳过这一元素，不牵连整条消息
                continue
            # 强转 str：type 写成 JSON 对象/数组时，下面的 `el_type == "text"` 与
            # `el_type in MEDIA_KINDS` 会因不可哈希而抛 TypeError（对象不能进 set/dict 查找），
            # 整份文件崩成一句 Python 内部错误——而这条消息本可以只算"未知元素"。
            el_type = str(el.get("type") or "")
            el_data = el.get("data") or {}
            if not isinstance(el_data, dict):
                el_data = {}
            if el_type == "text":
                # `or ""` 而不是默认值：{"text": null} 时 .get("text", "") 返回的是
                # None（键存在、值是 null），下面的 "".join 会当场 TypeError
                text_parts.append(el_data.get("text") or "")
            elif el_type == "face":
                try:
                    face_ids.append(int(el_data.get("id", 0)))
                except (ValueError, TypeError):
                    pass
                fname = el_data.get("name") or ""
                if fname:
                    face_names.append(fname)
            elif el_type == "market_face":
                # 商城大表情（[[叉腰]]/[13]…）：与系统表情同类信号，按表情口径统计；
                # 顺带记下它的 CDN 地址——"表情原图"这个可选功能靠它取图。
                # 注意放独立字段：同一消息里若同时有图片与表情，media_path 会归属图片。
                fname = _clean_face_name(el_data.get("name") or "")
                if fname:
                    face_names.append(fname)
                face_url = face_url or str(el_data.get("url") or "")
            elif el_type == "image":
                has_image = True
                # 图片自己记一份字节/张数/md5：media_* 那几个字段是"这条消息的第一个
                # 媒体元素"，一条既有图片又有文件的消息里它可能属于那个文件。
                # 少了这组分家口径，统计层只能拿合计硬套到图片上（就是双计的来源）。
                img_size = _to_int(el_data.get("size"))
                image_bytes += img_size
                image_count += 1
                media_bytes += img_size
                img_md5 = str(el_data.get("md5") or "")
                if img_md5:
                    image_ids.append(img_md5)
                media_id = media_id or img_md5
                media_path = media_path or str(el_data.get("url") or el_data.get("localPath") or "")
                media_w = media_w or _to_int(el_data.get("width"))
                media_h = media_h or _to_int(el_data.get("height"))
            elif el_type == "reply":
                is_reply = True
                # 只给被回复消息的 id：发言人要在全量消息里回查（见下面的回填）
                reply_to_id = str(el_data.get("referencedMessageId") or "")
            elif el_type == "at":
                # @ 是**明确的点名**，比"相邻两条换人"这种推断强得多（群聊互动分析用它）。
                # atType=1 / uid="all" 是 @全体成员：它不是某个人，单独记一个标记，
                # 绝不能塞进 mentions——否则"全体成员"会被当成一位成员去统计。
                auid = str(el_data.get("uid") or "")
                if auid in ("", "all") or _to_int(el_data.get("atType")) == 1:
                    mentions_all = True
                elif auid not in mentions:
                    mentions.append(auid)
            elif el_type in MEDIA_KINDS:
                # 文件/视频/转发卡片/红包/表情气泡/通话/卡片：让它们参与统计与 AI 分析
                # （正文仍保持干净，不塞占位符）。
                # **字节逐个记**：media_kind/media_label 仍是"第一个非图片媒体"（标签与
                # 按类计数是单条消息一个语义），但 media_bytes 若也只记第一个，一条"两个
                # 文件"的消息会把后一个的字节凭空丢掉——统计层的 other_bytes = 合计-图片，
                # 漏进的字节就哪个口径都找不回（旧注释还把这份合计说成"所有媒体的合计"）。
                media_bytes += _to_int(el_data.get("size"))
                if not media_kind:
                    media_kind = el_type
                    media_label = _media_label(el_type, el_data, raw_text)
                    media_id = media_id or str(el_data.get("md5") or "")
                    media_path = media_path or str(el_data.get("url") or el_data.get("localPath") or "")
            else:
                # 未知元素类型：不再静默吞掉（旧实现里 elif 链没有尾巴，导出器哪天
                # 改名 "text"，正文会丢光而统计照常出数——谁都看不见事故发生了）。
                # 记一次类型与次数，供仪表盘提示条与 inspect_chat 对账；这里**不**
                # 猜语义，猜错了比不猜更贵（会污染统计口径）。
                unknown_in_msg += 1
                if el_type:
                    chat.unknown_element_types[el_type] = chat.unknown_element_types.get(el_type, 0) + 1

        clean_text = "".join(text_parts).strip()
        # 没有结构化 text 元素但原始文本存在时（如无 elements 的纯文本消息），
        # 回退到原始文本，避免消息内容丢失；有 elements 的消息不回落，
        # 以免把 "[图片]" 之类的占位符当成正文统计。
        # 唯一的例外：elements 非空但**每个**元素都无法识别（格式漂移的征兆，
        # 比如导出器把 "text" 改了名）——此时结构化侧什么都给不出来，
        # 再不回落正文就整条丢光。识别成功过半的消息不回落（占位符污染照旧防住）。
        if not clean_text and raw_text and elements and unknown_in_msg == len(elements):
            clean_text = raw_text.strip()
        elif not clean_text and not elements and raw_text:
            clean_text = raw_text.strip()

        parsed = Message(
            # id 必须与 reply_to_id 同一个类型口径：下面 :618 那类取值处把
            # referencedMessageId 用 str() 归一了，而这里的 msg["id"] 若照原样收
            # （数字导出就是 int），回填时 `uid_by_id = {m.id: ...}` 建的是 int 键、
            # 查的是 str → 每条回复都查不到人，reply_to_uid 全空。
            # 症状是静默的：精确回复是群聊互动里最强的信号，它整体归零后被计入
            # reply_unresolved，显式互动矩阵全空，成员画像拿到"0 次"，而文件照样解析成功。
            id=str(msg.get("id") or ""),
            timestamp=timestamp,
            # 时间字符串一律由时间戳（权威字段）按北京时间重算：新版导出器的 time
            # 是 UTC ISO（"2024-01-01T00:00:00.000Z"），直接照抄会让 AI 对话行
            # 比统计头（CST）早 8 小时——"凌晨三点还在聊"会被读成下午，直接影响判断。
            time_str=datetime.fromtimestamp(timestamp / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
            sender_name=sender_name,
            sender_uid=sender_uid,
            text=clean_text,
            raw_text=raw_text,
            # 同上：msg["type"] 被 is_statistical 放进 set 里做成员判断，
            # 非字符串（数字/对象/数组）会导致 TypeError 或静默判错，一律归一。
            msg_type=str(msg.get("type") or ""),
            has_image=has_image,
            is_reply=is_reply,
            media_kind=media_kind,
            media_label=media_label,
            media_bytes=media_bytes,
            media_id=media_id,
            media_path=media_path,
            media_w=media_w,
            media_h=media_h,
            face_url=face_url,
            face_ids=face_ids,
            face_names=face_names,
            image_bytes=image_bytes,
            image_count=image_count,
            image_ids=image_ids,
            recalled=bool(msg.get("recalled", False)),
            system=bool(msg.get("system", False)),
            reply_to_id=reply_to_id,
            mentions=mentions,
            mentions_all=mentions_all,
        )
        chat.messages.append(parsed)

    chat.messages.sort(key=lambda m: m.timestamp)

    # 回填"这条回复的是谁"：reply 元素只给被回复消息的 id，要在全量消息里查一次发言人。
    # 必须等所有消息都解析完再做——被引用消息可能排在引用它的消息之后（实测绝大多数都能回查到）。
    # 查不到时 reply_to_uid 留空，由统计层如实计入"未解析回复"，不猜、不丢。
    if any(m.reply_to_id for m in chat.messages):
        uid_by_id = {m.id: m.sender_uid for m in chat.messages if m.id}
        for m in chat.messages:
            if m.reply_to_id:
                m.reply_to_uid = uid_by_id.get(m.reply_to_id, "")

    # 多人（群聊）防线：本工具的历史口径是"我 vs 对方"两类。若导入群聊导出，
    # 除 self 外的所有人都会静默并进"对方"名下——条数、回复速度、锐评全部失真，
    # 而且用户在界面上完全看不出来。宁可报错也不给出错误的分析。
    # 判定口径见 _multi_party_offenders：只看统计口径下的消息，且第三方需有实质发言。
    # 处置方式由 multi_party_action 统一给出（none/reject/merge/group）：
    # 默认 auto 在群聊轨就绪前仍是 reject，因此升级本版本**不改变任何既有行为**。
    action = multi_party_action(chat)
    if action == "reject":
        offenders = _multi_party_offenders(chat)
        total = sum(_statistical_sender_counts(chat).values()) or 1
        detail = "、".join(f"{n} 条（{n / total:.1%}）" for _uid, n in offenders[:3])
        raise ValueError(
            f"检测到 {len(offenders) + 2} 位有实质发言的参与者，这看起来是群聊导出，"
            f"而本工具只支持两人私聊：所有「其他人」会被并进「对方」名下，统计与 AI "
            f"分析都会失真（第三方发言：{detail}）。把环境变量 QQCHAT_GROUP_CHAT 设为 auto "
            "可以按群聊分别统计每位成员；设为 two_party 仍按「我 vs 其他人」两分类归并。"
            "（当前是 off，即升级前的拒收行为。）"
        )

    # 确定"对方"是谁：uid 与显示名必须同口径，都取自"统计口径下发言最多的那一方"。
    # uid 侧早就改成按发言量选了（旧实现取"文件中第一个非自己的 sender"，占位 sender
    # 一旦排在前面就被认成对方），但**名字侧当时漏了**：它仍按 statistics.senders 的
    # 文件顺序取第一条非自己项。两份口径不一致时，other_uid 指向真人、other_name 却是
    # "系统消息"——而 other_name 流向仪表盘"对方"标签、日志，以及喂给模型的对话里
    # 所有非我方行的署名（dialog._build_dialog 用它当显示名）。等于让模型把真人说的话
    # 标成"系统消息"，比数字算错更难被发现。
    # 占位 sender 在这里同样要排除：它可能"发言 1 条"因而选不上，也可能在一份
    # 全被过滤掉的空记录里成为唯一的非自己项。
    counts = _statistical_sender_counts(chat)
    names = chat.sender_names()
    ranked = sorted(
        (
            (uid, n)
            for uid, n in counts.items()
            if uid != self_uid and not is_placeholder_sender(uid, names.get(uid, ""))
        ),
        # 与 collect_participants 同一条 tie-break（发言量降序、同数按 uid 升序）：
        # 只按条数排的话，并列时"谁是第一名"退化成 counts 的插入顺序（=最早发言顺序），
        # 两套系统对同一份文件可以各认各的"对方"。确定性不该分两套。
        key=lambda kv: (-kv[1], kv[0]),
    )
    if ranked:
        chat.other_uid = ranked[0][0]
        # 名字优先取消息里实际用得最多的那个（比 statistics.senders 的汇总更贴近文件
        # 本体），退而查 senders 里该 uid 的名字，最后才用 chatInfo.name 兜底。
        sender_name = ""
        for s in senders:
            if isinstance(s, dict) and str(s.get("uid") or "") == chat.other_uid and s.get("name"):
                sender_name = str(s["name"])
                break
        chat.other_name = names.get(chat.other_uid) or sender_name or other_name

    if action == "group":
        # 群聊：没有单一的"对方"。这里**保留字段**（不改 property，见方案 B2）而不是
        # 让它悬空——日志与第三方调用点仍会读它。other_name 用群名，other_uid 置空，
        # 让"群聊里没有对方"这件事在数据上就是显式的；群聊轨一律改用 participants()。
        chat.is_group_chat = True
        chat.mode = "group"
        chat.other_uid = ""
        chat.other_name = chat.chat_name or "群聊"
    elif action == "merge":
        # 旧逃生阀：仍按"我 vs 其他人"两分类，如实记下这次的口径，便于排查
        chat.mode = "two_party"

    stats = raw.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}
    chat.total_count = _to_int(stats.get("totalMessages")) or len(chat.messages)
    time_range = stats.get("timeRange") or {}
    if not isinstance(time_range, dict):
        time_range = {}
    # 日期字段是字符串，原样保留但挡掉 null（模板与报告页会直接打印它，None 会印成
    # 一个 "None" 而不是空）
    chat.time_start = time_range.get("start") or ""
    chat.time_end = time_range.get("end") or ""
    # durationDays 必须过 _to_int：这是本函数里唯一一个直接采信文件数值的字段，
    # 而导出器把数字写成字符串是常态（media 的 size 同样如此，所以那边过了 _to_int）。
    # 没归一时 "31" 会一路带到 calc_overview 的除法里 → TypeError: unsupported operand
    # type(s) for /: 'int' and 'str' → 整份文件的统计全废，而界面上的症状只是
    # "上传说成功，仪表盘却把我踢回首页"。
    # 负数同样挡掉（_to_int 夹到 0）：-5 会算出负的日均消息数，页面平静地显示它。
    # 0 是安全值——calc_overview 见 0 会改按首末消息自己算跨度（days_basis=computed）。
    chat.duration_days = _to_int(time_range.get("durationDays"))

    return chat


def _group_by_month(messages: list[Message]) -> dict[str, list[Message]]:
    """按北京时间把消息装进 {"2024-01": [...]}，键已排序"""
    groups: dict[str, list[Message]] = {}
    for msg in messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        key = dt.strftime("%Y-%m")
        if key not in groups:
            groups[key] = []
        groups[key].append(msg)
    return dict(sorted(groups.items()))


def split_by_month(chat: ChatData) -> dict[str, list[Message]]:
    """按月分组消息，返回 {"2024-01": [messages]}

    走 ChatData.months() 的实例级缓存：同一次分析里多个维度会各调一次，
    缓存后只有第一次真正遍历消息。返回的字典是缓存对象本身——调用方只读它，
    需要改写请自行复制。
    """
    return chat.months()
