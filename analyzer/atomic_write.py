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
"""原子落盘的唯一实现：写临时文件 → `os.replace`。

为什么单独成一个模块（原先它是抄了 9 遍的同一段代码）：

1. **临时名必须唯一**。固定名 `f"{path}.tmp"` 等于让两个并发写者踩同一个文件：
   A 刚 open，B 把同一个文件重开覆盖；或任一方在失败分支里 `os.remove(tmp)` 删掉
   对方正在写的那份，最后 `os.replace` 就可能把**半截文件发布成正式缓存**。多浏览器/
   多设备共用一份聊天是本项目明确支持的用法，而月份缓存的补标记路径
   （`analyzer.month_cache._restamp_month_cache`）让"同一个键被并发写"成为常态。
   pid + 线程 id + 随机后缀保证每个写者各用各的；仍在同一目录内，所以 `os.replace`
   的原子性不变，清理侧也照旧按 `.tmp` 后缀识别（见 `webapp.store._TMP_SUFFIX_RE`）。
2. **抄本会漏掉修复**。上面那条唯一名是在第六轮审阅里定下来的，但当时只有
   `store`/`month_cache` 两处跟上，`vision`/`usage`/`face_images` 三处仍是固定名——
   收成一处之后，这类"修了三个副本、漏了三个"不可能再发生。

失败语义：本模块**只**保证"不留下自己的半成品"，然后把 `OSError` 原样交回调用方。
日志文案与善后各站点不同（manifest 写失败要把缓存条目弹掉、任务历史写失败不影响分析、
维度缓存写失败要说"重复付费"），所以不在这里统一——吞掉异常反而会让那些差异消失。
"""

import json
import os
import secrets
import threading


def tmp_sibling(path: str) -> str:
    """`path` 的临时同名文件：唯一、同目录、仍以 `.tmp` 结尾（清理侧按后缀认领）"""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"


def _discard(tmp: str) -> None:
    """尽力删掉自己的半成品；删不掉也不能盖掉正在往上调的那个异常"""
    try:
        os.remove(tmp)
    except OSError:
        pass


def _prepare(mkdir: "str | None") -> None:
    """目录可能就在这一秒被用户删掉（README 教用户"删掉 ai_cache/ 即可彻底清除数据"，
    而服务还开着）。不补目录，此后每一次写入都会静默失败。"""
    if mkdir:
        os.makedirs(mkdir, exist_ok=True)


def write_json_atomic(path: str, payload, *, mkdir: "str | None" = None, indent: "int | None" = None) -> None:
    """JSON 原子落盘（`ensure_ascii=False`，中文原样入库）。失败时清半成品并抛 `OSError`"""
    tmp = tmp_sibling(path)
    try:
        _prepare(mkdir)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except OSError:
        _discard(tmp)
        raise


def write_text_atomic(path: str, text: str, *, mkdir: "str | None" = None) -> None:
    """纯文本原子落盘（任务历史这类逐行账目）。失败语义同 `write_json_atomic`"""
    tmp = tmp_sibling(path)
    try:
        _prepare(mkdir)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        _discard(tmp)
        raise


def write_bytes_atomic(path: str, data: bytes, *, mkdir: "str | None" = None) -> None:
    """二进制原子落盘（导入的结果包条目、表情原图）。失败语义同上"""
    tmp = tmp_sibling(path)
    try:
        _prepare(mkdir)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        _discard(tmp)
        raise
