# 流程与重复出现的坑

## 群聊互动统计的稀疏缓存
- 日期：2026-10-10
- 适用：analyzer/group_stats.py、parser.qq_parser.ChatData；分支 codex/implement-issues-14-22
- 内容：画像使用 calc_member_interaction_summary 读取稀疏边及全员计数；calc_interaction_matrix 仅在展示边界生成旧 JSON 密集矩阵，显式 top_k=0 仍可能占用成员数平方级内存。每个聊天对象最多缓存 3 个展示矩阵。
- 验证：与 origin/main@39ef0a1 的原实现对合成样本逐字段比较，默认、全员、截断上限三种调用均相同；3000 人、2999 条相邻边的摘要计算峰值约 3411 KiB，且未创建密集矩阵。专项 unittest 与 Ruff 检查通过。
- 来源：本任务的合成差分脚本、tests/test_group_stats.py、tests/test_group_ai.py、tests/test_review_round7.py。命令不是执行授权。

## 合成分析基准与对比评测
- 日期：2026-10-11（原始验证记录见下）
- 适用：tools/analysis_benchmark.py、tests/fixtures/analysis_benchmark_v1.json；分支 codex/implement-issues-14-22
- 内容：固定 24 个全编造场景覆盖 8 类问题，以 dataset version 和 SHA-256 固定样本。validate-plan 在真实调用前检查同一批输入、调用/字符/token/输出限额及配置价格后的最坏费用；validate-run 核对结果结构、引用、实际 token/耗时/费用；compare-runs 对齐相同场景，并分别汇总引用指标、四项成对人工评分和用量差值。
- 成本与结论边界：离线校验和比较命令不调用模型。独立 run-real-plan 命令会发起真实调用，必须显式带 --execute-real 并配置专用 QQCHAT_BENCHMARK_API_KEY；执行器串行调用、关闭 SDK 自动重试、逐条原子落盘，并在调用前后核对限额。模拟输出不能作为质量提升证据。quality_evidence 依赖运行文件自声明 evaluation_type 为 real；文档已说明这是运行记录的信任边界，不是密码学来源证明。
- 验证：此前记录 test_analysis_benchmark.py 11 项通过、数据集 24 个样本校验通过，未调用真实模型；本轮没有重跑真实模型评测。#26 的实际基线/候选运行与人工评分仍未完成。
- 来源：docs/analysis_benchmark.md、tests/test_analysis_benchmark.py、tools/analysis_benchmark.py。

## 人物画像对话上下文选择
- 日期：2026-10-10
- 适用：analyzer/person_context.py、analyzer/deepseek_client.py、analyzer/group_client.py、webapp/evidence.py；分支 codex/implement-issues-14-22
- 内容：画像先留目标消息，再补目标消息明确回复的消息和同会话邻近消息；选择后按原顺序去重并标注。分析统计只以目标消息计算。证据核验调用同一选择器。profile 与 member_profiles 采用策略指纹，旧结果仅用于展示并有陈旧提示；设置 QQCHAT_PERSON_CONTEXT=0 可回退到旧选择及缓存路径。
- 回归经验：历史证据测试可能依赖旧抽样的确切输出；回退模式必须沿用原 _fit_lines，不能用新严格预算算法改变旧输出。
- 验证：人物上下文 11 项、test_p1 25 项、浏览器流程 3 项通过；群 AI 63 项、群基础 41 项通过；未调用真实模型。
- 来源：tests/test_person_context.py、tests/test_p1.py、tests/test_browser.py、docs/person_context.md。

