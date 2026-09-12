# 群聊分析功能 · 稳定优先实施方案（v3）

> 版本：v3（针对 v2 方案的三处阻塞项与十一处缺口修订）
> 日期：2026-09-12
> 基线：`docs/specs/2026-06-14-qq-chat-analysis-design.md`、`docs/reviews/2026-07-29-code-review.md`（P2 #17）
> 状态：设计定稿，待实施批准
>
> 本方案的第一原则是**稳定**：私聊轨的全部既有行为（统计形状、AI 结果、缓存键、页面渲染、接口）
> 逐字节不变，且每条不变量都有测试钉住。唯一一处有意的行为变更是"多人导出不再被拒收，而是按
> 群聊口径分析"，它由一个三态开关控制，可一键回到升级前的行为。

---

## 0. 稳定性契约

### 0.1 唯一一处行为变更

| 模式 | `QQCHAT_GROUP_CHAT` | 含 >=3 位有实质发言者的导出文件 |
|---|---|---|
| 自动（默认） | `auto` | 识别为群聊，走群聊分析轨 |
| 关闭（回滚） | `off` | **与升级前完全一致**：拒收并给出原文案错误 |
| 两方归并 | `two_party` | 与原 `QQCHAT_ALLOW_MULTI_PARTY=1` 一致：所有人并入「对方」 |

- `QQCHAT_ALLOW_MULTI_PARTY=1` 继续有效，等价于 `QQCHAT_GROUP_CHAT=two_party`（兼容 README 与既有用例）。
- **回滚 = 一个环境变量**：`QQCHAT_GROUP_CHAT=off` 后行为与升级前逐字节一致（含错误文案）。
- **暗发布闸门**：`auto` 的实际放行还受 `parser.qq_parser.GROUP_TRACK_READY` 控制。
  M0-M2 期间该常量为 `False`，于是**默认配置下多人导出仍被拒收**——升级本版本
  不会让任何用户看到半成品的"群聊分析"。M3（群聊页面就绪）时才在一处翻为 `True`，
  并在同一改动里更新错误文案、README 与 `.env.example`。

### 0.2 不许破坏的不变量

| 编号 | 不变量 | 现状依据 | 钉住方式 |
|---|---|---|---|
| I1 | 私聊 `other_name` / `other_uid` 解析结果不变 | `tests/test_hardening.py:158,168` | 既有用例 + 冻结用例 |
| I2 | `ChatData` 构造签名向后兼容 | 13 处构造点、21 个 `other_*` kwarg | 只加尾部带默认值的字段 |
| I3 | `ChatData` 相等性与 `repr` 语义不变 | `tests/test_review_round4.py:110-121` | 新缓存字段 `repr=False, compare=False` |
| I4 | 私聊 `PROMPT_FINGERPRINT` 逐字节不变 | 它同时进维度缓存文件名与月份缓存键 | 新增指纹隔离用例 |
| I5 | 私聊 `compute_stats()` 键集合与 overview 字段不变 | `web/templates/*.html` 逐字段引用 | 新增形状冻结用例 |
| I6 | 私聊 7 个页面渲染不含任何群聊标记 | `tests/test_smoke.py` | 既有 smoke + 新增标记用例 |
| I7 | 升级后旧的私聊统计缓存仍能命中 | `stats_<hash>.json`（`_v == 4`，无 `mode` 字段） | `_load_stats` 兼容旧 payload |
| I8 | 私聊 AI 维度名与接口不变 | `/api/analyze/<dim>` 五维 | 既有 smoke 用例 |

### 0.3 回滚策略

- 配置级：`QQCHAT_GROUP_CHAT=off`。
- 代码级：群聊轨全部是新增文件 + 新增分支；`git revert` 不触碰数据。
- 无数据迁移：不重命名、不删除任何既有缓存文件，不写迁移脚本。

---

## 1. 关键决策

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | **两轨制**：私聊轨冻结，群聊轨全新 | 私聊路径除分支入口外零改动；避免动 11 个 `calc_*`、5 套 prompt、7 个模板带来的回归面 |
| D2 | **同 URL 换模板**：7 个 URL 不变，群聊时渲染 `group_*.html` | 导航单份、书签不失效、既有 smoke 用例不需要改路径 |
| D3 | `other_uid` / `other_name` **保持字段**，不改 property | 改 property 会让 13 处构造点、21 个 kwarg 直接 TypeError |
| D4 | 判定**三拆**：`participants` / `group_detection` / `ai_members` | 三者门槛不同，混成一个列表必然二选一地出错 |
| D5 | **缓存零失效**：私聊指纹与月份键保持现值，群聊用独立指纹 | 否则新增群聊 prompt 会一次性作废所有私聊维度缓存与月份缓存 |
| D6 | 群聊**本地优先**：互动矩阵等本地算好再喂模型 | 同时解决 token 成本与幻觉（也让模型能"看见"互动） |
| D7 | 交付切分为 M0-M4，每个里程碑独立可发布、可回滚 | 每步结束条件 = 全套 `pytest` 绿 + 私聊零回归 |

---

