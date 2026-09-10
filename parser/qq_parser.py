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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# 北京时间固定偏移，供月份分组与本地统计共用，避免口径不一致
CST = timezone(timedelta(hours=8))

# 不应进入统计与 AI 分析的消息类型：合并转发 / 频道类 / 商城表情等
SKIP_MSG_TYPES = {"type_11", "type_17", "type_23"}

# 回退解析 time 字符串时支持的格式（导出器为 "%Y-%m-%d %H:%M:%S"）
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


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
    return None


@dataclass
class Message:
    """单条消息"""
    id: str
    timestamp: int          # 毫秒时间戳
    time_str: str           # "2025-09-16 21:56:49"
    sender_name: str        # 发送者显示名
    sender_uid: str         # 发送者 UID
    text: str               # 纯文本（不含图片/表情标记）
    raw_text: str           # 原始文本（含占位符）
    msg_type: str           # type_1 / type_3 / type_11
    has_image: bool
    is_reply: bool
    face_ids: list[int] = field(default_factory=list)
    face_names: list[str] = field(default_factory=list)  # 表情名称
    recalled: bool = False  # 已被撤回（导出器仍会保留该条目）
    system: bool = False    # 系统提示消息（"对方撤回了一条消息"等）


def is_statistical(m: "Message") -> bool:
    """是否应进入统计与 AI 分析：排除系统消息、撤回消息与转发类消息"""
    return (not m.system and not m.recalled
            and m.msg_type not in SKIP_MSG_TYPES)


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
    dropped_messages: int = 0   # 因时间戳不可用被丢弃的消息数（0 表示全部可用）

    def statistical(self) -> list["Message"]:
        """参与统计与分析的消息子集（过滤系统/撤回/转发）"""
        return [m for m in self.messages if is_statistical(m)]


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

        if sender_uid != self_uid and not chat.other_uid:
            chat.other_uid = sender_uid

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

        for el in elements:
            el_type = el.get("type", "")
            el_data = el.get("data", {})
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
            elif el_type == "image":
                has_image = True
            elif el_type == "reply":
                is_reply = True

        clean_text = "".join(text_parts).strip()
        # 没有结构化 text 元素但原始文本存在时（如无 elements 的纯文本消息），
        # 回退到原始文本，避免消息内容丢失；有 elements 的消息不回落，
        # 以免把 "[图片]" 之类的占位符当成正文统计。
        if not clean_text and not elements and raw_text:
            clean_text = raw_text.strip()

        parsed = Message(
            id=msg.get("id", ""),
            timestamp=timestamp,
            time_str=msg.get("time", ""),
            sender_name=sender_name,
            sender_uid=sender_uid,
            text=clean_text,
            raw_text=raw_text,
            msg_type=msg.get("type", ""),
            has_image=has_image,
            is_reply=is_reply,
            face_ids=face_ids,
            face_names=face_names,
            recalled=bool(msg.get("recalled", False)),
            system=bool(msg.get("system", False)),
        )
        chat.messages.append(parsed)

    chat.messages.sort(key=lambda m: m.timestamp)

    stats = raw.get("statistics", {})
    chat.total_count = stats.get("totalMessages", len(chat.messages))
    time_range = stats.get("timeRange", {})
    chat.time_start = time_range.get("start", "")
    chat.time_end = time_range.get("end", "")
    chat.duration_days = time_range.get("durationDays", 0)

    return chat


def split_by_month(chat: ChatData) -> dict[str, list[Message]]:
    """按月分组消息，返回 {"2025-09": [messages]}"""
    groups: dict[str, list[Message]] = {}
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        key = dt.strftime("%Y-%m")
        if key not in groups:
            groups[key] = []
        groups[key].append(msg)
    return dict(sorted(groups.items()))
