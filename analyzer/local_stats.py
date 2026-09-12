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
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional
import jieba
import jieba.analyse

from parser.qq_parser import CST, ChatData

# jieba 首次分词会往 stderr 打 "Building prefix dict..."，本地工具不需要这行噪音
jieba.setLogLevel(logging.ERROR)

# 对话段切分阈值：相邻消息间隔超过它就算"新的一段对话"（轮次统计与话题发起判定都用它）
SESSION_GAP_MINUTES = 30
SESSION_GAP_MS = SESSION_GAP_MINUTES * 60 * 1000


def is_session_start(prev_ts: Optional[int], ts: int) -> bool:
    """两条相邻消息是否分属不同对话段（"什么算新的一段"的唯一口径来源）。

    轮次统计、对话段划分、以及送进 prompt 的统计头原先各写一遍同样的比较：
    阈值虽然共用常量，但规则一旦要改（比如按"双方都在线"细分），
    三处必须同时改才不会出现"报表说 12 段、模型看到 9 段"的口径分裂。
    """
    return prev_ts is None or ts - prev_ts > SESSION_GAP_MS

# 中文停用词（常见虚词、标点、语气词、QQ 专用词汇）。
# 按语义分组、每项只出现一次：原先是一长串随手追加的字面量，"的/了/还是/因为"
# 之类重复了 2-3 次（set 下无害，但读的人分不清是笔误还是有意，也看不出真正的新增项）。
_STOP_WORDS: set[str] = {
    # —— 高频虚词、代词、介词 ——
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "什么",
    "怎么", "么", "得", "能", "做", "对", "与", "以", "及", "而", "或", "但",
    "被", "把", "从", "向", "于", "让", "给", "为", "所", "比", "还", "又",
    "再", "才", "只", "可", "来",
    # —— 语气词 ——
    "吗", "啊", "吧", "呢", "呀", "哦", "嗯", "哈", "嘛", "哇",
    "哎", "哟", "咯", "嗨", "呵", "喂", "啦", "呐", "唔", "噢",
    # —— 连词与高频副词短语 ——
    "如果", "因为", "所以", "然后", "但是", "而且", "虽然",
    "我们", "确实", "这么", "觉得", "算是", "还有", "知道", "应该",
    "其实", "现在", "有点", "不能", "可以", "那么", "那个", "这个",
    "怎么样", "为什么", "时候", "时间", "地方", "方式", "可能", "需要",
    "开始", "最后", "之后", "之前", "这些", "那些", "这样", "那样",
    "已经", "还是", "就是", "不是",
    # —— 笑声、口头禅与网络用语 ——
    "哈哈", "呵呵", "嘿嘿", "嘻嘻", "hhhh", "hhh", "hh",
    "草", "靠", "操", "tm", "tmd", "md", "吃糖", "问题", "答案", "这种",
    "不会", "你们", "他们",
    "emmm", "emmmm", "emmmmm", "emmmmmmm", "emmmmmmmm",
    # —— 标点与空白（长度 1 的纯标点/数字/字母另有 _RE_PUNCT 过滤）——
    " ", "", "：", "，", "。", "！", "？", "…", "·", "、",
    "（", "）", "【", "】", "—", "～", "~", "\"", "''",
}

# 纯标点符号正则（用于过滤）
_RE_PUNCT = re.compile(r"""^[/*《》「」『』【】〔〕（）——……·、，。！？：；""''～~.+=@#$%^|`<>&\s\d\-]+$""")


def calc_word_freq(chat: ChatData, top_n: int = 50) -> dict:
    """高频词统计，返回 {"self": [{"word":"...","count":N}, ...], "other": [...]}"""
    self_texts: list[str] = []
    other_texts: list[str] = []

    msgs, _ = _statistical(chat)
    for msg in msgs:
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


def _statistical(chat: ChatData) -> tuple[list, list[tuple[str, int, int, str]]]:
    """统计口径的消息 + 每条的 (日期, 小时, 星期, 月份)，整个 chat 只算一次。

    这些派生字段原本在每个 calc_* 里各算一遍：5 万条规模下 6 个函数累计
    约 300 ms 花在重复的时区转换上，而 statistical() 也会被重建 12 次。
    结果挂在 chat 实例上（ChatData 不是 frozen dataclass），随请求生命周期存续。
    """
    cache = getattr(chat, "_stats_cache", None)
    if cache is None:
        msgs = chat.statistical()
        fields = []
        for m in msgs:
            dt = datetime.fromtimestamp(m.timestamp / 1000, tz=CST)
            fields.append((dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(),
                           dt.strftime("%Y-%m")))
        cache = (msgs, fields)
        chat._stats_cache = cache  # type: ignore[attr-defined]
    return cache


