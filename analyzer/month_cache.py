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

"""月份级增量缓存：内容寻址键 + manifest 引用计数 + 孤儿回收。

注意 `_MONTH_CACHE_DIR` / `_MANIFEST_KEYS` 等模块状态须直接在本模块上读写或打桩，
不要改写 `deepseek_client` 中的兼容重导出别名。
"""

import hashlib
import json
import os
import re
import threading
import time
from typing import Iterable, Optional

from config import env_number
from analyzer import llm_runtime
from analyzer import purge_marks
from analyzer.cache_policy import refreshing, refresh_cancelled
from analyzer.atomic_write import tmp_sibling, write_json_atomic
from analyzer.logger import get_logger
from analyzer.result_schema import CachedModelResult

logger = get_logger("deepseek")

_MONTH_CACHE_DIR = ""
_MONTH_CACHE_LOCK = threading.Lock()
# manifest 已引用月份的 mtime 缓存：{manifest 文件名: (mtime, keys)}，由 _MONTH_CACHE_LOCK 保护
_MANIFEST_KEYS: dict[str, tuple[float, set]] = {}
_WRITE_WARN_INTERVAL = 300.0
_last_write_warning = [0.0]
# 无引用月份文件保留宽限期，便于重新导出追加月份时复用缓存，过期后由定期清理回收
MONTH_CACHE_GRACE_SECONDS = env_number("LLM_MONTH_CACHE_GRACE_HOURS", 24, 0, 24 * 30) * 3600

#: 月份缓存键安全字符集（防路径穿越/分隔符注入）
_MONTH_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_month_key(key) -> bool:
    """校验月份键是否可安全拼入 `month_<key>.json`，防止外部导入 manifest 导致路径穿越。"""
    return isinstance(key, str) and bool(_MONTH_KEY_RE.fullmatch(key))


def _tmp_sibling(path: str) -> str:
    """原子写入的临时文件名：**必须唯一**，不能用固定的 `f"{path}.tmp"`。

    口径只有 `analyzer.atomic_write.tmp_sibling` 一份（那里写着为什么）；保留这个函数名
    是因为用例把它当契约（tests/test_atomic_write.py 钉"临时名唯一且仍被清理侧认出"）。
    """
    return tmp_sibling(path)


def configure_month_cache(directory: str) -> None:
    """由应用层注入缓存目录；传空字符串即关闭月份级缓存"""
    global _MONTH_CACHE_DIR
    _MONTH_CACHE_DIR = directory or ""
    # 目录换了，上一个目录的 manifest 缓存必须丢弃（键只是文件名，会张冠李戴）
    with _MONTH_CACHE_LOCK:
        _MANIFEST_KEYS.clear()


def month_cache_enabled() -> bool:
    """返回月份级缓存是否已由应用层配置。"""
    return bool(_MONTH_CACHE_DIR)


def _month_key(system_prompt: str, user_content: str, fingerprint: "str | None" = None) -> str:
    """月份缓存的键：任何影响该月输出的因素（模型/提示词/格式/对话文本）都进哈希。

    fingerprint 默认取私聊指纹（既有行为，键值与升级前完全一致）；群聊维度传
    group_prompt_fingerprint()，两类月份的缓存互不干扰——私聊月份也不会因为
    新增群聊提示词而变成"无引用"被回收。

    延迟导入 deepseek_client：模型名与提示词指纹由 API 层拥有，而那一层反过来要 import
    本模块（它才是这些函数的调用方），模块级互相导入会成环。取的是**调用时**的值，
    所以打桩 deepseek_client.DEEPSEEK_MODEL / PROMPT_FINGERPRINT 依然生效。
    """
    digest = hashlib.sha256()
    for part in (
        llm_runtime.DEEPSEEK_MODEL,
        fingerprint or llm_runtime.default_prompt_fingerprint(),
        system_prompt,
        user_content,
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:20]


def month_cache_path(key: str) -> str:
    """构造月份缓存路径；非法 key 记录告警并返回空串（调用方以 OSError/False 安全跳过）。"""
    if not is_safe_month_key(key):
        logger.warning("月份缓存键形状非法，已拒绝拼路径（可能来自被篡改的 manifest）")
        return ""
    return os.path.join(_MONTH_CACHE_DIR, f"month_{key}.json")


