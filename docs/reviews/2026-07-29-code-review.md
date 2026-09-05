# 代码评审 — 2026-07-29（基于 main@edae647 全量精读）

> **状态更新（同日）**：P0 #1-#4 与 P1 #5-#11 已全部实施于 commit 178a955（19 个测试全绿，CI 已配置）。
> P2 拓展项未动，按计划另行推进。

范围：app.py、config.py、parser/、analyzer/、web/、tests/。
结论：安全加固（d4122de + edae647）后代码质量良好，CSRF/XSS/超时/并发/容错均已到位。
以下按优先级列出剩余问题与拓展机会。

---

## P0 — 真实缺陷（影响功能正确性）

### 1. 锐评输出被 max_tokens 截断，人物锐评大概率静默失败
- 位置：`analyzer/deepseek_client.py:189`（`max_tokens=2048`）
- 问题：`SYSTEM_PROMPT_PROFILE` 要求输出 30+ 字段、每条结论附原句证据的巨型 JSON，
  2048 token（约 1500~2000 汉字）几乎必然截断 → `json.loads` 抛错 → 重试 2 次（同样截断，
  纯浪费 API 费）→ `_analyze_person` 吞异常返回 None → 前端显示"分析未产生结果"。
- 修复：
  a. `_call_api` 增加 `max_tokens` 参数，按维度传值（emotion/topics/relationship=1024，
     habits=2048，profile=8192，deepseek-chat 支持 8K 输出）；
  b. 检查 `resp.choices[0].finish_reason == "length"`：截断时记日志并**不再原样重试**，
     可选降级为"缩小样本重试一次"；
  c. JSON 解析失败与网络失败区分处理（解析失败重试无意义）。

### 2. 解析器忽略 `recalled` / `system` 标记
- 位置：`parser/qq_parser.py:94-159`
- 问题：QQChatExporter 的消息对象带 `recalled: true`（已撤回）与 `system: true`（系统提示），
  解析器完全未读取 → 撤回的消息、"对方撤回了一条消息"等系统消息照常计入总览/轮次/响应时间/词云，
  并喂给 AI 污染分析。
- 修复：`Message` 增加 `recalled: bool` / `system: bool` 字段；`load_chat` 读取标记；
  `local_stats` 与 `_build_dialog`/`_has_content` 统一过滤（系统消息与撤回消息不进入统计与分析）。

### 3. 情绪强度 0（数据不足）被图表轴裁掉
- 位置：`web/static/js/charts.js`（emotionLineChart `yAxis: { min: 1, max: 10 }`）
- 问题：prompt 与 `_clamp_int` 都允许 0=数据不足，但折线图 y 轴 min=1，0 会被画成 1，
  视觉上"数据不足"变成"平静"，误导。
- 修复：yAxis min 改 0；或 0 值单独渲染为断点（`null`）+ 图例说明。

### 4. AI 结果只存 sessionStorage，跨标签页/重启浏览器即丢失 → 重复烧钱
- 位置：`web/templates/*.html`（sessionStorage 读写）、`app.py`（无缓存）
- 问题：分析结果仅存在于当前标签页；新开标签看 /report 拿不到数据；关掉浏览器全部重做，
  每次重做都是真实 API 费用。
- 修复（推荐服务端缓存）：分析成功后按 `sha256(聊天文件)+dimension+model` 存入 session 或
  `ai_cache/` 目录（加入 .gitignore！缓存含敏感内容）；页面加载时优先取缓存，提供"强制重新分析"按钮。
  低成本替代：前端换 localStorage + 按文件哈希键控（关浏览器不丢，但换设备无效）。

---

## P1 — 健壮性 / 体验改进

### 5. AI 分析仍是同步长请求，无进度反馈
- 24 个月 × emotion 维度 = 24 次调用，并发 3 也要数分钟；浏览器/代理可能提前超时，
  用户只能盯着转圈。
- 方案：后台线程执行 + `GET /api/analyze-status/<dim>` 轮询（或 SSE），返回
  "已完成 12/24 月"；前端进度条 + 取消按钮 + 完成自动渲染。与 #4 的缓存天然配套。

