# QQ 聊天记录分析工具

导入 [QQChatExporter](https://github.com/shuakami/qq-chat-exporter) 导出的 JSON 私聊记录：先在本机做完整统计，
再把对话交给任何 OpenAI 兼容接口做五个维度的分析，结果用 ECharts 呈现。**聊天记录、缓存与日志都只留在本机。**

不配置 API Key 时，本地统计部分照常可用。

[![tests](https://github.com/woowss/qq-chatlog-analyze-tool/actions/workflows/test.yml/badge.svg)](https://github.com/woowss/qq-chatlog-analyze-tool/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/woowss/qq-chatlog-analyze-tool)](https://github.com/woowss/qq-chatlog-analyze-tool/releases)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)

- **本地优先**：不联网也能看统计；前端资源（Bootstrap、jQuery、ECharts）已本地化，没有 CDN 依赖。
- **私聊与群聊都支持**：两人记录按「我 vs 对方」分析；群聊自动切换成成员视角——互动矩阵、成员画像、
  同时在聊高峰，并区分"精确回复/@（事实）"与"相邻接话（推断）"。
- **零成本起步**：不填 API Key 就是一个纯本地统计工具，填了才走模型。
- **只为新增内容付费**：结果按「月份 + 内容」缓存，重跑同一段对话、或重导出只多了几个月时，历史月份不再重复调用。
- **一行安装**：Release 里有 wheel，`pip install` 后直接用 `qqchatlog` 启动。

## 目录

- [功能](#功能)
- [快速开始](#快速开始)
- [Windows 直装版](#windows-直装版)
- [可选能力](#可选能力)
- [配置项](#配置项)
- [分析质量与费用](#分析质量与费用)
- [隐私与数据生命周期](#隐私与数据生命周期)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [开发与测试](#开发与测试)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [许可证](#许可证)

## 功能

### 本地统计（不需要 API Key）

| 页面 | 内容 |
|---|---|
| 仪表盘 | 消息总数、聊天天数（跨度与活跃天数分开）、日均消息、图片数量与体积、视频/文件/转发/语音/表情次数、撤回计数、按 md5 去重后的图片张数、每日消息量、24 小时与星期分布、双方消息数与字数对比、对话轮次、时光里程碑（连续聊天纪录、最长沉默期、跨零点夜晚、单日峰值、最活跃月份） |
| 月度趋势 | 逐月「我 vs 对方」条数与平均句长双轴图、≥3 天冷场后的重启次数与重启方、称呼（显示名）变迁、第一条消息原文——全部纯本地、零 API 成本 |
| 习惯 | 双方表情排行、一周活跃热力图、双方高频词云（jieba 分词；可用 `QQCHAT_STOPWORD_FILE` 追加停用词） |
| 关系 | 回复速度（均值与 P50/P90，界面以中位数为主）、对话轮次 |
| 消息记录 | 原始消息浏览与搜索：关键词/发言人/月份/日期区间/分页，点任意一条看前后文；撤回与系统消息如实列出并打标，空正文的媒体消息用占位标签（看得见也搜得着） |
| 报告 | 汇总以上全部统计，可打印、导出 PDF 或下载 HTML |

**群聊记录**（导出文件里 `chatInfo.type` 为 `group`，或有 3 位以上有实质发言的参与者）会自动切换到群聊视图：
成员分别统计、互动矩阵区分"精确回复/@（事实）"与"相邻接话（推断）"、成员画像还会带上该成员在群里的
互动数字（被谁回复、@过谁）。页面与维度见下方"群聊分析"。

非文本消息同样参与统计与分析。文件、视频、转发卡片、红包、通话记录、小程序卡片、商城大表情会被标注为
`[文件:示例表.xlsx]`、`[转发:某某的聊天记录（25条）]`、`[通话:未接听]`、`[表情:叉腰]` 这样的标记送进统计与
AI 分析，而消息正文保持干净（文件名和占位符不会进入词频与平均句长）。图片有尺寸和体积信息时，也会统计
图片总体积与去重张数。

表情排行的轴标签优先显示 QQ 表情原图，其次 Unicode emoji，最后回退到表情名。原图需要手动获取一次，见
[可选能力](#可选能力)。

### AI 分析（需要 API Key）

| 维度 | 内容 |
|---|---|
| 情绪 | 逐月双方情绪、情绪强度曲线、月度基调、情绪转折点 |
| 关系 | 互动模式、亲密程度、关系角色、谁更常开启话题 |
| 习惯 | 说话风格、口头禅、标点与表情习惯、回复模式 |
| 话题 | 核心话题及权重、逐月话题变化 |
| 锐评 | 性格画像（优缺点、思维特征、情绪模式、关系动态等） |
| 总括 | **跨月全量**的一次调用：时间线分段、转折点（带月份与原文证据）、长期节奏、"数字里没说破的事"——五个逐月维度各自只见单月，趋势类结论在这里才有看完整条时间线的答案。成本约为单维度的 1/5，独立缓存族（不影响其余维度）。总括页还带**提问角**：对这份记录随便问一个问题，单次调用作答；重问同一题免费命中缓存 |

五个维度可以单独运行，也可以点「全量分析」按顺序跑完（总括是独立按钮，不计入全量的成本承诺）；每个维度页都有页内运行按钮，不必回仪表盘。分析在后台线程里跑，页面实时显示进度，可以随时取消；跑完时若你已切走标签页，会有浏览器通知与提示音。

### 群聊分析（同一套页面，按记录类型自动切换）

| 页面 | 内容 |
|---|---|
| 群仪表盘 | 成员数、记录天数、日均、同时在线高峰（滑动窗口内不同发言者数的峰值）、成员发言量排行、互动热力矩阵、成员活跃时段堆叠、互动关系图、群里程碑 |
| 群关系 | 互动关系图（实线=精确回复/@，虚线=相邻接话；默认只画最强的推断边，可切换事实/推断两层、推断强度与成员数量）、互动矩阵、每位成员的"被回复/回复别人/被@/@别人"明细 |
| 成员活跃 | 发言量与占比、互动雷达、按成员拆分的 24 小时分布、活跃度明细表 |
| 群话题 | @点名矩阵、互动关系图，以及 AI 给出的逐月话题与"每个话题是谁在聊" |
| 群情绪 | 群情绪强度走势（逐月）、成员情绪对比、情绪转折点 |
| 成员画像 | 每位成员一张卡片：群内角色、互动模式、语言指纹、关键证据（默认前 10 位，自己必定入选） |
| 群报告 | 汇总以上全部，可打印 / 导出 PDF / 下载 HTML（与私聊报告共用同一套导出机制与 SRI 校验） |

群聊 AI 维度同样按「月份 + 内容」缓存：3 个群级维度按月计费、成员画像按人计费，
分析按钮上方会显示**本次预计调用次数**（例如 6 个月 × 3 维 + 10 位成员 = 28 次，示例）。
想让多人记录回到升级前的"直接拒收"，设 `QQCHAT_GROUP_CHAT=off`（见[配置项](#配置项)）。

结果按「月份 + 内容」缓存：同一个聊天重复分析零成本，重新导出后只有新增月份需要付费。仪表盘会显示按天、
按维度累计的 token 用量与费用估算。

## 快速开始

### 1. 导出聊天记录

用 QQChatExporter 导出私聊记录为 JSON。准备使用[图片理解](#图片理解可选)时，导出时勾选导出资源文件
（`includeResourceLinks`），导出目录下会有 `resources/`。

### 2. 安装

需要 Python 3.10 或更高版本。三种方式选一种：

```bash
# 方式 A（推荐）：直接装 Release 里的 wheel，装完即得 qqchatlog 命令
pip install https://github.com/woowss/qq-chatlog-analyze-tool/releases/download/v1.2.2/qqchatlog-1.2.2-py3-none-any.whl
qqchatlog --version

# 方式 B：从源码装成命令（想改代码就用可编辑安装）
git clone https://github.com/woowss/qq-chatlog-analyze-tool.git && cd qq-chatlog-analyze-tool
pip install -e ".[dev]"

# 方式 C：不安装，源码直跑
pip install -r requirements.txt
python app.py
```

新版本见 [Releases](https://github.com/woowss/qq-chatlog-analyze-tool/releases)（同时提供 `tar.gz` 源码包，
供自行构建）。wheel 里已经包含 `web/templates` 与 `web/static`（含本地化的 Bootstrap/jQuery/ECharts），
所以装完不依赖源码目录。

数据目录随运行方式而变，需要固定位置就用 `QQCHAT_DATA_DIR`：

| 运行方式 | 数据目录默认值 |
|---|---|
| 方式 A / B（pip 安装） | Windows `%LOCALAPPDATA%\qqchatlog`、Linux `$XDG_DATA_HOME/qqchatlog`（默认 `~/.local/share/qqchatlog`）、macOS `~/Library/Application Support/qqchatlog` |
| 方式 C（源码直跑） | 仓库目录本身 |
| 任意方式 + `QQCHAT_DATA_DIR=/somewhere` | 指定的目录（上传、缓存、日志、`.secret_key` 全都跟着走） |

> `QQCHAT_DATA_DIR` 本身只能来自环境变量或第 2 类 `.env`（项目/安装目录）——它决定了数据目录在哪，
> 自然不能写在数据目录里的 `.env` 中。

### 3. 配置 API Key（可选）

配置文件叫 `.env`，任何 OpenAI 兼容接口都可以。三处来源按优先级从高到低读取，先读到的不被后面的覆盖：

1. **真实环境变量**（例如 `DEEPSEEK_API_KEY=... qqchatlog`）；
2. **从安装/项目目录向上找到的 `.env`**：源码直跑就是仓库根目录，推荐放这里；
3. **数据目录下的 `.env`**：pip 安装后没有"项目根目录"可放，写到 [安装](#2-安装)那张表里的数据目录即可。

DeepSeek 官方：

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

全部可配项与默认值见 [.env.example](.env.example)（权威清单），最常动几项的取舍见[配置项](#配置项)。默认值即可直接使用。

### 4. 启动与停止

```bash
qqchatlog          # 方式 A / B 装出来的命令
python app.py      # 方式 C，等价入口
```

两者跑的是同一个 `app:main`：先打印启动横幅与自检结果（API Key、数据目录、上传上限、口令与 Origin 提醒），
再交给 Flask 起服务；也可以用 `flask --app app run`（只跳过横幅，其余一致）。按 `Ctrl+C` 停止。

`Ctrl+C`／`SIGTERM` 是**优雅停止**：收到信号后不再派发新的月份调用，最多等 5 秒让进行中的那个月收尾，
已完成月份的结果照常落盘（默认行为见 `QQCHAT_SHUTDOWN_GRACE_SECONDS`），随后退出。
等不下去就**再按一次** `Ctrl+C`，第二次信号立即退出、不再等待（进行中那个月的结果会丢，
但已完成的月份仍在缓存里，重跑不会重复付费）。

探活/编排用 `/health`：只回一行 `ok`，不建会话、不要求登录、不写日志，可以放心让反代每秒探一次。

浏览器打开 http://localhost:5000 。默认只监听 `127.0.0.1`，调试模式关闭。

端口被占用时（Windows 上 5000 常被 AirPlay/Hyper-V 占用）在 `.env` 里设 `FLASK_PORT=5001`。本地开发需要自动
重载时设 `FLASK_DEBUG=true`，注意调试器可以执行任意代码，只在本机使用。绑定地址、端口、调试开关都走
`.env`／环境变量：Origin 白名单是按启动时的 `FLASK_HOST` 算的，所以命令行不提供 `--host/--port`
（只有 `--version`）。

### 5. 使用

1. 上传导出的 `.json` 文件，进入仪表盘查看本地统计。
2. 配置了 API Key 时，按维度运行 AI 分析，或点「全量分析」跑完五个维度。
3. 已分析的结果缓存在 `ai_cache/`，重开页面不会重复调用接口；需要重跑时勾选「强制重新分析」。

## Windows 直装版

Release 同时提供 Windows 10/11 x64 安装包与免安装压缩包：

```text
QQChatLog-<版本>-windows-x64-Setup.exe
QQChatLog-<版本>-windows-x64-portable.zip
```

安装包内置 Python 和运行依赖，普通用户无需另装 Python。安装完成后从开始菜单或桌面快捷方式启动，
启动器会等待本机服务就绪并自动打开默认浏览器。启动管理窗口可以重新打开页面、打开配置文件、打开
数据目录和日志目录，也可以停止服务并退出。重复点击快捷方式会打开已经运行的实例。

Windows 直装版默认只监听 `127.0.0.1`，端口从 `.env` 中的 `FLASK_PORT` 开始寻找可用端口。
用户数据与程序目录分开保存，默认仍是 `%LOCALAPPDATA%\qqchatlog`；升级不会覆盖 `.env`、聊天记录、
分析缓存或日志，卸载程序也不会删除这些数据。AI 分析仍需在配置文件中填写自己的 OpenAI 兼容接口 Key。

开发者可以在 Windows 上安装 Inno Setup 6，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
```

构建产物会写入 `dist/windows/`，其中包含安装包、免安装 ZIP 和 `SHA256SUMS.txt`。构建过程不会把真实
聊天记录、`.env`、`.secret_key`、缓存或日志放入发布物。

## 可选能力

### 图片理解（可选）

需要把聊天里的图片也交给模型分析时，用首页的「选择导出目录（含图片）」按钮，选中 QQChatExporter 的导出
目录即可。流程是：浏览器先上传 JSON，服务端解析后告诉前端这次分析需要哪几十张图片，浏览器只把这些图片传到
本机服务。

这样不会把整个 `resources/` 目录搬一遍——实测一个近 1 GB 的导出目录，实际只需要上传几十张图片。图片副本
放在 `uploads/media/<哈希>/`，随 `uploads/` 的 24 小时策略回收；识别出的图片摘要按图片指纹长期缓存，
所以重跑分析不需要重新上传图片。

Firefox 等不支持目录选择的环境，可以在 `.env` 里设 `QQCHAT_MEDIA_DIR=导出目录`，由服务端直接读取。
两种方式可以并存，WebUI 上传的副本优先。

### 获取原始表情图（可选，默认关闭）

在 `.env` 里设 `QQCHAT_FACE_IMAGES=true`，然后到「习惯」页点一次「获取原始表情图」。程序会从 QQ 的公开
表情 CDN 把经典黄脸和商城表情的原图下载到本地缓存（`face_cache/`），之后离线复用、不再联网。

实测覆盖约 58% 的表情使用次数。一次点击最多联网抓 60 秒，抓不完的会在下次点击时继续（已抓到的都已落盘）。
QQ 的超级表情（吃糖、大怨种、菜汪之类）没有公开地址，抓不到，会继续用 emoji 或表情名显示。想要 100% 原样，
可以把 `QQCHAT_FACE_DIR` 指向自己准备的表情图目录，文件名支持 `<编号>.gif`、`e<编号+100>.gif`、
`<表情名>.gif`。断网或抓取失败时静默跳过，不影响其它功能。

## 配置项

全部可配项连同默认值与"为什么要设它"，逐条写在 **[.env.example](.env.example)** 里——那份清单与代码同源，各维度
输出预算还有测试对着代码默认值核验（见 `tests/test_review_round5.py`），因此以它为准；本文件不复述清单，
也刻意不写"共几项"这样的数字（数字会漂，写一次就要同步一次，与下面「开发与测试」里不写用例数同一立场）。

配置来源与优先级见 [配置 API Key](#3-配置-api-key可选)。

### 常用配置

只列最常需要动的几项，默认值与完整说明都在 [.env.example](.env.example)。

| 变量 | 什么时候需要动 |
|---|---|
| `DEEPSEEK_API_KEY` | 要用 AI 分析就填（留空则只用本地统计） |
| `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` | 换 OpenAI 兼容网关（如百炼 Qwen）时成对改 |
| `ACCESS_PASSWORD` | 绑定非回环地址（局域网/公网）**必填**，否则拒绝启动 |
| `ALLOWED_ORIGINS` | 用局域网 IP 或域名打开页面时必填，否则上传与分析请求被 403 拒绝 |
| `QQCHAT_COOKIE_SECURE` | 明文 http 访问局域网地址时设 `false`（口令与会话会明文过网，风险自负） |
| `FLASK_HOST` / `FLASK_PORT` | Windows 上 5000 常被 AirPlay/Hyper-V 占用，可改 5001 |
| `QQCHAT_DATA_DIR` | 想把聊天记录与 AI 结果搬到别的盘（见[安装](#2-安装)） |
| `LLM_MAX_CALLS_PER_RUN` | 想给一次运行算一个封顶的花费时设，例如 200（默认 0 = 不限） |
| `QQCHAT_MONTH_CACHE` | 设 `0` 关闭月份级增量缓存：每次整份重算，会重新付费 |
| `QQCHAT_CACHE_SLIDE_DAYS` / `QQCHAT_CACHE_MAX_DAYS` | 派生缓存回收（默认滑动 30 天 / 绝对 90 天）；想长期保留已付费结果就调大，`0` = 不过期 |
| `QQCHAT_MEDIA_DIR` | Firefox 等不支持选目录时，用它让服务端直接读图 |
| `LLM_VISION` | 设 `false` 完全不上传图片 |
| `QQCHAT_FACE_IMAGES` | 想显示 QQ 表情原图时打开，详见[可选能力](#可选能力) |
| `QQCHAT_GROUP_CHAT` | 多人记录的处置：`off` 直接拒收、`two_party` 按「我 vs 其他人」归并 |

其余全部可调项——思考模式与各维度输出预算（`LLM_THINKING`、`LLM_MAX_TOKENS_<维度>`）、并发与调用间隔、
单月与群聊对话字符预算、图片体积与张数上限、表情抓取参数与本地表情目录、缓存盐值与月份宽限期、
登录限流、任务 TTL、停机宽限、上传体积上限、停用词文件、费用单价、数据与日志目录的单独覆盖、
日志脱敏与保留天数——都按同一顺序写在 [.env.example](.env.example) 里，每一项都带默认值与理由。

## 分析质量与费用

默认配置按准确性优先设置：

- 单月对话预算 60 万字符，足以装下绝大多数月份的全部消息，正常情况下不抽样；
- 官方 DeepSeek 端点默认全维度开启思考模式，各维度输出预算 32k（锐评 49k），不会出现思维链吃满预算导致
  结果被截断丢弃；图片摘要固定走非思考模式（它的预算只有 512 tokens，思维链必然把摘要截成半截话）；
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

## 隐私与数据生命周期

聊天记录只保存在本机：作者不收集、也不接收任何数据；只有你启用 AI 分析时，对话文本才会发送到你自己配置的接口。前端资源（Bootstrap、jQuery、ECharts）已本地化，断网也能正常
使用。

AI 分析发送给**你自己配置的**接口，发送的内容包括：

- 对话文本，以及媒体元数据标记（文件名、转发标题、表情名等）；
- 开启图片理解时，每月最多 `LLM_VISION_MAX_PER_MONTH` 张图片（按 md5 去重、跳过表情包尺寸与超大文件）。
  视频和文件本体不会被读取；关掉 `LLM_VISION` 就完全不发送图片。

本地数据的保留策略：

| 目录 | 策略 |
|---|---|
| `uploads/`、`flask_session/` | 超过 24 小时回收（启动时一次，之后每小时随请求触发一次）。`uploads/` 是**递归**回收的：看图用的图片副本在 `uploads/media/<哈希>/`，属于子目录，早先只扫一层文件会导致它们永不回收 |
| `ai_cache/`、`stats_cache/` | 滑动 30 天 + 绝对 90 天，两条上限同时生效（`QQCHAT_CACHE_SLIDE_DAYS` / `QQCHAT_CACHE_MAX_DAYS` 可调，0=不过期——已付费结果想永久留在本机就调这里） |
| `logs/` | 按天轮转，默认保留 7 天，昵称脱敏；**上游接口回显的错误文本会先抹掉 API Key**（第三方网关常把它原样写回错误正文，而这段文字会显示在页面上、也会进日志）。`logs/job_history.jsonl` 只记任务维度/状态/计数（按 `LOG_RETENTION_DAYS` 过期 + 有界 1000 行双重上限），不含聊天内容与错误文本；它同样在「删除本聊天」时被一并抹掉——`chat_hash` 在本工具里就是那份记录的身份，"分析过它"这条元信息不该在删除后独自留下 |
| `face_cache/` | QQ 表情原图，与聊天内容无关，**不参与自动回收**（没有 TTL 是有意的：它不含任何聊天信息，随时手动删除即可） |

重新上传时，旧文件及其派生缓存立即删除——**即使那份上传副本已被 24 小时回收**（旧文件的
哈希取自会话，不依赖文件还在盘上）；删除聊天记录时，对应的统计缓存、AI 缓存、图片摘要、
**`uploads/media/<哈希>/` 里的图片副本**、以及任务历史里属于该聊天的行一并回收（仪表盘
「数据与隐私」的「删除本聊天」按钮可主动触发同一套清理，不必再靠"先传一份别的文件顶掉"）。
删不掉的（Windows 上文件被占用很常见）会**留下告警日志**而不是静默重试到永远。
想带着已付费结果换机器：「数据与隐私」的导出按钮打包统计 + 各维度结果 + 月份增量缓存，
新机在首页「导入结果包」即可继续复用。导入的包只允许写**它自己声明的那份聊天**的缓存
（`meta.json` 里的 `chat_hash` 是归属依据），条目路径一律不信；包里的月份清单（`months`）
还会再按**形状**过一道闸——它会被当月分文件的路径成分用，形状不对的键一律丢弃，
免得一个来路不明的 zip 借它去碰缓存目录之外的文件。如果那个包恰好对应你
当前正打开的聊天，会先要你确认一次再覆盖——免得一个来路不明的 zip 静默换掉你的结论。
缓存按**内容哈希**寻址，所以同一份记录被
两个浏览器/两台设备同时打开时，只有一方换文件不会删掉另一方正在用的统计与已付费结果；
等最后一个使用者也换走内容，清理才真正发生（引用随会话本身的 24 小时窗口失效，不会永久钉住）。
分析跑到一半时换文件，那一轮的维度结果与月份文件都不再落盘（任务如实记为已取消）。
想立即清除全部数据，删除上述目录即可，或者用
`QQCHAT_DATA_DIR` 把数据整体放到别处（`.secret_key` 也会跟着数据目录走）。

其它安全措施：

- 服务默认只绑定回环地址；绑定非回环地址时若未设置 `ACCESS_PASSWORD` 则拒绝启动；
- 登录成功后轮换服务端 session id，旧 id 立即失效（防会话固定）；会话 cookie 带 `HttpOnly`、
  `SameSite=Lax`，并在非回环绑定时自动加 `Secure`（见 `QQCHAT_COOKIE_SECURE`）；
  导航栏的「退出登录」（设了口令才出现）只接受 POST + CSRF，并会**作废服务端会话**：
  旧 session id 的存储连同内容一起删掉，浏览器拿到的是一个全新的匿名会话——旧 cookie
  即使被别人拿到也指不到任何东西（不是"只清内容、文件还躺在盘上等回收"）；
  会话过期时 API 回 401 JSON 而不是把人重定向去登录页（否则前端只会拿到一页 HTML）；
- POST 请求同时校验 CSRF token 与 Origin，Origin 白名单不信任请求自带的 Host（防 DNS rebinding）；
- 登录后的 `next=` 跳转只接受站内路径（含 `/\host` 这类反斜杠变体）；登录接口对同一客户端地址有
  失败限流（默认 5 次 / 5 分钟，超限返回 429 并带 `Retry-After`，成功即清零）——反代/NAT 之后所有
  请求共享一个地址，一个人输错会把所有人锁住，那种部署请调大 `QQCHAT_LOGIN_MAX_ATTEMPTS`；
- 所有响应都带 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy` 与同源 CSP，即便某处渲染漏了
  转义也拿不到跨站资源；
- 导出的 HTML 报告会剥掉 CSRF token、把第三方资源换回 CDN（并带上 SRI `integrity` + `crossorigin`）、
  自有样式与表情图内联，可以直接分享。SRI 哈希与本地 vendor 文件由 `tests/test_sri.py` 钉死，升级资源后可用
  `python tools/verify_vendor_sri.py` 联网复核。

## 项目结构

```
qq-chatlog-analyze-tool/
├── app.py                     # 组装入口：Flask 初始化 + 命令行 main()（qqchatlog 命令）
├── config.py                  # 配置读取（.env / 环境变量、数据目录、密钥）
├── parser/
│   ├── qq_parser.py           # QQ JSON 解析（含群聊判定与安全阀）
│   └── group_identity.py      # 群成员身份：参与者名单、同名成员唯一化、占位 sender 识别
├── analyzer/
│   ├── prompts.py             # 各维度 System Prompt（私聊）
│   ├── group_prompts.py       # 群聊维度 System Prompt（独立模块 → 不影响私聊缓存指纹）
│   ├── recap_prompts.py       # 总括/提问的 System Prompt（同样独立成模块，各自独立指纹族）
│   ├── recap_client.py        # 跨月总括维度 + 自定义提问：跨月事实摘要、独立缓存族、单次调用
│   ├── local_stats.py         # 本地统计（私聊）
│   ├── group_stats.py         # 群聊本地统计：成员活跃度、三张互动矩阵、群里程碑
│   ├── group_client.py        # 群聊 AI：对话构建、成员感知抽样、四个群聊维度
│   ├── deepseek_client.py     # API 层：接口调用、限流与重试、思考模式、各维度执行体、提示词指纹
│   ├── dialog.py              # 对话构建：把消息压成喂模型的文本（进指纹，改名/改注释会作废缓存）
│   ├── month_cache.py         # 月份级增量缓存：内容寻址键、manifest 引用计数、孤儿回收
│   ├── vision.py              # 图片理解：挑图、摘要、缓存
│   ├── face_emoji.py          # 表情名到 Unicode emoji
│   ├── face_images.py         # 表情原图：本地表情包 / 联网抓取 / 缓存
│   ├── usage.py               # token 用量统计
│   └── logger.py              # 日志（按天轮转、昵称脱敏）
├── webapp/                    # 应用层
│   ├── security.py            # CSRF、Origin 校验、口令登录与限流
│   ├── store.py               # 哈希、统计缓存、AI 缓存、后台统计任务、结果导出/导入、提问缓存
│   ├── jobs.py                # 分析任务表（去重、互斥、TTL）+ 任务历史落盘
│   ├── cleanup.py             # 临时文件与缓存的生命周期回收
│   ├── views.py               # 页面路由（含上传）
│   └── api.py                 # /api/* 路由
├── web/                       # 前端资源（作为数据包随 wheel 分发）
│   ├── __init__.py            # 让 web/ 成为包，并给出 templates/static 的包内绝对路径
│   ├── templates/             # 页面模板
│   └── static/
│       ├── css/style.css      # 样式与日/夜主题
│       ├── js/                # 图表渲染与任务轮询（group_charts.js = 群聊图表与结果渲染）
│       └── vendor/            # Bootstrap、jQuery、ECharts（本地化，版本见该目录 README）
├── tests/                     # unittest：core / hardening / optimizations / packaging / review_fixes /
│                              #   review_round2..5 / smoke / sri / group_*（地基·统计·AI·前端·真实格式）
├── tools/inspect_chat.py      # 导出文件体检（只读、默认脱敏，判断格式漂移与统计对账）
└── CHANGELOG.md               # 行为变更记录（含回滚方式）
├── tools/verify_vendor_sri.py # 导出报告用的 SRI 哈希：联网复核 本地 <-> CDN <-> 内联常量
├── tools/verify_wheel.py      # 拆 wheel 核对：代码、templates/static、qqchatlog 入口点、依赖元数据
├── docs/                      # 早期设计文档与界面预览（内容已过时，以本 README 为准）
├── pyproject.toml             # 打包元数据（console_scripts / package-data）+ 依赖声明 + ruff 配置
└── .github/workflows/test.yml # CI：ruff + py_compile + unittest，另有一条 wheel 构建/安装验证
```

`qqchatlog = app:main` 由 `[project.scripts]` 声明，`pip install` 时生成同名命令。上传目录、缓存目录、日志目录在
首次运行时自动创建。

## 技术栈

后端 Python 3.10+ 与 Flask 3，会话用 Flask-Session 存服务端文件（cachelib 的 `FileSystemCache` 后端）；
前端 Bootstrap 5.3.2、jQuery 3.7.1、
ECharts 5.6.0（词云用 echarts-wordcloud 2.1.0），全部本地化随包分发；中文分词用 jieba；模型调用走
OpenAI SDK 的兼容接口。

## 开发与测试

```bash
pip install -e ".[dev]"                      # 或 pip install -r requirements.txt
python -m ruff check .                       # 代码检查（CI 同款）
python -m ruff format .                      # 统一风格；CI 用 --check 卡住
python -m unittest discover -s tests -v      # 全量单测（含逐页冒烟、打包自检、会话后端自检）
```

测试默认无条件阻止真实 LLM 客户端，即使开发机的 `.env` 配置了 API Key；pytest 与 unittest
使用同一条护栏。只有明确执行联网测试时才设置 `QQCHAT_TESTS_ALLOW_REAL_LLM=1`，此时可能产生
模型费用。其它取值（包括 `0`、`false`）仍保持护栏开启。

用例数**刻意不写在这份文档里**：写死了就得每加一条用例同步改一次，而漏改的表现仅仅是
"README 说了个过时的数字"——此前已经漂移过两次，且没有守卫能发现。数量以这条命令输出里的
`Ran N tests` 为准。

`ruff format` 有意排除了两处（见 `pyproject.toml` 的 `[tool.ruff.format] exclude`）：
`analyzer/prompts.py`（提示词按"一段一行"手工排版，格式化只会把它改成括号 + 链）与 `docs/`
（早期设计文档，里面的示例代码不值得再动）。

打包相关的自检（CI 的 package job 跑的就是这两步）：

```bash
python -m build --wheel --outdir .tmp_dist        # 构建 wheel
python tools/verify_wheel.py .tmp_dist/*.whl      # 拆包核对：代码 + templates/static + 入口点 + 依赖元数据
```

CI（`.github/workflows/test.yml`）分两条：

- **test**：Python 3.10 / 3.12 / 3.13 / 3.14 矩阵，`ruff check` → `ruff format --check` → `py_compile` → 全量单测；
- **package**：真实构建 wheel、拆包核对，再装进干净 venv 跑一次 `qqchatlog --version`，并从仓库外的工作目录
  装配应用、渲染一次模板（证明模板与静态资源确实进了包）。

依赖更新由 Dependabot 每周开 PR。注意运行期依赖声明在两处，需要同步：`pyproject.toml` 的
`[project] dependencies`（范围）与 `requirements.txt`（精确锁定）。

发布新版本的流程：

1. 改 `pyproject.toml` 里的 `version`；
2. `python -m build --outdir dist && python tools/verify_wheel.py dist/*.whl`；
3. 提交、打 tag（`vX.Y.Z`）并推送，然后在 GitHub Release 里附上 wheel 与 sdist —— 这一步的内容与 CI
   package job 完全一致，本地核对过就不会有意外。

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

**pip 安装后，数据、缓存和日志在哪**

在用户数据目录（见[安装](#2-安装)那张表），不在 site-packages 里。想放回自己指定的位置，设
`QQCHAT_DATA_DIR=/somewhere`；日志是数据目录下的 `logs/app.log`。配置文件 `.env` 也可以直接放在这个
数据目录里（优先级低于环境变量与项目目录的 `.env`）。

**怎么升级到新版本**

从 [Releases](https://github.com/woowss/qq-chatlog-analyze-tool/releases) 拿新 wheel 再装一次即可：

```bash
pip install https://github.com/woowss/qq-chatlog-analyze-tool/releases/download/v新版本/qqchatlog-新版本-py3-none-any.whl
```

数据目录与缓存都不受影响，已分析过的月份继续命中缓存。

**必须联网吗**

不必须。页面、图表、样式全部本地化。只有两处涉及网络，而且都是可选的：图片理解（把图片发给你自己配置的
模型接口）与表情原图抓取。两者都可以关掉。

**怎么彻底清掉数据**

删除数据目录下的 `uploads/`、`flask_session/`、`ai_cache/`、`stats_cache/`、`logs/`、`face_cache/` 即可，
或者把 `QQCHAT_DATA_DIR` 指到一个临时目录后再启动。

**上传群聊记录的处理方式**

默认（`QQCHAT_GROUP_CHAT=auto`）会把它**当群聊分析**：每位成员分别统计，互动矩阵区分
"精确回复/@（事实）"与"相邻接话（推断）"，成员画像还会带上该成员在群里的互动数字。
如果你更希望回到升级前的行为（多人记录直接拒收，避免"其他人"被并进「对方」），
设 `QQCHAT_GROUP_CHAT=off`；确要按「我 vs 其他人」两分类归并，设 `two_party`
（等价于旧的 `QQCHAT_ALLOW_MULTI_PARTY=1`）。

## 已知限制

- 群聊与两人私聊都支持：群聊按成员分别统计（含互动矩阵、成员画像），私聊按「我 vs 对方」分析。
- 群聊的成员画像默认只分析发言最多的 10 位（`QQCHAT_GROUP_AI_MAX_MEMBERS` 可调）；
  互动矩阵默认只保留前 30 位成员的格子，其余成员的活跃度与群总览仍然完整。
- 群聊维度按"月 × 维度 + 人数"计费（例如 6 个月 × 3 个群级维度 + 10 位成员 = 28 次调用，示例），
  分析前页面上会给出预计调用次数。
- 视频和文件本体不参与分析，只使用文件名与体积等元数据；图片可选做视觉理解。
- QQ 超级表情的原图没有公开地址，需要自备表情包目录才能显示。
- 首页的目录选择依赖浏览器的 `webkitdirectory`（Chrome、Edge 支持），其它浏览器请用 `QQCHAT_MEDIA_DIR`。
- 大月份的分析较慢，实测约一年、数万条记录跑一个维度约 55 秒。
- 统计与月份划分固定按北京时间（UTC+8）计算，不随系统时区变化。
- 解析后的聊天数据会在内存中保留最近一份（数万条约几十 MB），第二次分析直接复用、不再重新解析。

## 许可证

[GPL-3.0-or-later](LICENSE)（源文件头部同样声明 "either version 3 of the License, or any later version"）。
