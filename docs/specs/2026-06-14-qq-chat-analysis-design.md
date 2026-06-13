# QQ 聊天记录分析工具 — 设计文档

## 1. 项目概述

基于 Python Flask 的 Web 应用，导入 QQChatExporter 导出的 JSON 聊天记录，通过本地统计 + DeepSeek API 实现多维度聊天分析，以 ECharts 可视化呈现。

### 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | Flask 3.x |
| 前端框架 | Bootstrap 5 + 原生 JS |
| 可视化 | ECharts 5 |
| 大模型 API | DeepSeek Chat (OpenAI 兼容接口) |
| 运行环境 | Python 3.10+ |

---

## 2. 项目结构

```
E:\qqchatlog\
├── app.py                      # Flask 主入口 + 路由
├── config.py                   # 配置（DeepSeek API Key, 可选）
├── requirements.txt            # 依赖清单
├── parser/
│   └── qq_parser.py            # QQ JSON 解析器
├── analyzer/
│   ├── local_stats.py          # 本地统计分析
│   └── deepseek_client.py      # DeepSeek API 调用
├── web/
│   ├── templates/
│   │   ├── index.html          # 首页 / 文件上传
│   │   ├── dashboard.html      # 总览仪表盘
│   │   ├── emotion.html        # 情绪分析
│   │   ├── relationship.html   # 人际关系
│   │   ├── habits.html         # 个人习惯
│   │   └── topics.html         # 话题趋势
│   └── static/
│       ├── css/
│       │   └── style.css       # 自定义样式
│       └── js/
│           └── charts.js       # ECharts 图表配置函数
└── uploads/                    # 上传的 JSON 文件暂存
```

---

## 3. 数据结构定义

### 3.1 原始 JSON 格式 (QQChatExporter V5)

```json
{
  "metadata": { "version": "5.5.64" },
  "chatInfo": { "name": "对方名", "type": "private", "selfName": "我" },
  "statistics": { "totalMessages": 12404, "timeRange": {...}, "senders": [...] },
  "messages": [
    {
      "id": "msg_id",
      "timestamp": 1758031009000,
      "time": "2025-09-16 21:56:49",
      "sender": { "name": "昵称", "nickname": "备注" },
      "type": "type_1",
      "content": {
        "text": "消息文本",
        "elements": [{"type": "text|face|image|reply", "data": {...}}]
      },
      "recalled": false,
      "system": false
    }
  ]
}
```

### 3.2 解析后的 Python 数据结构

```python
@dataclass
class Message:
    id: str
    timestamp: int          # 毫秒时间戳
    time_str: str           # 格式化时间
    sender_name: str        # 发送者显示名
    sender_uid: str         # 发送者 UID
    text: str               # 纯文本内容
    msg_type: str           # type_1 / type_3 / type_11 等
    has_image: bool         # 是否包含图片
    is_reply: bool          # 是否为回复消息
    face_ids: list[int]     # 使用的表情 ID 列表

@dataclass
class ChatData:
    chat_name: str          # 聊天对象名称
    self_name: str          # 自己的显示名
    other_name: str         # 对方的显示名
    messages: list[Message]
    total_count: int
    time_start: str
    time_end: str
    duration_days: int
```

### 3.3 分析结果结构

```python
@dataclass
class EmotionResult:
    """按时间段的情绪分析结果"""
    period: str             # 时间段标识 (如 "2025-09")
    self_emotion: str       # 自己情绪标签
    other_emotion: str      # 对方情绪标签
    self_intensity: float   # 自己情绪强度 1-10
    other_intensity: float  # 对方情绪强度 1-10
    keywords: list[str]     # 关键词

@dataclass
class HabitProfile:
    """个人习惯画像"""
    sender: str
    avg_length: float       # 平均发言长度
    total_messages: int
    face_frequency: float   # 表情使用频率
    active_hours: list[int] # 最活跃时段
    top_words: list[str]    # 高频词
    reply_ratio: float      # 回复占比

@dataclass
class TopicResult:
    """话题分析结果"""
    period: str
    topics: list[dict]      # [{"name": "话题名", "weight": 0.x}]
    summary: str

@dataclass
class RelationshipMetrics:
    """人际关系指标"""
    initiator_ratio: float       # 自己发起对话比例
    avg_response_time: float     # 平均响应时间(秒)
    message_ratio_self: float    # 自己消息占比
    message_ratio_other: float   # 对方消息占比
    cross_late_night: int        # 跨零点聊天次数
    total_exchanges: int         # 对话轮次
```