## 2. 模块与文件清单

### 2.1 不动清单（禁止改动）

- `analyzer/local_stats.py`：全部 13 个 `calc_*` 函数。
- `analyzer/deepseek_client.py`：`_build_dialog` / `_message_line` / `_fit_lines` / `_conversation_stats` /
  `_short_time` / `_analyze_periods` / `_analyze_person` / `analyze_*`。
  （这些函数的源码进私聊指纹，改一处 = 全量缓存作废，见 I4。）
- `analyzer/prompts.py`：5 个既有 `SYSTEM_PROMPT_*` 文本与 `_INPUT_NOTES` / `_OBSERVER_CREED`。
- 7 个私聊模板：除 `base.html` 的加法式条件外不改。
- `web/static/js/charts.js` / `analyze.js`：既有函数不改（只新增文件）。
- 既有测试断言：唯一例外见 §0.1（1 个用例改写）。

### 2.2 新增文件

| 文件 | 内容 |
|---|---|
| `parser/group_identity.py` | `Participant` 数据类、`collect_participants()`、`unique_display_names()`、`detect_group()` 包装 |
| `analyzer/group_stats.py` | 群聊本地统计（成员活跃度、互动矩阵、按成员小时分布、群总览扩展） |
| `analyzer/group_prompts.py` | 群聊专属 prompt 常量（独立模块，故不进私聊指纹） |
| `analyzer/group_client.py` | 群聊对话构建、群聊维度执行体（复用 `_analyze_periods`） |
| `web/templates/group_dashboard.html` | 群聊仪表盘（M3 只做这一个） |
| `web/static/js/group_charts.js` | 群聊图表（热力矩阵、力导向、堆叠面积、雷达） |
| `tests/test_group_detect.py` 等 4 个用例文件 | 见 §6 |
| `tests/fixtures/group_5p.json` | 5 人群聊 fixture |
| `tools/inspect_chat.py` | 导出文件体检（只读、默认脱敏、不打印正文） |

### 2.3 修改文件（全部加法式）

| 文件 | 改动 |
|---|---|
| `config.py` | 新增 `GROUP_MATRIX_MEMBERS`（`QQCHAT_GROUP_MATRIX_MEMBERS`）、`GROUP_PEAK_WINDOW_MINUTES`（`QQCHAT_GROUP_PEAK_WINDOW_MINUTES`）、`GROUP_AI_MAX_MEMBERS`（`QQCHAT_GROUP_AI_MAX_MEMBERS`）；群聊对话预算 `LLM_GROUP_MAX_DIALOG_CHARS` 与抽样保底 `LLM_GROUP_MEMBER_MIN_LINES` 在 `analyzer/group_client.py`（与私聊 `LLM_MAX_DIALOG_CHARS` 同风格） |
| `parser/qq_parser.py` | `Participant` 导入、`ChatData` 尾部新字段、`load_chat` 判定分支 |
| `webapp/store.py` | `compute_stats` 分支、`_load_stats` 兼容旧 payload、`_CHAT_CACHE` 键加 mode、群聊词频兜底形状 |
| `webapp/jobs.py` | 维度注册表按模式分组、`_run_analyze_all` 按模式取维度、`_done_str` 支持"人" |
| `webapp/api.py` | `api_analyze` / `api_analysis_result` 按 `session["chat_mode"]` 校验维度 |
| `webapp/views.py` | 7 个视图加模式分支、上传时写 `session["chat_mode"]`、日志区分群聊 |
| `web/templates/base.html` | 导航加法式条件（私聊输出不变） |
| `README.md` / `.env.example` / `docs/specs/*` | 文档同步 |

---

## 3. 三个阻塞项的设计

### 3.1 B1：提示词指纹拆分（缓存零失效）

**问题**：`deepseek_client._prompt_fingerprint()` 哈希 `analyzer.prompts` 里**所有** `SYSTEM_PROMPT_*`；
该值同时进入维度级缓存文件名（`webapp/store.py:384`）与月份级缓存键（`analyzer/deepseek_client.py:544-551`）。
新增群聊 prompt 会让**全部私聊缓存**失效，旧月份文件在 24 小时宽限期后被孤儿回收删除，
用户下次分析需全量重新付费。

**设计**：

1. 群聊 prompt 放**新模块** `analyzer/group_prompts.py`，常量前缀 `GROUP_SYSTEM_PROMPT_*`。
   `analyzer/prompts.py` 保持原样 → 私聊指纹公式与取值逐字节不变。
2. 新增 `group_prompt_fingerprint()`：哈希群聊 prompt + 群聊对话构建函数源码 +
   （`GROUP_MAX_DIALOG_CHARS`, `GROUP_AI_MAX_MEMBERS`, 各群聊维度 max_tokens, `SESSION_GAP_MS`, vision 常量）。
3. 月份缓存键：给 `_month_key` 增加**带默认值**的 `fingerprint` 参数。
   私聊调用不传 → 键与现值完全相同；群聊传 `GROUP_PROMPT_FINGERPRINT`。
4. 群聊维度缓存文件名：`group_<dim>_<chat_hash>_<model>_<GROUP_FP><suffix>.json`。

