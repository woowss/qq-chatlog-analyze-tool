# 分析质量基准

## 固定样本

`tests/fixtures/analysis_benchmark_v1.json` 是版本化的合成语料，共 24 个场景，覆盖身份归属、反讽、早期消息引用、争执与和解、证据不足、时间变化、群聊串话和低频成员。姓名、对话、日期、事实及解释均为编造内容。修改场景时提升 `dataset_version`；每次运行记录数据集文件的 SHA-256，防止把不同内容误认为同一批样本。

每个场景包括消息 ID、可核对事实及其证据 ID、可接受解释和应拒绝的过度结论。事实和解释分开保存；解释不是唯一标准答案。

场景 ID、类别、参考事实和原始消息 ID 只用于评测与人工复核，不放进候选模型请求。每次请求会把选中的消息 ID 临时映射为中性别名（`m1`、`m2` 等）；模型返回的证据别名在保存运行记录前还原为原始 ID，因此离线评分和导入格式保持不变。引用不在本次请求别名表中的消息会使运行记录无效，并停止后续调用。

## 离线检查

```powershell
python tools/analysis_benchmark.py validate-dataset
python tools/analysis_benchmark.py validate-plan path\to\real-plan.json
python tools/analysis_benchmark.py validate-run path\to\run.json
python tools/analysis_benchmark.py review-template path\to\run.json --output path\to\review.json
python tools/analysis_benchmark.py validate-review path\to\run.json path\to\review.json
python tools/analysis_benchmark.py compare-runs baseline.json candidate.json
python tools/analysis_benchmark.py compare-runs baseline.json candidate.json `
  --baseline-review baseline-review.json --candidate-review candidate-review.json
```

运行记录是模型或模拟结果的导入格式。离线工具核对数据集版本、场景覆盖、结果结构（记录了 `dimension` 时复用应用结果校验器）、证据 ID 是否属于该场景且确实送入模型、引用原文是否与证据消息相符，并分别汇总样本覆盖、引用覆盖、token、耗时和可选费用。它不判断语义是否正确，也不会调用模型。

计划须保存实际使用的 `system_prompt` 与 `user_prompt`。它们及选择的消息会组成每条真实请求；运行记录保存两段提示词的 SHA-256，不保存提示词正文。不要在自定义提示词中手动加入数据集类别、场景 ID 或原始消息 ID。不同条件应使用不同 `prompt_version` 和计划文件。

`compare-runs` 要求两份运行有不同的 `run_id`，并使用相同数据集哈希、评测类型和场景 ID 集合。它保留两侧的模型、提示词版本和输入策略，逐项报告机械引用指标、四项人工评分和用量差值。两个运行可以改变模型、提示词或输入策略；比较报告会保留这些条件，帮助解释结果。只有两侧人工评分文件都有效时才计算成对评分，评分的均值、胜平负和样本数按指标分别展示，不合并为单一质量分。

`review-template` 为每个场景带上完整消息、哪些消息进入模型输入、结果声明和基准事实；评审人填写 `reviewer` 及四项 1–5 分。为减少偏差，评审前可将条件名称改为 A/B；评审完成后保留各模板的 `run_id`，再分别传给 `compare-runs`。`validate-review` 检查评审文件与运行身份、数据集版本、场景覆盖、评分范围和遗漏事实 ID。

没有配置价格时 `estimated_cost` 为 `null`。配置价格后，仅在每条样本均提供输入/输出 token 时计算估算费用；真实评测还须记录限额与预算。模拟输出只能验证工具链和离线规则，不能作为模型质量提升的证据。

## 人工评审标准

对同一场景的基线和候选结果使用同一评审人或交叉评审，并先隐藏条件名称。每项按 1–5 分记录，分数越高越好：

| 指标 | 1 分 | 3 分 | 5 分 |
| --- | --- | --- | --- |
| 证据支持 | 多项结论无对应消息或与原文冲突 | 主要结论有证据，少量证据薄弱 | 关键结论均能由所引消息直接支持 |
| 关键事实覆盖 | 遗漏多数可核对事实 | 覆盖部分事实 | 覆盖所有与任务相关的事实 |
| 解释校准 | 把猜测写成事实或误读反讽/归属 | 大体合理，存在一处过度推断 | 解释贴合上下文，并明确区分事实与推断 |
| 不确定性校准 | 证据不足仍断言，或明显可答却一概拒绝 | 不确定性表达不完全匹配证据 | 证据不足时克制，证据充分时给出有边界的结论 |

另在 `missed_fact_ids` 填遗漏事实，在 `unsupported_claims` 记录不受支持的结论。出现严重身份错配、编造事件或将反讽按字面下结论时，应在备注标记，即使总分较高也需单独报告。各项分别报告，不合并成单一总分；同时披露每项样本数、评审人数和分歧处理方法。

## 运行记录与成本

运行记录至少标记固定样本版本与哈希、唯一运行 ID、条件名称、模型、提示词版本、输入策略名称/版本、评测类型、逐场景输入消息 ID、模型结果或声明、输入/输出 token 与耗时。支持成本比较的运行还记录输入/输出百万 token 单价和币种。比较双方须使用相同场景集合；不完整但对齐的场景子集会报告覆盖率，不能被当作完整基准结果。

真实模型评测计划必须显式标记 `evaluation_type: "real"`，保存实际提示词，列出每个场景和输入消息 ID，并配置正数 `limits.max_calls`、`limits.max_input_chars`、`limits.max_total_prompt_tokens`、`limits.max_output_tokens`。`max_input_chars` 包含系统提示词、任务提示词和全部选中消息；预检用 UTF-8 字节加消息封装余量估算输入 token，并以 `max_total_prompt_tokens` 核对最坏费用上界。

先运行 `validate-plan`。真实执行使用独立的评测 Key，避免误用应用配置：

```powershell
$env:QQCHAT_BENCHMARK_API_KEY = '专用于评测的 Key'
$env:QQCHAT_BENCHMARK_BASE_URL = 'https://api.deepseek.com/v1'
python tools/analysis_benchmark.py run-real-plan path\to\plan.json path\to\run.json --execute-real
```

没有 `--execute-real` 时命令拒绝调用；没有配置 `QQCHAT_BENCHMARK_API_KEY` 时也会拒绝。执行器逐条串行调用、关闭 SDK 自动重试，并将 `max_output_tokens` 作为每次请求的输出上限。每次调用前重核调用数、完整请求字符数和 token 估算；收到响应后核对服务端实际 token 与配置费用，超过限额即停止后续调用。计划的最坏费用预算会在第一条请求前核验。服务端用量是在响应后才能确认；若服务端实际 token 超过计划额度，该次请求已发生，执行器会记录超限并停止，无法撤销该次费用。

输出文件使用原子替换，在每条响应后保存进度。调用失败或输出格式无效会留下 `execution_status: "incomplete"`，`validate-run` 将拒绝把不完整记录用于有效比较。输出路径必须不存在且不能覆盖计划或数据集。配置价格时，计划和运行记录还须提供 `limits.cost_budget`；计划按限额上界核算，运行记录按实际 token 核算。评测命令不会在 CI 或普通离线命令中自动运行。

只有两侧完整覆盖 24 个固定场景、真实运行记录通过校验、并且两份人工评审均覆盖全部样本时，比较报告才标记 `quality_evidence: true`。该标记依赖运行文件对真实评测的声明；最终报告仍需注明评审人数、评审人是否相同及分歧处理方法。CI 默认只运行离线检查和模拟用例，不访问模型。真实评测未实际执行、人工评审未完成或场景覆盖不全时，质量结论保持未完成。
