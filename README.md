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

### 🤖 AI 深度分析（需 LLM API Key）
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

在项目根目录创建 `.env` 文件。任何 **OpenAI 兼容接口**均可使用，例如 DeepSeek：

```env
DEEPSEEK_API_KEY=你的API_Key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

或阿里云 Token Plan（Qwen）：

```env
DEEPSEEK_API_KEY=你的Token_Plan_Key
DEEPSEEK_MODEL=qwen3.8-flash
DEEPSEEK_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

可选运行 / 安全配置（默认值即安全）：

```env
# FLASK_DEBUG=false          # 调试模式（默认关闭，调试器可执行任意代码，仅限本机开发）
# FLASK_HOST=127.0.0.1       # 绑定地址；非回环地址必须同时设置 ACCESS_PASSWORD
# FLASK_PORT=5000            # 端口被占用时（Windows 5000 常见）可改 5001
# ACCESS_PASSWORD=           # 访问口令；设置后所有页面需登录
# SECRET_KEY=                # Session 签名密钥；留空自动生成并持久化到 .secret_key
```

> 不配置 API Key 也能使用本地统计功能。AI 分析需要任一 OpenAI 兼容服务的 Key（如 [DeepSeek](https://platform.deepseek.com/api_keys) 或阿里云 Token Plan）。

### 4. 启动

```bash
python app.py
```

浏览器打开 http://localhost:5000

> 默认关闭调试模式（避免调试器暴露风险）。本地开发需要自动重载时，设置环境变量 `FLASK_DEBUG=true` 再启动。
> 若提示端口被占用（Windows 上 5000 常被 AirPlay/Hyper-V 占用），在 `.env` 中设置 `FLASK_PORT=5001`。

### 5. 使用

1. 上传 QQChatExporter 导出的 `.json` 文件
2. 查看仪表盘获取本地统计数据
3. 如果配置了 API Key，点击 AI 分析按钮获取深度洞察（后台任务实时显示进度，可随时取消）
4. 已分析过的结果会缓存在 `ai_cache/`，重开页面/换标签查看**不再重复调用 API**；勾选"强制重新分析"才会重跑

## 📂 项目结构

```
qqchatlog/
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
│   │   ├── login.html         # 访问口令登录页
│   │   ├── dashboard.html     # 总览仪表盘
│   │   ├── emotion.html       # 情绪分析
│   │   ├── relationship.html  # 人际关系
│   │   ├── habits.html        # 个人习惯
│   │   ├── topics.html        # 话题趋势
│   │   ├── profile.html       # 人物锐评
│   │   └── report.html        # 全篇报告
│   └── static/
│       ├── css/style.css      # 自定义样式
│       └── js/
│           ├── charts.js      # ECharts 图表渲染
│           └── analyze.js     # AI 分析任务：轮询进度/取消/缓存读取
├── tests/
│   └── test_core.py           # 核心逻辑单元测试
├── .github/workflows/test.yml # CI：py_compile + unittest
├── uploads/                   # 上传文件暂存
├── ai_cache/                  # AI 结果缓存（敏感，已 gitignore，24h 自动清理）
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
| AI API | OpenAI 兼容接口（DeepSeek / 阿里云 Token Plan Qwen 等，`.env` 可配） |
| Session | flask-session（服务端文件存储） |

## 🔒 隐私说明

- 聊天记录**仅保存在本地**，不上传至任何第三方服务器
- AI 分析时仅将文本片段发送至所配置的 LLM API，多媒体文件不会被上传
- 上传文件、session 与 AI 结果缓存（`uploads/`、`flask_session/`、`ai_cache/`）超过 24 小时会在启动时自动清理；重新上传时旧文件即时删除。若需立即清除，手动删除这三个目录即可
- 服务默认仅绑定 `127.0.0.1`，POST 请求带 CSRF token 与 Origin 双重校验；绑定非回环地址时必须设置 `ACCESS_PASSWORD` 访问口令，否则拒绝启动

## 📦 依赖

核心依赖：Flask、Flask-Session、OpenAI SDK（DeepSeek 兼容接口）、python-dotenv、jieba。
完整锁定清单见 `requirements.txt`（`pip install -r requirements.txt` 即可）。

## ❓ 常见问题

**Q：AI 分析时报 429 "Allocated quota exceeded / insufficient_quota"？**

这是**每分钟限流（TPM/RPM）**，不是套餐额度耗尽（该报错文案有误导性，[官方文档](https://www.alibabacloud.com/help/en/model-studio/rate-limit) 明确限流按主账号聚合、通常 1 分钟内自动恢复）。应用已内置应对：全局 3 秒调用间隔平滑突发、命中 429 自动等待 25s 重试最多 4 次、多次不恢复才中止并保留部分结果。若仍频繁触发：

1. 在 `.env` 中下调：`LLM_CONCURRENCY=1`、`LLM_CALL_MIN_INTERVAL=5`，或减小 `LLM_MAX_DIALOG_CHARS=30000`（每请求更省 token）
2. 到百炼控制台「限流提额」页临时提升该模型的 TPM 配额（立即生效）
3. 避开同账号其他程序（如编码 Agent）的大量请求时段——所有 API Key 共享同一个限流池

## 📜 许可证

[GPL v3](LICENSE)