**验收**（`tests/test_group_cache_isolation.py`）：

- 往 `analyzer.prompts` 注入 `SYSTEM_PROMPT_GROUP_FAKE` 前后，私聊 `PROMPT_FINGERPRINT` 相等（I4）。
- 群聊指纹随群聊 prompt 内容变化而变化。
- 对固定输入，私聊月份键与升级前公式的取值一致（pin 值）。

### 3.2 B2：数据层（只加不改）

```python
@dataclass
class Participant:
    """群聊参与者身份。只承载身份，不承载统计快照——条数一律以统计层口径为准，
    避免"解析期算一遍、统计层再算一遍"导致两处数字对不上。"""

    uid: str
    name: str          # 唯一显示名（重名时已追加 uid 短后缀）
    raw_name: str = "" # 导出文件里的原始显示名（用于界面如实展示）
    is_self: bool = False
```

`ChatData` **尾部**新增（全部有默认值，既有 13 处构造点与相等性断言不受影响）：

```python
    is_group_chat: bool = False       # 默认 False == 私聊 == 既有语义
    mode: str = "private"             # private | group | two_party
    _participants_cache: Optional[list] = field(default=None, repr=False, compare=False)
```

- `participants()` 方法走实例级缓存，与 `months()` 同款（`tests/test_review_round4.py:110-121` 的模式）。
- **显示名唯一化**：同名成员追加 `#<uid 前 4 位>`；群聊轨（prompt、统计表、模板）一律用唯一名，
  避免群里两个「小明」在模型与界面里合并成一个人。

### 3.3 B3：判定三拆

| 概念 | 定义 | 用途 |
|---|---|---|
| `participants` | 所有 `sender_uid` 非空且有统计口径消息的发送者，**不设门槛** | 成员列表、本地统计、AI Top-K 的来源 |
| `group_detection` | 复用既有 `_multi_party_offenders`（门槛：`>=3` 条 且 `>=0.5%`，`parser/qq_parser.py:103-104`） | 决定 `is_group_chat` |
| `ai_members` | 按统计条数降序 Top-K，**始终包含自己** | 成员画像维度的调用范围 |

**为什么门槛保持不动**：现有门槛的存在理由正是"真实私聊导出里混进了上百条占位 sender，
其中 1 条 type_23 连 `system` 标记都没有"（`tests/test_review_fixes.py:158-181`）。
保持门槛不变 → 占位 sender 与 2 条零散第三方仍判私聊，两个既有用例原样通过。

**残留失真的如实告知**（不改行为，只提示）：私聊模式下若存在未达门槛的第三方发送者，
在仪表盘显示一条提示"检测到 N 位零散发言者，其消息已并入对方"，让用户知情。

---

## 4. 里程碑

### M0 地基（0.5-1 天，可独立合并）—— **已完成（2026-09-12）**

| 交付项 | 落地位置 | 验收证据 |
|---|---|---|
| 三态开关 + 旧变量别名 + 暗发布闸门 | `parser/qq_parser.py`：`group_chat_mode()` / `_allow_multi_party()` / `GROUP_TRACK_READY` / `multi_party_action()` | 判定矩阵 9 个用例 |
| 参与者身份（不外泄统计口径） | `parser/group_identity.py`（新）：`Participant` / `collect_participants()` / `unique_display_names()` | 数据层 8 个用例 |
| 数据层只加不改 | `ChatData` 尾部 `is_group_chat` / `mode` / `_participants_cache` + `participants()` | 兼容性 4 个用例 |
| 提示词指纹结构性隔离 | `analyzer/deepseek_client.py`：`_PRIVATE_PROMPT_NAMES`（封闭名单，调用时取值） | 指纹 5 个用例 |
| 月份键支持群聊独立指纹 | `analyzer/deepseek_client.py`：`_month_key(..., fingerprint=None)` | 同上 |
| 5 人群聊 fixture（126 条 / 跨 3 个月 / 同名成员 / 空 uid / 低频成员） | `tests/fixtures/group_5p.json`（新） | fixture 形态 3 个用例 |
| 私聊统计形状冻结 | `tests/test_group_foundation.py` | 形状冻结用例 |
| HTTP 级行为不变 | 上传路径 | 私聊上传 302 / 群聊上传 400 + 会话不受影响 |

**零回归实测**（改动前后对比）：

- `python -m pytest -q`：306 passed / 80 subtests → **338 passed / 91 subtests**（新增 32 个用例，既有用例 0 改动、0 失败）。
- `PROMPT_FINGERPRINT`：`b6c5074dc226` → `b6c5074dc226`（逐字节一致，私聊维度缓存与月份缓存全部继续命中）。
- 私聊月份键样本：`9a537cda027c9b559865` → `9a537cda027c9b559865`。
- `ruff check` / `ruff format --check`：全通过。

**M0 明确不做的事**：不改变任何用户可见行为（默认仍是拒收）、不新增群聊页面、
不动 `analyzer/local_stats.py` 与私聊模板。

