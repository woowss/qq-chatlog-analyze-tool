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

import hashlib
import logging
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date as _date
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
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "一个",
    "上",
    "也",
    "很",
    "到",
    "说",
    "要",
    "去",
    "你",
    "会",
    "着",
    "没有",
    "看",
    "好",
    "自己",
    "这",
    "他",
    "她",
    "它",
    "们",
    "那",
    "什么",
    "怎么",
    "么",
    "得",
    "能",
    "做",
    "对",
    "与",
    "以",
    "及",
    "而",
    "或",
    "但",
    "被",
    "把",
    "从",
    "向",
    "于",
    "让",
    "给",
    "为",
    "所",
    "比",
    "还",
    "又",
    "再",
    "才",
    "只",
    "可",
    "来",
    # —— 语气词 ——
    "吗",
    "啊",
    "吧",
    "呢",
    "呀",
    "哦",
    "嗯",
    "哈",
    "嘛",
    "哇",
    "哎",
    "哟",
    "咯",
    "嗨",
    "呵",
    "喂",
    "啦",
    "呐",
    "唔",
    "噢",
    # —— 连词与高频副词短语 ——
    "如果",
    "因为",
    "所以",
    "然后",
    "但是",
    "而且",
    "虽然",
    "我们",
    "确实",
    "这么",
    "觉得",
    "算是",
    "还有",
    "知道",
    "应该",
    "其实",
    "现在",
    "有点",
    "不能",
    "可以",
    "那么",
    "那个",
    "这个",
    "怎么样",
    "为什么",
    "时候",
    "时间",
    "地方",
    "方式",
    "可能",
    "需要",
    "开始",
    "最后",
    "之后",
    "之前",
    "这些",
    "那些",
    "这样",
    "那样",
    "已经",
    "还是",
    "就是",
    "不是",
    # —— 笑声、口头禅与网络用语 ——
    "哈哈",
    "呵呵",
    "嘿嘿",
    "嘻嘻",
    "hhhh",
    "hhh",
    "hh",
    "草",
    "靠",
    "操",
    "tm",
    "tmd",
    "md",
    "吃糖",
    "问题",
    "答案",
    "这种",
    "不会",
    "你们",
    "他们",
    "emmm",
    "emmmm",
    "emmmmm",
    "emmmmmmm",
    "emmmmmmmm",
    # —— 标点与空白（长度 1 的纯标点/数字/字母另有 _RE_PUNCT 过滤）——
    " ",
    "",
    "：",
    "，",
    "。",
    "！",
    "？",
    "…",
    "·",
    "、",
    "（",
    "）",
    "【",
    "】",
    "—",
    "～",
    "~",
    '"',
    "''",
}

# 纯标点符号正则（用于过滤）
_RE_PUNCT = re.compile(r"""^[/*《》「」『』【】〔〕（）——……·、，。！？：；""''～~.+=@#$%^|`<>&\s\d\-]+$""")

# 技术性过滤词（QQ 协议 / UID / XML 残留 / 消息格式标记）。
# 放在模块级而不是 calc_word_freq 内部：原先每次调用都要重建一遍这个集合。
_TECH_STOP: set[str] = {
    "jpg",
    "png",
    "gif",
    "bmp",
    "jpeg",
    "webp",
    "uid",
    "xml",
    "version",
    "encoding",
    "utf",
    "serviceID",
    "templateID",
    "action",
    "brief",
    "m_resid",
    "tSum",
    "flag",
    "title",
    "color",
    "size",
    "hr",
    "summary",
    "source",
    "senderName",
    "referencedMessageId",
    "msg",
    "item",
    "layout",
    "nickname",
    "remark",
    "selfUid",
    "selfUin",
    "selfName",
    "chatInfo",
    "statistics",
    "totalMessages",
    "timeRange",
    "messageTypes",
    "senders",
    "resources",
    "图片",
    "表情",
    "回复",
    "合并转发",
}


