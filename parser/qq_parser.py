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
from datetime import datetime
from typing import Optional


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

    # 确定对方的显示名
    senders = raw.get("statistics", {}).get("senders", [])
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

        if sender_uid != self_uid and not chat.other_uid:
            chat.other_uid = sender_uid

        content = msg.get("content", {})
        raw_text = content.get("text", "")
        elements = content.get("elements", [])

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

        parsed = Message(
            id=msg.get("id", ""),
            timestamp=msg.get("timestamp", 0),
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
        dt = datetime.fromtimestamp(msg.timestamp / 1000)
        key = dt.strftime("%Y-%m")
        if key not in groups:
            groups[key] = []
        groups[key].append(msg)
    return dict(sorted(groups.items()))
