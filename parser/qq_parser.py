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

# 不参与统计与 AI 分析的消息类型：type_11（合并转发）、type_23（小程序卡片）、system（部分缺 system 标记）。
# 保留 type_17（商城大表情）与 forward/json（走 MEDIA_KINDS 受控短标签）。
SKIP_MSG_TYPES = {"type_11", "type_23", "system"}

# 非文本媒体元素 → 中文标签
MEDIA_KINDS = {
    "file": "文件",
    "video": "视频",
    "forward": "转发",
    "wallet": "红包",
    "face_bubble": "表情气泡",
    "markdown": "Markdown消息",
    "json": "卡片消息",
    "av_record": "通话",
    "voice": "语音",  # 语音消息本体（按 MEDIA_LABEL_MAX 展示 duration/summary）
}
# 媒体标签的长度上限：文件名/转发标题可能很长，截断保留可读性
MEDIA_LABEL_MAX = 40

# 回退解析 time 字符串时支持的格式（导出器为 "%Y-%m-%d %H:%M:%S"）
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _to_int(value) -> int:
    """将体积等数值字段安全转为非负整数（含 Infinity → OverflowError），失败返回 0。"""
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
    """兼容旧开关 QQCHAT_ALLOW_MULTI_PARTY（作为 two_party 别名，走 config.env_bool 解析）。"""
    return env_bool("QQCHAT_ALLOW_MULTI_PARTY", False)


#: 群聊轨对外开放开关（配合 QQCHAT_GROUP_CHAT=auto|off|two_party 控制多人导出策略）
GROUP_TRACK_READY = True

#: QQCHAT_GROUP_CHAT 合法值：auto=按群聊解析，off=维持拒收，two_party=我 vs 其他人两分类归并
_GROUP_MODES = ("auto", "off", "two_party")


def group_chat_mode() -> str:
    """返回多人导出处置模式（优先读 QQCHAT_GROUP_CHAT，兼容 QQCHAT_ALLOW_MULTI_PARTY）。"""
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
    """多人防线处置：none | reject | merge | group。

    按门槛判定的实质第三方优先；无实质第三方但导出器声明 chatInfo.type='group' 且处于 auto 时也按 group 处理。
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


# 排除系统占位发言者后，第三方达到条数与占比门槛才视为实质多人参与者
MULTI_PARTY_MIN_MESSAGES = 3
MULTI_PARTY_MIN_SHARE = 0.005


def _statistical_sender_counts(chat: "ChatData") -> dict:
    """返回统计口径下的只读发言者计数缓存（见 ChatData.sender_counts）。"""
    return chat.sender_counts()


def _multi_party_offenders(chat: "ChatData") -> list[tuple[str, int]]:
    """返回除自己与主要对话方外达到门槛的第三方 (uid, count) 列表（已过滤占位 sender）。"""
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


#: 合法毫秒时间戳区间（约 2001~2033），用于识别并换算秒/微秒/纳秒错位单位，避免落入 1970-01 或触发 OSError
_MS_MIN = 1_000_000_000_000
_SEC_MIN, _SEC_MAX = 1_000_000_000, 2_000_000_000
_USEC_MIN, _USEC_MAX = 1_000_000_000_000_000, 2_000_000_000_000_000
_NSEC_MIN, _NSEC_MAX = 1_000_000_000_000_000_000, 2_000_000_000_000_000_000


def _normalise_epoch_ms(ts: int) -> Optional[int]:
    """将秒/微秒/纳秒量级的时间戳归一到毫秒合法区间；无法归一回 None。"""
    if _MS_MIN <= ts <= 2_000_000_000_000:
        return ts
    if _SEC_MIN <= ts <= _SEC_MAX:
        return ts * 1000
    if _USEC_MIN <= ts <= _USEC_MAX:
        return ts // 1000
    if _NSEC_MIN <= ts <= _NSEC_MAX:
        return ts // 1_000_000
    return None


def _parse_timestamp(value, time_str: str = "") -> Optional[int]:
    """统一时间戳为毫秒整数：优先数值并做量级归一，失败回退 time_str，均失败返回 None。"""
    try:
        ts = int(float(value))
        if ts > 0:
            normalised = _normalise_epoch_ms(ts)
            if normalised is not None:
                return normalised
    except (TypeError, ValueError, OverflowError):
        # 含 Infinity（OverflowError）等非法数值时回退到 time 字符串解析
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
    image_items: list[dict] = field(default_factory=list)  # 各图片元素的 path/md5/w/h 列表


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
    #: 密集展示矩阵按生效上限缓存；group_stats 限制最多保留 3 个不同展示尺寸。
    _matrix_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    # Browse/evidence indexes follow the parsed object's replacement/deletion lifecycle.
    _message_index: Optional[object] = field(default=None, repr=False, compare=False)
    #: 稀疏互动结果：实际出现的有向边与全员统计，作为画像和展示矩阵的共同数据源。
    _interaction_sparse_cache: Optional[dict] = field(default=None, repr=False, compare=False)
    #: 成员画像会逐人读取互动统计；缓存一次全群活跃表，避免每位成员都重扫全部消息。
    _member_activity_cache: Optional[list] = field(default=None, repr=False, compare=False)

    def statistical(self) -> list["Message"]:
        """参与统计与分析的消息子集（过滤系统/撤回/转发）"""
        return [m for m in self.messages if is_statistical(m)]

    def sender_counts(self) -> dict:
        """统计口径下的 发言者 uid → 条数（结果缓存在本对象上）。

        这份计数有三处消费点：多人防线的门槛判定（_multi_party_offenders）、
        拒收报错的占比文案、以及"谁是对方"（取发言最多的一方）。原先各调一次
        _statistical_sender_counts，数万条消息就是两到四遍全量遍历。
        与 months()/participants() 同样的安全前提：解析完成后 messages 不再变动。

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
            # 与 group_identity._Acc.display_name 保持一致：strip 后按次数降序、同数按名称升序取代表名
            self._sender_names_cache = {
                uid: min(by.items(), key=lambda kv: (-kv[1], kv[0]))[0] for uid, by in name_tally.items()
            }
            self._sender_counts_cache = counts
        return self._sender_counts_cache

    def sender_names(self) -> dict:
        """返回统计口径下的只读 uid → 常用显示名缓存（与 sender_counts 同趟计算）。"""
        self.sender_counts()
        return self._sender_names_cache or {}

    def participants(self) -> list[Participant]:
        """参与者列表（按发言降序且显示名已唯一化，不设数量门槛；结果实例内缓存）。"""
        if self._participants_cache is None:
            found = collect_participants(self.statistical(), self.self_uid)
            self._participants_cache = unique_display_names(found)
        return self._participants_cache

    def months(self) -> dict[str, list["Message"]]:
        """按 CST 月份分组的消息字典（解析后只读，结果实例内缓存）。"""
        if self._months_cache is None:
            self._months_cache = _group_by_month(self.messages)
        return self._months_cache