#: 外部停用词文件（QQCHAT_STOPWORD_FILE，评审遗留项 #18 的落地）：
#: 一行一词、# 注释、空行忽略；内置词表永远生效，外部文件只做**加法**——
#: 让用户能把"我们之间的口头禅"从词云里摘出去，而不必等版本内置。
_STOP_CACHE: dict = {"stamp": None, "words": frozenset(), "text": ""}
_STOP_READ_MAX_BYTES = 1 << 20  # 有界读取：停用词文件读到 1MB 为止，坏路径/大文件不拖垮请求


def _load_extra_stopwords() -> tuple:
    """返回 (frozenset 外部停用词, 参与指纹的原始文本)。按 (路径, mtime, size) 缓存。"""
    path = (os.getenv("QQCHAT_STOPWORD_FILE", "") or "").strip()
    stamp = None
    if path:
        try:
            st = os.stat(path)
            stamp = (path, int(st.st_mtime), st.st_size)
        except OSError:
            stamp = (path, "missing", 0)
    if _STOP_CACHE["stamp"] == stamp:
        return _STOP_CACHE["words"], _STOP_CACHE["text"]
    raw = ""
    if path and stamp and stamp[1] != "missing":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(_STOP_READ_MAX_BYTES)
        except OSError as e:
            logging.getLogger("app").warning("停用词文件读取失败（只用内置词表）: %s", e)
            raw = ""
    words = frozenset(w for line in raw.splitlines() for w in [line.strip()] if w and not w.startswith("#"))
    _STOP_CACHE.update(stamp=stamp, words=words, text=raw)
    return words, raw


