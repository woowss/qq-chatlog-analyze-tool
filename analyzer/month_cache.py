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

"""月份级增量缓存：内容寻址的键 + manifest 引用计数 + 孤儿回收

从 analyzer/deepseek_client.py 拆出。维度级缓存以"整份文件哈希"为键，导出文件只要多
一个月，历史月份就会全部重跑；月份级缓存改用内容寻址（模型 + 提示词指纹 + system prompt
+ 该月对话文本），于是重新导出同一段对话时历史月份直接命中，只为新增月份付费。

清理靠引用计数：每个聊天文件对应 manifest_<chat_hash>.json，记录它用过哪些月份文件；
该聊天被替换/删除时删掉 manifest，并只回收"没有其他 manifest 引用且已过宽限期"的月份文件
——宽限期是必要的，因为"同一段对话又多了几个月"正是最需要复用缓存的场景。

**注意本模块的模块级状态**（_MONTH_CACHE_DIR / _MANIFEST_KEYS / _last_write_warning 等）：
测试要打桩或读写它，请打在本模块上，而不是 deepseek_client 的重导出别名上
（那里的 _MONTH_CACHE_DIR 只是搬迁瞬间的值拷贝，改它不会影响这里的逻辑）。
"""

import hashlib
import json
import os
import threading
import time
from typing import Iterable, Optional

from config import env_number
from analyzer.logger import get_logger

logger = get_logger("deepseek")


# ---------------------------------------------------------------------------
# 月份级缓存（增量分析）
# ---------------------------------------------------------------------------
# 维度级缓存以"整份文件哈希"为键：导出文件只要多一个月，历史月份会全部重跑。
# 月份级缓存改用内容寻址键（模型 + 提示词指纹 + 系统提示词 + 该月对话文本），
# 于是重新导出同一段对话时历史月份直接命中，只为新增月份付费。
#
# 清理：每个聊天文件对应 manifest_{chat_hash}.json，记录它用过哪些月份文件；
# 该聊天被替换/删除时删掉 manifest，并只回收"没有其他 manifest 引用"的月份文件，
# 从而保留"不留孤儿敏感数据"的隐私属性。
_MONTH_CACHE_DIR = ""
_MONTH_CACHE_LOCK = threading.Lock()
# manifest 已引用月份的进程级缓存：{manifest 文件名: (mtime, keys)}。
# _referenced_keys_locked 会被 purge（每次上传）与 sweep（定期清理）调用，原实现
# 每次都要把目录下所有 manifest 完整读一遍再做 json.loads——分析过的聊天越多越慢。
# 这里缓存结果并用 mtime 校验：文件没被改过（stat 比"读文件 + 解析"便宜一个量级）
# 就直接复用。写 manifest 的那一处会同步更新缓存，不依赖 mtime 精度。
# 受 _MONTH_CACHE_LOCK 保护。
_MANIFEST_KEYS: dict[str, tuple[float, set]] = {}
# 缓存写失败的告警去抖（磁盘满时每次调用都会失败，不能每次刷一行）
_WRITE_WARN_INTERVAL = 300.0
_last_write_warning = [0.0]
# 无引用的月份缓存先留一段宽限期：上传新文件时的级联清理不能顺手删掉
# "同一段对话的历史月份"，否则增量分析就失去意义。孤儿文件由定期清理回收。
MONTH_CACHE_GRACE_SECONDS = env_number("LLM_MONTH_CACHE_GRACE_HOURS", 24, 0, 24 * 30) * 3600


def configure_month_cache(directory: str) -> None:
    """由应用层注入缓存目录；传空字符串即关闭月份级缓存"""
    global _MONTH_CACHE_DIR
    _MONTH_CACHE_DIR = directory or ""
    # 目录换了，上一个目录的 manifest 缓存必须丢弃（键只是文件名，会张冠李戴）
    with _MONTH_CACHE_LOCK:
        _MANIFEST_KEYS.clear()


def _month_key(system_prompt: str, user_content: str, fingerprint: "str | None" = None) -> str:
    """月份缓存的键：任何影响该月输出的因素（模型/提示词/格式/对话文本）都进哈希。

    fingerprint 默认取私聊指纹（既有行为，键值与升级前完全一致）；群聊维度传
    group_prompt_fingerprint()，两类月份的缓存互不干扰——私聊月份也不会因为
    新增群聊提示词而变成"无引用"被回收。

    延迟导入 deepseek_client：模型名与提示词指纹由 API 层拥有，而那一层反过来要 import
    本模块（它才是这些函数的调用方），模块级互相导入会成环。取的是**调用时**的值，
    所以打桩 deepseek_client.DEEPSEEK_MODEL / PROMPT_FINGERPRINT 依然生效。
    """
    from analyzer import deepseek_client as dc

    digest = hashlib.sha256()
    for part in (dc.DEEPSEEK_MODEL, fingerprint or dc.PROMPT_FINGERPRINT, system_prompt, user_content):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:20]


