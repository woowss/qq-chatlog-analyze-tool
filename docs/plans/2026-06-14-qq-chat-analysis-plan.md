# QQ 聊天记录分析工具 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement plan task-by-task.

**Goal:** 构建一个 Flask Web 应用，可导入 QQChatExporter JSON、执行本地统计 + DeepSeek API 分析、以 ECharts 可视化展示

**Architecture:** Flask 后端 + 原生 HTML/JS 前端 + ECharts 图表。数据存入 Flask session，无外部数据库依赖。DeepSeek API 按月度分段分析聊天记录。

**Tech Stack:** Python 3.10+, Flask 3.x, OpenAI SDK (DeepSeek 兼容), ECharts 5, Bootstrap 5

**Root:** `E:\qqchatlog\`

---

## 文件结构

```
E:\qqchatlog\
├── app.py                  # Flask 主应用 + 路由
├── config.py               # 配置读取（API Key 等）
├── requirements.txt        # 依赖清单
├── .env.example            # API Key 模板文件
├── parser/
│   └── qq_parser.py        # QQ JSON → ChatData 解析
├── analyzer/
│   ├── __init__.py
│   ├── prompts.py          # DeepSeek System Prompt 常量
│   ├── local_stats.py      # 本地统计分析
│   └── deepseek_client.py  # DeepSeek API 调用封装
├── web/
│   ├── templates/
│   │   ├── base.html           # 基础布局模板
│   │   ├── index.html          # 首页 / 上传
│   │   ├── dashboard.html      # 总览仪表盘
│   │   ├── emotion.html        # 情绪分析
│   │   ├── relationship.html   # 人际关系
│   │   ├── habits.html         # 个人习惯
│   │   └── topics.html         # 话题趋势
│   └── static/
│       ├── css/
│       │   └── style.css
│       └── js/
│           └── charts.js
└── uploads/                # 上传 JSON 暂存
```

---

### Task 1: 项目脚手架

**Files:**
- Create: `E:\qqchatlog\requirements.txt`
- Create: `E:\qqchatlog\config.py`
- Create: `E:\qqchatlog\.env.example`
- Create: `E:\qqchatlog\uploads\.gitkeep`
- Create: `E:\qqchatlog\analyzer\__init__.py`

- [ ] **Step 1: Create requirements.txt**

```
flask>=3.0
openai>=1.0
python-dotenv>=1.0
```

- [ ] **Step 2: Create config.py**

```python
"""应用配置：从 .env 读取 DeepSeek API 配置"""
import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# 应用配置
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
SECRET_KEY = os.urandom(24).hex()
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB 上传限制
```

- [ ] **Step 3: Create .env.example**

```
# ============================================
# 【必填】DeepSeek API Key 配置
# ============================================
# 1. 访问 https://platform.deepseek.com/api_keys 登录
# 2. 创建 API Key
# 3. 复制 Key 替换下面等号后的内容
# ============================================
DEEPSEEK_API_KEY=你的DeepSeek_API_Key

# 可选配置（一般不需要修改）
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

- [ ] **Step 4: Create __init__.py**

```python
# analyzer package
```

- [ ] **Step 5: Create uploads/.gitkeep + directories**

```bash
mkdir -p E:\qqchatlog\uploads
# .gitkeep is an empty file
```

---

### Task 2: QQ JSON 解析器

**Files:**
- Create: `E:\qqchatlog\parser\qq_parser.py`

- [ ] **Step 1: Define data classes and parser**

```python
"""QQ JSON 聊天记录解析器 — 支持 QQChatExporter V5 格式"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    """单条消息"""
    id: str
    timestamp: int          # 毫秒时间戳
    time_str: str           # 格式化时间 "2025-09-16 21:56:49"
    sender_name: str        # 发送者显示名
    sender_uid: str         # 发送者 UID
    text: str               # 纯文本内容（不含图片/表情标记）
    raw_text: str           # 原始文本（含图片/表情占位符）
    msg_type: str           # type_1 / type_3 / type_11 等
    has_image: bool         # 是否包含图片
    is_reply: bool          # 是否为回复消息
    face_ids: list[int] = field(default_factory=list)  # 表情 ID 列表


@dataclass
class ChatData:
    """解析后的完整聊天数据"""
    chat_name: str          # 聊天对象名称
    self_name: str          # 自己的显示名
    other_name: str         # 对方的显示名
    self_uid: str           # 自己的 UID
    other_uid: str          # 对方 UID
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

    # 验证格式
    if "chatInfo" not in raw or "messages" not in raw:
        raise ValueError("无效的 QQChatExporter JSON 格式")

    chat_info = raw["chatInfo"]
    self_uid = chat_info.get("selfUid", "")
    self_name = chat_info.get("selfName", "")

    # 确定双方名称
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

        # 确定对方 UID
        if sender_uid != self_uid and not chat.other_uid:
            chat.other_uid = sender_uid

        content = msg.get("content", {})
        raw_text = content.get("text", "")
        elements = content.get("elements", [])

        # 提取纯文本（去掉图片/表情占位符）
        text_parts = []
        face_ids = []
        has_image = False
        is_reply = False

        for el in elements:
            el_type = el.get("type", "")
            el_data = el.get("data", {})
            if el_type == "text":
                text_parts.append(el_data.get("text", ""))
            elif el_type == "face":
                face_ids.append(el_data.get("id", 0))
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
        )
        chat.messages.append(parsed)

    # 按时间排序
    chat.messages.sort(key=lambda m: m.timestamp)

    # 填充统计信息
    stats = raw.get("statistics", {})
    chat.total_count = stats.get("totalMessages", len(chat.messages))
    time_range = stats.get("timeRange", {})
    chat.time_start = time_range.get("start", "")
    chat.time_end = time_range.get("end", "")
    chat.duration_days = time_range.get("durationDays", 0)

    return chat


def split_by_month(chat: ChatData) -> dict[str, list[Message]]:
    """按月分组消息，返回 { "2025-09": [messages] }"""
    groups: dict[str, list[Message]] = {}
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000)
        key = dt.strftime("%Y-%m")
        if key not in groups:
            groups[key] = []
        groups[key].append(msg)
    return dict(sorted(groups.items()))
```

---

### Task 3: 本地统计分析

**Files:**
- Create: `E:\qqchatlog\analyzer\local_stats.py`

- [ ] **Step 1: Implement all local statistics functions**