def _party(msg, self_uid: str) -> str:
    return "self" if msg.sender_uid == self_uid else "other"


def calc_daily_counts(chat: ChatData, fill_gaps: bool = True) -> list[dict]:
    """每日消息量，返回 [{"date": "2025-09-16", "self": 5, "other": 3}]

    fill_gaps=True 时补齐首末之间的空档日期（计 0）。否则折线图用的是类目轴，
    中间"没聊天的日子"会被整段抹掉——3 个月没说话可能看起来像天天在聊。
    """
    msgs, fields = _statistical(chat)
    daily: dict[str, dict] = {}
    for i, msg in enumerate(msgs):
        key = fields[i][0]
        entry = daily.get(key)
        if entry is None:
            entry = daily[key] = {"date": key, "self": 0, "other": 0}
        entry[_party(msg, chat.self_uid)] += 1
    if not daily or not fill_gaps:
        return [daily[k] for k in sorted(daily)]

    from datetime import date as _date, timedelta as _timedelta
    first, last = min(daily), max(daily)
    out: list[dict] = []
    cur = _date.fromisoformat(first)
    end = _date.fromisoformat(last)
    while cur <= end:
        key = cur.isoformat()
        out.append(daily.get(key) or {"date": key, "self": 0, "other": 0})
        cur += _timedelta(days=1)
    return out


def calc_hourly_distribution(chat: ChatData) -> list[dict]:
    """24小时分布，返回 [{"hour": 0, "self": 10, "other": 8}]"""
    msgs, fields = _statistical(chat)
    hourly = [{"hour": h, "self": 0, "other": 0} for h in range(24)]
    for i, msg in enumerate(msgs):
        hourly[fields[i][1]][_party(msg, chat.self_uid)] += 1
    return hourly


def calc_weekly_distribution(chat: ChatData) -> list[dict]:
    """按星期分布（0=周一 … 6=周日）"""
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    msgs, fields = _statistical(chat)
    weekly = [{"weekday": i, "self": 0, "other": 0} for i in range(7)]
    for i, msg in enumerate(msgs):
        weekly[fields[i][2]][_party(msg, chat.self_uid)] += 1
    for i, w in enumerate(weekly):
        w["weekday_name"] = weekday_names[i]
    return weekly


def calc_message_length_stats(chat: ChatData) -> dict:
    """发言长度统计"""
    self_lens, other_lens = [], []
    msgs, _ = _statistical(chat)
    for msg in msgs:
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
    msgs, _ = _statistical(chat)
    for msg in msgs:
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
    """回复速度（秒）—— 只统计对方发来后本方做出的回复间隔。

    除均值外给出 P50/P90：均值极易被单次长间隔带偏（实测 19 次 5 秒 + 1 次
    5 分钟 → 均值 19.8 秒，而中位数只有 5 秒），界面以中位数为主指标更诚实。
    """
    self_times, other_times = [], []
    msgs, _ = _statistical(chat)
    for i in range(1, len(msgs)):
        prev, curr = msgs[i - 1], msgs[i]
        # 同一人连续发言不是"响应"，跳过，避免拉低/污染平均值
        if prev.sender_uid == curr.sender_uid:
            continue
        gap = (curr.timestamp - prev.timestamp) / 1000
        if gap > 3600 * 6:          # 超过 6 小时不算同轮
            continue
        (self_times if curr.sender_uid == chat.self_uid else other_times).append(gap)

    def _percentile(arr: list[float], q: float) -> float:
        if not arr:
            return 0.0
        s = sorted(arr)
        idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return round(s[idx], 1)

    def _stats(arr: list[float]) -> dict:
        return {
            "avg": round(sum(arr) / len(arr), 1) if arr else 0.0,
            "p50": _percentile(arr, 0.50),
            "p90": _percentile(arr, 0.90),
            "count": len(arr),
        }

    self_stats, other_stats = _stats(self_times), _stats(other_times)
    return {
        # 兼容旧字段（前端/报告仍在用）
        "self_avg_seconds": self_stats["avg"],
        "other_avg_seconds": other_stats["avg"],
        "self": self_stats,
        "other": other_stats,
        "session_gap_minutes": SESSION_GAP_MINUTES,
    }


