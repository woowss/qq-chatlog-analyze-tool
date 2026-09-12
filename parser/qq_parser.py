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

from parser.group_identity import Participant, collect_participants, unique_display_names

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
}
# 媒体标签的长度上限：文件名/转发标题可能很长，截断保留可读性
MEDIA_LABEL_MAX = 40

# 回退解析 time 字符串时支持的格式（导出器为 "%Y-%m-%d %H:%M:%S"）
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _to_int(value) -> int:
    """导出器把体积写成字符串，解析失败按 0 处理（统计不该被脏字段打断）"""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
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
    return ""


def _allow_multi_party() -> bool:
    """旧的 QQCHAT_ALLOW_MULTI_PARTY 开关（调用时读 env，方便测试与临时放行）。

    新口径统一由 group_chat_mode() 表达；本函数只保留"旧变量怎么解析"这唯一一处，
    作为 two_party 模式的别名来源。旧文档与旧用户配置都指向这个变量，不能删。
    """
    return (os.getenv("QQCHAT_ALLOW_MULTI_PARTY", "") or "").strip().lower() in ("1", "true", "yes", "on")


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
# 实测某份 数万条私聊导出里，若干占位消息中就有 1 条 type_23（商城表情）没有
# system 标记。若按"出现过就算一位"，正常私聊会被判成群聊直接拒收（这正是把
# 判定口径收紧到 is_statistical + 设门槛的原因）。
MULTI_PARTY_MIN_MESSAGES = 3
MULTI_PARTY_MIN_SHARE = 0.005


def _statistical_sender_counts(chat: "ChatData") -> dict:
    """统计口径下的发言者 → 条数（只算真正会进分析与统计的消息）"""
    counts: dict[str, int] = {}
    for m in chat.messages:
        if m.sender_uid and is_statistical(m):
            counts[m.sender_uid] = counts.get(m.sender_uid, 0) + 1
    return counts


def _multi_party_offenders(chat: "ChatData") -> list[tuple[str, int]]:
    """返回「除自己与主要对话方之外、且有实质发言」的第三方（条数降序）。

    私聊返回空列表；群聊会返回其余参与者，由调用方决定是否拒收。
    """
    counts = _statistical_sender_counts(chat)
    others = {uid: n for uid, n in counts.items() if uid != chat.self_uid}
    if len(others) <= 1:
        return []
    total = sum(counts.values()) or 1
    threshold = max(MULTI_PARTY_MIN_MESSAGES, int(total * MULTI_PARTY_MIN_SHARE))
    ranked = sorted(others.items(), key=lambda kv: kv[1], reverse=True)
    return [(uid, n) for uid, n in ranked[1:] if n >= threshold]