---

## 4. 模块详细设计

### 4.1 QQ JSON 解析器 (`parser/qq_parser.py`)

```
load_chat(path) -> ChatData
  1. 读取 JSON 文件
  2. 解析 metadata/chatInfo
  3. 遍历 messages，提取每个字段
  4. 过滤系统消息和撤回消息（可选）
  5. 返回 ChatData 对象
```

**特殊类型处理：**
- `type_1`: 普通文本/表情消息 → 直接提取 text 和 face IDs
- `type_3`: 回复消息 → 标记 `is_reply=True`，提取回复目标
- `type_11`: 合并转发 → 标记类型，保留 XML 摘要
- `type_17`: 商城表情 → 标记类型

### 4.2 本地统计分析 (`analyzer/local_stats.py`)

所有不依赖大模型的统计计算：

| 函数 | 产出 |
|---|---|
| `calc_daily_counts(msgs)` | 每日消息量时间序列 |
| `calc_hourly_distribution(msgs)` | 24小时分布 |
| `calc_message_length_stats(msgs)` | 发言长度统计 |
| `calc_face_stats(msgs)` | 表情使用排行榜 |
| `calc_response_time(msgs)` | 平均回复时间 |
| `calc_active_periods(msgs)` | 活跃时段 |
| `calc_exchange_rounds(msgs)` | 对话轮次 |
| `calc_image_stats(msgs)` | 图片发送统计 |

### 4.3 DeepSeek API 封装 (`analyzer/deepseek_client.py`)

**API 配置：**
- 端点: `https://api.deepseek.com/v1/chat/completions`
- 模型: `deepseek-chat`
- API Key 通过 `.env` 文件配置（详见第 7 章）
- 使用 `openai` Python SDK（DeepSeek 完全兼容 OpenAI API 格式）

**分段策略：**
由于单次 API 调用无法处理全部消息（12,404 条消息 × 270 天），按**月**分段分析：

```
messages → 按月分组（约 9 组）→ 每组调 API → 合并汇总结果
```

**模型参数设置：**
```python
completion_kwargs = {
    "model": "deepseek-chat",
    "temperature": 0.3,            # 低温确保分析稳定性
    "max_tokens": 2048,            # 单次输出上限
    "response_format": {"type": "json_object"},  # 强制 JSON 输出
}
```

**重试策略：** API 调用失败时，最多重试 2 次（指数退避），仍失败则跳过该段，页面标注提示。

---

#### Prompt 总体设计原则

1. **角色锚定** — 每个 prompt 开头设定 AI 角色，建立分析视角
2. **中文优先** — DeepSeek 中文理解能力强，全部用中文指令
3. **JSON Schema** — 用 TypeScript 风格的 interface 定义输出格式，模型更易遵循
4. **Few-shot 引导** — 关键维度提供输出样例，稳定格式
5. **分段感知** — 告知模型这是「某个月」的片段，语境化分析
6. **拒绝幻觉** — 明确要求「没有相关内容则返回空/无」

---

#### ① 情绪分析 System Prompt

```python
SYSTEM_PROMPT_EMOTION = """你是一位精通中文社交语言分析的心理学专家。
你的任务是从私聊对话片段中分析双方的情绪状态和变化。

## 分析规则
- 根据用词、语气、标点、表情符号判断情绪
- 注意中文网络语言的情绪色彩（如 "救命" 可能是调侃）
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

注意：不要添加任何额外字段，只返回 JSON。"""
```

#### ② 话题趋势 System Prompt