## 全量 unittest 与真实导出夹具隔离
- 日期：2026-10-11
- 适用：tests/test_group_real_export.py；当前 qqchatlog 测试环境
- 内容：该可选测试优先读 QQCHAT_TEST_EXPORT；未设置时会从用户常见导出目录搜索最新 JSON。为避免无意读取工作站聊天数据，完整套件应显式指定已批准的合成/测试导出；若没有适用真实样本，可将该变量指向一个不存在的测试路径，让可选真实导出类按设计跳过。
- 验证：此前完整 unittest 890 项通过、31 项跳过；当前最新总量见 handoff.md。Chromium 浏览器套件之前及本轮均使用合成 fixture。Ruff check、Ruff format --check 与 git diff --check 通过。
- 来源：tests/test_group_real_export.py 与本任务检查结果。未记录或保存任何聊天内容。

## 静态脱敏分享副本
- 日期：2026-10-11
- 适用：webapp/share_report.py、web/templates/share_report_copy.html、web/templates/share_report_builder.html
- 内容：由结构化本地统计单独生成静态 HTML；默认隐藏昵称、账号、原文、精确日期和统计数字，AI 自由文本、画像段落、证据、词频和表情名称省略。预览和下载复用同一 HTML，打印调用预览 iframe。账号清理应覆盖所有消息发送者 UID（包括未归属/占位发送者），日期和账号格式不规则时提示人工检查；用户主动包含原文后仍须检查预览。
- 缓存与安全：读取统计缓存使用 read_only=True，不触碰过期时间；源报告与聊天/AI 缓存不改写。分享副本不包含脚本、原文入口、外链或聊天 UID，模板自动转义选入的原文。
- 验证：此前分享副本单测 6 项、浏览器流程 7 项通过；当前最新检查见 handoff.md。
- 来源：tests/test_share_report.py、tests/test_browser.py、本次实现和此前检查结果。

## 消息片段身份、计划任务收尾与时段口径
- 日期：2026-10-11
- 适用：webapp/analysis_scope.py、webapp/api.py、webapp/jobs.py、web/static/js/analysis_plan.js、analyzer/period_compare.py；分支 codex/implement-issues-14-22
- 内容：片段范围以解析后聊天列表的位置为身份，避免重复/空消息 ID 扩大选择；唯一旧 ID 可映射到位置，歧义旧范围会提示重选并保留其它可恢复筛选。回复链接只在目标消息唯一且仍在范围内时保留。计划任务在失败、取消、配额中止时返回已保存的状态与部分结果；聊天清理后任务 API 遮蔽并清空内存结果。预览摘要按当前勾选项重算。时段会话开启数仅用区间内消息计算，区间外相邻消息只触发截断提示。
- 验证：此前 scope 20 项、plan 14 项、period 6 项通过；最新结果及测试数量见 handoff.md。没有真实模型调用。
- 来源：tests/test_analysis_scope.py、tests/test_analysis_plan.py、tests/test_period_compare.py、tests/test_browser.py。

## 复审修复与云端快进
- 日期：2026-10-11
- 适用：webapp/analysis_scope.py、webapp/evidence.py、webapp/api.py、web/static/js/evidence.js、tests/test_browser.py；分支 codex/implement-issues-14-22
- 内容：范围视图附带源消息位置映射，证据候选返回原始列表位置，前端用 around_pos 精确展开，避免重复 ID 打开错误消息。画像主体姓名只用于核对目标身份，不再将上下文引用限制为主体发言；明确的 citation sender 及 self_evidence/other_evidence 限制仍生效。任务结果清理守卫覆盖计划任务与旧接口的维度/全量任务。范围、计划、提问接口拒绝非对象 JSON，避免 500 错误和异常清空范围。
- 同步：云端 main 从 39ef0a1 前进到 d98c7e1；分支已安全快进，云端 PR #34 的 dashboard 缓存等待与慢响应浏览器回归已合并，工作区未提交。
- 验证：同步后 unittest 916 项通过、32 项跳过；Chromium 8 项通过；Ruff check 通过，format 检查 110 个文件已格式化，git diff --check 通过。没有真实模型调用。
- 来源：tests/test_analysis_scope.py、tests/test_analysis_plan.py、tests/test_p1.py、tests/test_browser.py；2026-10-11 当前工作树检查。
