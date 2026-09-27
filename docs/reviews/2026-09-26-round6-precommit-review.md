# 第六轮审查修复 · 提交前代码审查（2026-09-26）

> **处置结果（同日）**：P0-1/P0-2/P0-3/P0-4 与 P1 全部落地或显式记账，P2 择要一并修。
> 两处例外决定：**P0-4/P2-7** 采"记账不回调"（完整 uid 的正确性理由成立，文档四处同步、
> CHANGELOG 计费口径如实改写）；**P1-7（prompt 图片"张"数）保留旧口径并标注**——实测发现
> 这两个函数在指纹哈希集合里且 `*_LEGACY` 按原始源码哈希、函数体内连注释都不能加
> （在函数体内加说明注释会当场挤离 `GROUP_PROMPT_FINGERPRINT_LEGACY` 钉值，被 CI 指纹 pin
> 拦下——这道守卫是活的），统一必须先扩多代迁移链，方案留档在本报告。
> 修复后回归：578 passed / 1 skipped / 379 subtests，ruff check + format 全过；
> 逐条修复记录与回滚说明见 CHANGELOG「提交前复核」小节。

范围：`main@b892519` 之上的全部未提交改动（21 个修改文件 +721/−126，新增 `analyzer/purge_marks.py`、
`tests/test_review_round6.py`）。方法：人工逐行 + 三个并行审查子代理（parser 层 / 前端与文档 / 测试鉴别力）；
崩溃路径均以内存构造数据实测复现；测试鉴别力用运行时回退（monkeypatch 改回旧实现）逐条验证。
回退驱动草稿在 `temp/review/drafts/mutate_check.py`，项目文件审查期间未改动。

基线：567 passed / 1 skipped / 379 subtests，ruff check 与 format 全过。**测试全绿 ≠ 没问题**，
下述 4 条 P0 里只有 1 条能被现有测试网住。

---

## P0 —— 提交前必须处理

### P0-1 `p50` 中位数拿**未排序**列表计算（本轮"口径统一"自身引入）
- 位置：`analyzer/local_stats.py` L513 `"p50": round(_median(arr), 1)`；`_median`（L408）docstring
  明确要求"调用方须传入已排序的列表"。句长 median（L443→L449）与 `_percentile`（L505）都先排了序，
  **唯独回复间隔这一路漏了**——`self_times/other_times` 是按对话时间序 append 的。
- 复现（实测）：4 条间隔按时间序 [10, 100, 20, 30]，真中位数 25.0，实际返回 **60.0**。
  （逗号后**必须留空格**：逗号紧跟三位数字会被 `test_privacy_fingerprints.py` 的规则 D
  当成千分位大额统计量，把这条守卫弄红——本文件首次入库时就是这样被拦下的。）
- 影响：p50 是关系页（relationship.html L38/47）与报告页（report.html L55/56）的主展示指标，
  README 承诺"界面以中位数为主"；本轮 CHANGELOG 恰宣称"中位数只留一种算法"。
- 为什么 45 条用例 + 全仓测试都没抓到：`test_optimizations.py` L111-122 的 20 条间隔里唯一离群值
  落在前半部（中间位置恒为 5.0，排序与否同结果）；`test_review_round6.py` L967-981 刻意用 n=2
  （1s/99s→50.0），而偶数 n=2 取中间两数平均天然与顺序无关。**需要补一条 n≥3 且乱序的回归。**
- 修法：L513 改 `round(_median(sorted(arr)), 1)`。STATS_SCHEMA 无需再动（4→5 尚未发布，同批失效已覆盖）。

### P0-2 数字型 `sender.uid` 必崩——本轮新增回归（实测复现）
- 链路：`parser/qq_parser.py` L476 `sender_uid = sender.get("uid") or ""` 不做 str 强制
  （同轮改动里 md5/reply-id/at-uid 都包了 `str()`），L600 原样进 Message →
  `load_chat` L644 `multi_party_action` → L214 `is_placeholder_sender(uid, ...)` →
  `parser/group_identity.py` L52 `(uid or "").strip()` → `AttributeError: 'int' object has no attribute 'strip'`。
- 旧代码私聊轨只对 uid 做相等比较，**不会崩**；本轮把占位判定复用进主路径后，"以前能解析"的
  手工编辑/异版导出文件变成 400 + 一句 Python 内部错误——正是本轮 L420-424 注释宣称要消灭的症状。