def calc_exchange_rounds(chat: ChatData) -> int:
    """对话轮次：一次"发言交替"或"间隔超过 SESSION_GAP_MINUTES 的新段"算一轮。

    旧口径只数说话人交替，连珠炮互刷会虚高；现在加入时间约束，
    长时间中断后的第一条消息重新起一轮，数字更贴近"聊了多少个来回"。
    """
    msgs, _ = _statistical(chat)
    rounds = 0
    last_uid: Optional[str] = None
    last_ts: Optional[int] = None
    for msg in msgs:
        if (last_uid is None or msg.sender_uid != last_uid
                or is_session_start(last_ts, msg.timestamp)):
            rounds += 1
        last_uid, last_ts = msg.sender_uid, msg.timestamp
    return rounds


def calc_conversation_sessions(chat: ChatData) -> list[dict]:
    """对话段：间隔超过 SESSION_GAP_MINUTES 就切成新的一段（用于"谁先开口"等判断）"""
    msgs, fields = _statistical(chat)
    sessions: list[dict] = []
    for i, msg in enumerate(msgs):
        prev_ts = sessions[-1]["last_ts"] if sessions else None
        if is_session_start(prev_ts, msg.timestamp):
            sessions.append({"date": fields[i][0], "start_ts": msg.timestamp,
                             "last_ts": msg.timestamp, "count": 0,
                             "opener": _party(msg, chat.self_uid),
                             "opener_uid": msg.sender_uid})
        sessions[-1]["count"] += 1
        sessions[-1]["last_ts"] = msg.timestamp
    return sessions


def calc_initiator_stats(chat: ChatData) -> dict:
    """谁更常"开启话题"：以对话段的第一条消息归属来统计（本地可算，无需模型猜）"""
    sessions = calc_conversation_sessions(chat)
    total = len(sessions)
    self_open = sum(1 for s in sessions if s["opener"] == "self")
    other_open = total - self_open
    return {
        "sessions": total,
        "self_opened": self_open,
        "other_opened": other_open,
        "self_ratio": round(self_open / total, 2) if total else 0.0,
    }


def calc_weekly_activity(chat: ChatData) -> list[dict]:
    """星期×小时热力图 [{"weekday":0,"hour":0,"count":5}]"""
    msgs, fields = _statistical(chat)
    grid: dict[tuple[int, int], int] = defaultdict(int)
    for i in range(len(msgs)):
        grid[(fields[i][2], fields[i][1])] += 1
    return [{"weekday": w, "hour": h, "count": c} for (w, h), c in grid.items()]


def calc_overview(chat: ChatData) -> dict:
    """总览统计（口径：仅计入系统/撤回/转发之外的消息）

    total_days 是"记录跨度"（缺 timeRange 时按首末消息算），active_days 是
    "实际聊过的天数"——两者分开给，避免日均消息的分母口径悄悄变化。
    """
    msgs, fields = _statistical(chat)
    self_count = sum(1 for m in msgs if m.sender_uid == chat.self_uid)
    other_count = len(msgs) - self_count
    self_chars = sum(len(m.text) for m in msgs if m.sender_uid == chat.self_uid)
    other_chars = sum(len(m.text) for m in msgs if m.sender_uid != chat.self_uid)
    total_images = sum(1 for m in msgs if m.has_image)
    # 表情计数：商城大表情（type_17）没有数字 id，只有名字，取两者中有的那个
    total_faces = sum((len(m.face_names) or len(m.face_ids)) for m in msgs)
    # 媒体体积与去重：导出器的 size/md5 只对媒体本体有效。
    # 注意图片与非图片媒体分开统计——把两者混成一个"媒体合计"会让用户以为
    # 图片只占几十 MB（实测图片 2.2 GB、文件/视频 280 MB，混在一起就是误导）。
    image_msgs = [m for m in msgs if m.has_image]
    image_bytes = sum(m.media_bytes for m in image_msgs)
    other_media_bytes = sum(m.media_bytes for m in msgs if m.media_kind)
    image_ids = {m.media_id for m in image_msgs if m.media_id}
    unique_images = len(image_ids) if image_ids else None    # 无 md5 的导出器给 None
    # 非文本媒体（文件/视频/转发/红包/表情气泡/Markdown）：原先完全不统计，
    # 现在按类型计数，界面上与图片并列展示
    media = Counter(m.media_kind for m in msgs if m.media_kind)

    active_days = len({f[0] for f in fields})
    span_days = 0
    if fields:
        from datetime import date as _date
        span_days = (_date.fromisoformat(max(f[0] for f in fields))
                     - _date.fromisoformat(min(f[0] for f in fields))).days + 1
    days = chat.duration_days or span_days or 1

    return {
        "total_messages": len(msgs),
        "total_days": days,
        "days_basis": "file" if chat.duration_days else "computed",
        "active_days": active_days,
        "total_images": total_images,
        "total_faces": total_faces,
        "total_files": media.get("file", 0),
        "total_videos": media.get("video", 0),
        "total_forwards": media.get("forward", 0),
        "total_other_media": (media.get("wallet", 0) + media.get("face_bubble", 0)
                              + media.get("markdown", 0)),
        "image_bytes": image_bytes,
        "other_media_bytes": other_media_bytes,
        "unique_images": unique_images,
        "self_name": chat.self_name,
        "other_name": chat.other_name,
        "self_count": self_count,
        "other_count": other_count,
        "self_chars": self_chars,
        "other_chars": other_chars,
        "exchange_rounds": calc_exchange_rounds(chat),
        "avg_daily": round(len(msgs) / days, 1) if days else 0.0,
    }


