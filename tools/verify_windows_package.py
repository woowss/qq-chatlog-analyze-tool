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
# -*- coding: utf-8 -*-
"""检查 Windows 冻结目录是否包含程序和前端资源。"""

import argparse
from pathlib import Path


REQUIRED_FILES = (
    "QQChatLog.exe",
    "web/templates/base.html",
    "web/templates/index.html",
    "web/static/js/charts.js",
    "web/static/vendor/echarts.min.js",
)


def verify(bundle: Path) -> list[str]:
    # PyInstaller 6 的 onedir 布局把 Python 代码和 package-data 放在
    # ``_internal``，而 exe 本身留在外层；兼容这个布局也让校验脚本能检查
    # 旧版/自定义 spec 直接放在 bundle 根目录的产物。
    roots = [bundle / "_internal", bundle]
    root = next((candidate for candidate in roots if (candidate / "web").is_dir()), None)
    if root is None:
        raise SystemExit(f"找不到 Windows 包资源根目录: {bundle}")
    missing = [
        relative
        for relative in REQUIRED_FILES
        if not (bundle / relative if relative == "QQChatLog.exe" else root / relative).is_file()
    ]
    if missing:
        raise SystemExit("Windows 包缺少文件:\n" + "\n".join(f"- {item}" for item in missing))
    return [
        str(bundle / relative if relative == "QQChatLog.exe" else root / relative)
        for relative in REQUIRED_FILES
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查 QQChatLog Windows 冻结目录")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    for path in verify(args.bundle):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
