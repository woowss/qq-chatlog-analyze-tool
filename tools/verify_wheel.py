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
"""wheel 拆包核对：代码、模板/静态资源、console_scripts 一个都不能少

    python -m build --wheel --outdir .tmp_dist
    python tools/verify_wheel.py .tmp_dist/*.whl
    python tools/verify_wheel.py                 # 缺省扫 dist/ 与 .tmp_dist/

为什么还要这一层：tests/test_packaging.py 只能在源码侧用 glob 模拟 setuptools 的收集过程，
"模拟对了"不等于"打出来的包里真有"。这里读真实 wheel 的 zip 清单逐条比对，CI 的 package job
就是靠它把「templates/static 要能被装进 wheel」钉死的。

核对项：
  1. 代码：app.py / config.py 与 analyzer / parser / webapp 下的每个 .py 都在；
  2. 前端资源：web/templates 与 web/static 下的每个源文件都在（vendor/ 递归算）；
  3. 入口点：*.dist-info/entry_points.txt 里有 console_scripts 的 qqchatlog = app:main；
  4. 元数据：Requires-Dist 与 pyproject 的 dependencies 一致、Requires-Python 在、LICENSE 随包；
  5. 不该进包的没进：tests/ tools/ docs/ .github/ 与数据目录、__pycache__、*.pyc。

退出码 0 = 通过，1 = 有缺失或混入。
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

try:  # tomllib 是 3.11+ 的标准库；没有它时退化为“只查有没有依赖声明”
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅 Python 3.10
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
PY_MODULES = ("app.py", "config.py")
CODE_PACKAGES = ("analyzer", "parser", "webapp")
#: 这些前缀出现在包里就是打错了（测试/文档/工具/本地数据都不该随 wheel 分发）
FORBIDDEN_PREFIXES = (
    "tests/",
    "tools/",
    "docs/",
    ".github/",
    "build/",
    "__pycache__/",
    "ai_cache/",
    "logs/",
    "uploads/",
    "flask_session/",
    "stats_cache/",
    "face_cache/",
)
_REQUIRES_DIST_RE = re.compile(r"^Requires-Dist:\s*(?P<spec>.+)$", re.MULTILINE)
#: 版本约束/extra/环境标记的起始字符——截断到第一个即得裸发行名
_SPEC_CUTS = "[()<>=!~; \t"


def _normalize_dist(spec: str) -> str:
    """把依赖声明（可能带版本约束、extra、marker）归一到可比较的裸发行名"""
    name = spec.strip()
    for index, char in enumerate(name):
        if char in _SPEC_CUTS:
            name = name[:index]
            break
    return name.strip().lower().replace("_", "-")


def _runtime_requires_dist(metadata: str) -> set:
    """METADATA 里的运行期 Requires-Dist（带 extra == "..." 标记的选修依赖不算）"""
    shipped = set()
    for match in _REQUIRES_DIST_RE.finditer(metadata):
        spec = match.group("spec")
        marker = spec.split(";", 1)[1] if ";" in spec else ""
        if "extra ==" in marker:
            continue
        shipped.add(_normalize_dist(spec))
    return shipped


def _expected_dependencies() -> set:
    """pyproject 里声明的运行期依赖（拿不到 tomllib 时返回空集合，调用方跳过精确比对）"""
    if tomllib is None:
        return set()
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return {_normalize_dist(dep) for dep in tomllib.load(fh)["project"]["dependencies"]}


def _source_files() -> set:
    """源码树里必须进 wheel 的文件（仓库根相对 posix 路径）"""
    wanted = set(PY_MODULES)
    for package in CODE_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            if "__pycache__" not in path.parts:
                wanted.add(path.relative_to(REPO_ROOT).as_posix())
    for path in WEB_DIR.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            wanted.add(path.relative_to(REPO_ROOT).as_posix())
    return wanted


def _find_member(names, suffix: str):
    """wheel 里第一个以 suffix 结尾的成员名（找不到返回 None）"""
    for name in names:
        if name.endswith(suffix):
            return name
    return None


def verify(wheel: Path) -> int:
    """核对单个 wheel；返回 0/1"""
    problems = []
    with zipfile.ZipFile(wheel) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        nameset = set(names)

        def read(member: str) -> str:
            return archive.read(member).decode("utf-8", "replace")

        missing = sorted(_source_files() - nameset)
        if missing:
            problems.append(
                f"wheel 缺少 {len(missing)} 个源文件"
                "（前端资源漏了通常是 package-data 没写全）："
                + "、".join(missing[:8])
                + ("…" if len(missing) > 8 else "")
            )

        leaked = sorted(
            n for n in names if n.startswith(FORBIDDEN_PREFIXES) or n.endswith(".pyc") or ".egg-info/" in n
        )
        if leaked:
            problems.append(f"wheel 混入了不该分发的内容（{len(leaked)} 个）：" + "、".join(leaked[:8]))

        entry_member = _find_member(names, ".dist-info/entry_points.txt")
        if entry_member is None:
            problems.append("wheel 里没有 entry_points.txt（[project.scripts] 没生效）")
        else:
            entry_points = read(entry_member)
            if not re.search(r"(?m)^qqchatlog\s*=\s*app:main\s*$", entry_points):
                problems.append(f"entry_points.txt 里没有 qqchatlog = app:main：{entry_points.strip()!r}")

        metadata_member = _find_member(names, ".dist-info/METADATA")
        if metadata_member is None:
            problems.append("wheel 里没有 METADATA")
        else:
            metadata = read(metadata_member)
            declared = _expected_dependencies()
            shipped = _runtime_requires_dist(metadata)
            if declared and shipped != declared:
                problems.append(
                    f"Requires-Dist 与 pyproject 不一致：缺 {sorted(declared - shipped)}、"
                    f"多 {sorted(shipped - declared)}"
                )
            elif not shipped:
                problems.append("METADATA 没有任何 Requires-Dist（dependencies 没生效）")
            if "Requires-Python:" not in metadata:
                problems.append("METADATA 缺 Requires-Python")

        if not any(n.endswith("/LICENSE") or n == "LICENSE" for n in names):
            problems.append("wheel 里没有 LICENSE（license-files 没生效）")

    if problems:
        for item in problems:
            print(f"[FAIL] {wheel.name}: {item}")
        return 1
    print(f"[OK] {wheel.name}: {len(names)} 个文件，代码 + 前端资源齐全，qqchatlog 入口点与依赖元数据正常")
    return 0


def _discover() -> list:
    found = []
    for directory in ("dist", ".tmp_dist"):
        found.extend(sorted((REPO_ROOT / directory).glob("*.whl")))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 wheel 里是否带齐代码、模板/静态资源与入口点")
    parser.add_argument("wheels", nargs="*", help="wheel 路径；缺省扫描 dist/ 与 .tmp_dist/")
    args = parser.parse_args()

    wheels = [Path(p) for p in args.wheels] or _discover()
    if not wheels:
        print("[FAIL] 没找到 wheel：先跑 python -m build --wheel --outdir .tmp_dist")
        return 1
    for wheel in wheels:
        if not wheel.is_file():
            print(f"[FAIL] 文件不存在: {wheel}")
            return 1
    return max(verify(wheel) for wheel in wheels)


if __name__ == "__main__":
    sys.exit(main())