def calc_milestones(chat: ChatData) -> dict:
    """时光里程碑：纯本地计算的纪念性统计，零 API 成本。

    返回字段（口径均基于 statistical() 过滤后的消息）：
    - first_day/last_day: 首条/末条消息日期
    - active_days: 实际聊过的天数
    - longest_streak: 连续聊天纪录 {days, start, end}
    - longest_silence: 最长沉默期 {days, before, after}（两个活跃日之间的空档天数）
    - midnight_days/midnight_msgs: 跨零点（0-5 点有发言）的天数与条数
    - late_night_msgs: 凌晨 2-5 点的发言条数
    - peak_day: 单日消息峰值 {date, count}
    - busiest_month: 最活跃月份 {month, count}
    - mutual_nights: 双方都熬到凌晨 2-5 点的天数（互相陪伴的深夜）
    """
    from datetime import date as _date

    msgs, fields = _statistical(chat)
    if not msgs:
        return {}

    day_counter: Counter = Counter()
    midnight_msg_count = 0          # 0-6 点的发言条数
    late_night_msgs = 0             # 2-6 点的发言条数
    midnight_day_set: set[str] = set()
    late_by_day: dict[str, set] = {}   # date -> {self/other}
    month_counter: Counter = Counter()

    for i, m in enumerate(msgs):
        key, hour, _weekday, month = fields[i]
        day_counter[key] += 1
        month_counter[month] += 1
        if hour < 6:
            midnight_msg_count += 1
            midnight_day_set.add(key)
            if hour >= 2:
                late_night_msgs += 1
                late_by_day.setdefault(key, set()).add(_party(m, chat.self_uid))

    sorted_days = sorted(day_counter)
    first_day, last_day = sorted_days[0], sorted_days[-1]

    # 连续聊天纪录
    best_run = cur_run = 1
    best_start = best_end = cur_start = _date.fromisoformat(first_day)
    for prev, cur in zip(sorted_days, sorted_days[1:], strict=False):
        if (_date.fromisoformat(cur) - _date.fromisoformat(prev)).days == 1:
            cur_run += 1
        else:
            cur_run = 1
            cur_start = _date.fromisoformat(cur)
        if cur_run > best_run:
            best_run = cur_run
            best_start = cur_start
            best_end = _date.fromisoformat(cur)

    # 最长沉默期（相邻活跃日之间的空档）
    silence_days, sil_before, sil_after = 0, "", ""
    for prev, cur in zip(sorted_days, sorted_days[1:], strict=False):
        gap = (_date.fromisoformat(cur) - _date.fromisoformat(prev)).days - 1
        if gap > silence_days:
            silence_days, sil_before, sil_after = gap, prev, cur

    peak_day, peak_count = max(day_counter.items(), key=lambda kv: kv[1])
    busiest_month, busiest_count = max(month_counter.items(), key=lambda kv: kv[1])
    mutual_nights = sum(1 for v in late_by_day.values() if len(v) >= 2)

    return {
        "first_day": first_day,
        "last_day": last_day,
        "active_days": len(sorted_days),
        "longest_streak": {"days": best_run,
                           "start": best_start.isoformat(),
                           "end": best_end.isoformat()},
        "longest_silence": {"days": silence_days, "before": sil_before, "after": sil_after},
        "midnight_days": len(midnight_day_set),
        "midnight_msgs": midnight_msg_count,
        "late_night_msgs": late_night_msgs,
        "peak_day": {"date": peak_day, "count": peak_count},
        "busiest_month": {"month": busiest_month, "count": busiest_count},
        "mutual_nights": mutual_nights,
    }