```python
"""本地统计分析 — 不依赖大模型 API"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from parser.qq_parser import ChatData, Message

CST = timezone(timedelta(hours=8))


def calc_daily_counts(chat: ChatData) -> list[dict]:
    """每日消息量时间序列，返回 [{"date": "2025-09-16", "self": 5, "other": 3}, ...]"""
    daily: dict[str, dict] = {}
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        date_key = dt.strftime("%Y-%m-%d")
        if date_key not in daily:
            daily[date_key] = {"date": date_key, "self": 0, "other": 0}
        key = "self" if msg.sender_uid == chat.self_uid else "other"
        daily[date_key][key] += 1
    return [daily[k] for k in sorted(daily.keys())]


def calc_hourly_distribution(chat: ChatData) -> list[dict]:
    """24小时分布，返回 [{"hour": 0, "self": 10, "other": 8}, ...]"""
    hourly: list[dict] = [
        {"hour": h, "self": 0, "other": 0} for h in range(24)
    ]
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        h = dt.hour
        key = "self" if msg.sender_uid == chat.self_uid else "other"
        hourly[h][key] += 1
    return hourly


def calc_weekly_distribution(chat: ChatData) -> list[dict]:
    """按星期分布（0=周一 ... 6=周日）"""
    weekly: list[dict] = [
        {"weekday": i, "self": 0, "other": 0} for i in range(7)
    ]
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        w = dt.weekday()
        key = "self" if msg.sender_uid == chat.self_uid else "other"
        weekly[w][key] += 1
    for i, w in enumerate(weekly):
        w["weekday_name"] = weekday_names[i]
    return weekly


def calc_message_length_stats(chat: ChatData) -> dict:
    """发言长度统计"""
    self_lengths = []
    other_lengths = []
    for msg in chat.messages:
        length = len(msg.text)
        if msg.sender_uid == chat.self_uid:
            self_lengths.append(length)
        else:
            other_lengths.append(length)

    def stats(lengths: list[int]) -> dict:
        if not lengths:
            return {"avg": 0, "max": 0, "min": 0, "median": 0, "total": 0}
        sorted_l = sorted(lengths)
        n = len(sorted_l)
        return {
            "avg": round(sum(sorted_l) / n, 1),
            "max": max(sorted_l),
            "min": min(sorted_l),
            "median": sorted_l[n // 2],
            "total": n,
        }

    return {"self": stats(self_lengths), "other": stats(other_lengths)}


def calc_face_stats(chat: ChatData) -> dict:
    """表情使用统计，返回 {"self": {face_id: count}, "other": {face_id: count}}"""
    self_faces: Counter = Counter()
    other_faces: Counter = Counter()
    for msg in chat.messages:
        if msg.sender_uid == chat.self_uid:
            self_faces.update(msg.face_ids)
        else:
            other_faces.update(msg.face_ids)
    return {
        "self": dict(self_faces.most_common(20)),
        "other": dict(other_faces.most_common(20)),
    }


def calc_response_time(chat: ChatData) -> dict:
    """计算平均响应时间（秒），分别统计双方"""
    self_times: list[float] = []
    other_times: list[float] = []
    for i in range(1, len(chat.messages)):
        prev = chat.messages[i - 1]
        curr = chat.messages[i]
        gap = (curr.timestamp - prev.timestamp) / 1000  # 秒
        if gap > 3600 * 6:  # 超过 6 小时不算同一轮对话
            continue
        if curr.sender_uid == chat.self_uid:
            self_times.append(gap)
        else:
            other_times.append(gap)

    def avg(times: list[float]) -> float:
        return round(sum(times) / len(times), 1) if times else 0

    return {"self_avg_seconds": avg(self_times), "other_avg_seconds": avg(other_times)}


def calc_exchange_rounds(chat: ChatData) -> int:
    """计算对话轮次（连续由同一人发言算一轮）"""
    rounds = 0
    last_sender = ""
    for msg in chat.messages:
        if msg.sender_uid != last_sender:
            rounds += 1
            last_sender = msg.sender_uid
    return rounds


def calc_weekly_activity(chat: ChatData) -> list[dict]:
    """星期×小时 热力图数据，返回 [{"weekday": 0, "hour": 0, "count": 5}, ...]"""
    grid: dict = {}
    for msg in chat.messages:
        dt = datetime.fromtimestamp(msg.timestamp / 1000, tz=CST)
        key = (dt.weekday(), dt.hour)
        grid[key] = grid.get(key, 0) + 1
    result = []
    for (w, h), count in grid.items():
        result.append({"weekday": w, "hour": h, "count": count})
    return result


def calc_overview(chat: ChatData) -> dict:
    """总览统计数据"""
    msg_count = len(chat.messages)
    daily_counts = calc_daily_counts(chat)
    days_with_msg = len(daily_counts)
    total_images = sum(1 for m in chat.messages if m.has_image)
    total_faces = sum(len(m.face_ids) for m in chat.messages)

    # 字数统计
    self_chars = sum(len(m.text) for m in chat.messages if m.sender_uid == chat.self_uid)
    other_chars = sum(len(m.text) for m in chat.messages if m.sender_uid != chat.self_uid)

    return {
        "total_messages": msg_count,
        "total_days": chat.duration_days or days_with_msg,
        "total_images": total_images,
        "total_faces": total_faces,
        "self_name": chat.self_name,
        "other_name": chat.other_name,
        "self_count": sum(1 for m in chat.messages if m.sender_uid == chat.self_uid),
        "other_count": sum(1 for m in chat.messages if m.sender_uid != chat.self_uid),
        "self_chars": self_chars,
        "other_chars": other_chars,
        "exchange_rounds": calc_exchange_rounds(chat),
        "avg_daily": round(msg_count / max(chat.duration_days or days_with_msg, 1), 1),
    }
```

---

### Task 4: AI 提示词常量

**Files:**
- Create: `E:\qqchatlog\analyzer\prompts.py`

- [ ] **Step 1: Define all system prompts**