def stopwords_fingerprint() -> str:
    """当前生效停用词集合的短哈希。

    词频结果里带上它：外部文件一改，下一次进页面就会因指纹对不上而**重算词云**
    ——停用词只影响本地词频（重算只花 CPU 分词），与任何付费缓存、提示词指纹无关。
    """
    words, extra_text = _load_extra_stopwords()
    digest = hashlib.sha1()
    digest.update(",".join(sorted(_STOP_WORDS | words)).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(extra_text.encode("utf-8"))
    return digest.hexdigest()[:10]


def calc_word_freq(chat: ChatData, top_n: int = 50) -> dict:
    """高频词统计，返回 {"self": [{"word":"...","count":N}, ...], "other": [...], "stop_fp": "..."}"""
    self_texts: list[str] = []
    other_texts: list[str] = []
    extra_stop, _ = _load_extra_stopwords()
    stop_all = _STOP_WORDS | extra_stop

    msgs, _ = _statistical(chat)
    for msg in msgs:
        text = msg.text.strip()
        if not text or len(text) < 2:
            continue
        if msg.sender_uid == chat.self_uid:
            self_texts.append(text)
        else:
            other_texts.append(text)

    # UID 正则：16 位以上字母数字下划线组合
    _RE_UID = re.compile(r"^[a-zA-Z0-9_]{16,}$")
    # 单词+数字混合（如 "1W2g", "0123456789abcdef0123456789abcdef"）
    _RE_MIXED = re.compile(r"^(?:\d+[a-zA-Z]+|[a-zA-Z]+\d+)[a-zA-Z0-9]*$")

    def _count(texts: list[str]) -> list[dict]:
        # 逐条送进 jieba，而不是把整份发言 join 成一个可能上百万字符的大字符串：
        # 内存峰值从 O(全部文本) 降到 O(最长一条)，同一条消息的分词结果不变。
        # 逐条切分也避免了"跨消息把两个词粘成一个词"（原先靠 join 里的空格挡着，
        # 而 jieba 对空格边界的处理并不保证）。
        counter: Counter = Counter()
        for text in texts:
            for w in jieba.lcut(text):
                w = w.strip()
                if len(w) < 2:
                    continue
                if w.lower() in stop_all:
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
        return [{"word": w, "count": c} for w, c in counter.most_common(top_n)]

    return {
        "self": _count(self_texts),
        "other": _count(other_texts),
        # 生效停用词集合的指纹：外部文件改了 → 指纹对不上 → 消费方按需重算词云（只花 CPU）
        "stop_fp": stopwords_fingerprint(),
    }


def _statistical(chat: ChatData) -> tuple[list, list[tuple[str, int, int, str]]]:
    """统计口径的消息 + 每条的 (日期, 小时, 星期, 月份)，整个 chat 只算一次。

    这些派生字段原本在每个 calc_* 里各算一遍：数万条规模下 6 个函数累计
    约 300 ms 花在重复的时区转换上，而 statistical() 也会被重建 12 次。
    结果挂在 chat 的 _stats_cache 字段上（见 ChatData 的字段声明），随请求生命周期存续。
    """
    cache = chat._stats_cache
    if cache is None:
        msgs = chat.statistical()
        fields = []
        for m in msgs:
            dt = datetime.fromtimestamp(m.timestamp / 1000, tz=CST)
            fields.append((dt.strftime("%Y-%m-%d"), dt.hour, dt.weekday(), dt.strftime("%Y-%m")))
        cache = (msgs, fields)
        chat._stats_cache = cache
    return cache


def _party(msg, self_uid: str) -> str:
    return "self" if msg.sender_uid == self_uid else "other"


def calc_daily_counts(chat: ChatData, fill_gaps: bool = True) -> list[dict]:
    """每日消息量，返回 [{"date": "2024-01-01", "self": 5, "other": 3}]

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

    # 逐日补齐：按 date 的序数递增（3 年记录 ≈ 1100 天，比每次 date + timedelta
    # 造一个中间对象少一轮分配；结果与 while cur <= end 完全一致）。
    first, last = min(daily), max(daily)
    out: list[dict] = []
    for ordinal in range(_date.fromisoformat(first).toordinal(), _date.fromisoformat(last).toordinal() + 1):
        key = _date.fromordinal(ordinal).isoformat()
        out.append(daily.get(key) or {"date": key, "self": 0, "other": 0})
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


def _median(sorted_values: list) -> float:
    """中位数（教科书定义：偶数个取中间两个的平均）。

    为什么要专门写一个而不是就地 s[n//2]：`s[n//2]` 取的是**上中位**，偶数样本上
    它不是中位数——两句长度 [1, 9] 会被报成"中位句长 9"（真值 5.0），两句媒体
    [1, 100] 直接报成 100（真值 50.5），等于把最长的那句当成了中位数。
    回复间隔那侧的 p50 也走这里，于是"中位数"这个标签在本模块只有一种算法
    （口径唯一是本地统计的立身之本：同一个词在两张卡上算出不同的数，
    用户只会以为其中一张错了）。

    调用方须传入已排序的列表；空列表返回 0.0（各消费点本来就按 0 显示"没有数据"）。
    """
    n = len(sorted_values)
    if not n:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


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
            "max": max(s),
            "min": min(s),
            "median": round(_median(s), 1),
            "total": n,
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
        # gap 必须**严格大于 0**：导出器的时间戳常常只到秒，两人同一秒内各说一句
        # 就会算出 0 间隔。0 不是"回复速度"，把它计入会让均值和中位数一起塌到 0
        # ——实测 self 侧三条同秒配对直接报成"平均回复 0.0 秒"，界面显示的是一件
        # 不可能发生的事。对话构建那侧（analyzer/dialog.py）本来就用 0 < gap 过滤，
        # 两处必须同一个口径，否则喂给模型的与实际展示的对不上。
        if gap <= 0 or gap > 3600 * 6:  # 超过 6 小时不算同轮
            continue
        (self_times if curr.sender_uid == chat.self_uid else other_times).append(gap)

    def _percentile(arr: list[float], q: float) -> float:
        """分位数：取最近秩。加 0.5 再 floor，而不是用 Python 的 round()
        （round 是银行家舍入：round(0.5)=0、round(1.5)=2、round(2.5)=2，
        同一个数在 n=2 与 n=6 时会一边倒向上、一边倒向下，p90 跟着抖）。"""
        if not arr:
            return 0.0
        s = sorted(arr)
        idx = min(len(s) - 1, max(0, int(math.floor(q * (len(s) - 1) + 0.5))))
        return round(s[idx], 1)

    def _stats(arr: list[float]) -> dict:
        return {
            "avg": round(sum(arr) / len(arr), 1) if arr else 0.0,
            # p50 就是中位数，走与句长 median 同一个实现（口径唯一）。
            # **必须先排序**：arr 是按对话时间序 append 的间隔，未排序时"取中间"拿到的是
            # 时间序列的中间那条，不是中位数——4 条 [10,100,20,30] 的真中位数是 25.0，
            # 不排序会报 60.0，而 p50 恰恰是关系页与报告页的主展示指标。
            "p50": round(_median(sorted(arr)), 1),
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
        if last_uid is None or msg.sender_uid != last_uid or is_session_start(last_ts, msg.timestamp):
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
            sessions.append(
                {
                    "date": fields[i][0],
                    "start_ts": msg.timestamp,
                    "last_ts": msg.timestamp,
                    "count": 0,
                    "opener": _party(msg, chat.self_uid),
                    "opener_uid": msg.sender_uid,
                }
            )
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
    self_uid = chat.self_uid
    self_count = 0
    other_count = 0
    self_chars = 0
    other_chars = 0
    total_images = 0
    total_faces = 0
    image_bytes = 0
    other_media_bytes = 0
    unique_image_ids: set[str] = set()
    media: Counter = Counter()
    # 单次遍历：这段原本是 7 个独立的推导式（3 个 sum 计数、2 个 sum 求字数和、
    # 1 个 Counter、1 个 set），每个都完整扫一遍 msgs——数万条规模下累计数百毫秒。
    # 合并到同一个循环里累加，字段口径逐条对齐原实现，结果完全不变。
    for m in msgs:
        if m.sender_uid == self_uid:
            self_count += 1
            self_chars += len(m.text)
        else:
            other_count += 1
            other_chars += len(m.text)
        # 图片与非图片媒体的字节必须分家。media_bytes 是这条消息里**所有**媒体元素的
        # 合计，所以一条"图片 + 文件"的消息若把整份记进任何一边，就会一边漏算、另一边
        # 多算（实测：图片 2048 + 文件 4096 → "图片 6144 · 文件/视频 6144"，仪表盘
        # 并排显示两个 6144，而真实发送量只有 6144）。解析器现在按元素各记一份，
        # 这里优先用那份；手工构造的 Message（测试与第三方调用）没有分家字段时，
        # 只在"这条消息不含非图片媒体"的场合退回旧口径，免得又把两边混成一团。
        img_bytes = m.image_bytes or (m.media_bytes if (m.has_image and not m.media_kind) else 0)
        other_bytes = max(0, m.media_bytes - img_bytes)
        if m.has_image:
            # 按**张**数而不是按"含图的消息条数"：一条消息连发 3 张图，界面上的标签
            # 是"图片总数"，报 1 就是少算两张（而同一行的 image_bytes 已经把这 3 张
            # 的字节都算进去了 —— 两个字段自己的口径都不一致）。
            total_images += m.image_count or 1
            image_bytes += img_bytes
            for mid in m.image_ids or ([m.media_id] if m.media_id else []):
                if mid:
                    unique_image_ids.add(mid)
        # 表情计数：商城大表情（type_17）没有数字 id，只有名字，取两者中有的那个
        total_faces += len(m.face_names) or len(m.face_ids)
        # 媒体体积与去重：导出器的 size/md5 只对媒体本体有效。
        # 注意图片与非图片媒体分开统计——把两者混成一个"媒体合计"会让用户以为
        # 图片只占几十 MB（图片与文件/视频的体积能差一两个数量级，混在一起就是误导）。
        # 非文本媒体（文件/视频/转发/红包/表情气泡/Markdown/卡片/通话/语音）按类型计数，
        # 原先完全不统计，现在界面上与图片并列展示。
        if m.media_kind:
            other_media_bytes += other_bytes
            media[m.media_kind] += 1
    # 撤回计数：recalled 消息按口径已被 msgs 滤掉（正确——撤回的内容不该进正文统计），
    # 但"谁爱撤回"本身是关系信号，而且标记每个入口都解析了却零消费——数据在手上闲置。
    # 单独扫一遍全量列表数出来；系统消息不算（"对方撤回了一条消息"提示不是用户按的）。
    total_recalls = 0
    self_recalls = 0
    other_recalls = 0
    for m in chat.messages:
        if m.recalled and not m.system:
            total_recalls += 1
            if m.sender_uid == self_uid:
                self_recalls += 1
            else:
                other_recalls += 1
    unique_images = len(unique_image_ids) if unique_image_ids else None  # 无 md5 的导出器给 None

    active_days = len({f[0] for f in fields})
    span_days = 0
    if fields:
        span_days = (
            _date.fromisoformat(max(f[0] for f in fields)) - _date.fromisoformat(min(f[0] for f in fields))
        ).days + 1
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
        "total_voices": media.get("voice", 0),
        # "其他媒体"用差集而不是枚举：旧实现写死 wallet+face_bubble+markdown 三类，
        # 于是卡片消息(json)与通话记录(av_record)明明被解析、被计数，到汇总就丢了
        # ——界面上"通话/卡片"永远是 0（正是这次要修的 bug）。差集口径下，
        # 未来任何新增媒体种类都自动归入"其他"，不会再犯同一条错。
        # 一条消息只按第一个非图片媒体归类（单条一个语义），总数不会双计。
        "total_other_media": max(
            0,
            sum(media.values())
            - media.get("file", 0)
            - media.get("video", 0)
            - media.get("forward", 0)
            - media.get("voice", 0),
        ),
        # 撤回与格式漂移信号：加法字段，旧缓存/旧调用方 .get 缺省即安全。
        "total_recalls": total_recalls,
        "self_recalls": self_recalls,
        "other_recalls": other_recalls,
        #: 未识别元素类型 → 次数（导出器格式漂移探测器）。空 = 全部元素都能解析。
        #: 非空时仪表盘给出提示条，并引导用 tools/inspect_chat.py 体检。
        "unknown_element_types": dict(chat.unknown_element_types),
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
    msgs, fields = _statistical(chat)
    if not msgs:
        return {}

    day_counter: Counter = Counter()
    midnight_msg_count = 0  # 0-6 点的发言条数
    late_night_msgs = 0  # 2-6 点的发言条数
    midnight_day_set: set[str] = set()
    late_by_day: dict[str, set] = {}  # date -> {self/other}
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
        "longest_streak": {"days": best_run, "start": best_start.isoformat(), "end": best_end.isoformat()},
        "longest_silence": {"days": silence_days, "before": sil_before, "after": sil_after},
        "midnight_days": len(midnight_day_set),
        "midnight_msgs": midnight_msg_count,
        "late_night_msgs": late_night_msgs,
        "peak_day": {"date": peak_day, "count": peak_count},
        "busiest_month": {"month": busiest_month, "count": busiest_count},
        "mutual_nights": mutual_nights,
    }


#: "中断重启"的间隔门槛（天）：相邻发言隔过这么多天才算一次"冷场后重开"。
#: 3 天是刻意选的——隔一晚就算重启会把每个周末都数进去，数字失去意义；
#: 门槛是常量而非配置：口径进了缓存，改门槛=整族统计重算，不该由用户随手调。
RESTART_GAP_DAYS = 3


def calc_trends(chat: ChatData) -> dict:
    """时间趋势类统计（纯本地、零 API 成本）：数据早就解析在手上，只是没算过。

    - months：逐月「我 vs 对方」条数与非空正文的平均句长（互发比例随时间的变化，
      overview 只有一个全程汇总，看不出"这半年是谁在主动"）
    - restarts：中断 ≥ RESTART_GAP_DAYS 天后的重启次数、重启方与事件列表
      （"吵完架谁先开口"的量化答案；里程碑只有最长沉默，看不出模式）
    - name_history：双方在导出文件里出现过的显示名及各自的首末月与次数
      （改名/换备注是关系变化的信号，_Acc 早就在按名字记票，这里给私聊补上呈现）
    - first_message：第一条有正文的消息（纪念性；里程碑此前只有日期没有内容）

    口径：全部基于 statistical() 过滤后的消息，与 overview 同一门槛；句长只数
    非空正文（与 calc_message_length_stats 一致，媒体占位符不掺进来）。
    """
    msgs = chat.statistical()
    out: dict = {"months": [], "restarts": {}, "name_history": {}, "first_message": None}
    if not msgs:
        return out

    self_uid = chat.self_uid

    # —— 逐月计数与句长 ——
    monthly: dict[str, dict] = {}
    for m in msgs:
        month = m.time_str[:7]  # time_str 由解析器按北京时间重算，"YYYY-MM-DD HH:MM:SS"
        mm = monthly.setdefault(month, {"self": 0, "other": 0, "self_chars": 0, "other_chars": 0})
        side = "self" if m.sender_uid == self_uid else "other"
        mm[side] += 1
        if m.text:
            mm[side + "_chars"] += len(m.text)
    for month in sorted(monthly):
        mm = monthly[month]
        out["months"].append(
            {
                "month": month,
                "self": mm["self"],
                "other": mm["other"],
                "total": mm["self"] + mm["other"],
                "avg_len_self": round(mm["self_chars"] / mm["self"], 1) if mm["self"] else 0,
                "avg_len_other": round(mm["other_chars"] / mm["other"], 1) if mm["other"] else 0,
            }
        )

    # —— 中断重启 ——
    events: list[dict] = []
    prev = None
    for m in msgs:
        if prev is not None:
            gap_days = (m.timestamp - prev.timestamp) / 86_400_000  # ms → 天
            if gap_days >= RESTART_GAP_DAYS:
                events.append(
                    {
                        "gap_days": int(gap_days),
                        "date": m.time_str[:10],
                        "who": "self" if m.sender_uid == self_uid else "other",
                    }
                )
        prev = m
    out["restarts"] = {
        "threshold_days": RESTART_GAP_DAYS,
        "count": len(events),
        "self": sum(1 for e in events if e["who"] == "self"),
        "other": sum(1 for e in events if e["who"] == "other"),
        # 界面列最近 12 次即可（多年老群聊的冷场重启可能上百次，全量列表没有阅读价值）
        "events": events[-12:],
    }

    # —— 称呼变迁（显示名随导出时间的变化）——
    # 必须先 strip 再当键：带尾空格的昵称在真实导出里存在（同一份文件里见
    # qq_parser 与 group_identity 的同一条归一规则），不归一的话 "小明" 与 "小明 "
    # 会各占一个桶，仪表盘的「称呼变迁」卡片就会显示一次**从未发生过的改名**——
    # 而这张卡片的语义是"关系信号"，假信号比缺信号更糟。空名（纯空格）不是名字，
    # 一并跳过。
    names: dict[str, dict[str, dict]] = {"self": {}, "other": {}}
    for m in msgs:
        display = (m.sender_name or "").strip()
        if not display:
            continue
        side = "self" if m.sender_uid == self_uid else "other"
        rec = names[side].setdefault(
            display,
            {"name": display, "count": 0, "first_month": m.time_str[:7], "last_month": m.time_str[:7]},
        )
        rec["count"] += 1
        rec["last_month"] = m.time_str[:7]
    for side in names:
        items = sorted(names[side].values(), key=lambda r: r["first_month"])
        # 单个名字不叫"变迁"，但列表本身留着，模板按 len>1 决定是否呈现
        out["name_history"][side] = items

    # —— 第一条有正文的消息 ——
    for m in msgs:
        if m.text:
            out["first_message"] = {
                "date": m.time_str[:10],
                "time": m.time_str[11:16],
                "who": "self" if m.sender_uid == self_uid else "other",
                "name": m.sender_name,
                "text": m.text[:80] + ("…" if len(m.text) > 80 else ""),
            }
            break

    return out