### M1 数据层 + 本地统计（6-8 天）—— **已完成（2026-09-12）**

| 交付项 | 落地位置 | 验收证据 |
|---|---|---|
| 群聊统计全部函数 | `analyzer/group_stats.py`（新）：成员活跃度 / 互动矩阵 / 按成员小时 / 群总览 / 群里程碑 / `compute_group_stats()` | `tests/test_group_stats.py` 24 个用例 |
| 判定分支接入应用层 | `webapp/views.py`：`session["chat_mode"]`、群聊上传日志、统计缓存按口径取用 | HTTP 用例 + 上传日志 |
| 统计分支与缓存口径隔离 | `webapp/store.py`：`compute_stats` 按 mode 分支、`stats_mode_of()`、`_load_stats(expect_mode)`、`_save_stats(mode)`、`_CHAT_CACHE` 键加模式 | 缓存隔离 5 个用例 |
| 两套版本号（私聊 v4 不动） | `STATS_SCHEMA_VERSION=4` / `GROUP_STATS_SCHEMA_VERSION=1` + `mode` 判别字段 | 旧 payload 兼容用例 |
| 阈值可配且可测 | `config.py`：`GROUP_MATRIX_MEMBERS`（默认 30）、`GROUP_PEAK_WINDOW_MINUTES`（默认 10） | 截断与峰值用例 |
| 两方归并的如实告知 | `web/templates/dashboard.html` 加**条件块**（`chat_mode == 'two_party'` 才输出） | 私聊 8 页逐字节对照 |

**关键口径决定**

- 接话判定**只用** `is_session_start`（`SESSION_GAP_MINUTES=30`），不引入第二个间隔阈值；
- 成员条数之和 + 未知条数 == 统计口径总条数（对账用例）；无 `sender_uid` 的消息进"未知"桶；
- 群聊**不提供** `response_time`（"对方→我"的口径在群里不成立），`exchange_rounds` 置 `None`（连珠炮会放大，语义已变）；
- `directed[i][j]` 定义为"j 接了 i 的话"；`replies_to` = 列和（我接别人）、`replied_by` = 行和（别人接我）。

**实测数据**

- 50 人 × 2.5 万条：`compute_group_stats` **0.202s**（预算 2s），矩阵截断到 30 人并如实记账
  `dropped_replies=10499`；
- 全套 `pytest`：338 → **362 passed**（新增 24 个用例，既有用例 0 改动、0 失败）；
- `PROMPT_FINGERPRINT` 与私聊月份键仍与升级前逐字节一致；
- 私聊 8 个页面渲染结果与改动前**逐字节一致**（仅 CSRF token 因会话而异）。

### M1.5 真实导出文件验证（2026-09-12）—— **已完成**

用一份真实群聊导出（`group_<群号>_<导出时间戳>.json`，数 MB）
逐项核对了解析、判定与统计。文件画像：**数千条消息 / 数十位发言者 / 跨数月**，
元素以 image / face 最多，其次 reply / at，另有 file / forward / json（各数百条量级）。

**四条与解析器假设不同的真实格式**（已全部处理并固化为 `tests/test_group_real_format.py`）：

| 发现 | 影响 | 处理 |
|---|---|---|
| 消息 `type` 是**语义名**（text/reply/system/file/forward/json/video/type_17/type_31），旧格式的 `type_11`/`type_23` 一次都不匹配 | `SKIP_MSG_TYPES` 在新格式下形同虚设；系统消息靠 `system` 标记兜住（实测 206/206 都有） | 把 `"system"` 加入 `SKIP_MSG_TYPES`（旧格式不会出现该类型，零回归）；`forward`/`json` **故意不跳过**——新版给的是结构化短标签（标题+条数、卡片摘要），由 MEDIA_KINDS 承载且有长度上限，灌不进 prompt |
| `chatInfo.type = "group"`：导出器自报会话类型 | 比"数发言者"可靠：3 人小群里两位潜水时按门槛会误判成私聊，把其他人静默并进「对方」 | 新增判定输入（`ChatData.chat_type` + `multi_party_action` 第二条分支）：**严格加法**，只在"按老口径会判成私聊"的缝隙里生效；`off`/`two_party`/未就绪时行为与升级前逐字节一致 |
| `reply` 元素带 `referencedMessageId`（绝大多数带引用 id；少数为 null 表示原消息已删除） | 这是**精确**的"谁回复了谁"，比"相邻两条换人"的推断硬得多 | `Message.reply_to_id/reply_to_uid`（全量解析后回查 id→发言人）+ 互动矩阵新增 `explicit_*` 矩阵，与推断的 `directed` **分开统计** |
| `at` 元素带 uid（atType=1 是 @全体成员） | 原方案把「@ 关系网络」列为 Non-goal，理由是"导出器未提供稳定的 @ 结构字段"——**该假设被真实文件推翻** | `Message.mentions/mentions_all` + 互动矩阵新增 `mention_*`；@全体成员单独计数，绝不摊到成员头上 |

