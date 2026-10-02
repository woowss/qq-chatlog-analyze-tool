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

只记录数字（调用次数/prompt/completion tokens），不含任何聊天内容。
写入是**合并去抖**的：一次全量分析动辄几十次调用，原先每次调用都全量重写
JSON（锁内读-改-写，串行化在 API 节奏上）；现在增量先进内存，
5 秒内的连续调用合并成一次落盘。三个保底：get_usage() 读前冲刷、
线程在 FLUSH_DELAY 后自动冲刷、进程正常退出时 atexit 冲刷。
"""

import atexit
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from config import LOG_RETENTION_DAYS, TOKEN_USAGE_FILE
from analyzer.atomic_write import write_json_atomic
from analyzer.logger import get_logger

logger = get_logger("usage")

CST = timezone(timedelta(hours=8))

_LOCK = threading.Lock()
_EMPTY = {"days": {}, "dims": {}, "total": {"calls": 0, "prompt": 0, "completion": 0}}

# 待落盘的增量（与文件同构的 delta），由 _LOCK 保护
_PENDING = {"days": {}, "dims": {}, "total": {"calls": 0, "prompt": 0, "completion": 0}}
_DIRTY = False
_TIMER = None
FLUSH_DELAY_SECONDS = 5.0


def _accumulate(bucket: dict, key: str, calls: int, prompt: int, completion: int) -> None:
    """（须持有 _LOCK）把一个聚合条目并入 bucket；全 0 则不创建空条目"""
    if not (calls or prompt or completion):
        return
    entry = bucket.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0})
    entry["calls"] += calls
    entry["prompt"] += prompt
    entry["completion"] += completion


def record_call(model: str, dim: str, prompt_tokens: int, completion_tokens: int) -> None:
    """记录一次 API 调用的用量；失败静默（统计不能影响主流程）"""
    global _DIRTY
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    day = datetime.now(tz=CST).strftime("%Y-%m-%d")
    dim_key = f"{dim}|{model}"
    try:
        with _LOCK:
            _pending_add(day, dim_key, prompt_tokens, completion_tokens)
            _DIRTY = True
            _schedule_flush()
    except Exception as e:
        logger.debug("用量统计记录失败（忽略，不影响分析）: %s", e)


def _pending_add(day: str, dim_key: str, prompt: int, completion: int) -> None:
    """把一次调用并入内存增量（调用方持有 _LOCK）"""
    for bucket, key in ((_PENDING["days"], day), (_PENDING["dims"], dim_key)):
        entry = bucket.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0})
        entry["calls"] += 1
        entry["prompt"] += prompt
        entry["completion"] += completion
    t = _PENDING["total"]
    t["calls"] += 1
    t["prompt"] += prompt
    t["completion"] += completion


def _schedule_flush() -> None:
    """去抖：已有定时任务在飞就不再排（调用方持有 _LOCK）"""
    global _TIMER
    if _TIMER is None:
        _TIMER = threading.Timer(FLUSH_DELAY_SECONDS, _timer_flush)
        _TIMER.daemon = True
        _TIMER.start()


def _timer_flush() -> None:
    """定时落盘。整段自己兜异常：它跑在 Timer 线程里。

    少了这层包装时，`_flush_locked` 抛出的任何异常都会**杀死这个 Timer 线程**：
    线程没了、`_TIMER` 已置 None，看起来"下一次会重排"，但每一轮都死在同一行——
    用量永远落不了盘，而症状只是"页面数字不动"。统计是旁路观测，不许反过来
    影响主流程，更不许把自己弄死。
    """
    global _TIMER
    try:
        with _LOCK:
            _TIMER = None
            _flush_locked()
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            _TIMER = None
        logger.warning("token 用量定时落盘失败（增量保留在内存，稍后重试）: %s", e)


def flush() -> None:
    """把内存里的增量并入文件（读接口与进程退出前都会自动调用）"""
    with _LOCK:
        _flush_locked()


def _prune_days(data: dict) -> None:
    """按天聚合的用量只保留 LOG_RETENTION_DAYS 天。

    日志已经按天轮转只留 7 天，这里原先却把每天的调用次数无限累积：虽然只有数字，
    但与"不留无限历史"的口径不一致，而且没人需要三年前的调用次数。
    日期串是 YYYY-MM-DD，字典序即时间序，直接按字符串比较即可。
    """
    days = data.get("days")
    if not isinstance(days, dict) or not days:
        return
    cutoff = (datetime.now(tz=CST) - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    for key in [k for k in days if k < cutoff]:
        days.pop(key, None)


def _flush_locked() -> None:
    """（须持有 _LOCK）有增量才读写文件：N 次连续调用合并为一次落盘"""
    global _DIRTY
    if not _DIRTY:
        return
    data = _load()
    for section in ("days", "dims"):
        for key, delta in _PENDING[section].items():
            _accumulate(data[section], key, delta["calls"], delta["prompt"], delta["completion"])
    t = _PENDING["total"]
    for k in ("calls", "prompt", "completion"):
        data["total"][k] = data["total"].get(k, 0) + t[k]
    _prune_days(data)
    try:
        _dump(data)
    except OSError as e:
        # 落盘失败就把增量留在内存里下次再试：原先这里会把异常抛给调用方，
        # /api/usage 直接 500，而增量已被下面的清空逻辑丢掉（用量永久少记）。
        logger.warning("token 用量写入失败（保留增量，稍后重试）: %s", e)
        return
    _PENDING["days"].clear()
    _PENDING["dims"].clear()
    for k in t:
        t[k] = 0
    _DIRTY = False


# 价格表（元 / 百万 tokens），用于把用量换算成"大概花了多少钱"。
# 取 DeepSeek 官方标准（高峰）价：空闲时段是半价，所以这是上界估算。
# 可用 LLM_PRICE_IN / LLM_PRICE_OUT 统一覆盖（换服务商时方便）。
_PRICES = {
    "deepseek-flash": (2.0, 8.0),
    "deepseek-chat": (2.0, 8.0),  # 旧名，官方已路由到 flash
    "deepseek-v4-pro": (9.0, 27.0),
}


def _price_for(model: str) -> tuple:
    override_in = os.getenv("LLM_PRICE_IN", "").strip()
    override_out = os.getenv("LLM_PRICE_OUT", "").strip()
    if override_in and override_out:
        try:
            return float(override_in), float(override_out)
        except ValueError:
            pass
    name = (model or "").lower()
    for key, price in _PRICES.items():
        if name.startswith(key):
            return price
    return (0.0, 0.0)  # 未知模型不瞎猜，费用显示为 0/不展示


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """按百万 tokens 单价估算费用（元）"""
    price_in, price_out = _price_for(model)
    return round(prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out, 4)


def get_usage() -> dict:
    """读取聚合用量；附带派生字段 total_tokens 与估算费用。

    读前先冲刷增量，保证「调用完立刻看」不会出现数据滞后。
    """
    with _LOCK:
        _flush_locked()
        data = _load()
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
    dims = {}
    total_cost = 0.0
    for key, entry in (data.get("dims") or {}).items():
        e = dict(entry)
        model = key.split("|", 1)[1] if "|" in key else ""
        e["cost"] = estimate_cost(e.get("prompt", 0), e.get("completion", 0), model)
        total_cost += e["cost"]
        dims[key] = e
    out["dims"] = dims
    total = dict(data.get("total", {"calls": 0, "prompt": 0, "completion": 0}))
    total["total"] = total["prompt"] + total["completion"]
    total["cost"] = round(total_cost, 4)
    out["total"] = total
    out["pricing_note"] = "按模型标准价估算（空闲时段约为半价）"
    return out


def _coerce_counter_dict(value) -> dict:
    """把一个可疑的桶收成 {str: {"calls","prompt","completion"}}；不可用的条目直接丢弃。

    只校验顶层是不够的：`_accumulate` 与 `data["total"].get` 假定这三段都是 dict，
    而文件被手工编辑过、半个文件写坏过、或上一版格式不同，都会留下 `{"days": []}`
    或 `{"total": 5}` 这种"能解析、形状不对"的内容。后果原本是一整条静默死循环：
    `_flush_locked` 抛 AttributeError → record_call 把它吞进 except → `_DIRTY` 永远为真
    → 每次定时器醒来都在同一行死掉（顺带弄死一个 Timer 线程）、增量永不落盘，
    而 `get_usage()` 与 `flush()` 都会走同一段 → **/api/usage 从此永久 500**。
    `_prune_days` 早就对 days 做了 isinstance 守卫，漏的是这里的入口净化：
    坏数据在门口一次挡掉，比在每个消费点各防一遍可靠得多。
    """
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        out[key] = {
            "calls": _as_int(entry.get("calls")),
            "prompt": _as_int(entry.get("prompt")),
            "completion": _as_int(entry.get("completion")),
        }
    return out


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load() -> dict:
    try:
        with open(TOKEN_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(_EMPTY))
    if not isinstance(data, dict):
        return json.loads(json.dumps(_EMPTY))
    # 三段全部按形状净化后再交出去：下游三处消费点都假定它们是 dict-of-dict
    days = _coerce_counter_dict(data.get("days"))
    dims = _coerce_counter_dict(data.get("dims"))
    total_src = data.get("total")
    total = {
        k: _as_int((total_src or {}).get(k)) if isinstance(total_src, dict) else 0
        for k in ("calls", "prompt", "completion")
    }
    return {"days": days, "dims": dims, "total": total}


def _dump(data: dict) -> None:
    write_json_atomic(TOKEN_USAGE_FILE, data, indent=1, mkdir=os.path.dirname(TOKEN_USAGE_FILE))


atexit.register(flush)