```python
"""DeepSeek API 用 System Prompt 常量"""

SYSTEM_PROMPT_EMOTION = """你是一位精通中文社交语言分析的心理学专家。
你的任务是从私聊对话片段中分析双方的情绪状态和变化。

## 分析规则
- 根据用词、语气、标点、表情符号判断情绪
- 注意中文网络语言的情绪色彩（如"救命"可能是调侃而非真正绝望）
- 同一段内情绪可能有波动，取**主导情绪**
- 情绪强度 1-10，5 为中性基准
- 关键词提取最能体现情绪的核心短语，每方 2-5 个

## 必须严格遵守的输出 JSON 格式
{
  "self_emotion": "快乐 | 平静 | 焦虑 | 沮丧 | 愤怒 | 兴奋 | 疲惫 | 无奈 | 调侃 | 紧张",
  "other_emotion": "快乐 | 平静 | 焦虑 | 沮丧 | 愤怒 | 兴奋 | 疲惫 | 无奈 | 调侃 | 紧张",
  "self_intensity": 1-10 之间的整数,
  "other_intensity": 1-10 之间的整数,
  "self_keywords": ["情绪关键词1", "关键词2"],
  "other_keywords": ["情绪关键词1", "关键词2"],
  "overall_tone": "轻松愉快 | 严肃认真 | 平淡日常 | 紧张焦虑 | 温馨亲密"
}

注意：只返回 JSON，不要添加任何额外字段或说明。"""


SYSTEM_PROMPT_TOPICS = """你是一位话题建模分析师，擅长从对话中提取结构化话题。

## 分析规则
- 识别该月对话中出现的**核心话题**（3-6 个）
- 用词精炼（2-8 个字），如"项目开发"、"考试准备"、"日常闲聊"
- weight 表示话题在该月对话中的相对比重，所有话题 weight 之和不超过 1.0
- summary 用一句话概括该月对话主旋律
- topic_shift_detected 标记是否有明显的话题转变

## 必须严格遵守的输出 JSON 格式
{
  "topics": [
    {"name": "话题名", "weight": 0.35, "keywords": ["关键词1", "关键词2"]},
    {"name": "话题名", "weight": 0.25, "keywords": ["关键词1", "关键词2"]}
  ],
  "summary": "该月对话主要围绕...",
  "topic_shift_detected": false,
  "shift_description": ""
}

注意：对话内容太少无法分析时，返回 {"topics": [], "summary": "对话内容较少，无明显话题", "topic_shift_detected": false}"""


SYSTEM_PROMPT_RELATIONSHIP = """你是一位人际关系与沟通模式分析师。

## 分析规则
- 观察对话中的发起-响应模式：谁更常开启新话题？谁更常延续话题？
- 分析权力动态：谁在提要求/给建议？谁在提供情绪支持？
- 评估亲密程度：用词随意度、自我披露深度、幽默频率
- 注意中国校园/年轻人社交语境下的关系表达

## 必须严格遵守的输出 JSON 格式
{
  "initiator_tendency": "self | other | balanced",
  "initiator_ratio_self": 0.55,
  "interaction_style": "轻松调侃 | 深度交流 | 互助协作 | 日常问候 | 混合",
  "closeness_score": 1-10 之间的整数,
  "closeness_trend": "上升 | 下降 | 稳定",
  "self_role": "倾诉者 | 倾听者 | 建议者 | 吐槽伙伴 | 并肩作战",
  "other_role": "倾诉者 | 倾听者 | 建议者 | 吐槽伙伴 | 并肩作战",
  "emotional_support_self_to_other": "high | medium | low",
  "emotional_support_other_to_self": "high | medium | low",
  "relationship_summary": "一句话总结该段关系状态"
}

注意：只返回 JSON，不要添加额外字段。"""


SYSTEM_PROMPT_HABITS = """你是一位语言风格分析专家，专门分析中文网络聊天风格。

## 分析规则
- 基于该用户的发言，分析其独特的语言指纹
- 注意特征：句子长度偏好、网络用语习惯、表情符号使用模式、语气词、口头禅
- 标点使用习惯（是否喜欢用...、！！！、~ 等）
- 回复模式（秒回型/思考型/话题跳跃型）
- 不要过度解读，基于实际文本特征

## 必须严格遵守的输出 JSON 格式
{
  "personality_tags": ["幽默", "直率", "细腻", "简洁", "活泼", "理性", "感性", "毒舌", "温柔", "中二"],
  "common_phrases": ["口头禅1", "高频用语2"],
  "emoji_style": "丰富 | 适中 | 极少",
  "top_emojis": ["😊", "🤣"],
  "sentence_length": "短句为主 | 长短混合 | 长句较多",
  "reply_speed": "秒回型 | 适中 | 深思熟虑型",
  "topic_jumping": "经常跳跃 | 偶尔 | 专注一个话题",
  "unique_traits": ["独特习惯1", "独特习惯2"]
}

注意：只返回 JSON，不要添加额外字段。"""
```

---

### Task 5: DeepSeek API 客户端

**Files:**
- Create: `E:\qqchatlog\analyzer\deepseek_client.py`

- [ ] **Step 1: Implement DeepSeek API caller**

```python
"""DeepSeek API 调用封装"""
import json
import time
from typing import Any, Optional

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
from parser.qq_parser import ChatData, split_by_month
from analyzer.prompts import (
    SYSTEM_PROMPT_EMOTION,
    SYSTEM_PROMPT_TOPICS,
    SYSTEM_PROMPT_RELATIONSHIP,
    SYSTEM_PROMPT_HABITS,
)


def _get_client() -> Optional[OpenAI]:
    """获取 OpenAI 客户端（如果未配置 API Key 则返回 None）"""
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "你的DeepSeek_API_Key":
        return None
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def _build_dialog_text(messages: list, self_name: str, other_name: str) -> str:
    """将消息列表拼接为对话文本"""
    lines = []
    for m in messages:
        sender = self_name if m.sender_uid == messages[0].sender_uid else other_name
        # 简化处理：使用传入的 names
        lines.append(f"[{m.time_str}] {m.sender_name}: {m.text}")
    return "\n".join(lines)


def _build_dialog_with_names(messages: list, self_name: str, other_name: str) -> str:
    """拼接对话文本，统一显示名"""
    lines = []
    for m in messages:
        name = self_name if m.sender_uid == messages[0].sender_uid else other_name
        # Actually use the proper mapping
        name = self_name if m.sender_uid == m.sender_uid else other_name  # placeholder
        lines.append(f"[{m.time_str}] {name}: {m.text}")
    return "\n".join(lines)


def _call_api(system_prompt: str, user_content: str, retry: int = 2) -> Optional[dict]:
    """调用 DeepSeek API，返回解析后的 JSON"""
    client = _get_client()
    if client is None:
        return None

    for attempt in range(retry + 1):
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            if attempt < retry:
                time.sleep(2 ** attempt)
                continue
            raise e


def analyze_emotion(chat: ChatData) -> dict[str, Any]:
    """逐月分析情绪，返回 { "2025-09": {...}, ... }"""
    months = split_by_month(chat)
    results = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog_text(msgs, chat.self_name, chat.other_name)
        result = _call_api(SYSTEM_PROMPT_EMOTION, f"以下是 {period} 月的对话记录：\n\n{dialog}")
        if result:
            result["period"] = period
            result["month"] = period
            result["self_total"] = sum(1 for m in msgs if m.sender_uid == chat.self_uid)
            result["other_total"] = sum(1 for m in msgs if m.sender_uid != chat.self_uid)
            results[period] = result
    return results


def analyze_topics(chat: ChatData) -> dict[str, Any]:
    """逐月分析话题，返回 { "2025-09": {...}, ... }"""
    months = split_by_month(chat)
    results = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog_text(msgs, chat.self_name, chat.other_name)
        result = _call_api(SYSTEM_PROMPT_TOPICS, f"以下是 {period} 月的对话记录：\n\n{dialog}")
        if result:
            result["period"] = period
            result["month"] = period
            results[period] = result
    return results


def analyze_relationship(chat: ChatData) -> dict[str, Any]:
    """逐月分析人际关系"""
    months = split_by_month(chat)
    results = {}
    for period, msgs in months.items():
        if not msgs:
            continue
        dialog = _build_dialog_text(msgs, chat.self_name, chat.other_name)
        result = _call_api(SYSTEM_PROMPT_RELATIONSHIP, f"以下是 {period} 月的对话记录：\n\n{dialog}")
        if result:
            result["period"] = period
            result["month"] = period
            results[period] = result
    return results


def analyze_habits(chat: ChatData) -> dict[str, Any]:
    """按人分析习惯（将两人的消息分别发给 API）"""
    self_msgs = [m for m in chat.messages if m.sender_uid == chat.self_uid]
    other_msgs = [m for m in chat.messages if m.sender_uid != chat.self_uid]

    results = {}

    for person_name, msgs in [("self", self_msgs), ("other", other_msgs)]:
        # 取最近 200 条消息作为样本（避免过长）
        sample = msgs[-200:]
        dialog_lines = [f"[{m.time_str}] {m.text}" for m in sample]
        dialog = "\n".join(dialog_lines)
        display_name = chat.self_name if person_name == "self" else chat.other_name
        result = _call_api(
            SYSTEM_PROMPT_HABITS,
            f"分析以下 {display_name} 的发言，总结其说话风格：\n\n{dialog}",
        )
        if result:
            result["name"] = display_name
            result["total_messages"] = len(msgs)
            results[person_name] = result

    return results


def analyze_all(chat: ChatData) -> dict:
    """一次性运行所有分析"""
    return {
        "emotion": analyze_emotion(chat),
        "topics": analyze_topics(chat),
        "relationship": analyze_relationship(chat),
        "habits": analyze_habits(chat),
    }


def is_api_configured() -> bool:
    """检查 API Key 是否已配置"""
    return bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "你的DeepSeek_API_Key"
```

