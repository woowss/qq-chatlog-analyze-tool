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
"""web 包 —— 前端资源（Jinja2 模板与静态文件），本身没有 Python 逻辑

这里只有一个 `__init__.py`，作用是让这个目录成为一个「包」：setuptools 只把
包内声明的 package-data 打进 wheel（见 pyproject.toml 的 `[tool.setuptools.package-data]`）。
没有它，`pip install .` 之后 `web/templates/*.html` 与 `web/static/**` 全部丢失，
首页直接 500——`tests/test_packaging.py` 会在源码侧核对覆盖范围，
CI 的 package job 还会拆开 wheel 再核一遍。

模板与静态目录一律走下面的绝对路径（而不是相对当前工作目录的 "web/templates"）：
安装后包在 site-packages 里，从任何目录执行 `qqchatlog` 都要能找到它们。
"""
from pathlib import Path

#: web 包所在目录：源码运行时是仓库下的 web/，安装后是 site-packages/web/
ROOT = Path(__file__).resolve().parent
#: Jinja2 模板目录（Flask 的 template_folder）
TEMPLATES_DIR = ROOT / "templates"
#: 静态资源目录（Flask 的 static_folder，URL 前缀固定 /static）
STATIC_DIR = ROOT / "static"