def _manifest_path(chat_hash: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"manifest_{chat_hash}.json")


def _read_month_cache(
    key: str,
    expect_thinking: "Optional[bool]" = None,
    chat_hash: str = "",
    *,
    read_only: bool = False,
) -> Optional[dict]:
    """读取月份缓存并校验 `_thinking` 口径（无标记老文件首次命中后补写 `_thinking`）。"""
    if not _MONTH_CACHE_DIR or (refreshing() and not read_only):
        return None
    path = month_cache_path(key)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        created = data.pop("_created", None)
        stamped = data.pop("_thinking", None)
    else:
        return None
    if expect_thinking is not None and stamped is not None and bool(stamped) != bool(expect_thinking):
        logger.info("月份缓存是另一种思考模式算出来的，本轮重新分析: %s", key[:12])
        return None
    if not read_only:
        try:
            os.utime(path, None)  # 命中续期 mtime（30 天滑动窗口）
        except OSError:
            pass
    if not read_only and expect_thinking is not None and stamped is None:
        _restamp_month_cache(path, data, bool(expect_thinking), created, chat_hash)
    return CachedModelResult(data)


def _restamp_month_cache(path: str, data: dict, thinking: bool, created, chat_hash: str = "") -> None:
    """为无 `_thinking` 标记的老文件补写口径；写入前核对清理标记与文件存在性以防复活。"""
    payload = {
        "_created": created if created is not None else time.time(),
        "_thinking": bool(thinking),
        **data,
    }
    try:
        if chat_hash and purge_marks.is_marked(chat_hash):
            logger.info("该聊天的缓存刚被清理，跳过月份缓存补标记（不复活已删数据）")
            return
        if not os.path.exists(path):
            return
        write_json_atomic(path, payload)
    except OSError as e:
        _warn_write_failure("月份缓存补标记", path, e)


def _warn_write_failure(what: str, path: str, err: OSError) -> None:
    """记录缓存落盘失败告警（按 5 分钟去抖，避免磁盘满时刷屏）。"""
    now = time.monotonic()
    if now - _last_write_warning[0] < _WRITE_WARN_INTERVAL:
        return
    _last_write_warning[0] = now
    logger.warning("%s写入失败（缓存不生效，可能重复调用 API）: %s (%s)", what, path, err)


def _write_month_cache(key: str, result: dict, thinking: "Optional[bool]" = None) -> None:
    if not _MONTH_CACHE_DIR or refresh_cancelled():
        return
    path = month_cache_path(key)
    if not path:
        return
    # 将 _created 置于头部供 read_created_at 有界扫描执行 90 天硬上限回收；_thinking 由本层签发
    payload = dict(result) if isinstance(result, dict) else {"result": result}
    payload.pop("_created", None)
    payload.pop("_thinking", None)
    stamp = {"_thinking": bool(thinking)} if thinking is not None else {}
    try:
        write_json_atomic(path, {"_created": time.time(), **stamp, **payload}, mkdir=_MONTH_CACHE_DIR)
    except OSError as e:
        _warn_write_failure("月份缓存", path, e)


def _record_month_usage(chat_hash: str, keys: "Iterable[str]") -> None:
    """批量将本次分析用到的安全月份键合并写入 manifest 供引用计数与清理。"""
    if not _MONTH_CACHE_DIR or not chat_hash:
        return
    # 已被清理的聊天不再重建 manifest（需等待重传撤销 purge mark）
    if purge_marks.is_marked(chat_hash):
        logger.info("该聊天的缓存刚被清理，跳过 manifest 重建（月份文件交由孤儿回收处理）")
        return
    new_keys = {str(k) for k in keys if is_safe_month_key(k)}
    if not new_keys:
        return
    path = _manifest_path(chat_hash)
    name = os.path.basename(path)
    with _MONTH_CACHE_LOCK:
        data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
        merged = set(data.get("months") or [])
        merged |= new_keys
        merged = {k for k in merged if is_safe_month_key(k)}  # 写回时顺手洗掉非法历史键
        data["months"] = sorted(merged)
        data["updated"] = time.time()
        # _created 沿用首次创建时间并置顶，避免改写 manifest 延后 90 天绝对上限
        data["_created"] = data.get("_created") or time.time()
        payload = {"_created": data.pop("_created"), **data}
        try:
            write_json_atomic(path, payload, mkdir=_MONTH_CACHE_DIR)
        except OSError as e:
            _warn_write_failure("月份缓存 manifest", path, e)
            _MANIFEST_KEYS.pop(name, None)
        else:
            # 兜住并发清理窗口：写入与 getmtime 之间 manifest 可能刚被删除
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            _MANIFEST_KEYS[name] = (mtime, set(data["months"]))