---

### Task 6: Flask 主应用

**Files:**
- Create: `E:\qqchatlog\app.py`

- [ ] **Step 1: Write the Flask app**

```python
"""QQ 聊天记录分析工具 — Flask 主应用"""
import json
import os
import uuid

from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from config import UPLOAD_FOLDER, SECRET_KEY
from parser.qq_parser import load_chat
from analyzer.local_stats import (
    calc_overview,
    calc_daily_counts,
    calc_hourly_distribution,
    calc_weekly_distribution,
    calc_message_length_stats,
    calc_face_stats,
    calc_response_time,
    calc_exchange_rounds,
    calc_weekly_activity,
)
from analyzer.deepseek_client import (
    analyze_all,
    analyze_emotion,
    analyze_topics,
    analyze_relationship,
    analyze_habits,
    is_api_configured,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


@app.route("/")
def index():
    """首页 / 上传页面"""
    api_ok = is_api_configured()
    return render_template("index.html", api_ok=api_ok)


@app.route("/upload", methods=["POST"])
def upload():
    """接收上传的 JSON 文件，解析并存入 session"""
    if "file" not in request.files:
        return "请选择文件", 400

    file = request.files["file"]
    if file.filename == "":
        return "请选择文件", 400

    if not file.filename.endswith(".json"):
        return "仅支持 .json 文件", 400

    # 保存文件
    filename = f"{uuid.uuid4().hex}.json"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        chat = load_chat(filepath)
    except Exception as e:
        return f"解析失败：{str(e)}", 400

    # 存入 session
    session["chat_name"] = chat.chat_name
    session["self_name"] = chat.self_name
    session["other_name"] = chat.other_name
    session["filepath"] = filepath

    # 计算本地统计
    session["overview"] = calc_overview(chat)
    session["daily_counts"] = calc_daily_counts(chat)
    session["hourly_dist"] = calc_hourly_distribution(chat)
    session["weekly_dist"] = calc_weekly_distribution(chat)
    session["length_stats"] = calc_message_length_stats(chat)
    session["face_stats"] = calc_face_stats(chat)
    session["response_time"] = calc_response_time(chat)
    session["exchange_rounds"] = calc_exchange_rounds(chat)
    session["weekly_activity"] = calc_weekly_activity(chat)

    # 将 chat 序列化到 session（简化处理）
    session["total_messages"] = len(chat.messages)

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    """总览仪表盘"""
    if "overview" not in session:
        return redirect(url_for("index"))

    api_ok = is_api_configured()
    return render_template(
        "dashboard.html",
        overview=session.get("overview"),
        daily_counts=session.get("daily_counts"),
        hourly_dist=session.get("hourly_dist"),
        weekly_dist=session.get("weekly_dist"),
        length_stats=session.get("length_stats"),
        exchange_rounds=session.get("exchange_rounds"),
        api_ok=api_ok,
        chat_name=session.get("chat_name"),
    )


@app.route("/emotion")
def emotion():
    """情绪分析页面"""
    if "overview" not in session:
        return redirect(url_for("index"))
    api_ok = is_api_configured()
    return render_template("emotion.html", api_ok=api_ok, overview=session.get("overview"))


@app.route("/relationship")
def relationship():
    """人际关系页面"""
    if "overview" not in session:
        return redirect(url_for("index"))
    api_ok = is_api_configured()
    resp_time = session.get("response_time", {})
    exchange = session.get("exchange_rounds", 0)
    return render_template(
        "relationship.html",
        api_ok=api_ok,
        overview=session.get("overview"),
        response_time=resp_time,
        exchange_rounds=exchange,
    )


@app.route("/habits")
def habits():
    """个人习惯页面"""
    if "overview" not in session:
        return redirect(url_for("index"))
    api_ok = is_api_configured()
    return render_template(
        "habits.html",
        api_ok=api_ok,
        overview=session.get("overview"),
        face_stats=session.get("face_stats"),
        length_stats=session.get("length_stats"),
    )


@app.route("/topics")
def topics():
    """话题趋势页面"""
    if "overview" not in session:
        return redirect(url_for("index"))
    api_ok = is_api_configured()
    return render_template("topics.html", api_ok=api_ok, overview=session.get("overview"))


# ---------- AI 分析 API ----------


@app.route("/api/analyze/<dimension>", methods=["POST"])
def api_analyze(dimension: str):
    """调用 DeepSeek 分析指定维度"""
    if "filepath" not in session:
        return jsonify({"error": "请先上传聊天记录"}), 400

    if not is_api_configured():
        return jsonify({"error": "API Key 未配置"}), 400

    filepath = session["filepath"]
    if not os.path.exists(filepath):
        return jsonify({"error": "会话文件已过期，请重新上传"}), 400

    try:
        chat = load_chat(filepath)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    dimension_map = {
        "emotion": analyze_emotion,
        "topics": analyze_topics,
        "relationship": analyze_relationship,
        "habits": analyze_habits,
    }

    if dimension not in dimension_map:
        return jsonify({"error": f"未知分析维度: {dimension}"}), 400

    try:
        result = dimension_map[dimension](chat)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"AI 分析失败: {str(e)}"}), 500


@app.route("/api/status")
def api_status():
    """API 配置状态"""
    return jsonify({"api_ok": is_api_configured()})


if __name__ == "__main__":
    print("=" * 50)
    print("  QQ 聊天记录分析工具")
    print("  访问地址: http://localhost:5000")
    print("=" * 50)
    if not is_api_configured():
        print("  ⚠️  DeepSeek API Key 未配置")
        print(f"  请编辑 {os.path.join(os.path.dirname(__file__), '.env')} 填入 Key")
    else:
        print("  ✅ DeepSeek API 已配置")
    print("=" * 50)
    app.run(debug=True, host="127.0.0.1", port=5000)
```

---

### Task 7: 基础模板和首页

**Files:**
- Create: `E:\qqchatlog\web\templates\base.html`
- Create: `E:\qqchatlog\web\templates\index.html`

- [ ] **Step 1: Create base layout template**