- 修法：L476 强转 `str(...)`（或 `is_placeholder_sender` 入口防御，二者都做更稳）。

### P0-3 `test_logout_releases_reference` 是假绿用例（运行时回退坐实）
- 位置：`tests/test_review_round6.py` L289-302。终断言 L302
  `other_live_sessions(h, exclude_sid=sid)` == []，而"登出未清引用"留下的幽灵恰是 `sid` **自己**，
  `exclude_sid` 把它过滤掉了——把 `store.forget_live_chat` 改成 no-op（= 旧实现），用例照常通过。
- 真正该钉的症状零覆盖：幽灵引用会让**其它会话**换文件时 `other_live_sessions(old_hash, exclude_sid=别的sid)`
  看到幽灵、把级联清理挡最多 24 小时（`webapp/views.py` L210-216 的消费路径）。
- 该条也没有 docstring L19 承诺的反向验证注释。修法：终断言改 `exclude_sid=""`，或直接查
  `store._LIVE_CHAT_REFS`。
- 同型问题（P1 级）：L287 `other_live_sessions("hFresh", exclude_sid="sidB")` 不带 `now`，条目按真实
  时钟早已过期——**恒真**，删掉整个 exclude 过滤仍绿；exclude_sid 语义全套件实际无有效钉点。

### P0-4 完整 QQ 号进 prompt 与磁盘缓存：隐私承诺被单方面降级，且文档零同步
- 代码：`parser/group_identity.py` L160 重名成员显示名追加 `#完整uid`（旧为 `uid[:4]`），正确性理由成立
  （真实 QQ 号位数相同、前缀常同，两个"小明"会撞成同一个"小明#1000"在模型里合并成一个人）。
- 但这直接推翻了仓库自己列成**已验证安全性质**的口径：
  - `privacy-audit-report.md:39`："QQ uid 基本不外发……前 4 个字符（#uid4）"——未同步；
  - `docs/specs/2026-09-12-group-chat-analysis-design.md:159/:479`——仍写"前 4 位/#uid4"；
  - `analyzer/group_client.py:247` 注释"绝不显示原始 uid"——与 L248/L251 实际外发路径矛盾；
  - `tests/test_group_foundation.py:809` 断言文案"（#uid4）"陈旧。
- 计费连带（与"没人重新付费"总声称冲突）：`analyzer/month_cache.py` L97 月份键含 `user_content`，
  显示名变化 → **含重名成员的群**全部月份 miss、重跑整批重新付费；同理私聊里原先 `other_name` 被写成
  "系统消息"的文件，署名行修正后 prompt 内容也变、月份键变。`CHANGELOG.md:12-13`
  "缓存键与提示词指纹一个字都没动……没人会因为这次升级重新付费"需要按人群如实降级表述。
- 处理：这是**需要显式记账的决定**，不是改回代码——CHANGELOG 隐私段 + 审计报告 + 群聊设计文档三处同步，
  并写明代价（重名群升级后首轮重付）。

---

## P1 —— 强烈建议随本轮一起处理

1. **`_thinking` 标记只有写侧、读侧不固化**（`analyzer/month_cache.py` L132-142 读侧、L180+ 写侧）：
   升级前已存在的无标记月份文件被任何模式永久放行；"切换 LLM_THINKING 后不再串模式"只对升级后新写
   的文件成立。要么读侧命中时为老文件补标记（把未知口径钉成当前口径，需拍板），要么 CHANGELOG 降级表述。
2. **`store._write_cache`（维度缓存，L737）是四处写回点里唯一没装"写侧自查"的**：仅靠 `jobs._run_job`
   写前一次 `should_cancel()`，check→write 之间存在并发 purge 的复活窗口；本轮在 month_cache/vision/词频
   三处都改成写侧自查，唯独这里仍是调用点守卫。补 `if _is_recently_purged(chat_hash): return` 即可闭合。
3. **`purge_marks` 不去重、unmark 一次只撤一条**：双击上传触发两次 switching 清理可对同一哈希 mark 两份；
   重新上传时若统计缓存命中（views L435 不调 `start_stats_job`，也就没有第二次 unmark），残留标记会让该
   聊天的分析"永远停在取消状态"——正是本轮注释要防的症状。修法：mark 时跳过已在表里的哈希。
