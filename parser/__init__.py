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
# parser package —— QQChatExporter JSON 解析（qq_parser.py）
#
# 这个 __init__.py 是给打包用的：没有它，parser/ 只是一个 PEP 420 命名空间包，
# 装进 wheel 后会和 site-packages 里任何同名目录合并（谁先命中看 sys.path），
# 而 analyzer/ 与 webapp/ 都是常规包。补上后四个顶层名一律是确定的常规包。