def load_chat(filepath: str) -> ChatData:
    """加载并解析 QQChatExporter JSON 文件"""
    import json

    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError("无效的 QQChatExporter JSON 格式：顶层不是一个对象")
    if "chatInfo" not in raw or "messages" not in raw:
        raise ValueError("无效的 QQChatExporter JSON 格式")

    # messages 非数组时必须报错，避免 for 迭代 dict/str 静默吞掉整份记录；其余键为 null 时安全回退
    if not isinstance(raw.get("messages"), list):
        raise ValueError("无效的 QQChatExporter JSON 格式：messages 不是一个数组")
    chat_info = raw.get("chatInfo") or {}
    if not isinstance(chat_info, dict):
        raise ValueError("无效的 QQChatExporter JSON 格式：chatInfo 不是一个对象")
    # uid 强转为 str，避免数字型 uid 在后续 .strip()/查找中触发类型错误
    self_uid = str(chat_info.get("selfUid") or "")
    self_name = str(chat_info.get("selfName") or "")
    stats_raw = raw.get("statistics") or {}
    if not isinstance(stats_raw, dict):
        stats_raw = {}
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
        image_items: list[dict] = []
        media_kind = ""
        media_label = ""
        media_bytes = 0
        media_id = ""
        media_path = ""
        media_w = 0
        media_h = 0
        non_image_id = ""
        non_image_path = ""
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
                img_path = str(el_data.get("url") or el_data.get("localPath") or "")
                img_w = _to_int(el_data.get("width"))
                img_h = _to_int(el_data.get("height"))
                if img_md5:
                    image_ids.append(img_md5)
                image_items.append({"path": img_path, "id": img_md5, "w": img_w, "h": img_h})
                media_id = media_id or img_md5
                media_path = media_path or img_path
                media_w = media_w or img_w
                media_h = media_h or img_h
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
                # media_kind/label 取首个非图片媒体；media_bytes 累加所有非图片媒体字节，避免多文件消息漏计
                media_bytes += _to_int(el_data.get("size"))
                if not media_kind:
                    media_kind = el_type
                    media_label = _media_label(el_type, el_data, raw_text)
                    non_image_id = str(el_data.get("md5") or "")
                    non_image_path = str(el_data.get("url") or el_data.get("localPath") or "")
            else:
                # 记录未识别元素类型频次供格式漂移探测，不猜测语义
                unknown_in_msg += 1
                if el_type:
                    chat.unknown_element_types[el_type] = chat.unknown_element_types.get(el_type, 0) + 1

        clean_text = "".join(text_parts).strip()
        # 无结构化 text 时仅在纯文本或 elements 全部未知时回退 raw_text，防止把 "[图片]" 占位符混入正文
        if not clean_text and raw_text and elements and unknown_in_msg == len(elements):
            clean_text = raw_text.strip()
        elif not clean_text and not elements and raw_text:
            clean_text = raw_text.strip()

        if not has_image:
            media_id = media_id or non_image_id
            media_path = media_path or non_image_path

        parsed = Message(
            # id 与 reply_to_id 统一转 str，防止数字 ID 与字符串引用匹配失败
            id=str(msg.get("id") or ""),
            timestamp=timestamp,
            # 时间统一按北京时间重算，屏蔽导出器 UTC ISO 字符串造成的 8 小时偏差
            time_str=datetime.fromtimestamp(timestamp / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
            sender_name=sender_name,
            sender_uid=sender_uid,
            text=clean_text,
            raw_text=raw_text,
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
            image_items=image_items,
        )
        chat.messages.append(parsed)

    chat.messages.sort(key=lambda m: m.timestamp)

    # 回填"这条回复的是谁"：reply 元素只给被回复消息的 id，要在全量消息里查一次发言人。
    # 必须等所有消息都解析完再做——被引用消息可能排在引用它的消息之后（实测绝大多数都能回查到）。
    # 查不到或同一 id 对应多位不同发言人时 reply_to_uid 留空，由统计层如实计入"未解析回复"，不猜、不丢。
    if any(m.reply_to_id for m in chat.messages):
        _unset = object()
        global_uid: dict[str, Optional[str]] = {}
        for m in chat.messages:
            if not m.id:
                continue
            prev = global_uid.get(m.id, _unset)
            if prev is _unset:
                global_uid[m.id] = m.sender_uid
            elif prev != m.sender_uid:
                global_uid[m.id] = None

        seen_uid: dict[str, Optional[str]] = {}
        for m in chat.messages:
            if m.reply_to_id:
                resolved = seen_uid.get(m.reply_to_id, _unset)
                if resolved is _unset:
                    resolved = global_uid.get(m.reply_to_id, "")
                m.reply_to_uid = resolved or ""
            if m.id:
                prev_seen = seen_uid.get(m.id, _unset)
                if prev_seen is _unset:
                    seen_uid[m.id] = m.sender_uid
                elif prev_seen != m.sender_uid:
                    seen_uid[m.id] = None

    # 仅保留统计口径且非占位的第三方，按 multi_party_action 决定放行/归并/群聊/拒收
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

    # 对方 uid 与显示名均取统计口径下发言最多且非占位的另一方
    counts = _statistical_sender_counts(chat)
    names = chat.sender_names()
    ranked = sorted(
        (
            (uid, n)
            for uid, n in counts.items()
            if uid != self_uid and not is_placeholder_sender(uid, names.get(uid, ""))
        ),
        # 同 collect_participants 保持确定性 tie-break（条数降序、同数按 uid 升序）
        key=lambda kv: (-kv[1], kv[0]),
    )
    if ranked:
        chat.other_uid = ranked[0][0]
        sender_name = ""
        for s in senders:
            if isinstance(s, dict) and str(s.get("uid") or "") == chat.other_uid and s.get("name"):
                sender_name = str(s["name"])
                break
        chat.other_name = names.get(chat.other_uid) or sender_name or other_name

    if action == "group":
        # 群聊无单一“对方”：保留字段为群名/空串以兼容旧读取点，成员走 participants()
        chat.is_group_chat = True
        chat.mode = "group"
        chat.other_uid = ""
        chat.other_name = chat.chat_name or "群聊"
    elif action == "merge":
        chat.mode = "two_party"

    stats = raw.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}
    chat.total_count = _to_int(stats.get("totalMessages")) or len(chat.messages)
    time_range = stats.get("timeRange") or {}
    if not isinstance(time_range, dict):
        time_range = {}
    chat.time_start = time_range.get("start") or ""
    chat.time_end = time_range.get("end") or ""
    # durationDays 统一转非负整数（0 时由 calc_overview 按首末消息计算跨度）
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