def purge_month_cache(chat_hash: str) -> int:
    """删除当前聊天的 manifest，并清理已过宽限期且不再被其它 manifest 引用的月份缓存。"""
    if not _MONTH_CACHE_DIR or not chat_hash:
        return 0
    path = _manifest_path(chat_hash)
    name = os.path.basename(path)

    removed = 0
    with _MONTH_CACHE_LOCK:
        mine = _manifest_keys_locked(name)
        others = _referenced_keys_locked(exclude=name)
        now = time.time()
        for key in mine - others:
            target = month_cache_path(key)
            if not target:  # 非法 key（被篡改的 manifest）：绝不按它去 os.remove
                continue
            try:
                if now - os.path.getmtime(target) < MONTH_CACHE_GRACE_SECONDS:
                    continue  # 宽限期内：留给增量分析复用
                os.remove(target)
                removed += 1
            except OSError:
                pass
    try:
        os.remove(path)
    except OSError:
        pass
    with _MONTH_CACHE_LOCK:
        _MANIFEST_KEYS.pop(name, None)
    return removed


def _manifest_keys_locked(name: str) -> set:
    """（调用方须持有 _MONTH_CACHE_LOCK）单个 manifest 引用的月份 key 集合

    带 mtime 缓存：manifest 只由本模块写，写路径会同步刷缓存，所以命中时直接用。

    **读进来就按形状过滤**：`months` 是磁盘内容，而 manifest 可以由「导入结果包」
    写进本机（见 is_safe_month_key 里那段后果说明）。过滤放在这里，等于让"引用集"
    这个下游一切判断（回收、导出、钉引用）都只可能看到安全 key；非法项当成不存在，
    而不是把它带到 `os.path.join` 上去。
    """
    path = os.path.join(_MONTH_CACHE_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _MANIFEST_KEYS.pop(name, None)
        return set()
    cached = _MANIFEST_KEYS.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_keys = json.load(f).get("months") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        raw_keys = []
    keys = {k for k in raw_keys if is_safe_month_key(k)}
    _MANIFEST_KEYS[name] = (mtime, keys)
    return keys


def _referenced_keys_locked(exclude: str = "") -> set:
    """（调用方须持有 _MONTH_CACHE_LOCK）所有 manifest 引用到的月份 key"""
    keys: set = set()
    try:
        names = os.listdir(_MONTH_CACHE_DIR)
    except OSError:
        return keys
    live = {n for n in names if n.startswith("manifest_") and n.endswith(".json")}
    for name in live:
        if name == exclude:
            continue
        keys |= _manifest_keys_locked(name)
    # 目录里已经没有的 manifest，其缓存条目顺手清掉，避免随历史会话无限增长
    if len(_MANIFEST_KEYS) > len(live):
        for stale in [n for n in _MANIFEST_KEYS if n not in live]:
            _MANIFEST_KEYS.pop(stale, None)
    return keys


def sweep_orphan_month_cache() -> int:
    """回收"已无 manifest 引用且超过宽限期"的月份缓存（定期清理时调用）"""
    if not _MONTH_CACHE_DIR:
        return 0
    removed = 0
    with _MONTH_CACHE_LOCK:
        referenced = _referenced_keys_locked()
        now = time.time()
        try:
            names = os.listdir(_MONTH_CACHE_DIR)
        except OSError:
            return 0
        for name in names:
            if not name.startswith("month_") or not name.endswith(".json"):
                continue
            key = name[len("month_") : -len(".json")]
            if key in referenced:
                continue
            target = os.path.join(_MONTH_CACHE_DIR, name)
            try:
                if now - os.path.getmtime(target) >= MONTH_CACHE_GRACE_SECONDS:
                    os.remove(target)
                    removed += 1
            except OSError:
                continue
    return removed