```python
SYSTEM_PROMPT_TOPICS = """你是一位话题建模分析师，擅长从对话中提取结构化话题。

## 分析规则
- 识别该月对话中出现的**核心话题**（3-6 个）
- weight 表示该话题在本月对话中的占比，所有话题 weight 之和不超过 1.0
- summary 用一句话概括该月的对话主旋律
- 话题名称要精炼（2-8 个字），如 "项目开发"、"考试准备"、"日常闲聊"

## 必须严格遵守的输出 JSON 格式
{
  "topics": [
    {"name": "话题名", "weight": 0.35, "keywords": ["关键词1", "关键词2", "关键词3"]},
    {"name": "话题名", "weight": 0.25, "keywords": ["关键词1", "关键词2"]}
  ],
  "summary": "该月对话主要围绕...",
  "topic_shift_detected": true | false,
  "shift_description": "从XX话题转向了XX话题"  
}

注意：如果对话内容太少无法分析，返回 {"topics": [], "summary": "对话内容较少，无明显话题", "topic_shift_detected": false}"""
```

#### ③ 人际关系 System Prompt

```python
SYSTEM_PROMPT_RELATIONSHIP = """你是一位人际关系与沟通模式分析师。

## 分析规则
- 观察对话中的**发起-响应模式**：谁更常开启新话题？谁更常延续话题？
- 分析**权力动态**：谁在提要求/给建议？谁在提供情绪支持？
- 评估**亲密程度**：用词随意度、自我披露深度、幽默频率
- 注意中国校园/年轻人社交语境下的关系表达
- 结合该月数据判断关系是更亲密了、疏远了还是保持稳定

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
}"""
```

#### ④ 个人风格 System Prompt

```python
SYSTEM_PROMPT_HABITS = """你是一位语言风格分析专家，专门分析中文网络聊天风格。

## 分析规则
- 基于该用户的发言，分析其独特的**语言指纹**
- 注意特征：句子长度偏好、网络用语习惯、表情符号使用模式、语气词、口头禅
- 标点使用习惯（是否喜欢用...、！！！、~ 等）
- 回复模式（秒回型/思考型/话题跳跃型）
- 注意不要过度解读，基于实际文本特征

## 必须严格遵守的输出 JSON 格式
{
  "personality_tags": ["幽默", "直率", "细腻", "简洁", "活泼", "理性", "感性", "毒舌", "温柔", "中二"],
  "common_phrases": ["口头禅1", "高频用语2", "语气词3"],
  "emoji_style": "丰富 | 适中 | 极少",
  "top_emojis": ["😊", "🤣", "🙃"],
  "sentence_length": "短句为主 | 长短混合 | 长句较多",
  "reply_speed": "秒回型 | 适中 | 深思熟虑型",
  "topic_jumping": "经常跳跃 | 偶尔 | 专注一个话题",
  "unique_traits": ["独特习惯1", "独特习惯2"]
}"""
```

**实施细节：**
- 输入时，将对话文本拼接为 `"[时间] 发送者: 消息内容"` 格式
- 如果单月消息超过模型上下文限制，取该月最新的 60% 消息（近期行为更具代表性）
- 所有 prompt 在代码中作为常量定义在 `analyzer/prompts.py` 中，方便后续调整

### 4.4 路由设计 (`app.py`)

| 路由 | 方法 | 描述 |
|---|---|---|
| `/` | GET | 首页/上传页面 |
| `/upload` | POST | 接收 JSON 文件，解析并重定向到 dashboard |
| `/dashboard` | GET | 总览仪表盘 |
| `/emotion` | GET | 情绪分析页面 |
| `/relationship` | GET | 人际关系分析页 |
| `/habits` | GET | 个人习惯/风格分析页 |
| `/topics` | GET | 话题趋势分析页 |

**数据存储：** 使用 Flask `session` 存储解析后的 ChatData 和分析结果（JSON 序列化），避免数据库依赖。

### 4.5 前端页面设计

#### 首页 (`index.html`)
- 拖拽或点击上传 .json 文件
- 显示支持的格式说明
- 上传后自动跳转到仪表盘

#### 仪表盘 (`dashboard.html`)
- 顶部：统计卡片行（总消息数、时间跨度、日均消息、图片总数）
- 中间：消息量折线图（按日/周切换）
- 底部：活跃时段热力图 + 发送者占比饼图
- "开始 AI 分析" 按钮（触发 DeepSeek 分析）