**占位 sender 的真实数据**：上百条来自 uid 形如「未知…」的占位 sender，**全部带 system 标记**
（因此不会变成成员）。但"标记不齐全是实测踩过的坑"，因此仍加了 `is_placeholder_sender`
（UID 前缀 + 名字白名单）作为第二道防线：占位消息一律进"未知"桶，成员条数之和 + 未知条数
== 总条数 始终成立。

**账目必须闭合**（真实文件暴露过一次静默丢弃）：回复与 @ 的每一项都单列，任何未被计入的
互动都要有去处：

- `reply_total = reply_located + reply_no_target`（456 = 450 + 6，6 条是引用已删除）
- `reply_located = reply_resolved + reply_unresolved`
- `reply_resolved = 进矩阵的 + reply_unknown + reply_outside`
- `mention_total = 进矩阵的 + mention_unknown + mention_outside`（434 = 378 + 0 + 56）

**实测结果**：解析 139 ms、群聊统计 32 ms；对账一致（3433 = 3433）；同时在聊高峰 13 人；
互动矩阵按 30 人截断并如实记账（236 次互动未进矩阵）。

**新增工具**：`tools/inspect_chat.py` —— 导出文件体检（只读、不联网、不打印正文、昵称默认脱敏），
回答"格式是否漂移 / 会被判成什么 / 统计对不对账 / 有无可疑数据"四个问题，可直接用于任何导出文件。

**M2/M3 范围修订（因为 @ 与精确回复可用）**：

- `group_dynamics` 的输入从"相邻推断的互动矩阵"升级为**三张矩阵**（推断接话 / 精确回复 / @点名），
  并在 prompt 里明确区分三者的可信度——这正是"本地算好再喂模型"的最佳素材；
- 成员画像的群上下文头部改用**精确回复 + @点名**（谁最常回复他、他 @ 过谁、被谁 @），
  比推断值更硬；
- M3 的关系图区分实线（精确回复/@）与虚线（推断接话），避免把推断画得和事实一样重。

**M3 关系图的落地补充（同日显示重做）**：实践下来，只把两层分成实线/虚线**并不够**——两者都是
直线时，同一对成员的回复边与接话边会完全重叠，看起来仍是一条。现在改为：回复/@ 走实线并
**向上弯**、接话走虚线并**向下弯**；推断边默认只留最强的一批，图例旁如实报出"画了多少 / 一共
多少"；力导向只负责算坐标，算完换 `layout:'none'` 把坐标钉死。最后一条是被两个实测坑逼出来的：
力导向不收边（30 个节点能撑到 `y ∈ [-80, 565]`，画布才 460 高，两端节点与昵称被裁掉），
且每次 `setOption` 都会重跑一遍力导向（没有已保存坐标时用 `Math.random` 撒初始点），
所以"先画一遍、再补一个 `zoom` 去适配"必然落空。另：`layout:'none'` 会按数据包围盒的宽高比
等比装进"画布四周各缩进 10%"的框，可读性上能控制的只有包围盒的比例。行为变更与回滚见 `CHANGELOG.md`。

### M2 AI 层（6-8 天）—— **已完成（2026-09-12）**

| 交付项 | 落地位置 | 验收证据 |
|---|---|---|
| 群聊提示词（3 个群级维度 + 成员画像） | `analyzer/group_prompts.py`（新，独立模块） | 指纹隔离 4 个用例 |
| 群聊对话构建 + 成员感知抽样 + 群上下文头部 | `analyzer/group_client.py`（新） | 抽样/上下文用例 8 个 |
| 群聊指纹与按维度取指纹 | `group_client.group_prompt_fingerprint()` + `deepseek_client.fingerprint_for_dimension()` | 私聊指纹与月份键逐字节不变 |
| 月份缓存按维度取指纹 | `deepseek_client._analyze_periods(..., fingerprint=None)`（**加法式**：默认私聊，键不变） | 真实文件跑通 4 个月 |
| 维度级缓存文件名按维度取指纹 | `webapp/store._cache_path` → `fingerprint_for_dimension(dim)` | 私聊文件名不变 |
| 任务系统按模式取维度 | `webapp/jobs.py`：`dimensions_for_mode()` / `analyze_func_for()` / `dimension_unit()` | 注册表 3 个用例 |
| API 跨模式拒绝 | `webapp/api.py`：`_dimension_guard()` | HTTP 护栏 4 个用例 |
| 输出预算 | `deepseek_client._DEFAULT_MAX_TOKENS` 增加 4 个群聊维度（不进私聊指纹常量元组） | 预算用例 |

**真实文件实测（mock 掉 API，零网络）**：4 个月 × 3 个群级维度 + 10 位成员画像 = **22 次调用**；
每月 prompt 6.7k~37.8k 字符；**当月说过话的成员 100% 出现在 prompt 里**（覆盖率检查为 0 缺失）；
真实文件远低于对话预算，因此不触发抽样（抽样逻辑由单测用 1200 字符预算单独验证）。

**成本实测（一次意外但真实的对照）**：用真实 Key 跑了 12 次群级月度调用，
`deepseek-flash` 上共 **数十万 tokens ≈ $0.95**（项目自带
费用估算）。由此推断一次完整群聊全量四维约 **40 万 tokens、$1.5 上下**（成员画像开思考模式时
输出是主要成本）。→ M3 必须在发起前把"预计调用次数与成员数"显示给用户（本里程碑未做）。