```html
<!-- web/templates/base.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>QQ 聊天记录分析 - {% block title %}首页{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
    {% block head %}{% endblock %}
</head>
<body>
    <!-- 导航栏 -->
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
        <div class="container">
            <a class="navbar-brand" href="/">📊 QQ 聊天分析</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav me-auto">
                    <li class="nav-item"><a class="nav-link" href="/">首页</a></li>
                    {% if overview %}
                    <li class="nav-item"><a class="nav-link" href="/dashboard">仪表盘</a></li>
                    <li class="nav-item"><a class="nav-link" href="/emotion">😊 情绪</a></li>
                    <li class="nav-item"><a class="nav-link" href="/relationship">👥 关系</a></li>
                    <li class="nav-item"><a class="nav-link" href="/habits">🧑 习惯</a></li>
                    <li class="nav-item"><a class="nav-link" href="/topics">📈 话题</a></li>
                    {% endif %}
                </ul>
                <span class="navbar-text">
                    {% if api_ok %}
                    <span class="badge bg-success">✅ API 已配置</span>
                    {% else %}
                    <span class="badge bg-warning text-dark">⚠️ API 未配置</span>
                    {% endif %}
                </span>
            </div>
        </div>
    </nav>

    <!-- 主内容 -->
    <div class="container">
        {% block content %}{% endblock %}
    </div>

    <footer class="footer mt-5 py-3 bg-light">
        <div class="container text-center text-muted small">
            QQ 聊天记录分析工具 · 数据仅保存在本地
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"></script>
    <script src="{{ url_for('static', filename='js/charts.js') }}"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 2: Create index.html**

```html
<!-- web/templates/index.html -->
{% extends "base.html" %}
{% block title %}首页{% endblock %}
{% block content %}
<div class="row justify-content-center">
    <div class="col-lg-8">
        <div class="card shadow-sm">
            <div class="card-body text-center p-5">
                <h1 class="display-5 mb-3">📊 QQ 聊天记录分析</h1>
                <p class="lead text-muted mb-4">
                    导入 QQChatExporter 导出的 JSON 聊天记录，<br>
                    通过 DeepSeek AI 分析你们的情绪、话题、关系和习惯
                </p>

                {% if not api_ok %}
                <div class="alert alert-warning">
                    <strong>⚠️ DeepSeek API Key 未配置</strong><br>
                    <span class="small">本地统计功能可用，AI 分析需配置 Key。请编辑项目根目录的 <code>.env</code> 文件。</span>
                </div>
                {% endif %}

                <!-- 上传区域 -->
                <form action="/upload" method="post" enctype="multipart/form-data" id="uploadForm">
                    <div class="upload-zone mb-3" id="dropZone">
                        <div class="upload-icon">📁</div>
                        <p class="mb-2">拖拽 JSON 文件到此处，或点击选择文件</p>
                        <input type="file" name="file" id="fileInput" accept=".json" class="d-none">
                        <button type="button" class="btn btn-outline-primary" onclick="$('#fileInput').click()">
                            选择文件
                        </button>
                        <div id="fileName" class="mt-2 small text-success d-none"></div>
                    </div>
                    <button type="submit" class="btn btn-primary btn-lg" id="submitBtn" disabled>
                        🚀 开始分析
                    </button>
                </form>

                <hr class="my-4">

                <div class="text-start small text-muted">
                    <h6>📋 使用说明</h6>
                    <ol>
                        <li>使用 <a href="https://github.com/shuakami/qq-chat-exporter" target="_blank">QQChatExporter</a> 导出私聊 JSON</li>
                        <li>上传导出的 .json 文件</li>
                        <li>查看本地统计仪表盘</li>
                        <li>点击 AI 分析按钮获取深度洞察</li>
                    </ol>
                    <h6>🔒 隐私说明</h6>
                    <p>所有数据仅在本地处理，上传的文件不会离开你的设备。</p>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
$(function() {
    // 文件选择提示
    $('#fileInput').on('change', function() {
        const file = this.files[0];
        if (file) {
            $('#fileName').text('✅ 已选择: ' + file.name).removeClass('d-none');
            $('#submitBtn').prop('disabled', false);
        }
    });

    // 拖拽上传
    const dropZone = $('#dropZone');
    dropZone.on('dragover', function(e) {
        e.preventDefault();
        $(this).addClass('drag-over');
    });
    dropZone.on('dragleave', function() {
        $(this).removeClass('drag-over');
    });
    dropZone.on('drop', function(e) {
        e.preventDefault();
        $(this).removeClass('drag-over');
        const files = e.originalEvent.dataTransfer.files;
        if (files.length > 0) {
            $('#fileInput')[0].files = files;
            $('#fileName').text('✅ 已选择: ' + files[0].name).removeClass('d-none');
            $('#submitBtn').prop('disabled', false);
        }
    });

    // 上传中
    $('#uploadForm').on('submit', function() {
        $('#submitBtn').prop('disabled', true).text('⏳ 上传解析中...');
    });
});
</script>
{% endblock %}
```

---

### Task 8: 仪表盘模板

**Files:**
- Create: `E:\qqchatlog\web\templates\dashboard.html`

- [ ] **Step 1: Create dashboard.html**

```html
<!-- web/templates/dashboard.html -->
{% extends "base.html" %}
{% block title %}仪表盘{% endblock %}

{% block content %}
<h2 class="mb-4">📊 总览仪表盘 — <span class="text-primary">{{ chat_name }}</span></h2>

<!-- 统计卡片 -->
<div class="row mb-4">
    <div class="col-md-3 mb-3">
        <div class="card stat-card text-center h-100">
            <div class="card-body">
                <div class="stat-value">{{ overview.total_messages }}</div>
                <div class="stat-label">总消息数</div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card text-center h-100">
            <div class="card-body">
                <div class="stat-value">{{ overview.total_days }}</div>
                <div class="stat-label">聊天天数</div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card text-center h-100">
            <div class="card-body">
                <div class="stat-value">{{ overview.avg_daily }}</div>
                <div class="stat-label">日均消息</div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card text-center h-100">
            <div class="card-body">
                <div class="stat-value">{{ overview.total_images }}</div>
                <div class="stat-label">图片总数</div>
            </div>
        </div>
    </div>
</div>

<!-- 双方对比 -->
<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">👤 双方消息对比</div>
            <div class="card-body">
                <div id="messageRatioChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">📝 发言字数对比</div>
            <div class="card-body">
                <div id="charRatioChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<!-- 每日消息量 -->
<div class="card mb-4">
    <div class="card-header">📅 每日消息量</div>
    <div class="card-body">
        <div id="dailyChart" style="height: 350px;"></div>
    </div>
</div>

<!-- 时段分布 + 星期分布 -->
<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">⏰ 活跃时段分布</div>
            <div class="card-body">
                <div id="hourlyChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">📆 星期分布</div>
            <div class="card-body">
                <div id="weeklyChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<!-- 额外信息 -->
<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">📏 平均发言长度</div>
            <div class="card-body text-center">
                <div class="row">
                    <div class="col-6">
                        <div class="stat-value text-primary">{{ length_stats.self.avg }}</div>
                        <div class="stat-label">{{ overview.self_name }}</div>
                        <small class="text-muted">共 {{ overview.self_count }} 条</small>
                    </div>
                    <div class="col-6">
                        <div class="stat-value text-success">{{ length_stats.other.avg }}</div>
                        <div class="stat-label">{{ overview.other_name }}</div>
                        <small class="text-muted">共 {{ overview.other_count }} 条</small>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">💬 对话轮次</div>
            <div class="card-body text-center">
                <div class="stat-value">{{ exchange_rounds }}</div>
                <div class="stat-label">次话题切换</div>
            </div>
        </div>
    </div>
</div>

