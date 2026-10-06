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

"""跨 Python 版本稳定的源码指纹归一化。"""

import ast
import inspect
import textwrap


def hashed_source(func, *, canonical_ast, logger) -> str:
    """忽略注释、空白与引号风格，保留影响行为的函数语法树。"""
    src = inspect.getsource(func)
    try:
        return canonical_ast(ast.parse(textwrap.dedent(src)))
    except (SyntaxError, ValueError) as e:
        logger.warning(
            "提示词指纹：%s 的源码无法解析成 AST（%s），该类回退为原文哈希——"
            "这意味着它的注释/格式改动也会换键",
            getattr(func, "__name__", func),
            e,
        )
        return src


def canonical_ast(node) -> str:
    """按节点类型、排序字段和非空语义值生成跨版本稳定的规范串。"""
    if isinstance(node, ast.AST):
        parts = [type(node).__name__]
        for field in sorted(getattr(node, "_fields", ())):
            value = getattr(node, field, None)
            if value is None or value == [] or value == "":
                continue
            parts.append(f"{field}={canonical_ast(value)}")
        return "(" + " ".join(parts) + ")"
    if isinstance(node, (list, tuple)):
        return "[" + " ".join(canonical_ast(item) for item in node) + "]"
    return repr(node)