#### 情绪分析 (`emotion.html`)
- 情绪变化折线图（双方情绪强度随时间变化）
- 情绪分布饼图
- 情绪关键词卡片

#### 人际关系 (`relationship.html`)
- 发起对话比例环形图
- 响应时间分布柱状图
- 互动指标卡片

#### 个人习惯 (`habits.html`)
- 高频词词云（使用 wordcloud 或 ECharts 词云）
- 24小时活跃热力图
- 表情排行柱状图
- 发言长度分布

#### 话题趋势 (`topics.html`)
- 话题分类饼图
- 话题随时间变化堆叠面积图
- 各月话题摘要卡片

---

## 5. 数据流

```
┌──────────┐    ┌────────────┐    ┌──────────────────┐
│ 上传JSON │ → │ qq_parser  │ → │ local_stats      │
│ 文件     │    │ 解析消息   │    │ 本地统计          │
└──────────┘    └────────────┘    └────────┬─────────┘
                                           │
                  ┌─────────────────────────┘
                  │
          ┌───────▼────────┐
          │ Flask Session  │ ← 缓存中间结果
          │ (内存)         │
          └───────┬────────┘
                  │
     ┌────────────┼────────────┐
     │            │            │
┌────▼───┐ ┌──────▼──────┐ ┌──▼──────┐
│ 页面    │ │ ECharts     │ │ DeepSeek│
│ 路由    │ │ 可视化渲染   │ │ API     │
└────────┘ └─────────────┘ └─────────┘
```

---

## 6. 错误处理

| 场景 | 处理方式 |
|---|---|
| 上传非 JSON 文件 | 前端校验 + 后端返回错误提示 |
| JSON 格式不匹配 | 解析异常捕获，返回友好提示 |
| DeepSeek API 失败 | 重试 2 次，仍失败则跳过该段，页面标注"部分分析不可用" |
| API Key 未配置 | 仪表盘显示提示，本地统计功能正常使用 |
| 超大文件 (>50MB) | 前端限制上传大小 |

---

## 7. 环境要求与部署

### 依赖安装

```
# requirements.txt
flask>=3.0
openai>=1.0          # DeepSeek 使用 OpenAI 兼容 SDK
python-dotenv>=1.0   # 管理 API Key
```

```bash
pip install -r requirements.txt
python app.py
# 访问 http://localhost:5000
```

### ⚠️ API Key 配置（必填）

项目使用 `.env` 文件管理 DeepSeek API Key，**请按以下步骤操作：**

#### 步骤 1：在项目根目录创建 `.env` 文件

`E:\qqchatlog\.env` 文件内容如下（把 key 填进去就行）：

```
# ============================================
# 【必填】在这里填入你的 DeepSeek API Key
# ============================================
# 1. 访问 https://platform.deepseek.com/api_keys 登录
# 2. 创建 API Key（如果没有的话）
# 3. 把下面的 "这里填入你的Key" 替换成实际的 Key
# ============================================
DEEPSEEK_API_KEY=这里填入你的Key

# 可选配置（一般不需要修改）
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

#### 步骤 2：验证是否配置成功

启动项目后访问首页，页面顶部会显示 ✅ **已配置** 或 ❌ **未配置 API Key** 的状态。

> **注意：** 即使不配置 API Key，本地统计分析（消息量、活跃时段、表情排行等）也能正常使用。只有 AI 分析功能（情绪、话题、人际关系、个人风格）需要 API Key。

#### 配置文件对应关系

| 文件 | 用途 |
|---|---|
| `.env` | **存放 API Key 的秘密文件**，不要提交到 Git |
| `config.py` | 读取 `.env` 中的配置并提供给程序使用 |
| `.env.example` | 模板文件，不含真实 Key，可提交到 Git |

---

## 8. 未来扩展（非 MVP）

- [ ] 多 JSON 文件对比分析
- [ ] 群聊记录支持
- [ ] 导出分析报告 PDF
- [ ] 长期趋势跟踪（多次上传同一聊天的历史对比）
- [ ] 使用本地模型（如 Ollama）替代 DeepSeek API
