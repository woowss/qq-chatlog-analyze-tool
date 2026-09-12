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


def _compute_asset_version() -> str:
    """自有 JS/CSS 的版本号 = 这些文件里最新的 mtime（秒）。

    用途是给 <script>/<link> 加 ?v=... 破浏览器缓存：Flask 对 static 默认发
    Cache-Control: no-cache（会带 ETag 回源校验），但反向代理与浏览器仍可能按
    URL 长期缓存——改了前端却还在跑旧 JS 时，症状是"新模板配旧脚本"，页面直接
    不可用，而用户完全看不出该强刷。带上版本参数后，任一文件改动都会让 URL 变化。

    只扫自有的 js/ 与 css/：vendor/ 是固定版本的第三方文件（Bootstrap/jQuery/
    ECharts），按文件名本身就带版本，不必每次启动重算它们的 mtime。
    每次启动算一次即可——开发时改前端需要重启服务，与 Flask 模板自动重载的
    预期一致（Flask 的模板是每次请求重读，静态资源不是）。
    """
    newest = 0.0
    for sub in ("js", "css"):
        directory = STATIC_DIR / sub
        try:
            entries = list(directory.glob("*"))
        except OSError:  # 目录缺失（异常安装）：退化成固定版本号，不影响启动
            continue
        for path in entries:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return str(int(newest))


#: 模板里用 {{ asset_v }} 追加到自有 JS/CSS 的 URL 上
ASSET_VERSION = _compute_asset_version()
