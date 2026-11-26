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

"""AI 分析任务实时输出流：内存增量缓存、磁盘持久化与重载恢复。

设计要点：
- 独立 `_STREAM_LOCK`，严禁与 `JOBS_LOCK` 嵌套，所有磁盘写入在锁外原子完成。
- 磁盘文件命名为 `joblog_{chat_hash}_{job_id}.json`，放在 `AI_CACHE_DIR` 下并保证
  `_created` 作为首个 JSON 键，从而自动兼容过期清理、删除聊天联动清理与隐私守卫。
- 按 `(sid_hash, chat_hash, scope_id)` 严格隔离，服务重启后孤立的 `running` 文件
  在恢复快照中自动收口为可读的 `error` 中断状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional
import uuid

from analyzer.logger import get_logger
from analyzer import purge_marks

logger = get_logger("job_stream")

_STREAM_LOCK = threading.Lock()
_STREAMS: dict[str, dict[str, Any]] = {}

_SAFE_SEGMENT_RE = re.compile(r"^[0-9a-zA-Z-]{1,64}$")
_FLUSH_INTERVAL_SEC = 0.6
_IMMEDIATE_KINDS = frozenset({"stage", "cache", "warn", "error", "result"})
_COALESCE_KINDS = frozenset({"thinking", "content"})
_MAX_MEMORY_JOBS = 20
_MAX_EVENTS_PER_JOB = 12000
_MAX_TEXT_PER_EVENT = 32768
_INTERRUPTED_MSG = "分析任务因服务重启而中断；已完成单元已保留在缓存中，可重新发起分析补齐。"


def sid_digest(sid: str) -> str:
    """将 session sid 映射为不可逆摘要，用于会话隔离校验与落盘比对。"""
    raw = str(sid or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_safe_segment(val: str) -> bool:
    return bool(val and _SAFE_SEGMENT_RE.fullmatch(val))


def _ai_cache_dir() -> str:
    from webapp import store

    return store.AI_CACHE_DIR


def _is_purged(chat_hash: str) -> bool:
    if not chat_hash:
        return False
    from webapp import store

    return bool(purge_marks.is_marked(chat_hash) or store._is_recently_purged(chat_hash))


def _log_path(chat_hash: str, job_id: str) -> Optional[str]:
    if not (_is_safe_segment(chat_hash) and _is_safe_segment(job_id)):
        return None
    cache_dir = _ai_cache_dir()
    if not cache_dir:
        return None
    return os.path.join(cache_dir, f"joblog_{chat_hash}_{job_id}.json")


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _coalesce_for_disk(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """落盘时合并相邻同单元流式 delta，保持磁盘日志紧凑。"""
    out: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("kind") or "stage"
        unit = ev.get("unit") or ""
        text = ev.get("text") or ""
        if (
            out
            and kind in _COALESCE_KINDS
            and out[-1].get("kind") == kind
            and out[-1].get("unit") == unit
            and len(out[-1].get("text") or "") + len(text) <= _MAX_TEXT_PER_EVENT
        ):
            out[-1]["text"] = f"{out[-1].get('text') or ''}{text}"
            out[-1]["ts"] = ev.get("ts") or out[-1].get("ts")
            continue
        item = {
            "seq": len(out),
            "ts": ev.get("ts") or 0.0,
            "kind": kind,
            "unit": unit,
            "label": ev.get("label") or "",
            "text": text,
        }
        if "data" in ev and ev["data"] is not None:
            item["data"] = ev["data"]
        out.append(item)
    return out


def _snapshot_for_disk(rec: dict[str, Any], *, compact: bool) -> Optional[dict[str, Any]]:
    chat_hash = rec.get("chat_hash") or ""
    if not (_is_safe_segment(chat_hash) and _is_safe_segment(rec.get("job_id") or "")):
        return None
    if _is_purged(chat_hash):
        return None
    events_src = list(rec["events"])
    events = _coalesce_for_disk(events_src) if compact else [dict(e) for e in events_src]
    # `_created` 必须作为 JSON 首键以契合过期清理扫描与单测守卫
    return {
        "_created": rec["created_at"],
        "job_id": rec["job_id"],
        "sid_hash": rec["sid_hash"],
        "chat_hash": chat_hash,
        "scope_id": rec["scope_id"],
        "dim": rec["dim"],
        "dim_label": rec["dim_label"],
        "status": rec["status"],
        "done": rec["done"],
        "total": rec["total"],
        "detail": rec["detail"],
        "error": rec["error"],
        "updated_at": rec["updated_at"],
        "events": events,
    }


def _write_snapshot(chat_hash: str, job_id: str, payload: Optional[dict[str, Any]]) -> None:
    if payload is None or _is_purged(chat_hash):
        return
    path = _log_path(chat_hash, job_id)
    if not path:
        return
    try:
        _atomic_write_json(path, payload)
    except OSError as e:
        logger.warning("写入任务实时日志失败: %s", e)


def _evict_finished_locked() -> None:
    if len(_STREAMS) <= _MAX_MEMORY_JOBS:
        return
    finished = [
        (rec.get("updated_at", 0.0), jid) for jid, rec in _STREAMS.items() if rec.get("status") != "running"
    ]
    finished.sort()
    while len(_STREAMS) > _MAX_MEMORY_JOBS and finished:
        _, old_id = finished.pop(0)
        _STREAMS.pop(old_id, None)


def init_job(
    job_id: str,
    *,
    sid: str,
    chat_hash: str,
    dim: str,
    dim_label: str = "",
    scope_id: str = "",
    total: int = 0,
    detail: str = "",
) -> None:
    """初始化任务流记录并写入初始日志快照。"""
    if not _is_safe_segment(job_id):
        return
    now = time.time()
    rec = {
        "job_id": job_id,
        "sid_hash": sid_digest(sid),
        "chat_hash": str(chat_hash or ""),
        "scope_id": str(scope_id or ""),
        "dim": str(dim or ""),
        "dim_label": str(dim_label or dim or ""),
        "status": "running",
        "done": 0,
        "total": int(total or 0),
        "detail": str(detail or ""),
        "error": "",
        "created_at": now,
        "updated_at": now,
        "last_flush": time.monotonic(),
        "last_flushed_kind": "",
        "next_seq": 0,
        "events": [],
    }
    payload: Optional[dict[str, Any]] = None
    with _STREAM_LOCK:
        if _is_purged(rec["chat_hash"]):
            return
        _STREAMS[job_id] = rec
        _evict_finished_locked()
        payload = _snapshot_for_disk(rec, compact=False)
    _write_snapshot(rec["chat_hash"], job_id, payload)


def update_job_meta(
    job_id: str,
    *,
    status: Optional[str] = None,
    done: Optional[int] = None,
    total: Optional[int] = None,
    detail: Optional[str] = None,
    error: Optional[str] = None,
    force_flush: bool = False,
) -> None:
    """更新任务进度/状态元数据并在必要时落盘。"""
    payload: Optional[dict[str, Any]] = None
    chat_hash = ""
    with _STREAM_LOCK:
        rec = _STREAMS.get(job_id)
        if rec is None:
            return
        chat_hash = rec["chat_hash"]
        if _is_purged(chat_hash):
            _STREAMS.pop(job_id, None)
            return
        if status is not None:
            rec["status"] = str(status)
        if done is not None:
            rec["done"] = int(done)
        if total is not None:
            rec["total"] = int(total)
        if detail is not None:
            rec["detail"] = str(detail)
        if error is not None:
            rec["error"] = str(error)
        rec["updated_at"] = time.time()
        now_mono = time.monotonic()
        terminal = rec["status"] != "running"
        if force_flush or terminal or (now_mono - rec["last_flush"] >= _FLUSH_INTERVAL_SEC):
            rec["last_flush"] = now_mono
            payload = _snapshot_for_disk(rec, compact=terminal)
    _write_snapshot(chat_hash, job_id, payload)


def append_event(job_id: str, event: dict[str, Any]) -> None:
    """向指定任务追加一条实时输出事件，按节奏异步安全落盘。"""
    payload: Optional[dict[str, Any]] = None
    chat_hash = ""
    kind = str(event.get("kind") or "stage")
    text = str(event.get("text") or "")
    if len(text) > _MAX_TEXT_PER_EVENT:
        text = text[: _MAX_TEXT_PER_EVENT - 12] + "...(已截断)"
    unit = str(event.get("unit") or "")
    label = str(event.get("label") or "")
    data = event.get("data")

    with _STREAM_LOCK:
        rec = _STREAMS.get(job_id)
        if rec is None:
            return
        chat_hash = rec["chat_hash"]
        if _is_purged(chat_hash):
            _STREAMS.pop(job_id, None)
            return
        events = rec["events"]
        if len(events) >= _MAX_EVENTS_PER_JOB:
            # 超过软上限时压缩最老一段同类型流式 delta，保留 stage/error/result 关键事件
            if (
                kind in _COALESCE_KINDS
                and events
                and events[-1].get("kind") == kind
                and events[-1].get("unit") == unit
            ):
                return
        seq = rec["next_seq"]
        rec["next_seq"] = seq + 1
        now = time.time()
        entry: dict[str, Any] = {
            "seq": seq,
            "ts": now,
            "kind": kind,
            "unit": unit,
            "label": label,
            "text": text,
        }
        if isinstance(data, dict):
            entry["data"] = dict(data)
        events.append(entry)
        rec["updated_at"] = now
        now_mono = time.monotonic()
        should_flush = (
            kind in _IMMEDIATE_KINDS
            or rec.get("last_flushed_kind") != kind
            or (now_mono - rec["last_flush"] >= _FLUSH_INTERVAL_SEC)
        )
        if should_flush:
            rec["last_flush"] = now_mono
            rec["last_flushed_kind"] = kind
            payload = _snapshot_for_disk(rec, compact=False)
    _write_snapshot(chat_hash, job_id, payload)


def make_sink(job_id: str) -> Callable[[dict[str, Any]], None]:
    """返回供 llm_stream.stream_context 使用的事件写入回调。"""

    def _sink(event: dict[str, Any]) -> None:
        append_event(job_id, event)

    return _sink


def finalize_job(
    job_id: str,
    *,
    status: str,
    done: Optional[int] = None,
    total: Optional[int] = None,
    detail: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """将任务收口并强制以压缩形态落盘。"""
    update_job_meta(
        job_id,
        status=status,
        done=done,
        total=total,
        detail=detail,
        error=error,
        force_flush=True,
    )


def _format_snapshot(
    data: dict[str, Any],
    *,
    after: int = 0,
    live_check: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    job_id = str(data.get("job_id") or "")
    status = str(data.get("status") or "error")
    error = str(data.get("error") or "")
    raw_events = list(data.get("events") or [])

    interrupted = status == "running" and not (live_check and live_check(job_id))
    events = [dict(ev) for ev in raw_events]
    if interrupted:
        status = "error"
        if not error:
            error = _INTERRUPTED_MSG
        next_idx = (int(events[-1]["seq"]) + 1) if events else 0
        if not (events and events[-1].get("text") == _INTERRUPTED_MSG):
            events.append(
                {
                    "seq": next_idx,
                    "ts": float(data.get("updated_at") or time.time()),
                    "kind": "warn",
                    "unit": "system",
                    "label": "系统提示",
                    "text": _INTERRUPTED_MSG,
                }
            )

    stream_seq = (int(events[-1]["seq"]) + 1) if events else 0
    if "next_seq" in data:
        stream_seq = max(stream_seq, int(data["next_seq"]))
    sliced = [ev for ev in events if int(ev.get("seq", -1)) >= after]
    return {
        "job_id": job_id,
        "dim": str(data.get("dim") or ""),
        "dim_label": str(data.get("dim_label") or data.get("dim") or ""),
        "status": status,
        "done": int(data.get("done") or 0),
        "total": int(data.get("total") or 0),
        "detail": str(data.get("detail") or ""),
        "error": error,
        "scope_id": str(data.get("scope_id") or ""),
        "created_at": float(data.get("_created") or data.get("created_at") or 0.0),
        "updated_at": float(data.get("updated_at") or 0.0),
        "stream_seq": stream_seq,
        "events": sliced,
    }


def _read_disk_record(path: str) -> Optional[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def get_job_snapshot(
    job_id: str,
    *,
    sid: str,
    chat_hash: str,
    after: int = 0,
    live_check: Optional[Callable[[str], bool]] = None,
) -> Optional[dict[str, Any]]:
    """读取单个任务的流快照（先查内存，再查磁盘，按 sid_hash 与 chat_hash 校验）。"""
    if not (_is_safe_segment(job_id) and _is_safe_segment(chat_hash)):
        return None
    if _is_purged(chat_hash):
        return None
    want_sid = sid_digest(sid)
    after_val = max(0, int(after or 0))

    with _STREAM_LOCK:
        rec = _STREAMS.get(job_id)
        if rec is not None:
            if rec.get("chat_hash") != chat_hash or rec.get("sid_hash") != want_sid:
                return None
            snap_src = {
                "job_id": rec["job_id"],
                "dim": rec["dim"],
                "dim_label": rec["dim_label"],
                "status": rec["status"],
                "done": rec["done"],
                "total": rec["total"],
                "detail": rec["detail"],
                "error": rec["error"],
                "scope_id": rec["scope_id"],
                "created_at": rec["created_at"],
                "updated_at": rec["updated_at"],
                "next_seq": rec["next_seq"],
                "events": list(rec["events"]),
            }
            return _format_snapshot(
                snap_src,
                after=after_val,
                live_check=live_check or (lambda _jid: True),
            )

    path = _log_path(chat_hash, job_id)
    if not path:
        return None
    data = _read_disk_record(path)
    if not data:
        return None
    if data.get("chat_hash") != chat_hash or data.get("sid_hash") != want_sid:
        return None
    return _format_snapshot(data, after=after_val, live_check=live_check)


def find_latest_job(
    *,
    sid: str,
    chat_hash: str,
    scope_id: str = "",
    after: int = 0,
    live_check: Optional[Callable[[str], bool]] = None,
) -> Optional[dict[str, Any]]:
    """按 (sid, chat_hash, scope_id) 查找最新分析任务及输出流，用于页面重载恢复。"""
    if not _is_safe_segment(chat_hash) or _is_purged(chat_hash):
        return None
    want_sid = sid_digest(sid)
    want_scope = str(scope_id or "")
    after_val = max(0, int(after or 0))

    best_running: Optional[dict[str, Any]] = None
    best_finished: Optional[dict[str, Any]] = None
    seen_ids: set[str] = set()

    with _STREAM_LOCK:
        mem_records = list(_STREAMS.values())

    for rec in mem_records:
        if rec.get("chat_hash") != chat_hash or rec.get("sid_hash") != want_sid:
            continue
        if str(rec.get("scope_id") or "") != want_scope:
            continue
        seen_ids.add(rec["job_id"])
        with _STREAM_LOCK:
            cur = _STREAMS.get(rec["job_id"])
            if cur is None:
                continue
            snap = _format_snapshot(
                {
                    "job_id": cur["job_id"],
                    "dim": cur["dim"],
                    "dim_label": cur["dim_label"],
                    "status": cur["status"],
                    "done": cur["done"],
                    "total": cur["total"],
                    "detail": cur["detail"],
                    "error": cur["error"],
                    "scope_id": cur["scope_id"],
                    "created_at": cur["created_at"],
                    "updated_at": cur["updated_at"],
                    "next_seq": cur["next_seq"],
                    "events": list(cur["events"]),
                },
                after=after_val,
                live_check=live_check or (lambda _jid: True),
            )
        if snap["status"] == "running":
            if best_running is None or snap["updated_at"] > best_running["updated_at"]:
                best_running = snap
        else:
            if best_finished is None or snap["updated_at"] > best_finished["updated_at"]:
                best_finished = snap

    if best_running is not None:
        return best_running

    cache_dir = _ai_cache_dir()
    if cache_dir:
        prefix = f"joblog_{chat_hash}_"
        try:
            entries = os.listdir(cache_dir)
        except OSError:
            entries = []
        for name in entries:
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            jid = name[len(prefix) : -len(".json")]
            if not _is_safe_segment(jid) or jid in seen_ids:
                continue
            data = _read_disk_record(os.path.join(cache_dir, name))
            if not data:
                continue
            if data.get("chat_hash") != chat_hash or data.get("sid_hash") != want_sid:
                continue
            if str(data.get("scope_id") or "") != want_scope:
                continue
            snap = _format_snapshot(data, after=after_val, live_check=live_check)
            if snap["status"] == "running":
                if best_running is None or snap["updated_at"] > best_running["updated_at"]:
                    best_running = snap
            else:
                if best_finished is None or snap["updated_at"] > best_finished["updated_at"]:
                    best_finished = snap

    return best_running or best_finished


def purge_chat(chat_hash: str) -> int:
    """删除内存中属于指定聊天的所有任务流记录（磁盘文件由 _purge_chat_caches 清扫）。"""
    if not chat_hash:
        return 0
    removed = 0
    with _STREAM_LOCK:
        to_drop = [jid for jid, rec in _STREAMS.items() if rec.get("chat_hash") == chat_hash]
        for jid in to_drop:
            _STREAMS.pop(jid, None)
            removed += 1
    return removed


def purge_session(sid: str, chat_hash: str) -> int:
    """删除归属于指定会话 + 聊天的任务输出流（不牵连其他复用同一聊天的会话）。"""
    if not (sid and _is_safe_segment(chat_hash)):
        return 0
    want_sid = sid_digest(sid)
    removed = 0
    with _STREAM_LOCK:
        to_drop = [
            jid
            for jid, rec in _STREAMS.items()
            if rec.get("chat_hash") == chat_hash and rec.get("sid_hash") == want_sid
        ]
        for jid in to_drop:
            _STREAMS.pop(jid, None)
            removed += 1
    cache_dir = _ai_cache_dir()
    if cache_dir:
        prefix = f"joblog_{chat_hash}_"
        try:
            entries = os.listdir(cache_dir)
        except OSError:
            entries = []
        for name in entries:
            if not (name.startswith(prefix) and name.endswith(".json")):
                continue
            path = os.path.join(cache_dir, name)
            data = _read_disk_record(path)
            if not data or data.get("sid_hash") != want_sid:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed
