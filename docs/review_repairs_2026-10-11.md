# 九项审查问题的修复与独立复审

日期：2026-10-11，Asia/Hong_Kong。用户要求由 GPT-6 Luna / max 子代理完成全部修复，再由根代理审查。

本轮审查确认的 **7 项 P2、2 项 P3 均已修复**。根代理逐项检查最终实现、失败和清理路径、缓存兼容性及回归证据，并独立运行完整自动验证；在本轮修复范围内，未发现仍需处理的审查问题。

## 工作范围

- Worktree：`C:\Users\admin\.codex\worktrees\8883\qqchatlog`。
- 分支：`codex/implement-issues-14-22`；HEAD：`d98c7e1d94681f8660fa96e499a150d04583d07a`，修复后未变。
- 继承并保留当前未提交实现；三名 GPT-6 Luna / max 子代理分别负责后端、分析与图片缓存、前端及浏览器回归，根代理负责指导、独立复审和整体验证。
- 修复开始前保存源码快照与 SHA-256 清单；修复前验证在独立副本中执行，没有反转或覆盖活动源码。最终核对快照内容未变。
- 仅用合成聊天、临时缓存、模拟模型和本地 Chromium。真实 LLM 调用已禁用，真实群聊及私聊导出路径均设为不存在的哨兵。
- 没有提交、推送、创建 PR、修改 Issue 或核查远端状态。旧 `.codex-memory/` 未读取或修改；项目交接记录写入已确认身份的 V2 Worktree 状态。

## 九项修复

| 编号 | 原问题 | 最终行为与验证重点 |
| --- | --- | --- |
| 1 / P2 | 脱敏选项改变后，旧分享响应恢复旧下载 | 选项变化立即清空 iframe、导出内容及告警，使在途请求失效；过期成功、失败和收尾回调均不再覆盖新状态。Chromium 覆盖选项变化、新请求和旧请求乱序返回。 |
| 2 / P2 | 跨标签页的范围结果污染全量 sessionStorage 回退 | 服务端响应提供实际 `chat_hash + scope_id`；任务轮询使用创建时冻结的身份，HTML 也冻结文档身份。结果显示和缓存写入前重新确认服务端状态，缓存键及内容同时绑定聊天、范围和维度；旧 `ai_*` 无身份结果不再使用。覆盖 GET、任务完成、404/断网回退、不同聊天、其他标签页换范围，以及初始/乱序身份响应。 |
| 3 / P2 | 月份缓存关闭时部分计划绕过结构校验 | 新月份、人物和成员结果的 schema 校验独立于缓存开关；部分计划的聚合结果在成功、部分返回或取消保留前也必须合规。无效结果不会标成完成或写成完整维度缓存。 |
| 4 / P2 | 后续成员失败使已成功的付费缓存失去 manifest 引用 | 部分成员执行通过 `finally` 登记已成功的缓存；普通成员失败可继续独立成员，配额失败中止后续维度。任务保留合规部分结果。临时缓存实测 manifest 引用和零宽限期孤儿清理，已完成成员文件仍保留。 |
| 5 / P2 | 时长口径修复后仍命中旧范围问答缓存 | 对实际过滤过的问答输入使用带时长语义版本的范围键；旧键不再命中。全量输入的缓存保持兼容。回归预置旧答案并确认重新计算。 |
| 6 / P2 | benchmark 向候选模型泄漏类别和语义 ID | 请求不包含场景 ID、类别或原始消息 ID，消息改用每次请求内的中性别名 `m1`、`m2` 等。模型返回的证据 ID 在保存前还原；未知别名使记录无效并停止后续调用。原始记录、离线验证和人工评分仍使用原始 ID。 |
| 7 / P2 | 不同聊天同路径图片串用进程摘要，清理不删 memo | memo 按聊天隔离，新缓存键包含实际文件内容 SHA-256，同路径文件替换会失效。旧缓存仅在导出 MD5 与实际文件一致时迁移，兼容两种历史键。聊天清理删除对应 memo，清理标记期间不再填回 memo 或迁移旧文件。保留同聊天跨维度复用和只读预览。 |
| 8 / P3 | 仅改变画像目标导致相同问答重复调用 | 问答缓存和并发去重按实际输入身份计算；target-only 范围与全量消息输入共用同一问答键，页面响应仍携带真实范围身份。覆盖缓存及在途去重。 |
| 9 / P3 | 较旧搜索响应覆盖新查询 | 消息搜索和关键词时间线分别使用请求序号；旧成功、失败和收尾回调均失效，Reset 也使在途时间线失效。Chromium 使用可达的搜索/时间线操作验证乱序和重置。 |

第 7 项和第 9 项在原 HEAD 基线中已经存在，其余为当前新增功能或修复的边界问题。

