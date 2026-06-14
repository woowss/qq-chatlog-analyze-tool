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
"""本地统计分析 — 不依赖大模型 API"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import jieba
import jieba.analyse

from parser.qq_parser import ChatData, Message

CST = timezone(timedelta(hours=8))

# 中文停用词（常见虚词、标点、语气词、QQ 专用词汇）
_STOP_WORDS: set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "什么",
    "怎么", "吗", "啊", "吧", "呢", "呀", "哦", "嗯", "哈", "嘛", "哇",
    "哎", "哟", "咯", "嗨", "呵", "喂", "啦", "呐", "唔", "噢",
    "的", "了", "么", "得", "能", "做", "对", "与", "以", "及",
    "而", "或", "但", "被", "把", "从", "向", "在", "于", "让",
    "给", "为", "所", "比", "还", "又", "再", "才", "只", "可",
    "如果", "因为", "所以", "然后", "但是", "而且", "虽然", "虽然", "因为",
    "我们", "确实", "这么", "觉得", "算是", "还有", "知道", "应该",
    "其实", "现在", "有点", "不能", "可以", "这么", "那么", "那个",
    "这个", "什么", "怎么", "怎么样", "为什么", "时候", "时间", "地方",
    "方式", "一个", "可能", "需要", "开始", "最后", "之后", "之前",
    "这个", "那个", "这些", "那些", "这样", "那样",
    "可以", "没有", "已经", "还是", "还是", "就是", "不是",
    "哈哈", "呵呵", "嘿嘿", "嘻嘻", "hhhh", "hhh", "hh",
    "草", "靠", "操", "tm", "tmd", "md","吃糖","问题","答案","这种",
    "不会","你们","他们",
    " ", "", "：", "：", "，", "。", "！", "？", "…", "·", "、",
    "（", "）", "【", "】", "—", "～", "~", "\"", "\"", "''",
    "的", "了", "是", "不", "我", "你", "他", "她", "它",
    "有", "在", "就", "也", "都", "这", "那", "和", "与",
    "把", "被", "让", "从", "对", "到", "去", "来", "说",
    "会", "能", "要", "可以", "没有", "还", "就", "很",
    # 长度 1 的纯标点/数字/字母会在代码中过滤
}

# 纯标点符号正则（用于过滤）
_RE_PUNCT = re.compile(r"""^[/*《》「」『』【】〔〕（）——……·、，。！？：；""''～~.+=@#$%^|`<>&\s\d\-]+$""")


def calc_word_freq(chat: ChatData, top_n: int = 50) -> dict:
    """高频词统计，返回 {"self": [{"word":"...","count":N}, ...], "other": [...]}"""
    self_texts: list[str] = []
    other_texts: list[str] = []

    for msg in chat.messages:
        # 跳过转发消息（含 XML 碎片）、撤回消息、系统消息
        if msg.msg_type in ("type_11", "type_17"):
            continue
        text = msg.text.strip()
        if not text or len(text) < 2:
            continue
        if msg.sender_uid == chat.self_uid:
            self_texts.append(text)
        else:
            other_texts.append(text)

    # 技术性过滤词（QQ 协议 / UID / XML 残留 / 消息格式标记）
    _TECH_STOP = {
        "jpg", "png", "gif", "bmp", "jpeg", "webp",
        "uid", "xml", "version", "encoding", "utf", "serviceID",
        "templateID", "action", "brief", "m_resid", "tSum", "flag",
        "title", "color", "size", "hr", "summary", "source",
        "senderName", "referencedMessageId", "msg", "item", "layout",
        "nickname", "remark", "selfUid", "selfUin", "selfName",
        "chatInfo", "statistics", "totalMessages", "timeRange",
        "messageTypes", "senders", "resources",
        "图片", "表情", "回复", "合并转发",
    }

    # UID 正则：16 位以上字母数字下划线组合
    _RE_UID = re.compile(r"^[a-zA-Z0-9_]{16,}$")
    # 单词+数字混合（如 "1W2g", "3bcc2a8b5d9b8f30171ee1fba56fb201"）
    _RE_MIXED = re.compile(r"^(?:\d+[a-zA-Z]+|[a-zA-Z]+\d+)[a-zA-Z0-9]*$")

    def _count(texts: list[str]) -> list[dict]:
        if not texts:
            return []
        merged = " ".join(texts)
        words = jieba.lcut(merged)
        counter: Counter = Counter()
        for w in words:
            w = w.strip()
            if len(w) < 2:
                continue
            if w.lower() in _STOP_WORDS:
                continue
            if w.lower() in _TECH_STOP:
                continue
            if _RE_PUNCT.match(w):
                continue
            if _RE_UID.match(w):
                continue
            if _RE_MIXED.match(w):
                continue
            # 合并不同长度的 "emmm" → "emmm"
            if re.fullmatch(r"[Ee]m{2,}", w):
                w = "emmm"
            counter[w] += 1
        return [
            {"word": w, "count": c}
            for w, c in counter.most_common(top_n)
        ]

    return {
        "self": _count(self_texts),
        "other": _count(other_texts),
    }


def calc_daily_counts(chat: ChatData) -> list[dict]:
    """每日消息量，返回 [{"date": "2025-09-16", "self": 5, "other": 3}]"""
    daily: dict[str, dict] = {}
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        key = dt.strftime("%Y-%m-%d")
        if key not in daily:
            daily[key] = {"date": key, "self": 0, "other": 0}
        k = "self" if msg.sender_uid == chat.self_uid else "other"
        daily[key][k] += 1
    return [daily[k] for k in sorted(daily.keys())]


def calc_hourly_distribution(chat: ChatData) -> list[dict]:
    """24小时分布，返回 [{"hour": 0, "self": 10, "other": 8}]"""
    hourly = [{"hour": h, "self": 0, "other": 0} for h in range(24)]
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        h = dt.hour
        k = "self" if msg.sender_uid == chat.self_uid else "other"
        hourly[h][k] += 1
    return hourly


def calc_weekly_distribution(chat: ChatData) -> list[dict]:
    """按星期分布（0=周一 … 6=周日）"""
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekly = [{"weekday": i, "self": 0, "other": 0} for i in range(7)]
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        w = dt.weekday()
        k = "self" if msg.sender_uid == chat.self_uid else "other"
        weekly[w][k] += 1
    for i, w in enumerate(weekly):
        w["weekday_name"] = weekday_names[i]
    return weekly


def calc_message_length_stats(chat: ChatData) -> dict:
    """发言长度统计"""
    self_lens, other_lens = [], []
    for msg in chat.messages:
        L = len(msg.text)
        if msg.sender_uid == chat.self_uid:
            self_lens.append(L)
        else:
            other_lens.append(L)

    def _stats(arr: list[int]) -> dict:
        if not arr:
            return {"avg": 0, "max": 0, "min": 0, "median": 0, "total": 0}
        s = sorted(arr)
        n = len(s)
        return {
            "avg": round(sum(s) / n, 1),
            "max": max(s), "min": min(s),
            "median": s[n // 2], "total": n,
        }

    return {"self": _stats(self_lens), "other": _stats(other_lens)}


def calc_face_stats(chat: ChatData) -> dict:
    """表情使用排行，返回 {"self": {"name": count}, "other": {...}}"""
    self_faces: Counter = Counter()
    other_faces: Counter = Counter()
    for msg in chat.messages:
        names = [n for n in msg.face_names if n]
        if not names:
            # 回退到 face_ids
            names = [str(fid) for fid in msg.face_ids]
        if msg.sender_uid == chat.self_uid:
            self_faces.update(names)
        else:
            other_faces.update(names)
    return {
        "self": dict(self_faces.most_common(20)),
        "other": dict(other_faces.most_common(20)),
    }


def calc_response_time(chat: ChatData) -> dict:
    """平均响应时间（秒）"""
    self_times, other_times = [], []
    for i in range(1, len(chat.messages)):
        prev, curr = chat.messages[i - 1], chat.messages[i]
        gap = (curr.timestamp - prev.timestamp) / 1000
        if gap > 3600 * 6:          # 超过 6 小时不算同轮
            continue
        if curr.sender_uid == chat.self_uid:
            self_times.append(gap)
        else:
            other_times.append(gap)

    def _avg(arr: list[float]) -> float:
        return round(sum(arr) / len(arr), 1) if arr else 0

    return {
        "self_avg_seconds": _avg(self_times),
        "other_avg_seconds": _avg(other_times),
    }


def calc_exchange_rounds(chat: ChatData) -> int:
    """对话轮次（同一人连续发言算一轮）"""
    rounds = 0
    last = ""
    for msg in chat.messages:
        if msg.sender_uid != last:
            rounds += 1
            last = msg.sender_uid
    return rounds


def calc_weekly_activity(chat: ChatData) -> list[dict]:
    """星期×小时热力图 [{"weekday":0,"hour":0,"count":5}]"""
    grid: dict[tuple[int, int], int] = defaultdict(int)
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        grid[(dt.weekday(), dt.hour)] += 1
    return [{"weekday": w, "hour": h, "count": c} for (w, h), c in grid.items()]


def calc_overview(chat: ChatData) -> dict:
    """总览统计"""
    self_count = sum(1 for m in chat.messages if m.sender_uid == chat.self_uid)
    other_count = sum(1 for m in chat.messages if m.sender_uid != chat.self_uid)
    self_chars = sum(len(m.text) for m in chat.messages if m.sender_uid == chat.self_uid)
    other_chars = sum(len(m.text) for m in chat.messages if m.sender_uid != chat.self_uid)
    total_images = sum(1 for m in chat.messages if m.has_image)
    total_faces = sum(len(m.face_ids) for m in chat.messages)
    days = chat.duration_days or max(len(calc_daily_counts(chat)), 1)

    return {
        "total_messages": len(chat.messages),
        "total_days": chat.duration_days or days,
        "total_images": total_images,
        "total_faces": total_faces,
        "self_name": chat.self_name,
        "other_name": chat.other_name,
        "self_count": self_count,
        "other_count": other_count,
        "self_chars": self_chars,
        "other_chars": other_chars,
        "exchange_rounds": calc_exchange_rounds(chat),
        "avg_daily": round(len(chat.messages) / days, 1),
    }