4. **`vision.digest` 被守卫挡住时连 `_memo_put` 一起跳过**（L357-363）：被清理那一轮内重复出现的图片批次
   会重复调 vision API（重复付费）。memo 与落盘解耦：内存照常记，只是不落盘。
5. **坏形状容错仍有六种漏网**（`parser/qq_parser.py`，前四种实测崩、一种静默、一种半崩）：
   a) `statistics` 为非空 list → L430 AttributeError（L701 的 isinstance 守卫在函数尾部，太晚）；
   b) `senders:[null]` 且缺 selfUid → L436-437 找回自己的循环没有 isinstance——本轮给 L453、L683 两个
      兄弟循环都加了，独漏此处；
   c) `messages` 是 dict/str → L467 逐 key 迭代、L470 全计 dropped：**不抛异常但整份文件被静默吞掉**
      （实测 msgs=0 而 total_count 照抄 statistics）——比崩更难发现，恰是本仓库最警惕的失真类型；
      messages 是数字则 TypeError；
   d) JSON `Infinity`（json.loads 默认接受）→ `_to_int` L64-69、`_parse_timestamp` L232-237 只捕
      TypeError/ValueError，`int(float('inf'))` 抛 OverflowError 未捕（实测两处都崩）。
6. **名字聚合口径分叉 + 注释作虚假承诺**：`qq_parser.py` L367-369 name_tally 用未 strip 的 sender_name，
   `group_identity.py` L125 用 strip 后的；L370-371 声称"同一条规则、两处不会打架"不成立（带尾空格昵称
   会让 other_name 与成员表分叉、tie-break 漂移）；判群门槛（L214）用聚合名、身份层逐条名判占位。
7. **"图片按张"只修了仪表盘，prompt 三处仍是旧口径**：`analyzer/dialog.py` L215/229、
   `analyzer/deepseek_client.py` L1113、`analyzer/group_client.py` L174/182 仍按"含图消息条数"标"张"，
   `local_stats.py` L631 已改 `image_count`——一条 3 图消息仪表盘说 3 张、喂给模型的统计头说 1 张，
   模型写的数字会和界面当面对不上（L628-630 注释痛斥的正是这件事）。
8. **`.env.example:124` 把 `LLM_GROUP_MEMBER_MIN_LINES` 描述成成员画像"入选门槛"**——实际是群聊对话
   成员感知抽样的**每人保底行数**（`group_client.py` L65/L191/L220；入选逻辑是 `select_ai_members`
   纯 Top-K，不存在"说够 N 行才分析"）。用户按文档调大它"过滤低频成员"会静默改变抽样 → 进指纹
   （`group_client.py` L572）→ **全部群聊缓存失效重新付费**。`CHANGELOG.md:88` 继承同一错误。
9. **导出件仍带 CSRF token**：`web/templates/base.html` L92-93 登出表单的 hidden `csrf_token` 不在
   `_report_assets.html` L147-179 的剥离范围（只摘 script/button/.no-print-class）；README L348
   "报告会剥掉 CSRF token"与 L153 注释"含 token 的脚本绝不外流"名不符实。可利用性低（需配会话 cookie），
   但属承诺失真。修法：clone 里一并摘 `input[name=csrf_token]`，TestFrontendGuards 补一条断言。
10. **CHANGELOG"23 条回归用例"数错了**：23 = 六个缓存生命周期类之和（2+5+6+4+2+4），漏掉了同节正文
    逐条列出的解析（8）、统计口径（9）、前端（5）共 22 条。应写 45 条并给构成。另 docstring L19
    "每条用例都注明反向验证"实为 26/45 条（脚本扫描坐实）。
11. **`_run_analyze_all` 的清理守卫路径未测**（jobs.py L335-340 写回不复活）：docstring 第 2 条把维度
    缓存说成全覆盖，实际只测了 `_run_job`；删掉该判断全部用例仍绿。

---

## P2 —— 顺手修或记账（择要）

- `group_charts.js` L82 旧头注释"X = 谁先说，Y = 谁接话"方向仍是**反的**（与本轮新注释、三处卡片标题、
  服务端约定四方矛盾）——正是这句错注释养成了本轮修的 tooltip bug，留着是新陷阱。
