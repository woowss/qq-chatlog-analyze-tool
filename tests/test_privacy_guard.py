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
"""隐私形状守卫：挡住"从某一份真实导出抄进仓库"的三类痕迹（防回归）

被禁的是形状，不是某个具体数值——本文件里不保存任何真实数据，
失败信息也只给"文件:行 + 规则名"，不打印命中内容（CI 日志是公开的）：

A. 金额精度过高（三位以上小数）。项目里金额只用 `$X.XX` / `¥1.2` 这类量级，
   小数点后三四位是"从真实账单里抄下来"的典型特征；
B. 晚于 2025-03 的 13 位毫秒时间戳。合成夹具统一用 2024-01 / 2025-01~03 的基线，
   引入更晚的时间戳没有业务理由；
C. 2025-04 及之后的 `YYYY-MM-DD` 日期串，理由同 B。

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
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".html", ".js", ".css", ".example", ".sh", ".ps1", ".in",
}
TEXT_NAMES = {".gitattributes", ".gitignore", ".env.example"}
#: 本地专用、被 .gitignore 排除的审计产物：不进仓库，也不该把本地文件名当成违规
LOCAL_ONLY = {"privacy-audit-report.md"}
#: 超过这个体积的文本文件不扫（避免为了守卫去读大文件）
MAX_BYTES = 2 * 1024 * 1024

MONEY_TOO_PRECISE = re.compile(r"\$\d+\.\d{3,}")
MS_TIMESTAMP = re.compile(r"\b17\d{11}\b")
#: 夹具时间戳上限：2025-04-01 UTC。晚于它的 13 位时间戳视为越界。
FIXTURE_CEILING_MS = 1743465600000
LATE_2025_DATE = re.compile(r"2025-(?:0[4-9]|1[0-2])-\d\d")

RULES = (
    ("A 金额精度过高（>2 位小数）", MONEY_TOO_PRECISE),
    ("B 时间戳晚于夹具基线（2025-04 起）", MS_TIMESTAMP),
    ("C 日期串晚于夹具基线（2025-04 起）", LATE_2025_DATE),
)


def _tracked_files():
    """优先问 git 要受版本控制的文件；没有 git（sdist 场景）就退化为目录遍历。"""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            names = out.stdout.decode("utf-8", "replace").split("\0")
            return [ROOT / n for n in names if n]
    except (OSError, subprocess.SubprocessError):
        pass
    skip_dirs = {".git", "__pycache__", "uploads", "ai_cache", "stats_cache",
                 "logs", "dist", "build", ".venv", "node_modules", "vendor"}
    files = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def _scan():
    """返回 [(规则名, 相对路径, 行号)]，只报位置不报内容。"""
    problems = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(VENDOR_PREFIX) or path.name in LOCAL_ONLY:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, rx in RULES:
                m = rx.search(line)
                if not m:
                    continue
                if name.startswith("B") and int(m.group(0)) < FIXTURE_CEILING_MS:
                    continue
                problems.append((name, rel, lineno))
    return problems


class TestNoCorpusShapedValues(unittest.TestCase):
    """仓库里只允许出现"明显合成"的时间与金额"""

    def test_tracked_files_have_no_corpus_shaped_values(self):
        problems = _scan()
        if problems:
            detail = "\n".join(f"  - {p}:{ln}  [{name}]" for name, p, ln in problems[:20])
            self.fail(
                f"发现 {len(problems)} 处疑似从真实数据抄来的形状（命中内容不打印）：\n{detail}\n"
                "夹具请改用 2024-01 / 2025-01~03 基线时间与 `$X.XX` 量级金额。"
            )

    def test_scan_actually_covers_files(self):
        """守卫本身别退化成"什么都没扫"（历史上有过扫描器静默失效的事故）"""
        files = [p for p in _tracked_files()
                 if p.relative_to(ROOT).as_posix().startswith("tests/")]
        self.assertGreater(len(files), 5, "扫描集合为空或过小，守卫失效")

    def test_rules_do_flag_the_shapes_they_claim(self):
        """负向对照：三条规则必须真的能命中各自的目标形状

        样本在运行时拼装，避免本文件自己被自己的规则命中。
        """
        money = "$" + "12." + "3456"
        self.assertTrue(MONEY_TOO_PRECISE.search(money), "规则 A 失效")
        late_ms = str(FIXTURE_CEILING_MS + 86400000)
        self.assertIsNotNone(MS_TIMESTAMP.search(f"ts={late_ms}"), "规则 B 匹配失效")
        self.assertGreater(int(late_ms), FIXTURE_CEILING_MS - 1, "规则 B 边界失效")
        early_ms = str(FIXTURE_CEILING_MS - 86400000)
        m = MS_TIMESTAMP.search(f"ts={early_ms}")
        self.assertIsNotNone(m, "规则 B 匹配失效")
        self.assertLess(int(m.group(0)), FIXTURE_CEILING_MS, "规则 B 边界失效")
        late_date = "2025-" + "05" + "-01"
        self.assertTrue(LATE_2025_DATE.search(late_date), "规则 C 失效")
        ok_date = "2025-" + "03" + "-01"
        self.assertIsNone(LATE_2025_DATE.search(ok_date), "规则 C 误报基线内日期")


if __name__ == "__main__":
    unittest.main()