### 6. 端口写死 5000，Windows 上常被 AirPlay/Hyper-V 占用
- 位置：`app.py:452`
- 修复：`FLASK_HOST`/`FLASK_PORT` 环境变量（默认 127.0.0.1:5000）；`OSError` 时给出
  "端口被占用，请设置 FLASK_PORT" 的友好提示。若开放非回环绑定，必须同时要求访问口令。

### 7. 上传解析失败留下孤儿文件；清理只在 `__main__` 执行
- `upload()` 先落盘再解析，解析失败文件留在 uploads/ 直到 24h 过期清理 → 失败分支立即删除。
- `_cleanup_old_files` 仅在 `python app.py` 时跑；`flask --app app run` 启动则永不触发 →
  移到模块导入后（app 工厂/create_all 钩子）。

### 8. 重试策略不区分错误类型
- `_call_api` 对 JSON 解析错误、429、超时统一重试。建议：429 读取 `Retry-After`；
  解析错误不重试（见 #1c）；日志记录 `resp.usage`（prompt/completion tokens）便于看成本。

### 9. 统计口径不一致：响应时间/轮次未过滤转发与系统消息
- `calc_word_freq` 跳过 type_11/17，但 `calc_response_time`、`calc_exchange_rounds`、
  `calc_overview` 统计全部消息。合并转发的时间戳会制造虚假"秒回"。统一过滤集合（配合 #2）。

### 10. 文档同步
- README「📦 依赖」段仍是范围版本（flask>=3.0…），requirements.txt 已改精确锁定 → 同步；
- 首页"上传的文件不会离开你的设备"建议补一句"AI 分析时文本片段会发送至 DeepSeek API"（与
  隐私说明一致，避免歧义）。

### 11. 加 CI
- 仓库已有 13 个 unittest，但没有自动化。新增 `.github/workflows/test.yml`：
  push/PR 时 `pip install -r requirements.txt` + `python -m unittest discover -s tests` +
  `python -m py_compile`。半小时工作量，防止未来改动破坏核心逻辑。

---

## P2 — 功能拓展（按性价比排序）

### 12. 本地"时光里程碑"统计（零 API 成本，情感价值最高）
- 连续聊天天数纪录、最长沉默期（天）、跨零点聊天次数、"凌晨三点还在聊"次数、
  单日消息峰值（哪天、多少条）、第一次对话日期、表情包大战榜。
- 全部可从现有 Message 列表纯本地计算，加一个新页面或并入仪表盘。

### 13. 一键全量分析
- `analyze_all()` 目前是死代码（无路由）。配合 #5 的异步任务框架，做成"一键生成完整报告"：
  五个维度并发跑、进度页、完成后跳转 report。

### 14. 报告导出增强
- 当前 report.html 纯表格无图表。方案：ECharts `getDataURL({type:'pixelmap'|png})` 把仪表盘
  图表转 PNG 内嵌进报告；长期可上 weasyprint 出真 PDF（依赖重，本地工具可不急）。

### 15. GitHub Pages 落地页
- `docs/index.html` 已是完整 landing page → Settings 开启 Pages（deploy from /docs），
  README 顶部加链接与截图 badges。

### 16. 增量分析
- 与 #4 缓存配合：重新上传更长的记录时，只分析新增月份（按 period 键跳过已缓存月份），
  历史越久越省钱。

### 17. 群聊支持（大工程，单列）
- 解析器假设双人（self/other）；群聊需要 N 人统计矩阵、@关系网络、成员对比。
  建议作为 v2 独立里程碑，先做数据层抽象（ChatData.participants 列表）。

### 18. 词云停用词外部化
- `_STOP_WORDS` 有大量重复项且难维护 → 移到 `analyzer/stopwords.txt`，加载时去重；
  用户可自定义加词。

---

## 已确认良好（无需动）
- CSRF token + Origin 白名单实现正确；XSS 转义覆盖所有 DOM 注入点（canvas 标题除外，已修）；
  SECRET_KEY 持久化；日志轮转 + GBK 兼容；对话抽样算法合理；prompt 工程质量高
  （采样感知/证据约束/数据不足兜底/JSON 契约注释）；测试覆盖了解析器与防护的关键路径。

## 建议实施顺序
1. #1 + #2 + #3（一次提交：正确性三连）
2. #4 + #5（一次提交：缓存 + 异步进度，核心体验升级）
3. #6-#11（杂项加固一批）
4. #12/#13/#15（功能拓展按兴趣挑）