def _parse_timestamp(value, time_str: str = "") -> Optional[int]:
    """把导出文件里的时间戳统一成毫秒整数；无法解析时返回 None。

    导出文件可能存在缺失/为 null/是字符串的时间戳。缺键时会 get 到 0，
    若照单全收，该消息会被归入 1970-01 并作为一个"月份"送去 AI 分析；
    为 null 或字符串时则会让排序/统计直接抛异常。这里统一兜底：
    先按数值解析，失败再用 time 字符串回退，两者都不可用则返回 None（调用方丢弃该条）。
    """
    try:
        ts = int(float(value))
        if ts > 0:
            return ts
    except (TypeError, ValueError):
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
    dropped_messages: int = 0  # 因时间戳不可用被丢弃的消息数（0 表示全部可用）
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

    def statistical(self) -> list["Message"]:
        """参与统计与分析的消息子集（过滤系统/撤回/转发）"""
        return [m for m in self.messages if is_statistical(m)]

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

    if "chatInfo" not in raw or "messages" not in raw:
        raise ValueError("无效的 QQChatExporter JSON 格式")

    chat_info = raw["chatInfo"]
    self_uid = chat_info.get("selfUid", "")
    self_name = chat_info.get("selfName", "")
    senders = raw.get("statistics", {}).get("senders", [])

    # 缺少 selfUid 时，先按显示名从 senders 里找回自己的 UID。
    # 顺序很关键：必须先确定自己是谁，否则下面挑"对方"时会把
    # senders 里的第一条（有可能就是自己）当成对方。
    if not self_uid and self_name:
        for s in senders:
            if s.get("name") == self_name and s.get("uid"):
                self_uid = s["uid"]
                break

    # 双方身份无法确定时明确报错：继续跑下去会把所有消息静默判给"对方"，
    # 统计与 AI 分析全盘失真（且用户看不出问题）。
    if not self_uid:
        raise ValueError(
            "导出文件缺少 chatInfo.selfUid/selfName，无法区分自己与对方；"
            "请用 QQChatExporter 重新导出，或在文件中补齐 selfUid"
        )

    # 确定对方的显示名
    other_name = chat_info.get("name", "对方")
    for s in senders:
        if s.get("uid") != self_uid and s.get("name"):
            other_name = s["name"]
            break

    chat = ChatData(
        chat_name=chat_info.get("name", ""),
        self_name=self_name,
        other_name=other_name,
        self_uid=self_uid,
        other_uid="",
        # 导出器自报类型（群聊为新版导出器的 "group"）；缺失时留空，判定退回"数发言者"
        chat_type=(chat_info.get("type") or "").strip().lower(),
    )

    for msg in raw.get("messages", []):
        sender = msg.get("sender", {})
        sender_uid = sender.get("uid", "")
        sender_name = sender.get("name", "") or sender.get("nickname", "")

        # 时间戳不可用（缺失/null/非数值/<=0）时丢弃该条：
        # 留着会让排序崩溃或把消息塞进 1970-01，进而多出一次无意义的 AI 调用。
        timestamp = _parse_timestamp(msg.get("timestamp"), msg.get("time", ""))
        if timestamp is None:
            chat.dropped_messages += 1
            continue

        content = msg.get("content", {})
        if isinstance(content, str):
            # 部分导出器把 content 直接写成纯文本
            raw_text = content
            elements = []
        elif isinstance(content, dict):
            raw_text = content.get("text", "")
            elements = content.get("elements", [])
        else:
            raw_text = ""
            elements = []

        text_parts = []
        face_ids = []
        face_names = []
        has_image = False
        is_reply = False
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

        for el in elements:
            el_type = el.get("type", "")
            el_data = el.get("data", {}) or {}
            if el_type == "text":
                text_parts.append(el_data.get("text", ""))
            elif el_type == "face":
                try:
                    face_ids.append(int(el_data.get("id", 0)))
                except (ValueError, TypeError):
                    pass
                fname = el_data.get("name", "")
                if fname:
                    face_names.append(fname)
            elif el_type == "market_face":
                # 商城大表情（[[叉腰]]/[13]…）：与系统表情同类信号，按表情口径统计；
                # 顺带记下它的 CDN 地址——"表情原图"这个可选功能靠它取图。
                # 注意放独立字段：同一消息里若同时有图片与表情，media_path 会归属图片。
                fname = _clean_face_name(el_data.get("name", ""))
                if fname:
                    face_names.append(fname)
                face_url = face_url or str(el_data.get("url") or "")
            elif el_type == "image":
                has_image = True
                media_bytes += _to_int(el_data.get("size"))
                media_id = media_id or str(el_data.get("md5") or "")
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
            elif el_type in MEDIA_KINDS and not media_kind:
                # 文件/视频/转发卡片/红包/表情气泡/通话/卡片：记下类型与短标签，
                # 让它们参与统计与 AI 分析（正文仍保持干净，不塞占位符）
                media_kind = el_type
                media_label = _media_label(el_type, el_data, raw_text)
                media_bytes += _to_int(el_data.get("size"))
                media_id = media_id or str(el_data.get("md5") or "")
                media_path = media_path or str(el_data.get("url") or el_data.get("localPath") or "")

        clean_text = "".join(text_parts).strip()
        # 没有结构化 text 元素但原始文本存在时（如无 elements 的纯文本消息），
        # 回退到原始文本，避免消息内容丢失；有 elements 的消息不回落，
        # 以免把 "[图片]" 之类的占位符当成正文统计。
        if not clean_text and not elements and raw_text:
            clean_text = raw_text.strip()

        parsed = Message(
            id=msg.get("id", ""),
            timestamp=timestamp,
            # 时间字符串一律由时间戳（权威字段）按北京时间重算：新版导出器的 time
            # 是 UTC ISO（"2024-01-01T00:00:00.000Z"），直接照抄会让 AI 对话行
            # 比统计头（CST）早 8 小时——"凌晨三点还在聊"会被读成下午，直接影响判断。
            time_str=datetime.fromtimestamp(timestamp / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
            sender_name=sender_name,
            sender_uid=sender_uid,
            text=clean_text,
            raw_text=raw_text,
            msg_type=msg.get("type", ""),
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

    # 确定对方的 UID：交给"统计口径下发言最多的一方"。
    # 旧实现取"文件中第一个非自己的 sender"，但导出文件里常混入占位 sender
    # （name="系统消息"、uid 形如"未知…"），它一旦排在真实对话方之前就会被
    # 误认成"对方"——任何依赖该字段的功能都会静默指错人。
    counts = _statistical_sender_counts(chat)
    ranked = sorted(
        ((uid, n) for uid, n in counts.items() if uid != self_uid), key=lambda kv: kv[1], reverse=True
    )
    if ranked:
        chat.other_uid = ranked[0][0]

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

    stats = raw.get("statistics", {})
    chat.total_count = stats.get("totalMessages", len(chat.messages))
    time_range = stats.get("timeRange", {})
    chat.time_start = time_range.get("start", "")
    chat.time_end = time_range.get("end", "")
    chat.duration_days = time_range.get("durationDays", 0)

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
