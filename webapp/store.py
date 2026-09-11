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
"""持久层：聊天哈希、进程内 ChatData 复用、统计缓存、AI 维度缓存

从 app.py 拆出。函数读取本模块全局（AI_CACHE_DIR / STATS_CACHE_DIR /
thinking_enabled / _save_stats ...）——测试打桩请打在 webapp.store 上。
所有落盘都走"临时文件 + os.replace"，进程中断不会留下半截 JSON。
"""
import hashlib
import json
import os
import threading
import time
from collections import deque

from flask import session

from config import AI_CACHE_DIR, DEEPSEEK_MODEL, STATS_CACHE_DIR
from parser.qq_parser import load_chat
from analyzer.local_stats import (
    calc_overview, calc_daily_counts, calc_hourly_distribution,
    calc_weekly_distribution, calc_message_length_stats, calc_face_stats,
    calc_response_time, calc_exchange_rounds, calc_weekly_activity,
    calc_word_freq, calc_milestones,
)
from analyzer.deepseek_client import (
    PROMPT_FINGERPRINT, purge_month_cache, thinking_enabled)
from analyzer.logger import get_logger

logger = get_logger("app")


# ---------------------------------------------------------------------------
# 聊天文件内容哈希
# ---------------------------------------------------------------------------


def _chat_hash(filepath: str) -> str:
    """聊天文件内容哈希（前 16 位），作为缓存键的一部分"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def save_and_hash(file_storage, dest_path: str) -> tuple[int, str]:
    """保存上传文件的同时增量算哈希：50MB 文件少一趟完整重读。

    返回 (字节数, 内容哈希[:16])；流不可回退时降级为落盘后 _chat_hash。
    """
    h = hashlib.sha256()
    size = 0
    try:
        stream = file_storage.stream
        try:
            stream.seek(0)
        except (OSError, ValueError):
            pass
        with open(dest_path, "wb") as out:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except OSError:
        # 流式读写中途出错：回退到标准保存。注意先把流拨回起点——
        # save() 是从流当前位置复制的，不回拨会落下一个"半截 JSON"。
        try:
            file_storage.stream.seek(0)
        except (OSError, ValueError, AttributeError):
            pass
        file_storage.save(dest_path)
        size = os.path.getsize(dest_path)
        return size, _chat_hash(dest_path)
    if not size:                      # 空流（防御：让旧路径兜底而非静默哈希空串）
        return size, _chat_hash(dest_path)
    return size, h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 已解析聊天数据的进程内复用（一次全量分析原本要为每个维度重新解析一遍）
# ---------------------------------------------------------------------------

_CHAT_CACHE: dict[tuple, object] = {}
_CHAT_CACHE_LOCK = threading.Lock()


def _load_chat_cached(filepath: str):
    """按 (路径, mtime, 大小) 复用已解析的 ChatData；只保留最近一份，避免大文件堆积"""
    try:
        st = os.stat(filepath)
        key = (os.path.abspath(filepath), st.st_mtime_ns, st.st_size)
    except OSError:
        return load_chat(filepath)
    with _CHAT_CACHE_LOCK:
        cached = _CHAT_CACHE.get(key)
    if cached is not None:
        return cached
    chat = load_chat(filepath)
    with _CHAT_CACHE_LOCK:
        _CHAT_CACHE.clear()
        _CHAT_CACHE[key] = chat
    return chat


# ---------------------------------------------------------------------------
# 本地统计结果的磁盘缓存（内容寻址，与 AI 缓存同生命周期）
# ---------------------------------------------------------------------------

# 统计结果的结构版本：字段形状变了就 +1，老缓存会被判为过期并重算（避免模板 500）
# v3：overview 增加 total_files / total_videos / total_forwards / total_other_media
# v4：overview 增加 image_bytes / media_bytes / unique_images（媒体体积与去重）
STATS_SCHEMA_VERSION = 4

# 统计计算挪出请求线程后的在跑任务：chat_hash -> Thread
_STATS_THREADS: dict[str, threading.Thread] = {}
_STATS_LOCK = threading.Lock()
# 等待后台统计的最长时间（首屏可以稍微等一会儿，好过把"计算中"甩给用户）
STATS_WAIT_SECONDS = 20.0
# 统计失败的哈希 -> 失败原因：异步化之后错误不再有 HTTP 响应可承载，
# 不记下来的话用户只会看到"上传成功但仪表盘又跳回首页"，无从排查。
_STATS_ERRORS: dict[str, str] = {}
# 最近被级联清理过的哈希：防止仍在跑的统计线程把结果"复活"成孤儿缓存
_RECENTLY_PURGED: deque = deque(maxlen=256)


def stats_error(chat_hash: str) -> str:
    """该聊天上次统计失败的原因（没有则空串），供首页提示用"""
    return _STATS_ERRORS.get(chat_hash or "", "")


def vision_enabled() -> bool:
    """是否要做图片理解（开关开着且有可用图片来源）。

    延迟到调用点判断，避免 store→vision 的模块级循环依赖。
    """
    from analyzer import vision
    return vision.VISION_ENABLED and vision.VISION_MAX_PER_MONTH > 0


def _stats_path(chat_hash: str) -> str:
    return os.path.join(STATS_CACHE_DIR, f"stats_{chat_hash}.json")


def _load_stats(chat_hash: str):
    if not chat_hash:
        return None
    try:
        with open(_stats_path(chat_hash), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or "overview" not in data
            or data.get("_v") != STATS_SCHEMA_VERSION):
        return None
    try:
        os.utime(_stats_path(chat_hash), None)
    except OSError:
        pass
    return data


def _save_stats(chat_hash: str, stats: dict) -> None:
    if not chat_hash:
        return
    os.makedirs(STATS_CACHE_DIR, exist_ok=True)
    path = _stats_path(chat_hash)
    tmp = f"{path}.tmp"
    payload = dict(stats)
    payload["_v"] = STATS_SCHEMA_VERSION
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("统计缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _delete_stats(chat_hash: str) -> None:
    if not chat_hash:
        return
    try:
        os.remove(_stats_path(chat_hash))
    except OSError:
        pass


def compute_stats(chat) -> dict:
    """十项本地统计（词频除外：jieba 是耗时大头，由 habits 页懒算）"""
    return {
        "overview": calc_overview(chat),
        "daily_counts": calc_daily_counts(chat),
        "hourly_dist": calc_hourly_distribution(chat),
        "weekly_dist": calc_weekly_distribution(chat),
        "length_stats": calc_message_length_stats(chat),
        "face_stats": calc_face_stats(chat),
        "response_time": calc_response_time(chat),
        "exchange_rounds": calc_exchange_rounds(chat),
        "weekly_activity": calc_weekly_activity(chat),
        "milestones": calc_milestones(chat),
    }


def start_stats_job(chat, chat_hash: str) -> None:
    """把统计计算放进后台线程：上传请求只做「保存 + 解析校验」，尽快返回。

    50MB 记录的十项统计要 0.2~1s+，压在请求线程上纯属让浏览器干转圈；
    结果是确定性的、又要落盘复用，天然适合异步。首个页面请求经
    _current_stats() 短暂 join，用户几乎无感。
    """
    if not chat_hash:
        return
    # 用户清掉后又重新上传同一份文件：这是新会话的正当计算，撤销旧的"已清理"标记
    try:
        _RECENTLY_PURGED.remove(chat_hash)
    except ValueError:
        pass
    with _STATS_LOCK:
        if chat_hash in _STATS_THREADS:
            return

    def _work():
        t0 = time.time()
        try:
            if _load_stats(chat_hash) is not None:
                return
            stats = compute_stats(chat)
            # 守卫必须放在计算之后、落盘之前复查：计算期间用户可能已清理该聊天，
            # 开算前查一次是不够的（那正是"晚到的线程复活孤儿缓存"的窗口）。
            if chat_hash in _RECENTLY_PURGED:
                logger.info("该聊天的缓存已被清理，放弃写入后台统计结果（避免孤儿）")
                return
            _save_stats(chat_hash, stats)
            _STATS_ERRORS.pop(chat_hash, None)
            logger.info("本地统计完成（%.0f ms，后台线程），已落盘复用",
                        (time.time() - t0) * 1000)
        except Exception as e:
            # 异步之后没有 HTTP 响应能承载这个错误：记在案，首页会提示用户
            _STATS_ERRORS[chat_hash] = f"{type(e).__name__}: {e}"
            while len(_STATS_ERRORS) > 64:          # 只留最近的失败记录
                _STATS_ERRORS.pop(next(iter(_STATS_ERRORS)), None)
            logger.error("本地统计失败（该文件统计结果不可用）: %s", e)
        finally:
            with _STATS_LOCK:
                _STATS_THREADS.pop(chat_hash, None)

    t = threading.Thread(target=_work, daemon=True, name=f"stats-{chat_hash[:8]}")
    with _STATS_LOCK:
        _STATS_THREADS[chat_hash] = t
    t.start()


def wait_for_stats(chat_hash: str, timeout: float = STATS_WAIT_SECONDS) -> None:
    """等某个聊天的后台统计跑完（没有在跑就直接返回）"""
    with _STATS_LOCK:
        t = _STATS_THREADS.get(chat_hash)
    if t is not None:
        t.join(timeout)


def _current_stats():
    """当前会话的统计数据（磁盘缓存；后台还在算则短暂等待），没有则返回 None"""
    chat_hash = session.get("chat_hash", "")
    stats = _load_stats(chat_hash)
    if stats is None and chat_hash:
        with _STATS_LOCK:
            busy = chat_hash in _STATS_THREADS
        if busy:
            wait_for_stats(chat_hash)
            stats = _load_stats(chat_hash)
    return stats


def _stats_with_word_freq(stats: dict, chat_hash: str):
    """词频按需计算并写回统计缓存：jieba 分词占统计耗时的大头（数万条约 0.9 秒）"""
    if stats is None or stats.get("word_freq"):
        return stats
    filepath = session.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return stats
    try:
        chat = _load_chat_cached(filepath)
        stats["word_freq"] = calc_word_freq(chat, top_n=80)
        _save_stats(chat_hash, stats)
    except Exception as e:                      # 词频失败不该拖垮页面
        logger.error("词频统计失败: %s", e)
        stats.setdefault("word_freq", {"self": [], "other": []})
    return stats


# ---------------------------------------------------------------------------
# AI 维度缓存（内容寻址 + 指纹键）
# ---------------------------------------------------------------------------


def _cache_path(dimension: str, chat_hash: str) -> str:
    # 键含提示词/格式指纹：PROMPT_FINGERPRINT 由 SYSTEM_PROMPT_* 与对话格式化函数
    # 自动哈希而来，改了提示词或输入格式后旧缓存自动失效（不再依赖人工 bump 版本号）。
    # 键含思考模式：同一模型开关 thinking 前后的结果差异很大，必须分开存放，
    # 否则切换 LLM_THINKING(_DIMS) 后会命中另一种模式的旧结果（看起来"没区别"）。
    # 非思考模式不加后缀，保持既有缓存键兼容。
    suffix = "_think" if thinking_enabled(dimension) else ""
    return os.path.join(AI_CACHE_DIR,
                        f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}_{PROMPT_FINGERPRINT}{suffix}.json")


def _purge_chat_caches(chat_hash: str) -> int:
    """删除某聊天文件的全部缓存（跨版本/模型）。聊天源文件被删时联动调用，
    避免派生的分析结果（含聊天内容摘要）成为孤儿残留。"""
    if not chat_hash:
        return 0
    removed = 0
    try:
        entries = os.listdir(AI_CACHE_DIR)
    except OSError:
        return 0
    for name in entries:
        if f"_{chat_hash}_" in name:
            try:
                os.remove(os.path.join(AI_CACHE_DIR, name))
                removed += 1
            except OSError:
                pass
    # 本地统计缓存
    try:
        os.remove(_stats_path(chat_hash))
        removed += 1
    except OSError:
        pass
    # 月份级缓存：删 manifest，并回收不再被其他聊天引用的月份文件
    removed += purge_month_cache(chat_hash)
    # 记下这次清理：若该哈希的后台统计线程还在跑，落盘前会检查这里并放弃写入，
    # 避免把刚清掉的缓存"复活"成没人认领的孤儿。
    _RECENTLY_PURGED.append(chat_hash)
    _STATS_ERRORS.pop(chat_hash, None)
    return removed


def _read_cache(dimension: str, chat_hash: str):
    path = _cache_path(dimension, chat_hash)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # 新格式带创建时间（供绝对上限清理用）；旧格式直接就是结果本身
    if isinstance(data, dict) and "_created" in data and "result" in data:
        data = data["result"]
    # 命中即续期：清理任务按 mtime 判断过期，只读不写会让天天用的缓存
    # 在 30 天后照样被删掉（然后重新花钱分析）
    try:
        os.utime(path, None)
    except OSError:
        pass
    return data


def _write_cache(dimension: str, chat_hash: str, result) -> None:
    """原子写入：临时文件 + os.replace，避免进程中断留下半截 JSON。

    记录创建时间：命中读取会刷新 mtime（滑动窗口续期），若只有 mtime，
    天天查看的结果将永远不会被回收，与"保留 30 天"的隐私承诺不符。
    """
    path = _cache_path(dimension, chat_hash)
    tmp = f"{path}.tmp"
    payload = {"_created": time.time(), "result": result}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("AI 缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
