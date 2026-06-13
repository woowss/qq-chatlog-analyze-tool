# 📊 QQ 聊天记录分析工具

> 导入 QQChatExporter 导出的 JSON 聊天记录，通过本地统计 + DeepSeek AI 实现多维度聊天分析，以 ECharts 可视化图表呈现。

## ✨ 功能

### 📈 本地统计（无需 API Key）
- **总览仪表盘** — 消息总数、聊天天数、日均消息、图片数量
- **消息趋势** — 每日消息量折线图
- **活跃时段** — 24 小时分布柱状图 + 星期分布
- **双方对比** — 消息数、发言字数、平均句长
- **表情排行** — 双方表情使用 Top 10
- **高频词云** — 使用 jieba 分词提取高频词，以词云图展示
- **对话轮次** — 统计对话来回次数
- **回复速度** — 双方平均响应时间

### 🤖 AI 深度分析（需 DeepSeek API Key）
| 功能 | 说明 |
|---|---|
| 😊 **情绪分析** | 逐月分析双方情绪变化、情绪强度曲线 |
| 👥 **人际关系** | 分析互动模式、亲密程度、关系角色 |
| 🧑 **个人习惯** | 说话风格、口头禅、标点习惯、回复模式 |
| 📈 **话题趋势** | 提取核心话题及占比、逐月话题变化 |
| 🎯 **人物锐评** | 深度性格画像（含优缺点、思维特征、情绪模式、关系动态等） |

### 📄 全篇报告导出
- 一键生成包含所有统计 + AI 分析的报告
- 支持浏览器打印 / 导出 PDF / 下载 HTML

## 🚀 快速开始

### 1. 导出聊天记录

使用 [QQChatExporter](https://github.com/shuakami/qq-chat-exporter) 导出私聊记录为 JSON 文件。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 API Key（可选）

在项目根目录创建 `.env` 文件：

```env
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

> 不配置 API Key 也能使用本地统计功能。AI 分析需要 [DeepSeek API Key](https://platform.deepseek.com/api_keys)。

### 4. 启动

```bash
python app.py
```

浏览器打开 http://localhost:5000

### 5. 使用

1. 上传 QQChatExporter 导出的 `.json` 文件
2. 查看仪表盘获取本地统计数据
3. 如果配置了 API Key，点击 AI 分析按钮获取深度洞察

## 📂 项目结构

```
E:\qqchatlog\
├── app.py                     # Flask 主应用 + 路由
├── config.py                  # 配置读取
├── requirements.txt           # 依赖清单
├── .env                       # API Key（不提交到 Git）
├── .env.example               # 配置模板
├── .gitignore
├── parser/
│   └── qq_parser.py           # QQ JSON → ChatData 解析器
├── analyzer/
│   ├── __init__.py
│   ├── prompts.py             # DeepSeek System Prompt 常量
│   ├── local_stats.py         # 本地统计分析
│   ├── deepseek_client.py     # DeepSeek API 调用封装
│   └── logger.py              # 日志记录模块
├── web/
│   ├── templates/             # HTML 模板
│   │   ├── base.html          # 基础布局
│   │   ├── index.html         # 首页 / 上传
│   │   ├── dashboard.html     # 总览仪表盘
│   │   ├── emotion.html       # 情绪分析
│   │   ├── relationship.html  # 人际关系
│   │   ├── habits.html        # 个人习惯
│   │   ├── topics.html        # 话题趋势
│   │   ├── profile.html       # 人物锐评
│   │   └── report.html        # 全篇报告
│   └── static/
│       ├── css/style.css      # 自定义样式
│       └── js/charts.js       # ECharts 图表渲染
├── uploads/                   # 上传文件暂存
├── flask_session/             # Session 文件（自动生成）
├── logs/                      # 日志文件（自动生成）
└── docs/                      # 设计文档与计划
    ├── specs/
    └── plans/
```

## 🛠️ 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+, Flask 3.x |
| 前端 | Bootstrap 5, jQuery, ECharts 5 |
| 分词 | jieba |
| AI API | DeepSeek Chat（OpenAI 兼容接口） |
| Session | flask-session（服务端文件存储） |

## 🔒 隐私说明

- 聊天记录**仅保存在本地**，不上传至任何第三方服务器
- AI 分析时仅将文本片段发送至 DeepSeek API，多媒体文件不会被上传
- 所有会话数据存储在 `flask_session/` 目录，重启后自动清理

## 📦 依赖

```
flask>=3.0
flask-session>=0.8
openai>=1.0
python-dotenv>=1.0
jieba>=0.42
```

## 📜 许可证

[MIT](LICENSE)