<!-- AI 分析入口 -->
{% if api_ok %}
<div class="card mb-4 border-primary">
    <div class="card-header bg-primary text-white">🤖 AI 深度分析</div>
    <div class="card-body text-center">
        <p class="mb-3">点击按钮调用 DeepSeek API 分析聊天记录（请确保 .env 中已配置 API Key）</p>
        <div class="btn-group" role="group">
            <button class="btn btn-outline-primary btn-analyze" data-dim="emotion">😊 情绪分析</button>
            <button class="btn btn-outline-primary btn-analyze" data-dim="relationship">👥 人际关系</button>
            <button class="btn btn-outline-primary btn-analyze" data-dim="habits">🧑 个人习惯</button>
            <button class="btn btn-outline-primary btn-analyze" data-dim="topics">📈 话题趋势</button>
        </div>
        <div id="analyzeStatus" class="mt-3"></div>
    </div>
</div>

<script>
$('.btn-analyze').on('click', function() {
    const dim = $(this).data('dim');
    const btn = $(this);
    const status = $('#analyzeStatus');
    btn.prop('disabled', true).text('⏳ 分析中...');
    status.html('<div class="spinner-border text-primary" role="status"></div> 正在分析，请稍候...');

    $.post('/api/analyze/' + dim, function(data) {
        if (data.error) {
            status.html('<div class="alert alert-danger">' + data.error + '</div>');
        } else {
            status.html('<div class="alert alert-success">✅ 分析完成！请切换到对应页面查看。</div>');
            sessionStorage.setItem('ai_' + dim, JSON.stringify(data));
        }
    }).fail(function(xhr) {
        status.html('<div class="alert alert-danger">分析失败: ' + xhr.responseJSON?.error + '</div>');
    }).always(function() {
        btn.prop('disabled', false).text(btn.text().replace('⏳ 分析中...', btn.data('dim')));
        location.href = '/' + dim;
    });
});
</script>
{% endif %}

<script>
// 消息占比
renderPieChart('messageRatioChart', [
    {name: '{{ overview.self_name }}', value: {{ overview.self_count }}},
    {name: '{{ overview.other_name }}', value: {{ overview.other_count }}}
], '消息数');

// 字数占比
renderPieChart('charRatioChart', [
    {name: '{{ overview.self_name }}', value: {{ overview.self_chars }}},
    {name: '{{ overview.other_name }}', value: {{ overview.other_chars }}}
], '字数');

// 每日消息量
renderLineChart('dailyChart', {{ daily_counts | tojson }}, '消息数');

// 时段分布
renderBarChart('hourlyChart', {{ hourly_dist | tojson }}, '消息数');

// 星期分布
renderWeeklyChart('weeklyChart', {{ weekly_dist | tojson }}, '消息数');
</script>
{% endblock %}
```

---

### Task 9: 子页面模板

**Files:**
- Create: `E:\qqchatlog\web\templates\emotion.html`
- Create: `E:\qqchatlog\web\templates\relationship.html`
- Create: `E:\qqchatlog\web\templates\habits.html`
- Create: `E:\qqchatlog\web\templates\topics.html`

- [ ] **Step 1: Create emotion.html**

```html
{% extends "base.html" %}
{% block title %}情绪分析{% endblock %}
{% block content %}
<h2 class="mb-4">😊 情绪分析</h2>

{% if not api_ok %}
<div class="alert alert-warning">⚠️ 请先配置 DeepSeek API Key 以使用 AI 分析功能</div>
{% endif %}

<div class="row mb-4">
    <div class="col-12">
        <div class="card">
            <div class="card-header">📈 情绪强度变化</div>
            <div class="card-body">
                <div id="emotionLineChart" style="height: 400px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">😊 {{ overview.self_name }} 情绪分布</div>
            <div class="card-body">
                <div id="selfEmotionPie" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">😊 {{ overview.other_name }} 情绪分布</div>
            <div class="card-body">
                <div id="otherEmotionPie" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="card mb-4">
    <div class="card-header">📋 逐月情绪详情</div>
    <div class="card-body" id="emotionDetails">
        <p class="text-muted text-center">请先点击仪表盘的"情绪分析"按钮获取数据</p>
    </div>
</div>

<script>
$(function() {
    const data = sessionStorage.getItem('ai_emotion');
    if (data) {
        const parsed = JSON.parse(data);
        renderEmotionCharts(parsed);
    }
});
</script>
{% endblock %}
```

- [ ] **Step 2: Create relationship.html**

```html
{% extends "base.html" %}
{% block title %}人际关系{% endblock %}
{% block content %}
<h2 class="mb-4">👥 人际关系分析</h2>

{% if not api_ok %}
<div class="alert alert-warning">⚠️ 请先配置 DeepSeek API Key 以使用 AI 分析功能</div>
{% endif %}

<div class="row mb-4">
    <div class="col-md-4 mb-3">
        <div class="card text-center h-100">
            <div class="card-body">
                <div class="stat-value">{{ exchange_rounds }}</div>
                <div class="stat-label">对话轮次</div>
            </div>
        </div>
    </div>
    <div class="col-md-4 mb-3">
        <div class="card text-center h-100">
            <div class="card-body">
                <div class="stat-value text-primary">{{ response_time.self_avg_seconds }}s</div>
                <div class="stat-label">{{ overview.self_name }} 平均回复时间</div>
            </div>
        </div>
    </div>
    <div class="col-md-4 mb-3">
        <div class="card text-center h-100">
            <div class="card-body">
                <div class="stat-value text-success">{{ response_time.other_avg_seconds }}s</div>
                <div class="stat-label">{{ overview.other_name }} 平均回复时间</div>
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">📊 消息占比</div>
            <div class="card-body">
                <div id="relationPieChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">⚡ 平均回复速度对比</div>
            <div class="card-body">
                <div id="responseChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="card mb-4">
    <div class="card-header">🤖 AI 关系洞察</div>
    <div class="card-body" id="relationshipInsight">
        <p class="text-muted text-center">请先点击仪表盘的"人际关系"按钮获取 AI 分析</p>
    </div>
</div>

<script>
$(function() {
    renderPieChart('relationPieChart', [
        {name: '{{ overview.self_name }}', value: {{ overview.self_count }}},
        {name: '{{ overview.other_name }}', value: {{ overview.other_count }}}
    ], '消息数');

    renderResponseChart('responseChart', {
        self: {{ response_time.self_avg_seconds }},
        other: {{ response_time.other_avg_seconds }},
        selfName: '{{ overview.self_name }}',
        otherName: '{{ overview.other_name }}'
    });

    const data = sessionStorage.getItem('ai_relationship');
    if (data) {
        const parsed = JSON.parse(data);
        renderRelationshipInsight(parsed);
    }
});
</script>
{% endblock %}
```

- [ ] **Step 3: Create habits.html**

```html
{% extends "base.html" %}
{% block title %}个人习惯{% endblock %}
{% block content %}
<h2 class="mb-4">🧑 个人习惯与风格</h2>

{% if not api_ok %}
<div class="alert alert-warning">⚠️ 请先配置 DeepSeek API Key 以使用 AI 分析功能</div>
{% endif %}