def month_cache_path(key: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"month_{key}.json")


def _manifest_path(chat_hash: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"manifest_{chat_hash}.json")


def _read_month_cache(key: str) -> Optional[dict]:
    if not _MONTH_CACHE_DIR:
        return None
    path = month_cache_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data.pop("_created", None)  # 元数据不进调用方拿到的结果
    try:
        os.utime(path, None)  # 命中即续期，避免常用缓存被 30 天 TTL 回收
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _warn_write_failure(what: str, path: str, err: OSError) -> None:
    """缓存落盘失败必须出声。

    静默吞掉 OSError 的后果不是"少一个文件"，而是月份缓存与 manifest 从此写不进去：
    用户以为命中了缓存，实际上每个月都在重复付费，且界面上完全看不出来。
    磁盘满时会高频失败，所以按 5 分钟去抖，避免刷爆日志。
    """
    now = time.monotonic()
    if now - _last_write_warning[0] < _WRITE_WARN_INTERVAL:
        return
    _last_write_warning[0] = now
    logger.warning("%s写入失败（缓存不生效，可能重复调用 API）: %s (%s)", what, path, err)


def _write_month_cache(key: str, result: dict) -> None:
    if not _MONTH_CACHE_DIR:
        return
    path = month_cache_path(key)
    tmp = f"{path}.tmp"
    try:
        # _created 是"绝对 90 天"硬上限的依据（cleanup 读它）。缺了它就只能按 mtime 判，
        # 而 mtime 在每次命中时被续期（见 _read_month_cache）——含聊天原句引用的这族
        # 缓存会因此无限期留存。读侧会把它 pop 掉，调用方拿到的结果不变。
        # 放在**最前面**写：清理任务只扫文件头就能取到，不必整份解析这些最敏感的月份文件
        # （见 webapp.store.read_created_at）。
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        payload.pop("_created", None)
        # 目录可能被用户按 README 的指引删掉来"彻底清除数据"，而服务还开着：
        # 这里不补目录，月份缓存从此再也写不进去，增量分析静默失效（每月重复付费）。
        os.makedirs(_MONTH_CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"_created": time.time(), **payload}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        _warn_write_failure("月份缓存", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _record_month_usage(chat_hash: str, keys: "Iterable[str]") -> None:
    """把这一批用到的月份缓存记进该聊天的 manifest，供级联清理做引用计数。

    调用方按"一次分析"批量传入（见 _analyze_periods）：原先每完成一个月就
    「读 manifest → 改 → 写回」，24 个月就是 48 次文件 I/O，而写进去的内容
    只是同一个集合在变大。现在整个维度只读一次、写一次。
    """
    if not _MONTH_CACHE_DIR or not chat_hash:
        return
    new_keys = set(keys)
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
        data["months"] = sorted(merged)
        data["updated"] = time.time()
        # setdefault 语义：绝对上限看的是"首次创建"，重写 manifest 不该把它续期。
        # 重排到最前面写，让清理任务只扫文件头就能取到（见 webapp.store.read_created_at）。
        data["_created"] = data.get("_created") or time.time()
        payload = {"_created": data.pop("_created"), **data}
        tmp = f"{path}.tmp"
        try:
            # 同 _write_month_cache：manifest 写不进去 = 这些月份文件会变成"无引用"，
            # 宽限期后被孤儿回收删掉，增量分析白跑。
            os.makedirs(_MONTH_CACHE_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            # manifest 写不进去同样只影响"重新导出时能否复用历史月份"，
            # 但会让增量分析静默失效（每次都全量付费），所以也要出声
            _warn_write_failure("月份缓存 manifest", path, e)
            _MANIFEST_KEYS.pop(name, None)
        else:
            _MANIFEST_KEYS[name] = (os.path.getmtime(path), set(data["months"]))


def purge_month_cache(chat_hash: str) -> int:
    """删除该聊天的 manifest，并回收不再被任何 manifest 引用的月份缓存。

    这里必须带 **宽限期**：上传新文件时会立刻触发本函数，而"新文件其实是同一段
    对话又多了几个月"恰恰是最需要复用月份缓存的场景——立刻删除会让增量分析失效。
    因此只回收"无引用 **且** 已超过 MONTH_CACHE_GRACE_SECONDS 未被动过"的文件；
    换成完全不同的对话时，旧的月份文件也会在宽限期后被 sweep_orphan_month_cache 收走。
    """
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
            keys = set(json.load(f).get("months") or [])
    except (OSError, json.JSONDecodeError):
        keys = set()
    _MANIFEST_KEYS[name] = (mtime, keys)
    return keys


def _referenced_keys_locked(exclude: str = "") -> set:
    """（调用方须持有 _MONTH_CACHE_LOCK）所有 manifest 引用到的月份 key"""
    keys: set = set()
    try:
        names = os.listdir(_MONTH_CACHE_DIR)
    except OSError:
        return keys
    live = {n for n in names if n.startswith("manifest_")}
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
