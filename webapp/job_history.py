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

"""分析任务历史文件的纯读写逻辑；运行时路径、锁和日志由 store 注入。"""

import json
import os
import time


MAX_LINES = 1000
FIELDS = ("t", "dim", "status", "done", "total", "chat")


def prune_lines(lines: list, max_age_days: int, drop_prefixes: tuple = (), *, now=None) -> list:
    """按年龄与聊天哈希前缀过滤行；无法解析的非空行原样保留。"""
    if now is None:
        now = time.time()
    cutoff = (now - max_age_days * 86400) if max_age_days and max_age_days > 0 else None
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if not isinstance(entry, dict):
            out.append(line)
            continue
        stamp = entry.get("t")
        if cutoff is not None and isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            if stamp < cutoff:
                continue
        chat = str(entry.get("chat") or "")
        if drop_prefixes and chat and chat.startswith(drop_prefixes):
            continue
        out.append(json.dumps(entry, ensure_ascii=False))
    return out


def purge_job_history(
    chat_hash: str,
    *,
    path: str,
    lock,
    prune,
    write_text_atomic,
    logger,
) -> int:
    """按聊天哈希前缀清除历史账目。"""
    if not chat_hash:
        return 0
    with lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return 0
        kept = prune(lines, 0, drop_prefixes=(chat_hash[:12],))
        removed = len([line for line in lines if line.strip()]) - len(kept)
        if removed <= 0:
            return 0
        try:
            write_text_atomic(path, "".join(line + "\n" for line in kept))
        except OSError as e:
            logger.warning("任务历史按聊天清理失败（可能仍残留哈希前缀）: %s", e)
            return 0
    return removed


def append_job_history(
    entry: dict,
    *,
    log_dir: str,
    max_age_days: int,
    path: str,
    lock,
    prune,
    max_lines: int,
    fields: tuple,
    write_text_atomic,
    logger,
) -> None:
    line = {key: entry.get(key) for key in fields if entry.get(key) is not None}
    if not line:
        return
    with lock:
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            original = len([item for item in lines if item.strip()])
            kept = prune(lines, max_age_days)
            if len(kept) > max_lines:
                kept = kept[-(max_lines // 2) :]
            if len(kept) == original:
                return
            write_text_atomic(path, "".join(item + "\n" for item in kept))
        except OSError as e:
            logger.warning("任务历史写入失败（不影响分析本身）: %s", e)


def read_job_history(path: str, limit: int = 30) -> list:
    """返回最近的历史，新到旧；文件缺失或坏行不会影响读取。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    lines = raw.splitlines()[-max(1, min(int(limit), 500)) :]
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    out.reverse()
    return out