<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">{{ overview.self_name }} 的表情排行</div>
            <div class="card-body">
                <div id="selfFaceChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-header">{{ overview.other_name }} 的表情排行</div>
            <div class="card-body">
                <div id="otherFaceChart" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-12">
        <div class="card">
            <div class="card-header">⏰ 一周活跃热力图</div>
            <div class="card-body">
                <div id="heatmapChart" style="height: 350px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="card mb-4">
    <div class="card-header">🤖 AI 风格分析</div>
    <div class="card-body" id="habitsInsight">
        <p class="text-muted text-center">请先点击仪表盘的"个人习惯"按钮获取 AI 分析</p>
    </div>
</div>

<script>
$(function() {
    const data = sessionStorage.getItem('ai_habits');
    if (data) {
        const parsed = JSON.parse(data);
        renderHabitsInsight(parsed);
    }
});
</script>
{% endblock %}
```

- [ ] **Step 4: Create topics.html**

```html
{% extends "base.html" %}
{% block title %}话题趋势{% endblock %}
{% block content %}
<h2 class="mb-4">📈 话题趋势分析</h2>

{% if not api_ok %}
<div class="alert alert-warning">⚠️ 请先配置 DeepSeek API Key 以使用 AI 分析功能</div>
{% endif %}

<div class="card mb-4">
    <div class="card-header">📊 话题分布</div>
    <div class="card-body">
        <div id="topicsChart" style="height: 400px;"></div>
    </div>
</div>

<div class="card mb-4">
    <div class="card-header">📋 逐月话题摘要</div>
    <div class="card-body" id="topicsDetails">
        <p class="text-muted text-center">请先点击仪表盘的"话题趋势"按钮获取 AI 分析</p>
    </div>
</div>

<script>
$(function() {
    const data = sessionStorage.getItem('ai_topics');
    if (data) {
        const parsed = JSON.parse(data);
        renderTopicsCharts(parsed);
    }
});
</script>
{% endblock %}
```

---

### Task 10: 静态资源

**Files:**
- Create: `E:\qqchatlog\web\static\css\style.css`
- Create: `E:\qqchatlog\web\static\js\charts.js`

- [ ] **Step 1: Create style.css**

```css
/* web/static/css/style.css */
body {
    background-color: #f5f7fa;
    font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
}

/* 统计卡片 */
.stat-card {
    border: none;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    transition: transform 0.2s;
}
.stat-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 16px rgba(0,0,0,0.1);
}
.stat-value {
    font-size: 2.2rem;
    font-weight: 700;
    color: #1a1a2e;
}
.stat-label {
    font-size: 0.9rem;
    color: #6c757d;
    margin-top: 4px;
}

/* 上传区域 */
.upload-zone {
    border: 2px dashed #ccc;
    border-radius: 16px;
    padding: 40px 20px;
    background: #fafafa;
    cursor: pointer;
    transition: all 0.3s;
}
.upload-zone:hover, .upload-zone.drag-over {
    border-color: #0d6efd;
    background: #eaf4ff;
}
.upload-icon {
    font-size: 3rem;
    margin-bottom: 10px;
}

/* 卡片通用 */
.card {
    border: none;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.card-header {
    background: white;
    border-bottom: 1px solid #f0f0f0;
    font-weight: 600;
    border-radius: 12px 12px 0 0 !important;
}

/* 导航栏 */
.navbar {
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}

/* 页脚 */
.footer {
    border-top: 1px solid #eee;
}

/* 响应式调整 */
@media (max-width: 768px) {
    .stat-value {
        font-size: 1.6rem;
    }
    .btn-group {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }
}
```

- [ ] **Step 2: Create charts.js**

```javascript
// web/static/js/charts.js
// ECharts 通用图表渲染函数

/**
 * 渲染饼图
 */
function renderPieChart(domId, data, name) {
    const chart = echarts.init(document.getElementById(domId));
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de'];
    chart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: { bottom: 0 },
        series: [{
            type: 'pie',
            radius: ['40%', '65%'],
            center: ['50%', '45%'],
            data: data.map((d, i) => ({ ...d, itemStyle: { color: colors[i % colors.length] } })),
            label: { show: true, formatter: '{b}\n{d}%' },
            emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } }
        }]
    });
    window.addEventListener('resize', () => chart.resize());
}

/**
 * 渲染折线图
 */
function renderLineChart(domId, data, yName) {
    const chart = echarts.init(document.getElementById(domId));
    const dates = data.map(d => d.date);
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        xAxis: {
            type: 'category',
            data: dates,
            axisLabel: { rotate: 45, fontSize: 10 }
        },
        yAxis: { type: 'value', name: yName },
        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
        series: [
            {
                name: '对方',
                type: 'line',
                data: data.map(d => d.other),
                smooth: true,
                lineStyle: { color: '#91cc75' },
                itemStyle: { color: '#91cc75' },
                areaStyle: { color: 'rgba(145, 204, 117, 0.15)' }
            },
            {
                name: '自己',
                type: 'line',
                data: data.map(d => d.self),
                smooth: true,
                lineStyle: { color: '#5470c6' },
                itemStyle: { color: '#5470c6' },
                areaStyle: { color: 'rgba(84, 112, 198, 0.15)' }
            }
        ]
    });
    window.addEventListener('resize', () => chart.resize());
}

/**
 * 渲染柱状图
 */
function renderBarChart(domId, data, yName) {
    const chart = echarts.init(document.getElementById(domId));
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        xAxis: { type: 'category', data: data.map(d => d.hour + '时') },
        yAxis: { type: 'value', name: yName },
        series: [
            {
                name: '对方',
                type: 'bar',
                data: data.map(d => d.other),
                itemStyle: { color: '#91cc75', borderRadius: [4,4,0,0] }
            },
            {
                name: '自己',
                type: 'bar',
                data: data.map(d => d.self),
                itemStyle: { color: '#5470c6', borderRadius: [4,4,0,0] }
            }
        ]
    });
    window.addEventListener('resize', () => chart.resize());
}

/**
 * 渲染星期分布
 */
function renderWeeklyChart(domId, data) {
    const chart = echarts.init(document.getElementById(domId));
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['对方', '自己'], bottom: 0 },
        xAxis: { type: 'category', data: data.map(d => d.weekday_name) },
        yAxis: { type: 'value', name: '消息数' },
        series: [
            {
                name: '对方',
                type: 'bar',
                data: data.map(d => d.other),
                itemStyle: { color: '#91cc75', borderRadius: [4,4,0,0] }
            },
            {
                name: '自己',
                type: 'bar',
                data: data.map(d => d.self),
                itemStyle: { color: '#5470c6', borderRadius: [4,4,0,0] }
            }
        ]
    });
    window.addEventListener('resize', () => chart.resize());
}

/**
 * 渲染响应速度对比图
 */
function renderResponseChart(domId, data) {
    const chart = echarts.init(document.getElementById(domId));
    chart.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: [data.selfName, data.otherName] },
        yAxis: { type: 'value', name: '秒' },
        series: [{
            type: 'bar',
            data: [
                { value: data.self, itemStyle: { color: '#5470c6' } },
                { value: data.other, itemStyle: { color: '#91cc75' } }
            ],
            barWidth: '40%',
            label: { show: true, formatter: '{c}s', position: 'top' }
        }]
    });
    window.addEventListener('resize', () => chart.resize());
}