**成员画像的群上下文**（真实数据示例，脱敏）：`发言 120 条（占全群 8%）、活跃 30 天｜
被精确回复 9 次、主动回复 12 次｜被 @ 5 次、主动 @ 7 次｜被接话 22 次、接话 18 次｜
主要互动对象：A（24 次）、B（8 次）` —— 事实（回复/@）与推断（接话）分列，模型据此判断
"捧哏王"还是"话题主导者"。
**四个维度（实际实现）**

| 维度 | 粒度 | 说明 |
|---|---|---|
| `group_dynamics` | 逐月 | 群氛围、核心成员与角色、小圈子（带数字证据）、权力结构、潜水比例、冲突时刻、群节奏、我的角色 |
| `group_topics` | 逐月 | 话题分布 + **每个话题是谁在聊**（key_members），权重归一化到 1.0 |
| `group_emotion` | 逐月 | 群整体情绪 + 强度 + 成员情绪对比（事实证据）+ 情绪走势与转折点 |
| `member_profiles` | 每人 | Top-K（自己必定入选）各一次调用；沿用私聊锐评的 JSON 契约 + `group_specific`（群内角色/互动模式/出现方式） |

**两处刻意取代（v2 计划 → 实际实现）**

- `calc_role_classification`（本地规则角色分类）**不实现**：本地启发式与 AI 的
  `group_dynamics.core_members[].role` / `member_profiles.group_specific.group_role` 会给出
  两套分类学，同一批人两个页面两个说法。角色判断统一交给 AI（它有原句证据），本地只提供数字。
- `calc_topic_participation` / `calc_response_network`（v2 计划）**被更精确的信号取代**：
  真实导出文件的 `reply`与 `at`直接给出"谁对谁做了什么"，
  比"按对话段推断参与度"硬得多，因此 M1 落成三张矩阵（推断接话 / 精确回复 / @点名）。

**三个群级维度共用的输入纪律**（写进 `_GROUP_OBSERVER_CREED`）：
"精确回复/@点名是事实、相邻接话是推断"必须分开引用；发言量高度不均不代表关系亲疏；
@全体成员是广播，不能用来论证任何两人的亲疏。

**验收**：22 次 mock 调用跑通四个维度的 prompt 构建与 JSON 契约（零网络，用
`_get_client` 抛异常的硬护栏证明）；配额中止保留部分结果、取消不再发起新调用；
既有 5 个私聊维度与缓存键一个字都没变。

### M3 前端 + 集成（5-7 天）—— **已完成（2026-09-12）**

| 交付项 | 落地位置 | 验收证据 |
|---|---|---|
| 六个群聊页面 + 群聊报告 | `web/templates/group_*.html`（新，含 `group_report.html`） | `tests/test_group_web.py` 15 个用例 |
| 群聊图表与结果渲染 | `web/static/js/group_charts.js`（新，复用 charts.js 的 esc/themeTokens/mountChart） | 页面渲染 + JS 语法检查 |
| 视图按模式分发 | `webapp/views.py`：`_is_group()` / `_group_context()` / `_group_ai_plan()` | 群/私聊两套页面用例 |
| 导航按模式切换 | `web/templates/base.html`（条件块，私聊输出逐字节不变） | 私聊 8 页逐字节对照 8/8 |
| 报告导出机制抽成 partial | `_report_style.html` / `_report_assets.html`，私聊与群聊报告共用 | 用 HEAD 原始模板做 A/B：**渲染逐字节一致** |
| 成本预提示 | `_group_ai_plan()`：月份 × 3 + 成员数 | 用例断言"预计调用 14 次"（5 人 fixture） |
| 上传页清理两个轨道的会话缓存 | `web/templates/index.html`（唯一有意变更的私聊页面文本） | 用例断言 9 个维度键都被清理 |

**闸门已打开**：`GROUP_TRACK_READY = True`。默认 `QQCHAT_GROUP_CHAT=auto` 下多人导出**按群聊分析**；
`off` 完全回到升级前的拒收（错误文案改为指向新开关）；`two_party` 保留旧的两分类归并。
README 与 `.env.example` 同批更新（新增 4 个群聊环境变量、"已知限制"改写、故障排查改写）。

**真实文件端到端（默认配置、零出网）**：上传 → 302 + `chat_mode=group` → 群聊统计落盘 →
八个页面全 200 → 成本预提示显示 22 次调用 → `group_dynamics` 任务 done（4 个月）→
群报告含全部 vendor SRI 常量与五个群章节。

**既有测试的改动（全部是"工件搬家"或"计划内的行为变更"，断言语义未削弱）**：

1. `tests/test_sri.py` / `tools/verify_vendor_sri.py`：SRI 表的校验对象指向 `_report_assets.html`；
2. `tests/test_review_fixes.py` 中 6 处读取 report.html 文本的断言 → 指向 partial（其中一条同时读
   partial 与 report.html：导出机制在 partial、"渲染期兜底 asList"仍在模板里）；
