# Copyright (C) 2026 woowss
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#
"""指纹护栏：挡住"从某一份真实导出抄进仓库"的形状（防回归）

与 `test_privacy_guards.py` 是**两件事**，别改错文件：那个守的是"日志脱敏 / 缓存 _created /
非回环 fail-closed"三条运行时承诺；这个守的是"仓库文本与提交信息里不许出现真实数据的形状"。

被禁的是形状，不是黑名单里的具体数值——本文件不保存任何真实数据，失败信息也只给
"文件:行 + 规则名"、不打印命中内容（CI 日志对公开仓库是公开的）：

A. 具体美元金额（`$` 紧跟数字）。项目只用 `$X.XX` 占位与 `¥` 计价，写出具体美元数额
   基本等于把真实账单抄进来（这一条同时覆盖"三位以上小数"这种精度特征）；
B. 晚于 2025-03 的 13 位毫秒时间戳。合成夹具统一用 2024-01 / 2025-01~03 的基线；
C. 2025-04 及之后的 `YYYY-MM-DD` 日期串，理由同 B；
D. 文档里出现"非整千、非整 1024 的 5 位以上数字"（含 `12,4xx` 这种千分位写法）。
   真实统计量（消息总数、丢弃条数）就是长这样；合成示例一律写成 20,000 / 600000 这类量级；
E. 32 位十六进制串（除下方登记的合成占位值）。真实导出里的 UID/媒体 md5 会以这个形状出现；
F. 提交信息与环境上下文：只要 `.git` 可用，提交信息（含历史上的）也按 A–E 扫一遍——
   本文件拦不住"人把真实值写进 commit message"这种最常见的漏法。

扫描集合是 `git ls-files`（没有 .git 时退化为目录遍历，供 sdist 内的用例使用）；
`web/static/vendor/` 是第三方库，跳过。
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import bootstrap  # noqa: E402

bootstrap()

ROOT = Path(__file__).resolve().parent.parent

#: 第三方库目录（相对仓库根，前缀匹配）
VENDOR_PREFIX = ("web/static/vendor/",)
#: 只扫这些后缀（外加几个无后缀的配置文件）
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".html",
    ".js",
    ".css",
    ".example",
    ".sh",
    ".ps1",
    ".in",
}
TEXT_NAMES = {".gitattributes", ".gitignore", ".env.example"}
#: 本地专用、被 .gitignore 排除的审计产物：不进仓库，也不该把本地文件名当成违规
LOCAL_ONLY = {"privacy-audit-report.md"}
#: 超过这个体积的文本文件不扫（避免为了守卫去读大文件）
MAX_BYTES = 2 * 1024 * 1024

#: 规则 A：具体美元金额
MONEY = re.compile(r"\$[0-9]")
#: 规则 B：语料窗口之后的毫秒时间戳（上限 = 2025-04-01 UTC）
MS_TIMESTAMP = re.compile(r"\b17\d{11}\b")
FIXTURE_CEILING_MS = 1743465600000
#: 规则 C：语料窗口之后的日期串
LATE_2025_DATE = re.compile(r"2025-(?:0[4-9]|1[0-2])-\d\d")
#: 规则 D：5 位以上数字（含千分位写法）。
#: 前缀豁免 `[0-9a-fA-F#,:.`：十六进制/色号、逗号链（rgba(84,112,198,…)）、
#: 行号列表（foo.py:158,168）、小数点后的小数——这些形状与"统计量"重合但语义无关。
BIG_NUMBER = re.compile(r"(?<![0-9a-fA-F#,:.`])(?:\d{1,3}(?:,\d{3})+|\d{5,})(?![0-9a-fA-F])")
#: 规则 E：32 位十六进制串
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
#: 登记在案的合成占位值：新增占位值必须显式写进来（守卫宁可严格）
SYNTHETIC_HEX32 = {"0123456789abcdef0123456789abcdef"}
#: 规则 D 只作用于"文档类"文件：代码里的常量（超时、字节上限）天然是这些形状
DOC_PREFIXES = ("docs/",)
DOC_FILES = {"README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"}

#: 守卫自身必须跳过：它必然要写出基线常量、形状说明与合成占位值
SELF = Path(__file__).resolve()


def _tracked_files():
    """优先问 git 要受版本控制的文件；没有 git（sdist 场景）就退化为目录遍历。"""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            names = out.stdout.decode("utf-8", "replace").split("\0")
            return [ROOT / n for n in names if n]
    except (OSError, subprocess.SubprocessError):
        pass
    skip_dirs = {
        ".git",
        "__pycache__",
        "uploads",
        "ai_cache",
        "stats_cache",
        "logs",
        "dist",
        "build",
        ".venv",
        "node_modules",
        "vendor",
    }
    files = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def _big_number_hits(line: str) -> list:
    """规则 D：返回行内可疑的大数字（已按"年份 / 整千 / 整 1024"豁免）。"""
    hits = []
    for m in BIG_NUMBER.finditer(line):
        if m.group(0).count(",") >= 2:  # 三段以上的逗号链是 rgba(...) 这类分量表，不是统计量
            continue
        value = int(m.group(0).replace(",", ""))
        if 1900 <= value <= 2099:  # 年份
            continue
        if value % 1000 == 0 or value % 1024 == 0:  # 合成量级（20,000 / 600000 / 65536）
            continue
        hits.append(m.group(0))
    return hits


def _text_problems(rel: str, text: str, is_doc: bool) -> list:
    """返回 [(规则名, 行号)]，只报位置不报内容。"""
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if MONEY.search(line):
            problems.append(("A 具体美元金额", lineno))
        for m in MS_TIMESTAMP.finditer(line):
            if int(m.group(0)) >= FIXTURE_CEILING_MS:
                problems.append(("B 时间戳晚于夹具基线", lineno))
                break
        if LATE_2025_DATE.search(line):
            problems.append(("C 日期串晚于夹具基线", lineno))
        if is_doc and _big_number_hits(line):
            problems.append(("D 疑似真实统计量（大额非整圆数字）", lineno))
        for m in HEX32.finditer(line):
            if m.group(0) not in SYNTHETIC_HEX32:
                problems.append(("E 32 位十六进制串", lineno))
                break
    return problems


def _scan_files() -> list:
    """返回 [(规则名, 相对路径, 行号)]。"""
    problems = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.resolve() == SELF or rel.startswith(VENDOR_PREFIX) or path.name in LOCAL_ONLY:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        is_doc = rel.startswith(DOC_PREFIXES) or rel in DOC_FILES
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, lineno in _text_problems(rel, text, is_doc):
            problems.append((name, rel, lineno))
    return problems


def _scan_messages() -> list:
    """规则 F：提交信息也按同一套形状扫（没有 .git 时返回空）。"""
    try:
        out = subprocess.run(
            ["git", "log", "--all", "--format=%H%x00%B%x00"], cwd=ROOT, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    parts = out.stdout.decode("utf-8", "replace").split("\0")
    problems = []
    for i in range(0, len(parts) - 1, 2):
        sha, body = parts[i].strip(), parts[i + 1]
        for name, lineno in _text_problems("commit", body, is_doc=True):
            problems.append((name, f"commit {sha[:8]}", lineno))
    return problems


class TestNoCorpusShapedValues(unittest.TestCase):
    """仓库里只允许出现"明显合成"的时间、金额与统计量"""

    def test_tracked_files_have_no_corpus_shaped_values(self):
        problems = _scan_files()
        if problems:
            detail = "\n".join(f"  - {p}:{ln}  [{name}]" for name, p, ln in problems[:20])
            self.fail(
                f"发现 {len(problems)} 处疑似从真实数据抄来的形状（命中内容不打印）：\n{detail}\n"
                "请改用 2024-01 / 2025-01~03 的基线时间、`$X.XX` 占位金额与 20,000 这类量级数字。"
            )

    def test_commit_messages_have_no_corpus_shaped_values(self):
        problems = _scan_messages()
        if problems:
            detail = "\n".join(f"  - {p}（第 {ln} 段）  [{name}]" for name, p, ln in problems[:20])
            self.fail(
                f"提交信息里有 {len(problems)} 处疑似真实数据的形状（命中内容不打印）：\n{detail}\n"
                "提交信息同样会公开，请改成量级/占位表述。"
            )

    def test_scan_actually_covers_files(self):
        """守卫本身别退化成"什么都没扫"（历史上有过扫描器静默失效的事故）"""
        files = [p for p in _tracked_files() if p.relative_to(ROOT).as_posix().startswith("tests/")]
        self.assertGreater(len(files), 5, "扫描集合为空或过小，守卫失效")

    def test_rules_do_flag_the_shapes_they_claim(self):
        """负向对照：五条规则必须真的能命中各自的目标形状

        样本在运行时拼装，且一律用合成的同形值，避免本文件自己被规则命中、也避免把真实值再抄一遍。
        """
        money = "$" + "9." + "99"
        self.assertTrue(MONEY.search(money), "规则 A 失效")
        self.assertTrue(MONEY.search("$" + "1." + "2345"), "规则 A 失效")
        self.assertIsNone(MONEY.search("$" + "X.XX"), "规则 A 误报占位金额")
        self.assertIsNone(MONEY.search("¥" + "1.2"), "规则 A 误报人民币计价")

        late_ms = str(FIXTURE_CEILING_MS + 86400000)
        early_ms = str(FIXTURE_CEILING_MS - 86400000)
        m = MS_TIMESTAMP.search(f"ts={late_ms}")
        self.assertIsNotNone(m, "规则 B 匹配失效")
        self.assertGreaterEqual(int(m.group(0)), FIXTURE_CEILING_MS, "规则 B 边界失效")
        m = MS_TIMESTAMP.search(f"ts={early_ms}")
        self.assertIsNotNone(m, "规则 B 匹配失效")
        self.assertLess(int(m.group(0)), FIXTURE_CEILING_MS, "规则 B 边界失效")

        self.assertTrue(LATE_2025_DATE.search("2025-" + "05" + "-01"), "规则 C 失效")
        self.assertIsNone(LATE_2025_DATE.search("2025-" + "03" + "-01"), "规则 C 误报基线内日期")

        self.assertTrue(_big_number_hits("总数 " + "13579" + " 条"), "规则 D 失效")
        self.assertTrue(_big_number_hits("总数 " + "13,579" + " 条"), "规则 D 失效")
        self.assertFalse(_big_number_hits("总数 " + "20,000" + " 条"), "规则 D 误报整千量级")
        self.assertFalse(_big_number_hits("上限 " + "65536" + " tokens"), "规则 D 误报整 1024 量级")
        self.assertFalse(_big_number_hits("日期 " + "2026" + "-09-13"), "规则 D 误报年份")
        self.assertFalse(_big_number_hits("色号 #" + "5470c6"), "规则 D 误报色号")

        synthetic = sorted(SYNTHETIC_HEX32)[0]
        self.assertIsNotNone(HEX32.search(synthetic), "规则 E 匹配失效")
        self.assertIn(synthetic, SYNTHETIC_HEX32, "规则 E 白名单失效")
        other = "13579bdf" * 4
        self.assertIsNotNone(HEX32.search(other), "规则 E 匹配失效")
        self.assertNotIn(other, SYNTHETIC_HEX32, "规则 E 白名单越界")


if __name__ == "__main__":
    unittest.main()
