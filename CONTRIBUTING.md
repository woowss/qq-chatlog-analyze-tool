# 参与开发

这个项目是**单机自用**的本地工具，欢迎 issue 与 PR——但请先读下面几条，能省掉来回。

## 一条红线：不要提交任何真实聊天数据

仓库里只允许出现**编造的测试数据**（见 `tests/fixtures/group_5p.json`：群名"摸鱼群"、
成员"小明/小红/阿强"）。以下内容不要出现在提交、截图、日志粘贴或 issue/PR 正文里：

- 真实导出的 JSON，以及 `uploads/`、`ai_cache/`、`stats_cache/`、`logs/`、`face_cache/` 里的任何文件；
- 真实群名、群号、昵称、`u_…` 形式的 uid、导出文件名；
- **由真实语料算出来的统计量**：总条数、成员数、对账数字、媒体体积、API 花费等。它们看起来"只是个数字"，
  但足以指纹化某一份具体导出——项目为此专门做过一轮脱敏，请保持这个口径；
- `.env`、`.secret_key`、任何 Key。

`.gitignore` 覆盖了这些**路径**，但它拦不住你手写进文档、注释、提交信息里的**数字**。改文案时请自查这一点。

## 环境

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install ruff==0.16.6                          # 与 CI 固定同一版本
```

Python 3.10–3.14 都在 CI 矩阵里，3.10 是最低线（唯一的分支差异是标准库有没有 `tomllib`）。

## 提交前请全跑一遍

```bash
python -m unittest discover -s tests -v     # 与 CI 完全一致的跑法
python -m ruff check .
python -m ruff format --check .
```

- 测试进程自带隔离与网络护栏（`tests/_bootstrap.py`）：数据目录指向进程独占的临时目录；
  **没有配置真实 API Key 时禁止一切真实 LLM 调用**。确实要走真实网络，必须显式设置
  `QQCHAT_TESTS_ALLOW_REAL_LLM=1` 并确认这是有意为之（否则可能真的花钱）。
- 新增或修改行为都要带回归用例；**隐私相关改动**（日志脱敏、缓存保留期、鉴权）请带护栏用例
  （参考 `tests/test_privacy_guards.py`），并在 PR 里说明"把修复还原后哪条用例会变红"。
- 时序相关用例不要赌线程调度：上传后的统计是异步落盘的，需要它时用 `tests/_stats.py` 的
  `ensure_stats`，而不是裸 `wait_for_stats`（原因写在那个文件里）。

## 代码风格

- 注释写**为什么**，不写"做了什么"——这个仓库的既有风格是把踩过的坑留在注释里，请延续。
- 面向用户的文案（README / CHANGELOG / 页面）一律中文，且**隐私表述必须与实现一致**：
  "只在本地处理"这种话，只有在真的不外发时才允许写。
- 提交信息用 `type(scope): 中文摘要`（`feat` / `fix` / `docs` / `test` / `ci` / `refactor`），
  正文说明动机与取舍；同样不要写真实语料的数字。

## 发布流程

打 tag `vX.Y.Z`（必须与 `pyproject.toml` 的 version 一致）→ `.github/workflows/release.yml`
从该 tag 构建 wheel 与 sdist 并挂到**草稿** Release → 人工确认后点 Publish。
不要手工上传发布件：附件必须能由 tag 复现。

## 安全

漏洞请走**私密通道**，不要开公开 issue——见 [`SECURITY.md`](SECURITY.md)。