3. `test_three_participant_upload_is_rejected` → 拆成"默认识别为群聊" + "`off` 时仍拒收"两条。

### M4 收尾（2-3 天）—— **已完成（2026-09-12）**

| 交付项 | 落地位置 |
|---|---|
| 行为变更记录（含唯一变更与三种回滚方式、缓存/隐私、费用） | `CHANGELOG.md`（新） |
| README：顶部特性、功能表新增"群聊分析"整节、AI 维度表、项目结构、环境变量表、故障排查、已知限制 | `README.md` |
| `.env.example`：群聊开关与四个规模/成本变量 | `.env.example` |
| 旧设计文档的"群聊记录支持"复选框 | `docs/specs/2026-06-14-qq-chat-analysis-design.md` |

**未能本地执行的一项**：`tools/verify_wheel.py` 需要先 `python -m build` 出 wheel，而本机 Python 3.14
环境没有 setuptools/build，无法构建。源码侧的覆盖范围由 `tests/test_packaging.py` 逐个文件核对
（19 个用例全绿），wheel 级核对留给 CI 的 package job。

---

## 附：真实数据 + 真实 AI 的完整验收（2026-09-12）

在真实群聊导出（数千条 / 数十位成员 / 跨数月）上跑完整链路（**真实 API 调用**）：

| 维度 | 调用 | 结果 |
|---|---|---|
| group_dynamics | 4 | 4 个月全有；核心成员角色附原文证据、小圈子有数字支撑 |
| group_topics | 4 | 4 个月全有；话题 + "谁在聊" + 月度片名 |
| group_emotion | 4（含补跑 1） | 4 个月全有；情绪走势与转折点具体到日期 |
| member_profiles | 12（含补跑 2） | 10 位成员；含群内角色、反例自查、脱敏后的锐评 |

**真实数据暴露并修掉的三处**（详见 CHANGELOG）：裸控制字符的 JSON、成员画像缺按人缓存、
对话头未标注"我"。**契约审计**：模型真实输出的字段与前端渲染器读取的字段逐项对齐，
仅 1 位成员少两个可选字段（渲染器跳过该行，不报错）。

**费用**（项目自带估算，可在用量页核对）：

| 项 | 调用 | 费用 |
|---|---|---|
| 最初 12 次（mock 失误造成的意外消耗） | 12 | $0.9484 |
| 授权后的真实分析（含补跑失败项、提示词修复后重跑） | 47 | $2.8937 |
| **群聊维度合计** | **59** | **$3.8421** |

结果已落在 `ai_cache/`：在浏览器里重新上传同一份导出（内容寻址，哈希一致）即可直接命中，
不再付费；`QQCHAT_GROUP_CHAT=off` 不影响私聊的任何缓存。

## 附：整个里程碑的最终状态

| 里程碑 | 状态 | 用例数 |
|---|---|---|
| M0 地基 | 完成 | 32 |
| M1 本地统计 | 完成 | 24 |
| M1.5 真实验证 | 完成 | 20 |
| M2 AI 层 | 完成 | 30 |
| M3 前端 + 集成 | 完成 | 15 |
| M4 收尾 | 完成 | — |
| 合计 | **426 passed / 160 subtests** | 既有用例 0 失败 |

**不变量**（全程每步复核）：私聊提示词指纹 `b6c5074dc226` 与月份键 `9a537cda027c9b559865`
逐字节未变；私聊 8 个页面渲染与 M3 前逐字节一致（唯一有意变更：上传页的会话缓存清理列表）。

### M4 收尾（2-3 天）[历史计划]

1. `base.html` 导航加法式条件（`is_group_chat` 来自 session，零解析成本）。
2. `group_dashboard.html`：成员条数柱状图 + 互动热力矩阵 + 24 小时堆叠面积 + 群里程碑。
3. `group_charts.js`：热力矩阵、力导向关系图、堆叠面积、角色雷达；
   沿用 `charts.js` 的 `themeTokens()` / `mountChart()` 约定，不改既有函数。
4. 七个视图的模式分支：群聊时渲染群模板；`report` 走 `report.html` 内的条件块，
   **保住 CDN/SRI 导出逻辑与 `tests/test_sri.py`**。
5. `dashboard.html` 的维度按钮与 `DIM_LABEL`、`index.html` 的维度清理列表改为按模式生成（私聊输出不变）。
6. 形态稳定后再拆 `group_relations` / `group_activity` / `group_topics` / `group_profiles`（可选，M4）。

**验收**：私聊 7 页无群聊标记（I6）；群聊 7 页 200、无 Traceback、无 emoji；既有 smoke 用例绿。

### M4 收尾（2-3 天）

- `README.md` 群聊章节与开关表、"已知限制"段改写；`.env.example` 群聊段；`docs/specs` 复选框。
- 成本提示文案与用量维度标签。
- 50 人群聊端到端 + 私聊全量回归 + CHANGELOG（明确写出唯一行为变更与回滚开关）。

---

## 5. 缓存方案一览