/**
 * 渲染情绪图表
 */
function renderEmotionCharts(data) {
    const months = Object.keys(data).sort();
    const selfIntensity = months.map(m => data[m].self_intensity);
    const otherIntensity = months.map(m => data[m].other_intensity);
    const selfEmotions = months.map(m => data[m].self_emotion);
    const otherEmotions = months.map(m => data[m].other_emotion);

    // 情绪强度折线图
    const lineChart = echarts.init(document.getElementById('emotionLineChart'));
    lineChart.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                const idx = params[0].dataIndex;
                const month = months[idx];
                let html = `<strong>${month}</strong><br>`;
                params.forEach(p => {
                    html += `${p.marker} ${p.seriesName}: ${p.value}<br>`;
                });
                html += `😊 自己: ${selfEmotions[idx]}<br>`;
                html += `😊 对方: ${otherEmotions[idx]}`;
                return html;
            }
        },
        legend: { data: ['自己情绪强度', '对方情绪强度'], bottom: 0 },
        xAxis: { type: 'category', data: months },
        yAxis: { type: 'value', name: '情绪强度', min: 1, max: 10 },
        series: [
            {
                name: '自己情绪强度',
                type: 'line',
                data: selfIntensity,
                smooth: true,
                lineStyle: { color: '#5470c6', width: 3 },
                itemStyle: { color: '#5470c6' },
                areaStyle: { color: 'rgba(84,112,198,0.15)' }
            },
            {
                name: '对方情绪强度',
                type: 'line',
                data: otherIntensity,
                smooth: true,
                lineStyle: { color: '#91cc75', width: 3 },
                itemStyle: { color: '#91cc75' },
                areaStyle: { color: 'rgba(145,204,117,0.15)' }
            }
        ]
    });

    // 情绪分布（简化：统计各情绪出现次数）
    const selfEmoCount = {};
    const otherEmoCount = {};
    selfEmotions.forEach(e => { selfEmoCount[e] = (selfEmoCount[e] || 0) + 1; });
    otherEmotions.forEach(e => { otherEmoCount[e] = (otherEmoCount[e] || 0) + 1; });

    renderPieChart('selfEmotionPie',
        Object.entries(selfEmoCount).map(([k,v]) => ({name: k, value: v})),
        '月份数'
    );
    renderPieChart('otherEmotionPie',
        Object.entries(otherEmoCount).map(([k,v]) => ({name: k, value: v})),
        '月份数'
    );

    // 情绪详情
    let html = '';
    months.forEach(m => {
        const d = data[m];
        html += `<div class="card mb-2">
            <div class="card-body py-2">
                <strong>${m}</strong>
                <span class="badge bg-primary ms-2">自己: ${d.self_emotion}(${d.self_intensity})</span>
                <span class="badge bg-success ms-1">对方: ${d.other_emotion}(${d.other_intensity})</span>
                <span class="badge bg-info ms-1">基调: ${d.overall_tone}</span>
                <div class="mt-1 small text-muted">
                    自己关键词: ${(d.self_keywords || []).join('、')}<br>
                    对方关键词: ${(d.other_keywords || []).join('、')}
                </div>
            </div>
        </div>`;
    });
    $('#emotionDetails').html(html);
    window.addEventListener('resize', () => { lineChart.resize(); });
}

/**
 * 渲染关系洞察
 */
function renderRelationshipInsight(data) {
    let html = '';
    const months = Object.keys(data).sort();
    months.forEach(m => {
        const d = data[m];
        html += `<div class="card mb-2">
            <div class="card-body py-2">
                <strong>${m}</strong>
                <span class="badge bg-info ms-2">亲密: ${d.closeness_score}/10</span>
                <span class="badge bg-secondary ms-1">趋势: ${d.closeness_trend}</span>
                <span class="badge bg-warning ms-1">风格: ${d.interaction_style}</span>
                <div class="mt-1 small text-muted">
                    自己角色: ${d.self_role} · 对方角色: ${d.other_role}<br>
                    ${d.relationship_summary || ''}
                </div>
            </div>
        </div>`;
    });
    if (html) {
        $('#relationshipInsight').html(html);
    }
}

/**
 * 渲染习惯洞察
 */
function renderHabitsInsight(data) {
    let html = '';
    ['self', 'other'].forEach(key => {
        const d = data[key];
        if (!d) return;
        html += `<div class="card mb-3">
            <div class="card-header">${d.name}</div>
            <div class="card-body">
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>性格标签:</strong> ${(d.personality_tags || []).join('、')}</p>
                        <p><strong>口头禅:</strong> ${(d.common_phrases || []).join('、')}</p>
                        <p><strong>表情风格:</strong> ${d.emoji_style}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>句子长度:</strong> ${d.sentence_length}</p>
                        <p><strong>回复速度:</strong> ${d.reply_speed}</p>
                        <p><strong>话题跳跃:</strong> ${d.topic_jumping}</p>
                    </div>
                </div>
                <p><strong>独特习惯:</strong> ${(d.unique_traits || []).join('、')}</p>
            </div>
        </div>`;
    });
    if (html) {
        $('#habitsInsight').html(html);
    }
}

/**
 * 渲染话题图表
 */
function renderTopicsCharts(data) {
    // 汇总所有话题
    const topicMap = {};
    const months = Object.keys(data).sort();
    months.forEach(m => {
        const d = data[m];
        if (!d.topics) return;
        d.topics.forEach(t => {
            if (!topicMap[t.name]) topicMap[t.name] = 0;
            topicMap[t.name] += t.weight;
        });
    });

    const sorted = Object.entries(topicMap).sort((a,b) => b[1] - a[1]);
    renderPieChart('topicsChart',
        sorted.map(([k,v]) => ({name: k, value: Math.round(v * 100)})),
        '话题比重'
    );

    // 话题详情
    let html = '';
    months.forEach(m => {
        const d = data[m];
        if (!d) return;
        html += `<div class="card mb-2">
            <div class="card-body py-2">
                <strong>${m}</strong>
                <div class="mt-1">
                    ${(d.topics || []).map(t =>
                        `<span class="badge bg-secondary me-1">${t.name} (${Math.round(t.weight*100)}%)</span>`
                    ).join('')}
                </div>
                <div class="small text-muted mt-1">${d.summary || ''}</div>
            </div>
        </div>`;
    });
    if (html) {
        $('#topicsDetails').html(html);
    }
}
```

---

### Task 11: 集成验证

**Files:** (none)

- [ ] **Step 1: 验证项目结构完整**

```bash
cd E:\qqchatlog
pip install -r requirements.txt
```

Expected: 所有依赖安装成功

- [ ] **Step 2: 启动应用**

```bash
cd E:\qqchatlog
python app.py
```

Expected: 应用启动在 http://localhost:5000

- [ ] **Step 3: 功能测试**
    1. 访问 http://localhost:5000 — 看到首页上传页面
    2. 上传 JSON 文件 — 跳转到仪表盘，显示统计图表
    3. 配置 `.env` 中的 API Key 后重启 — 看到 AI 分析按钮
    4. 点击"情绪分析" — 调用 API，结果显示在情绪页面
    5. 测试其他 AI 分析维度
    6. 不配置 API Key — 确认本地统计功能正常，AI 按钮不显示