主要实现：[analysis_scope.js](C:/Users/admin/.codex/worktrees/8883/qqchatlog/web/static/js/analysis_scope.js)、[analyze.js](C:/Users/admin/.codex/worktrees/8883/qqchatlog/web/static/js/analyze.js)、[share_report_builder.html](C:/Users/admin/.codex/worktrees/8883/qqchatlog/web/templates/share_report_builder.html)、[messages.html](C:/Users/admin/.codex/worktrees/8883/qqchatlog/web/templates/messages.html)、[jobs.py](C:/Users/admin/.codex/worktrees/8883/qqchatlog/webapp/jobs.py)、[api.py](C:/Users/admin/.codex/worktrees/8883/qqchatlog/webapp/api.py)、[vision.py](C:/Users/admin/.codex/worktrees/8883/qqchatlog/analyzer/vision.py)、[analysis_benchmark.py](C:/Users/admin/.codex/worktrees/8883/qqchatlog/tools/analysis_benchmark.py)。

## 根代理复审中的补充修正

- 页面身份直接来自服务端渲染，不由第一次异步范围读取重新定义；旧身份读取不能覆盖较新的读取或提前释放等待者。
- 成功、失败和取消任务的结果均验证身份；无法确认服务端身份时停止显示本地回退内容，并提示重试。
- 范围广播复用页面的监听频道，避免同一标签页创建第二个频道后收到自己的广播、抢先刷新。真实浏览器确认发起页只发生预期跳转，另一标签页只刷新一次。
- 部分结果保留也受清理边界约束。普通任务在锁内检查清理标记；计划任务在最终结果更新的 `JOBS_LOCK` 内重新检查，防止清理后回填结果。
- 图片摘要从磁盘读取或旧键迁移时再检查清理标记；只读预览不会调整 memo 的 LRU 顺序。兼容迁移同时覆盖原始源码指纹和 AST 指纹的历史键。
- 全套检查发现的旧模拟结果替换为现有 schema 合规夹具，原取消、进度、提示词、无完整缓存等断言保留；旧“清理后保留 memo”断言改为当前删除语义。浏览器测试补真实身份、受控证据响应和明确导航等待，没有全局吞掉请求错误或添加自动重试。

## 本轮实际验证

新增 **40 项测试方法**：后端修复 19、图片缓存 10、benchmark 2、Chromium 9。

| 检查 | 根代理最终结果 |
| --- | --- |
| `py -3 -m unittest discover -s tests -v` | 981 项；936 通过，45 跳过，0 失败/错误。跳过包括单独执行的 21 项浏览器测试及未选用真实导出的 24 项测试。 |
| 完整 opt-in Chromium 套件 | 21/21 通过；真实浏览器、合成数据、模拟 LLM。 |
| `py -3 -m ruff check --no-cache .` | 通过。 |
| `py -3 -m ruff format --check .` | 113 个 Python 文件格式通过。 |
| `git diff --check` | 通过。 |
| 纯源码 wheel 构建及 `tools/verify_wheel.py` | 通过；101 个 wheel 文件，代码、前端资源、入口点和依赖元数据完整。95 个应用源码/资源文件与构建输入逐文件比对，0 差异。 |
| 修复前快照完整性 | SHA-256 清单核对通过；活动分支 HEAD 未变。 |

浏览器检查过程中保留了两次未通过的记录：第一次暴露测试响应缺身份、受控本地缓存没有对应证据接口以及多余导航中止请求的问题，按真实合同修正后定向测试通过；第二次遇到本机 `ERR_ADDRESS_IN_USE` / `ERR_NETWORK_CHANGED`，静态资源和上传跳转也失败。只读检查发现系统 TCP 动态端口范围为 1024–15000，TIME_WAIT 约 14972。最终等待连接释放后，使用客户端动态端口范围之外的独立 loopback 服务端口运行同一完整套件，21 项通过；未改变系统 TCP 配置、业务代码或请求错误断言。

修复前证据有明确行为断言失败：后端原源码上的 18 项修复测试中 16 项失败、2 项已有保护通过，另有终态清理竞态回归单独失败。根代理再次在分析原源码副本中确认两个 benchmark 回归失败，以及不同聊天同路径摘要错误复用。当前完整套件覆盖这些回归并通过。修复前失败不是当前版本的测试结果，原始全面审查报告中的缺陷复现也应视为历史证据。

验证材料位于 Git 忽略的 `_audit/repair-review-2026-10-11/`：

- [最终 unittest 日志](C:/Users/admin/.codex/worktrees/8883/qqchatlog/_audit/repair-review-2026-10-11/root-unittest-final.log)。
- [最终 Chromium 日志](C:/Users/admin/.codex/worktrees/8883/qqchatlog/_audit/repair-review-2026-10-11/root-browser-isolated-port.log)，逐项 trace、截图及诊断保存在 `browser-artifacts-reviewed/`。
- [wheel 构建与核对日志](C:/Users/admin/.codex/worktrees/8883/qqchatlog/_audit/repair-review-2026-10-11/root-wheel.log)。
- [修复前清单](C:/Users/admin/.codex/worktrees/8883/qqchatlog/_audit/repair-review-2026-10-11/before-manifest.json)、`backend-before-test/` 和 `analysis-before-test/` 的独立回归记录。

## 验证边界

以上是本地代码、合成运行和真实浏览器的验证结果，没有测量真实模型准确率或 benchmark 类别泄漏造成的实际分数增幅。未运行真实聊天数据、其他平台、安装后 wheel 启动、Windows 安装器或多进程并发验证。Issue #26 的真实 baseline/candidate 运行及人工评分不属于本轮九项修复的完成声明。