| 缓存 | 键 | 本次改动 | 影响面 |
|---|---|---|---|
| 私聊 AI 维度缓存 | `dim_chat_model_FP[_think].json` | 不变 | 0（I4 钉住） |
| 群聊 AI 维度缓存 | `group_dim_chat_model_GFP[_think].json` | 新增 | 无 |
| 月份缓存 | `month_<sha20(model, FP, sysprompt, content)>` | `_month_key` 加默认参数 | 私聊键不变（I4） |
| 私聊统计缓存 | `stats_<hash>.json` | `_load_stats` 兼容无 `mode` 的旧 payload | 0（I7） |
| 群聊统计缓存 | `stats_<hash>.json`（含 `mode: "group"` 与独立版本号） | 新增 | 无 |
| 进程内 `ChatData` | `(path, mtime, size, mode)` | 键加 `mode` | 多一份缓存对象 |
| 月份 manifest | `manifest_<hash>.json` | 记账键不同，逻辑不变 | 无 |

---

## 6. 测试与验收矩阵

| 组 | 用例要点 | 钉住的不变量 |
|---|---|---|
| 判定 | 5 人 -> 群聊；占位 sender -> 私聊；2 条零散 -> 私聊；`off` -> 拒收；`two_party`/旧别名 -> 归并 | §0.1、I1 |
| 指纹 | 注入伪群聊 prompt 后私聊指纹不变；群聊指纹随群 prompt 变；月份键 pin 值 | I4 |
| 数据层 | 构造签名兼容；相等性与 repr；`participants()` 缓存；同名成员唯一名 | I2、I3 |
| 统计 | 成员条数求和 == 总数（含空 uid）；矩阵 30 分钟口径；Top-K 截断；50 人性能 | — |
| 私聊冻结 | 统计形状（键集合 pin）；7 页渲染无群聊标记；旧统计缓存仍命中 | I5、I6、I7 |
| 群聊端到端 | 上传 -> 7 页 200 -> mock 分析 -> 报告导出（含 SRI 常量） | — |
| 模式切换 | 同一文件在 auto/off/two_party 下互不串味（不命中错误缓存） | I7 |

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 群聊 prompt 引入导致私聊缓存全量失效 | 用户重新付费分析 | §3.1 指纹拆分 + I4 用例钉死 |
| 判定口径变化把私聊误判成群聊 | 全盘失真 | 复用既有门槛（`>=3` 条且 `>=0.5%`）+ 既有占位 sender 用例 |
| 未达门槛的第三方被静默并入对方 | 统计轻微失真 | 仪表盘如实提示（不改行为） |
| 群聊页面/图表把私聊渲染带偏 | 私聊回归 | 两轨制 + I5/I6 用例 |
| 同名成员合并 | 模型与界面认错人 | 唯一显示名（`#uid4`） |
| 低频成员被等间隔抽样整段丢掉 | `lurker_ratio` 结论反向 | 成员感知抽样（Top-K 每人保底 20 条） |
| 大群统计与矩阵体积 | 缓存文件与前端渲染变慢 | Top-30 截断 + 后台线程统计（既有模式） |
| 群聊 token 成本是私聊的 10-20 倍 | 用户账单意外 | 成员数上限 + 发起前显示预计调用次数 + 用量页 |
| `report.html` 的 CDN/SRI 导出逻辑被复制成两份 | 分享的 HTML 静默丢样式 | 复用 `report.html` 条件块，不新建报告模板 |
| 成员画像进度显示"N/M 月" | 界面文案错误 | `_done_str` 增加单位参数（私聊默认不变） |

---

## 8. 工时

| 里程碑 | 人日 |
|---|---|
| M0 地基 | 0.5-1 |
| M1 数据层 + 本地统计 | 6-8 |
| M2 AI 层 | 6-8 |
| M3 前端 + 集成 | 5-7 |
| M4 收尾 | 2-3 |
| **合计** | **20-27** |

（v2 方案估 14-18 天，上调原因是稳定性工程：指纹隔离、判定拆分、缓存兼容、
群上下文输入、私聊冻结用例与文档面。）

---

## 9. Non-goals（本期不做）

- 不做"统一 N 人管线"：私聊轨保持独立，避免改动 11 个 `calc_*` 与 5 套 prompt。
- 不做本地社区发现（子群检测交给 AI，本地只给互动矩阵数字）。
- ~~不做 @ 关系网络（导出器未提供稳定的 @ 结构字段）~~ —— **2026-09-12 修订**：真实导出文件里
  `at` 元素带 uid（含 @全体成员标记），该假设不成立，@ 关系网络已纳入 M1 数据层与 M2/M3
  的分析与可视化范围。
- 不做跨群对比、不做 PDF 导出、不做群成员跨文件身份合并。
- 不做回复引用的 `senderUin → uid` 兜底映射：真实文件里两套命名空间不重合（uin 是 QQ 号、
  uid 是 `u_…` 内部标识），6 条引用已删除的回复（1.3%）如实计入 `reply_no_target`，
  不为了 1.3% 引入第二套身份映射。