- `freezeCharts` 冻结底色取 `--bs-body-bg`（暗色 #151718）而图表容器在 `.card`（`--surface` #202425）：
  深色主题导出后每张图自带更深的矩形色块；深色导出再打印（打印 CSS 强制白底）会印出深底块。
- 烘焙图主题=导出者的、data-theme=收件人的，主题相反时页深图浅——固有代价，CHANGELOG"真正的静态报告"
  未如实交代；"快照在渲染完成后克隆"对**异步 AI 小节**不成立（打开即下载会把"尚未分析"烘焙进分享件）。
- `qq_parser.py` L295-298 注释称 media_bytes 是"所有媒体元素的合计"，实际 L576-581 只记**第一个**非图片
  媒体（实测：图 2048+文件 4096+图 1024+文件 9999 → 第二个文件的 9999 在两个口径里凭空消失）。双计修好，
  多文件漏账是存量，注释把它说成已闭环是增量失真。
- `chat_name` L458 未挡 null（`"name": null` 模板直印 "None"，L707-710 刚修过的同类）；L323
  dropped_messages 注释未随语义扩容更新；ranked 为空的兜底（L452-455）不排占位，"对方=系统消息"在
  零有效消息的退化文件里仍可能出现；L668-676 other_name 并列只靠稳定序（首次发言顺序），与
  `collect_participants` L127 的 `(-count, uid)` 不同口径；`participants()` L394 仍全量再走一遍且
  `statistical()` 无缓存——"单次遍历"的收益被吃掉一半（load 期实为 2 遍）。
- 测试卫生：月份迁移用例首断言恒真（:624）；:225/:266 未验上传返回码可空转；`assertIn("402", ...+str(job))`
  弱化；"two_party"命名与默认 auto 模式不符；宽限期依赖默认 env 不自钉；跨用例不清 JOBS/_CHAT_CACHE。
- `store._prune_live_refs_locked` docstring 说"先清过期，再按上限淘汰最旧的"，代码只清空 holders 的 dict
  （过期靠上限淘汰/读侧过滤兜底）——行为可接受，注释失实。
- `load_chat` 读文件用 `encoding="utf-8"`，带 BOM 的文件裸抛 JSONDecodeError（存量，非本轮；QQChatExporter
  不产 BOM，值得记账）。

## 已核实为好的部分（不必再查）

- 复活守卫 12 项（词频/维度缓存/manifest/月份文件/标记时机/.tmp 两方向/双会话互删/_thinking 读侧与
  切换重发等）经运行时回退实测**确有鉴别力**；配额中止在 `_run_job`/`_run_analyze_all` 双入口闭合，
  manifest 记账先于 raise，已付月份保住。
- counts 共享缓存三个消费点无就地修改；`_CHAT_CACHE` 按（路径,mtime,size,mode）实例键，无跨请求串数据。
- `.tmp` 命名全仓统一 `f"{path}.json.tmp"`，与 `_cache_belongs_to` 剥离顺序匹配；`is_marked("")` 恒 False。
- tooltip 方向修正与坐标轴/卡片标题/totals 账目四方一致；ECharts canvas 渲染器下 `getDataURL` 冻结路线
  成立；剥脚本后无残留引用（on* 全挂在被摘的 button 上）；group_emotion 判空覆盖三条 null 路径。
- README 本轮 9 行改动逐条与实现相符；`.env.example` 除 P1-8 外全部对得上代码，反向清点无遗漏；
  STATS 4→5、GROUP 2→3 与声称一致；`self_replies` 落地且 schema 钉对齐全。

## 建议处理顺序

1. 一行级修复：P0-1、P0-2、P1-2、P1-3、P1-4、P1-8（改两行文档）、P1-9（摘 input）、P2 注释类。
2. 测试：P0-3 改断言 + 补 p50 乱序回归、vision 守卫、_run_analyze_all 守卫、数字 uid、Infinity、
   messages-dict 四条用例；docstring 与 CHANGELOG 的用例数口径改写。
3. 需要拍板的决定：P0-4 隐私记账（写进 CHANGELOG 隐私段并同步 4 处文档）、P1-5 的六种坏形状补漏范围、
   P1-1 老月份文件的口径处置方向。
4. 全部落地后重跑 `pytest -q` + `ruff check/format`，按主题拆 commit；再谈 push main + tag 的发布收尾。
