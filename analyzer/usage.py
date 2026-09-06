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
"""LLM token 用量统计 — 按「天 × 维度」聚合持久化到 logs/token_usage.json。

只记录数字（调用次数/prompt/completion tokens），不含任何聊天内容；
线程安全（并发月份分析共享写入），落盘采用临时文件 + 原子替换。
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from config import TOKEN_USAGE_FILE

CST = timezone(timedelta(hours=8))

_LOCK = threading.Lock()
_EMPTY = {"days": {}, "dims": {}, "total": {"calls": 0, "prompt": 0, "completion": 0}}


def record_call(model: str, dim: str, prompt_tokens: int, completion_tokens: int) -> None:
    """记录一次 API 调用的用量；失败静默（统计不能影响主流程）"""
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    day = datetime.now(tz=CST).strftime("%Y-%m-%d")
    dim_key = f"{dim}|{model}"
    try:
        with _LOCK:
            data = _load()
            for bucket, key in ((data["days"], day), (data["dims"], dim_key)):
                entry = bucket.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0})
                entry["calls"] += 1
                entry["prompt"] += prompt_tokens
                entry["completion"] += completion_tokens
            t = data["total"]
            t["calls"] += 1
            t["prompt"] += prompt_tokens
            t["completion"] += completion_tokens
            _dump(data)
    except Exception:
        pass


def get_usage() -> dict:
    """读取聚合用量；附带派生字段 total_tokens"""
    with _LOCK:
        data = _load()
    total = data.get("total", {"calls": 0, "prompt": 0, "completion": 0})
    total = dict(total)
    total["total"] = total["prompt"] + total["completion"]
    data = dict(data)
    data["total"] = total
    return data


def _load() -> dict:
    try:
        with open(TOKEN_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "days" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return json.loads(json.dumps(_EMPTY))


def _dump(data: dict) -> None:
    os.makedirs(os.path.dirname(TOKEN_USAGE_FILE), exist_ok=True)
    tmp = TOKEN_USAGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TOKEN_USAGE_FILE)
