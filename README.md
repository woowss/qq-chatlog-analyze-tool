# QQ 聊天记录分析工具

导入 [QQChatExporter](https://github.com/shuakami/qq-chat-exporter) 导出的 JSON 聊天记录，先做本地统计，
再调用 OpenAI 兼容接口做大模型分析，结果用 ECharts 图表呈现。仅支持两人私聊记录。

不配置 API Key 时，本地统计部分照常可用。

## 功能

### 本地统计（不需要 API Key）

| 页面 | 内容 |
|---|---|
| 仪表盘 | 消息总数、聊天天数（跨度与活跃天数分开）、日均消息、图片数量与体积、视频/文件/转发/表情次数、按 md5 去重后的图片张数、每日消息量、24 小时与星期分布、双方消息数与字数对比、对话轮次、时光里程碑（连续聊天纪录、最长沉默期、跨零点夜晚、单日峰值、最活跃月份） |
| 习惯 | 双方表情排行、一周活跃热力图、双方高频词云（jieba 分词） |
| 关系 | 回复速度（均值与 P50/P90，界面以中位数为主）、对话轮次 |
| 报告 | 汇总以上全部统计，可打印、导出 PDF 或下载 HTML |

非文本消息同样参与统计与分析。文件、视频、转发卡片、红包、通话记录、小程序卡片、商城大表情会被标注为
`[文件:示例表.xlsx]`、`[转发:某某的聊天记录（25条）]`、`[通话:未接听]`、`[表情:叉腰]` 这样的标记送进统计与
AI 分析，而消息正文保持干净（文件名和占位符不会进入词频与平均句长）。图片有尺寸和体积信息时，也会统计
图片总体积与去重张数。

表情排行的轴标签优先显示 QQ 表情原图，其次 Unicode emoji，最后回退到表情名。原图需要手动获取一次，见下文。

### AI 分析（需要 API Key）

| 维度 | 内容 |
|---|---|
| 情绪 | 逐月双方情绪、情绪强度曲线、月度基调、情绪转折点 |
| 关系 | 互动模式、亲密程度、关系角色、谁更常开启话题 |
| 习惯 | 说话风格、口头禅、标点与表情习惯、回复模式 |
| 话题 | 核心话题及权重、逐月话题变化 |
| 锐评 | 性格画像（优缺点、思维特征、情绪模式、关系动态等） |

五个维度可以单独运行，也可以点「全量分析」按顺序跑完。分析在后台线程里跑，页面实时显示进度，可以随时取消。

结果按「月份 + 内容」缓存，重新导出只多了几个月时，历史月份直接命中缓存，不再重复付费与等待；
同一个聊天重复分析也是零成本。仪表盘会显示按天、按维度累计的 token 用量与费用估算。

## 快速开始

### 1. 导出聊天记录

用 QQChatExporter 导出私聊记录为 JSON。准备使用图片理解时，导出时勾选导出资源文件
（`includeResourceLinks`），导出目录下会有 `resources/`。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10 或更高版本。

### 3. 配置 API Key（可选）

在项目根目录创建 `.env`。任何 OpenAI 兼容接口都可以，例如 DeepSeek 官方：

```env
DEEPSEEK_API_KEY=你的API_Key
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

或阿里云百炼的 Qwen：

```env
DEEPSEEK_API_KEY=你的Token_Plan_Key
DEEPSEEK_MODEL=qwen3.8-flash
DEEPSEEK_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

完整可配项见下文「配置项」，默认值即可直接使用。

### 4. 启动

```bash
python app.py
```

浏览器打开 http://localhost:5000 。默认只监听 `127.0.0.1`，调试模式关闭。

端口被占用时（Windows 上 5000 常被占用）在 `.env` 里设 `FLASK_PORT=5001`。本地开发需要自动重载时设
`FLASK_DEBUG=true`，注意调试器可以执行任意代码，只在本机使用。

### 5. 使用

1. 上传导出的 `.json` 文件，进入仪表盘查看本地统计。
2. 配置了 API Key 时，按维度运行 AI 分析，或点「全量分析」跑完五个维度。
3. 已分析的结果缓存在 `ai_cache/`，重开页面不会重复调用接口；需要重跑时勾选「强制重新分析」。

### 图片理解（可选）

需要把聊天里的图片也交给模型分析时，用首页的「选择导出目录（含图片）」按钮，选中 QQChatExporter 的导出
目录即可。流程是：浏览器先上传 JSON，服务端解析后告诉前端这次分析需要哪几十张图片，浏览器只把这些图片传到
本机服务。

这样不会把整个 `resources/` 目录搬一遍——实测一个 近 1 GB 的导出目录，实际只需要上传几十张图片。图片副本
放在 `uploads/media/<哈希>/`，随 `uploads/` 的 24 小时策略回收；识别出的图片摘要按图片指纹长期缓存，
所以重跑分析不需要重新上传图片。

Firefox 等不支持目录选择的环境，可以在 `.env` 里设 `QQCHAT_MEDIA_DIR=导出目录`，由服务端直接读取。
两种方式可以并存，WebUI 上传的副本优先。

### 获取原始表情图（可选，默认关闭）

在 `.env` 里设 `QQCHAT_FACE_IMAGES=true`，然后到「习惯」页点一次「获取原始表情图」。程序会从 QQ 的公开
表情 CDN 把经典黄脸和商城表情的原图下载到本地缓存（`face_cache/`），之后离线复用、不再联网。

实测覆盖约 58% 的表情使用次数。QQ 的超级表情（吃糖、大怨种、菜汪之类）没有公开地址，抓不到，会继续用
emoji 或表情名显示。想要 100% 原样，可以把 `QQCHAT_FACE_DIR` 指向自己准备的表情图目录，文件名支持
`<编号>.gif`、`e<编号+100>.gif`、`<表情名>.gif`。断网或抓取失败时静默跳过，不影响其它功能。

## 分析质量与费用

默认配置按准确性优先设置：

- 单月对话预算 60 万字符，足以装下绝大多数月份的全部消息，正常情况下不抽样；
- 官方 DeepSeek 端点默认全维度开启思考模式，各维度输出预算 32k（锐评 49k），不会出现思维链吃满预算导致
  结果被截断丢弃；
- 每月最多送 20 张图片做视觉理解，摘要按图片指纹缓存；
- 输出被截断时自动改用非思考模式重试一次，避免某个月从结果里消失。

实测数据（数万条私聊、约一年）：

| 项目 | 实测 |
|---|---|
| 跑一个维度 | 每月一次调用，prompt 数十万 tokens，约 ¥1.2，约 55 秒 |
| 五个维度全量 | 约 ¥6 |
| 同一批月份重跑 | 0 成本（命中月份缓存） |

想省钱可以调小 `LLM_MAX_DIALOG_CHARS`（代价是长月份被抽样）、设 `LLM_THINKING=disabled`、
把 `LLM_VISION_MAX_PER_MONTH` 调低。费用按模型标准价估算，空闲时段大约半价。

## 配置项

以下是全部可配项，都写在 `.env` 里，默认值可直接使用。

### 模型接口

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | API Key，留空则只用本地统计 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型名 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | 接口地址 |
| `LLM_CONCURRENCY` | 官方 6 / 其它 2 | 并发月份数 |
| `LLM_CALL_MIN_INTERVAL` | 官方 0.5 / 其它 3 | 两次调用最小间隔（秒） |
| `LLM_MAX_DIALOG_CHARS` | 600000 | 单月对话文本上限（字符） |
| `LLM_THINKING` | 官方端点开启 | `disabled` 关闭思考模式 |
| `LLM_THINKING_DIMS` | 空 | 只对指定维度开启，逗号分隔 |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | 按模型内置 | 费用估算单价（元/百万 tokens） |
| `PROMPT_CACHE_SALT` | 空 | 手动强制失效缓存的盐值 |
| `QQCHAT_MONTH_CACHE` | 开 | 设 `0` 关闭月份级增量缓存 |
| `LLM_MONTH_CACHE_GRACE_HOURS` | 24 | 无引用的月份缓存宽限期 |

### 图片理解与表情图

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCHAT_MEDIA_DIR` | 空 | 导出目录；留空则不用服务端直接读图 |
| `LLM_VISION` | `true` | 图片理解总开关，`false` 则完全不上传图片 |
| `LLM_VISION_MAX_PER_MONTH` | 20 | 每月最多送几张图 |
| `LLM_VISION_DETAIL` | `high` | 送图清晰度，`low` 会压到 512×512 |
| `LLM_VISION_MIN_SIDE` | 200 | 小于该像素的图按表情包跳过 |
| `LLM_VISION_MAX_BYTES` | 12582912 | 单张图片体积上限（字节） |
| `QQCHAT_FACE_IMAGES` | `false` | 是否允许抓取 QQ 表情原图 |
| `QQCHAT_FACE_DIR` | 空 | 本地表情包目录，优先级高于联网抓取 |
| `QQCHAT_FACE_FETCH_LIMIT` | 300 | 一次抓取的表情图数量上限 |
| `QQCHAT_FACE_FETCH_TIMEOUT` | 6 | 单张表情图下载超时（秒） |

### 运行与安全

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FLASK_HOST` | `127.0.0.1` | 绑定地址；非回环地址必须同时设置访问口令 |
| `FLASK_PORT` | 5000 | 端口 |
| `FLASK_DEBUG` | `false` | 调试模式与自动重载 |
| `ACCESS_PASSWORD` | 空 | 访问口令，设置后所有页面需登录 |
| `ALLOWED_ORIGINS` | 空 | 额外允许的浏览器来源主机，用局域网 IP 或域名访问时必填 |
| `SECRET_KEY` | 自动生成 | Session 签名密钥，留空则生成并持久化到 `.secret_key` |
| `QQCHAT_JOB_TTL_SECONDS` | 900 | 内存任务记录的存活时间 |
| `QQCHAT_ALLOW_MULTI_PARTY` | `false` | 设为 `1` 才允许分析多人记录 |

### 数据与日志

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QQCHAT_DATA_DIR` | 项目目录 | 数据总目录，可整体迁到别处 |
| `UPLOAD_DIR` / `SESSION_DIR` / `AI_CACHE_DIR` / `STATS_CACHE_DIR` / `LOG_DIR` / `FACE_CACHE_DIR` | 数据目录下各子目录 | 单独覆盖某一类数据的位置 |
| `TOKEN_USAGE_FILE` | `logs/token_usage.json` | token 用量统计文件位置 |
| `LOG_RETENTION_DAYS` | 7 | 日志按天轮转保留天数 |
| `LOG_REDACT_NAMES` | `true` | 日志里的昵称与原始文件名脱敏 |

## 隐私与数据生命周期

聊天记录只保存在本机，不上传任何第三方服务器。前端资源（Bootstrap、jQuery、ECharts）已本地化，
断网也能正常使用。

AI 分析发送给你自己配置的接口，发送的内容包括：

- 对话文本，以及媒体元数据标记（文件名、转发标题、表情名等）；
- 开启图片理解时，每月最多 `LLM_VISION_MAX_PER_MONTH` 张图片（按 md5 去重、跳过表情包尺寸与超大文件）。
  视频和文件本体不会被读取；关掉 `LLM_VISION` 就完全不发送图片。

本地数据的保留策略：

| 目录 | 策略 |
|---|---|
| `uploads/`、`flask_session/` | 超过 24 小时回收（启动时一次，之后每小时随请求触发一次） |
| `ai_cache/`、`stats_cache/` | 滑动 30 天 + 绝对 90 天，两条上限同时生效 |
| `logs/` | 按天轮转，默认保留 7 天，昵称脱敏 |
| `face_cache/` | QQ 表情原图，与聊天内容无关，可随时删除 |

重新上传时，旧文件及其派生缓存立即删除；删除聊天记录时，对应的统计缓存、AI 缓存、图片摘要一并回收。
想立即清除全部数据，删除上述目录即可，或者用 `QQCHAT_DATA_DIR` 把数据整体放到别处。

其它安全措施：服务默认只绑定回环地址，POST 请求同时校验 CSRF token 与 Origin；绑定非回环地址时若未设置
`ACCESS_PASSWORD` 则拒绝启动，Origin 白名单不信任请求自带的 Host（防 DNS rebinding）；登录接口对同一 IP
有失败限流。导出的 HTML 报告会剥掉 CSRF token、把第三方资源换回 CDN、自有样式与表情图内联，可以直接分享。

## 项目结构

```
qqchatlog/
├── app.py                     # 组装入口：Flask 初始化、启动横幅、测试兼容重导出
├── config.py                  # 配置读取
├── parser/qq_parser.py        # QQ JSON 解析（含多人记录防线）
├── analyzer/
│   ├── prompts.py             # 各维度 System Prompt
│   ├── local_stats.py         # 本地统计
│   ├── deepseek_client.py     # 接口调用、月份级缓存、图片摘要注入
│   ├── vision.py              # 图片理解：挑图、摘要、缓存
│   ├── face_emoji.py          # 表情名到 Unicode emoji
│   ├── face_images.py         # 表情原图：本地表情包 / 联网抓取 / 缓存
│   ├── usage.py               # token 用量统计
│   └── logger.py              # 日志（按天轮转、昵称脱敏）
├── webapp/                    # 应用层
│   ├── security.py            # CSRF、Origin 校验、口令登录与限流
│   ├── store.py               # 哈希、统计缓存、AI 缓存、后台统计任务
│   ├── jobs.py                # 分析任务表（去重、互斥、TTL）
│   ├── cleanup.py             # 临时文件与缓存的生命周期回收
│   ├── views.py               # 页面路由（含上传）
│   └── api.py                 # /api/* 路由
├── web/
│   ├── templates/             # 页面模板
│   └── static/
│       ├── css/style.css      # 样式与日/夜主题
│       ├── js/                # 图表渲染与任务轮询
│       └── vendor/            # Bootstrap、jQuery、ECharts（本地化）
├── tests/                     # unittest：core / hardening / optimizations / review_fixes / smoke
├── docs/                      # 早期设计文档与界面预览（内容已过时，以本 README 为准）
├── pyproject.toml             # ruff 配置
└── .github/workflows/test.yml # CI：ruff + py_compile + unittest
```

上传目录、缓存目录、日志目录在首次运行时自动创建。

## 技术栈

后端 Python 3.10+ 与 Flask 3，会话用 flask-session 存服务端文件；前端 Bootstrap 5、jQuery、ECharts 5，
图表与词云都由本地文件提供；中文分词用 jieba；模型调用走 OpenAI SDK 的兼容接口。

## 常见问题

**AI 分析报 429，提示 `Allocated quota exceeded` 或 `insufficient_quota`**

这是每分钟限流（TPM/RPM），不是套餐额度耗尽——该报错文案有误导性，限流按主账号聚合，通常一分钟内自动恢复。
程序已经内置应对：全局调用间隔平滑突发，命中 429 后全局冷却等待 25 秒并重试最多 4 次，多次不恢复才中止并
保留已完成的部分。仍然频繁触发时：

1. 调低请求强度：`LLM_CONCURRENCY=1`、`LLM_CALL_MIN_INTERVAL=5`，或调小 `LLM_MAX_DIALOG_CHARS`；
2. 到服务商控制台临时提升该模型的 TPM 限额；
3. 避开同账号其它程序的大量请求时段，所有 Key 共享同一个限流池。

**端口被占用**

Windows 上 5000 端口常被 AirPlay 或 Hyper-V 占用，在 `.env` 里设 `FLASK_PORT=5001`。

**必须联网吗**

不必须。页面、图表、样式全部本地化。只有两处涉及网络，而且都是可选的：图片理解（把图片发给你自己配置的
模型接口）与表情原图抓取。两者都可以关掉。

**怎么彻底清掉数据**

删除 `uploads/`、`flask_session/`、`ai_cache/`、`stats_cache/`、`logs/`、`face_cache/` 即可，或者把
`QQCHAT_DATA_DIR` 指到一个临时目录后再启动。

**上传群聊记录被拒绝**

工具只支持两人私聊。多人记录里除自己外的所有人都会被并进「对方」，统计与 AI 分析会整体失真，所以默认
直接拒收。确实要按「我 vs 其他人」分析时，设 `QQCHAT_ALLOW_MULTI_PARTY=1`。

## 已知限制

- 只支持两人私聊记录，多人记录默认拒收。
- 视频和文件本体不参与分析，只使用文件名与体积等元数据；图片可选做视觉理解。
- QQ 超级表情的原图没有公开地址，需要自备表情包目录才能显示。
- 首页的目录选择依赖浏览器的 `webkitdirectory`（Chrome、Edge 支持），其它浏览器请用 `QQCHAT_MEDIA_DIR`。
- 大月份的分析较慢，实测约一年、数万条记录跑一个维度约 55 秒。
- 统计与月份划分固定按北京时间（UTC+8）计算，不随系统时区变化。
- 解析后的聊天数据会在内存中保留最近一份（6 万条约几十 MB），第二次分析直接复用、不再重新解析。
- `docs/index.html` 是早期的界面预览，内容与当前版本不一致，以本 README 为准。

## 许可证

[GPL v3](LICENSE)
