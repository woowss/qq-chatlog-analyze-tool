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
"""持久层入口：聊天哈希、进程内 ChatData 复用、统计缓存、AI 维度缓存

从 app.py 拆出。运行时设置与兼容入口保留在本模块（AI_CACHE_DIR / STATS_CACHE_DIR /
thinking_enabled / _save_stats ...）；打包与任务历史委托给小模块，由这里注入当前配置、
锁和回调——测试打桩仍打在 webapp.store 上。所有落盘都走"临时文件 + os.replace"，
进程中断不会留下半截 JSON。
"""

import hashlib
import json
import os
import re
import threading
import time
from typing import Optional

from flask import session

from config import AI_CACHE_DIR, DEEPSEEK_MODEL, LOG_DIR, LOG_RETENTION_DAYS, STATS_CACHE_DIR
from parser.qq_parser import group_chat_mode, load_chat
from analyzer.local_stats import (
    calc_overview,
    calc_daily_counts,
    calc_hourly_distribution,
    calc_weekly_distribution,
    calc_message_length_stats,
    calc_face_stats,
    calc_response_time,
    calc_weekly_activity,
    calc_word_freq,
    stopwords_fingerprint,
    calc_milestones,
    calc_trends,
)
from analyzer.group_stats import compute_group_stats
from analyzer import month_cache, purge_marks
from analyzer.atomic_write import tmp_sibling, write_bytes_atomic, write_json_atomic, write_text_atomic
from analyzer.deepseek_client import (
    analysis_cache_fingerprint as analysis_cache_fingerprint,
    fingerprint_for_dimension,
    purge_month_cache,
    thinking_enabled,
)
from analyzer.logger import get_logger
from webapp import cache_bundle
from webapp import job_history

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


def _rewind(stream) -> bool:
    """把上传流拨回起点；不可回退时返回 False"""
    try:
        stream.seek(0)
    except (OSError, ValueError, AttributeError):
        return False
    return True


def save_and_hash(file_storage, dest_path: str) -> tuple[int, str]:
    """保存上传文件的同时增量算哈希：50MB 文件少一趟完整重读。

    返回 (字节数, 内容哈希[:16])。

    两条路径都必须先把流拨回起点：Werkzeug 的 save() 与本函数都从流的**当前**
    位置开始拷贝，而中途失败的自定义拷贝可能已经把位置推到一半——不回绕就会
    写出一份"从中间开始"的半截 JSON，而它在 HTTP 层看起来是上传成功的
    （要到解析阶段才报错，用户只会以为自己的导出文件坏了）。
    """
    stream = getattr(file_storage, "stream", None)

    # 快路径：自己流式拷贝，边读边算哈希
    if stream is not None and _rewind(stream):
        h = hashlib.sha256()
        size = 0
        ok = True
        try:
            with open(dest_path, "wb") as out:
                while True:
                    chunk = stream.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        except OSError:
            # 拷贝中途失败：半截文件作废。这里**不能**用已累计的 size/哈希充数，
            # 否则会把"从中间截断的 JSON"当成上传成功的完整文件返回。
            ok = False
        if ok:
            # 空流也在这里收口：文件已落成空文件，直接哈希它即可，
            # 不必为一个空文件再走一次 save()。
            return size, (h.hexdigest()[:16] if size else _chat_hash(dest_path))

    # 兜底：自定义拷贝失败（或根本没有 stream），交给 Werkzeug 的 save()。
    # 代价是落盘后要把文件整读一遍算哈希——这是出错路径，一次额外 I/O 换
    # "文件一定完整"是值得的。
    if stream is None or _rewind(stream):
        file_storage.save(dest_path)
        size = os.path.getsize(dest_path)
        return size, _chat_hash(dest_path)

    # 流既不可回绕、自定义拷贝也没成功：宁可报错，也不交出"可能是半个文件"的结果
    raise OSError("上传流不可回退，无法保证写入完整文件")


# ---------------------------------------------------------------------------
# 已解析聊天数据的进程内复用（一次全量分析原本要为每个维度重新解析一遍）
# ---------------------------------------------------------------------------

#: 统计缓存里记录"这份结果是用哪种口径算出来的"。定义在这里是因为进程内 ChatData
#: 缓存与统计缓存都要用；两处都必须能区分私聊/群聊口径，否则会串味。
STATS_MODE_PRIVATE = "private"
STATS_MODE_GROUP = "group"

_CHAT_CACHE: dict[tuple, object] = {}
_CHAT_CACHE_LOCK = threading.Lock()


def _load_chat_cached(filepath: str):
    """按 (路径, mtime, 大小, 当前模式) 复用已解析的 ChatData；只保留最近一份。

    键里带上模式是必要的：同一份文件在 QQCHAT_GROUP_CHAT 改动前后会被解析成不同
    对象（私聊 / 群聊 / 两方归并），只按文件属性做键会把上一次的模式结果复用出去。
    取的是**配置**模式而不是"这份文件实际被判成什么"——后者必须先解析才知道，
    那正好是我们要避免的开销；而配置模式一变，键就变，语义上已经足够。
    """
    try:
        st = os.stat(filepath)
        key = (os.path.abspath(filepath), st.st_mtime_ns, st.st_size, group_chat_mode())
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

# 统计结果结构版本号：私聊与群聊各自独立，不共用版本号（避免互相误触发重算）
STATS_SCHEMA_VERSION = 7
GROUP_STATS_SCHEMA_VERSION = 4

_STATS_THREADS: dict[str, threading.Thread] = {}
_STATS_LOCK = threading.Lock()
STATS_WAIT_SECONDS = 20.0
_STATS_ERRORS: dict[str, str] = {}
# _STATS_ERRORS 与 purge_marks 读写用独立锁，避免与等待后台线程 join 时产生锁竞争
_META_LOCK = threading.Lock()


def stats_error(chat_hash: str) -> str:
    """该聊天上次统计失败的原因（没有则空串），供首页提示用"""
    with _META_LOCK:
        return _STATS_ERRORS.get(chat_hash or "", "")


def _record_stats_error(chat_hash: str, message: str) -> None:
    with _META_LOCK:
        _STATS_ERRORS[chat_hash] = message
        while len(_STATS_ERRORS) > 64:  # 只留最近的失败记录
            _STATS_ERRORS.pop(next(iter(_STATS_ERRORS)), None)


def _clear_stats_error(chat_hash: str) -> None:
    with _META_LOCK:
        _STATS_ERRORS.pop(chat_hash, None)


def _mark_purged(chat_hash: str) -> None:
    purge_marks.mark(chat_hash)
    with _META_LOCK:
        _STATS_ERRORS.pop(chat_hash, None)


def _unmark_purged(chat_hash: str) -> None:
    purge_marks.unmark(chat_hash)


def _is_recently_purged(chat_hash: str) -> bool:
    return purge_marks.is_marked(chat_hash)


# 存活会话引用表：按内容哈希跟踪活跃会话，仅在无其它会话共用时才清理派生缓存（TTL 同会话 24h）
_LIVE_CHAT_REFS: dict[str, dict[str, float]] = {}
_LIVE_REFS_LOCK = threading.Lock()
LIVE_CHAT_REF_TTL = 86400.0
#: 引用表条目硬上限；超限时按最久未活跃顺序淘汰，防止长期运行后内存无界增长
LIVE_CHAT_REFS_MAX = 512


def _prune_live_refs_locked() -> None:
    """（调用方须持有 _LIVE_REFS_LOCK）先回收空壳哈希，再按上限淘汰最旧的 (哈希, 会话) 对。

    过期条目不在这里主动清：读侧 `other_live_sessions` 会按 TTL 过滤并就地收敛，
    而本表的上限淘汰按时间戳从最旧开始——长期不动的过期对最终也会被它请出去，
    表因此是有界的。误判方向只是"提前删一份没人看的缓存"，不会挡住隐私清理。
    """
    expired = [h for h, holders in _LIVE_CHAT_REFS.items() if not holders]
    for h in expired:
        _LIVE_CHAT_REFS.pop(h, None)
    total = sum(len(h) for h in _LIVE_CHAT_REFS.values())
    if total <= LIVE_CHAT_REFS_MAX:
        return
    flat = [(stamp, h, sid) for h, holders in _LIVE_CHAT_REFS.items() for sid, stamp in holders.items()]
    flat.sort()
    for _stamp, h, sid in flat[: total - LIVE_CHAT_REFS_MAX]:
        holders = _LIVE_CHAT_REFS.get(h)
        if not holders:
            continue
        holders.pop(sid, None)
        if not holders:
            _LIVE_CHAT_REFS.pop(h, None)


def note_live_chat(chat_hash: str, sid: str, now: Optional[float] = None) -> None:
    """记一笔"这个会话正在用这份聊天内容"（每个请求都刷一次时间戳）"""
    if not chat_hash or not sid:
        return
    stamp = time.time() if now is None else now
    with _LIVE_REFS_LOCK:
        _LIVE_CHAT_REFS.setdefault(chat_hash, {})[sid] = stamp
        _prune_live_refs_locked()


def forget_live_chat(chat_hash: str, sid: str) -> None:
    """该会话不再使用这份内容（换文件时先摘掉自己）"""
    if not chat_hash or not sid:
        return
    with _LIVE_REFS_LOCK:
        holders = _LIVE_CHAT_REFS.get(chat_hash)
        if not holders:
            return
        holders.pop(sid, None)
        if not holders:
            _LIVE_CHAT_REFS.pop(chat_hash, None)


def live_session_exists(sid: str) -> Optional[bool]:
    """该 sid 在**会话后端**里是否还存在。None = 判断不了（调用方按"还活着"处理）。

    为什么需要它：`other_live_sessions` 原先只按时间戳（24h TTL）判断"谁还在用"，
    而时间戳与"会话是否真的还在"是两件事——没点退出就关掉浏览器（或换了台设备、
    清了 cookie）会留下一个幽灵引用，最长把隐私清理挡住 24 小时，而界面还告诉用户
    "仍被其它浏览器使用"（假话，且没有任何重试入口）。

    这里问的是真正的会话存储（flask-session 的 cachelib 后端，键前缀 `session:`），
    它的 `has()` **自带过期判定**（实测：default_timeout 之后 has() 返回 False），
    所以答案比 TTL 猜测准确。取不到后端（没有应用上下文、换过后端、接口异常）时
    一律返回 None —— 保守方向是"当作还活着"，宁可少删一次，也不误删别人正在看的
    已付费结果（那正是引用表当初要修的症状）。
    """
    if not sid:
        return True
    try:
        from flask import current_app

        cache = current_app.config.get("SESSION_CACHELIB")
    except Exception:  # noqa: BLE001 —— 没有应用上下文（脚本/后台线程）就判断不了
        return None
    if cache is None or not hasattr(cache, "has"):
        return None
    try:
        return bool(cache.has(f"session:{sid}"))
    except Exception:  # noqa: BLE001 —— 后端自身的毛病不该影响隐私清理的主流程
        return None


def other_live_sessions(
    chat_hash: str, exclude_sid: str = "", now: Optional[float] = None, require_live_session: bool = False
) -> list:
    """除了 exclude_sid 之外，还有哪些**存活**会话正在用这份内容。

    require_live_session=True 时额外核对会话后端（见 live_session_exists）：
    「删除本聊天」这条**用户显式要求清干净**的路径用它，好让一个早就不存在的会话
    不再假装"还在用"，从而把该删的数据挡住。换文件的路径**不用**它：那里误判的代价
    是删掉别的会话正在用的已付费结果，而"内容寻址"意味着闲置用户回头重传同一份文件
    本该免费命中——两种代价不对称，所以口径按路径分开。
    """
    if not chat_hash:
        return []
    stamp = time.time() if now is None else now
    with _LIVE_REFS_LOCK:
        holders = _LIVE_CHAT_REFS.get(chat_hash)
        if not holders:
            return []
        alive = {s: t for s, t in holders.items() if stamp - t <= LIVE_CHAT_REF_TTL}
        if alive:
            _LIVE_CHAT_REFS[chat_hash] = alive
        else:
            _LIVE_CHAT_REFS.pop(chat_hash, None)
        candidates = [s for s in alive if s != exclude_sid]
    if not require_live_session:
        return candidates
    # 查会话后端要在锁外做（那是一次文件 stat，不该占着引用表的锁）
    return [s for s in candidates if live_session_exists(s) is not False]


def clear_live_chat_refs() -> None:
    """测试用：清空引用表，避免用例之间互相影响"""
    with _LIVE_REFS_LOCK:
        _LIVE_CHAT_REFS.clear()


def vision_enabled() -> bool:
    """是否要做图片理解（开关开着且有可用图片来源）。

    延迟到调用点判断，避免 store→vision 的模块级循环依赖。
    """
    from analyzer import vision

    return vision.VISION_ENABLED and vision.VISION_MAX_PER_MONTH > 0


#: `_created` 的有界扫描窗口：文件头与文件尾各读这么多字节。
#:
#: 为什么不干脆整份 json.load：这个值唯一的消费方是清理任务，而清理挂在
#: before_request 上（每小时随请求去抖触发一次），ai_cache/ 里体积最大的恰好就是
#: month_*.json（整月分析结果）。为了取一个浮点数把最敏感也最大的文件在请求线程里
#: 解析一遍，是拿用户的响应时间换一件本可以只看几十字节的事。
#:
#: 写入方都刻意把 `_created` 放在**最前面**（维度缓存 / 统计 / 图片摘要 / manifest），
#: 但历史遗留的月份缓存把它写在了**末尾**，所以两头各扫一次才能全覆盖。
_CREATED_WINDOW = 8192
_CREATED_RE = re.compile(rb'"_created"\s*:\s*([0-9.eE+-]+)')


def read_created_at(path: str) -> Optional[float]:
    """取缓存文件的首次写入时间；没有或读不到就返回 None，由调用方决定回退口径。

    头部命中时要求匹配**没有贴着缓冲区末尾**：贴着就说明数值被窗口切断了，
    此时按截断前的部分解析会算出一个错误的时间（可能把新文件判成远古文件而提前删除）。
    尾部窗口锚在 EOF，取到的值必然是完整的。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(_CREATED_WINDOW)
            tail = b""
            if size > _CREATED_WINDOW:
                f.seek(size - _CREATED_WINDOW)
                tail = f.read(_CREATED_WINDOW)
    except OSError:
        return None
    for blob, anchored_end in ((head, False), (tail, True)):
        match = _CREATED_RE.search(blob)
        if not match:
            continue
        if not anchored_end and match.end() >= len(blob):
            continue  # 被窗口切断：交给尾部窗口处理
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _stats_path(chat_hash: str) -> str:
    return os.path.join(STATS_CACHE_DIR, f"stats_{chat_hash}.json")


def _tmp_sibling(path: str) -> str:
    """原子写入的临时文件名。

    口径只有 `analyzer.atomic_write.tmp_sibling` 一份（唯一名的理由写在那个模块的文件
    说明里：固定名会让两个并发写者互相删半成品，并把半截文件发布成正式缓存）。
    这里保留同名函数，是因为清理侧与用例都把它当契约（见 _cache_belongs_to 与
    tests/test_privacy_guards.py 的临时名唯一性用例）。
    """
    return tmp_sibling(path)


def _load_stats(chat_hash: str, expect_mode: str = STATS_MODE_PRIVATE, *, read_only: bool = False):
    """读统计缓存。expect_mode 决定接受哪一套形状：

    - private（默认，兼容既有调用点）：`_v == STATS_SCHEMA_VERSION` 且没有 mode 字段
      （**旧缓存就是这种**）或 mode == "private"；
    - group：`_v == GROUP_STATS_SCHEMA_VERSION` 且 mode == "group"。

    两份不同口径的结果不会互相命中：同一份文件在两种模式下 chat_hash 相同，若只按
    哈希取用，切换模式后会拿到另一种口径的统计（数字看着正常、含义已变），属于最难
    发现的那类错。
    """
    if not chat_hash:
        return None
    try:
        with open(_stats_path(chat_hash), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "overview" not in data:
        return None
    mode = data.get("mode") or STATS_MODE_PRIVATE
    if mode != expect_mode:
        return None
    want_v = GROUP_STATS_SCHEMA_VERSION if expect_mode == STATS_MODE_GROUP else STATS_SCHEMA_VERSION
    if data.get("_v") != want_v:
        return None
    if not read_only:
        try:
            os.utime(_stats_path(chat_hash), None)
        except OSError:
            pass
    # _created 是"绝对 90 天"硬上限的依据，只给清理任务看：命中续期 mtime 之后，
    # 判定过期全靠它。摘掉再返回，调用方拿到的形状与加这个字段之前完全一致
    # （与月份缓存的读取口径相同，见 deepseek_client._read_month_cache）。
    data.pop("_created", None)
    return data


def _save_stats(chat_hash: str, stats: dict, mode: str = STATS_MODE_PRIVATE) -> None:
    if not chat_hash:
        return
    path = _stats_path(chat_hash)
    payload = dict(stats)
    payload.pop("_created", None)  # 不让调用方塞进来的值生效：首次创建时间只认盘上那份
    payload["mode"] = mode
    payload["_v"] = GROUP_STATS_SCHEMA_VERSION if mode == STATS_MODE_GROUP else STATS_SCHEMA_VERSION
    # 统计是确定性的，重算/补算（词频懒算后回写）都会重写同一个文件；
    # 绝对上限看的是**首次**创建时间，所以这里必须保留盘上已有的值——
    # 否则"每次查看词频就把 90 天硬上限往后推一格"，与 README 的保留承诺相反。
    created = read_created_at(path) or time.time()
    try:
        # 目录补建必须发生在写入之前、与 _write_cache 同一层：README 教用户"删掉
        # stats_cache/ 即可彻底清除数据"，而服务可能还开着（重建的目录随后被删、或那个
        # 位置被一个同名文件占住）。目录问题抛出来的是 OSError，落到这里的 except 就只是
        # 一行告警；放在 try 外面它会一路穿到请求层，变成"统计页 500"这种与真实原因
        # 毫不相干的症状。_created 放最前面，让清理任务只扫文件头就能拿到它
        # （见 read_created_at）。
        write_json_atomic(path, {"_created": created, **payload}, mkdir=STATS_CACHE_DIR)
    except OSError as e:
        logger.warning("统计缓存写入失败: %s", e)


def _delete_stats(chat_hash: str) -> None:
    if not chat_hash:
        return
    try:
        os.remove(_stats_path(chat_hash))
    except OSError:
        pass


def stats_mode_of(chat) -> str:
    """该 ChatData 该走哪套统计口径（群聊轨未就绪时永远不会有群聊 ChatData）"""
    return STATS_MODE_GROUP if getattr(chat, "is_group_chat", False) else STATS_MODE_PRIVATE


def compute_stats(chat) -> dict:
    """本地统计。群聊走独立分支，私聊分支与它的形状**一个字都没改**。

    词频两条轨都不在这里算：jieba 是耗时大头（数万条约 0.9 秒），由页面懒算。
    """
    if getattr(chat, "is_group_chat", False):
        return compute_group_stats(chat)
    overview = calc_overview(chat)
    return {
        "overview": overview,
        "daily_counts": calc_daily_counts(chat),
        "hourly_dist": calc_hourly_distribution(chat),
        "weekly_dist": calc_weekly_distribution(chat),
        "length_stats": calc_message_length_stats(chat),
        "face_stats": calc_face_stats(chat),
        "response_time": calc_response_time(chat),
        # 复用 overview 里已经算出的轮次：calc_overview 内部就调过一次
        # calc_exchange_rounds，再单独调一遍等于把 数万条消息白遍历一次。
        "exchange_rounds": overview["exchange_rounds"],
        "weekly_activity": calc_weekly_activity(chat),
        "milestones": calc_milestones(chat),
        # 时间趋势类（逐月互发/句长、中断重启、称呼变迁、首条消息）：全部纯本地
        "trends": calc_trends(chat),
    }


def start_stats_job(chat, chat_hash: str) -> None:
    """后台异步计算本地十项统计并落盘；判重、登记与启动必须在同一把锁内完成。"""
    if not chat_hash:
        return
    _unmark_purged(chat_hash)

    def _work():
        t0 = time.time()
        mode = stats_mode_of(chat)
        try:
            if _load_stats(chat_hash, expect_mode=mode) is not None:
                return
            stats = compute_stats(chat)
            # 落盘前复查 purge mark，防止清理期间算完的后台线程复活孤儿统计
            if _is_recently_purged(chat_hash):
                logger.info("该聊天的缓存已被清理，放弃写入后台统计结果（避免孤儿）")
                return
            _save_stats(chat_hash, stats, mode=mode)
            _clear_stats_error(chat_hash)
            logger.info("本地统计完成（%.0f ms，后台线程），已落盘复用", (time.time() - t0) * 1000)
        except Exception as e:
            _record_stats_error(chat_hash, f"{type(e).__name__}: {e}")
            logger.error("本地统计失败（该文件统计结果不可用）: %s", e)
        finally:
            with _STATS_LOCK:
                # 仅摘除当前线程自身登记，避免并发上传时误删仍在跑的新线程
                if _STATS_THREADS.get(chat_hash) is t:
                    _STATS_THREADS.pop(chat_hash, None)

    t = threading.Thread(target=_work, daemon=True, name=f"stats-{chat_hash[:8]}")
    # 在同一把锁内判重、登记并 start()，防止未启动线程窗口被 wait_for_stats join 抛 RuntimeError
    with _STATS_LOCK:
        existing = _STATS_THREADS.get(chat_hash)
        if existing is not None and existing.is_alive():
            return
        _STATS_THREADS[chat_hash] = t
        t.start()


def wait_for_stats(chat_hash: str, timeout: float = STATS_WAIT_SECONDS) -> None:
    """等某个聊天的后台统计跑完（没有在跑就直接返回）"""
    with _STATS_LOCK:
        t = _STATS_THREADS.get(chat_hash)
    if t is not None and t.is_alive():
        t.join(timeout)


def _current_stats():
    """当前会话的统计数据（磁盘缓存；后台还在算则短暂等待），没有则返回 None

    期望口径取自会话里记下的 chat_mode：写成脚本改过环境变量、或同一份文件在两种
    模式下都用过时，也不会把另一种口径的结果当成自己的（见 _load_stats）。
    """
    chat_hash = session.get("chat_hash", "")
    expect = STATS_MODE_GROUP if session.get("chat_mode") == STATS_MODE_GROUP else STATS_MODE_PRIVATE
    stats = _load_stats(chat_hash, expect_mode=expect)
    if stats is None and chat_hash:
        with _STATS_LOCK:
            busy = chat_hash in _STATS_THREADS
        if busy:
            wait_for_stats(chat_hash)
            stats = _load_stats(chat_hash, expect_mode=expect)
    return stats


def _stats_with_word_freq(stats: dict, chat_hash: str):
    """词频按需计算并写回统计缓存：jieba 分词占统计耗时的大头（数万条约 0.9 秒）

    群聊目前只做群整体词频（与私聊同形：self=我、other=其他所有成员），
    按成员拆分的词频等群聊页面成型后再加，避免先造一批没人用的数据结构。
    缓存有效性还看**停用词指纹**：外部停用词文件（QQCHAT_STOPWORD_FILE）一改，
    词云就重算一次——只花本地分词的 CPU，不动任何付费缓存。旧格式词频没有
    stop_fp 字段，按"过期"处理（首次升级多算 0.9 秒，此后一直命中）。
    """
    if stats is None:
        return stats
    wf = stats.get("word_freq")
    if wf and wf.get("stop_fp") == stopwords_fingerprint():
        return stats
    filepath = session.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return stats
    try:
        chat = _load_chat_cached(filepath)
        stats["word_freq"] = calc_word_freq(chat, top_n=80)
        # 守卫不能省：词频是"看着仪表盘顺手算一遍"的，算上一秒多、写盘一次。这一秒里
        # 另一个标签页完全可能换了一份聊天并触发级联清理——没有这道守卫，刚清掉的
        # 统计缓存（含聊天高频词）就会被这次回写原地复活。
        if _is_recently_purged(chat_hash):
            logger.info("该聊天的缓存已被清理，词频结果不再回写统计缓存")
        else:
            _save_stats(chat_hash, stats, mode=stats_mode_of(chat))
    except Exception as e:  # 词频失败不该拖垮页面
        logger.error("词频统计失败: %s", e)
        stats.setdefault("word_freq", {"self": [], "other": []})
    return stats


# ---------------------------------------------------------------------------
# AI 维度缓存（内容寻址 + 指纹键）
# ---------------------------------------------------------------------------


def _cache_path(dimension: str, chat_hash: str, fingerprint: "str | None" = None) -> str:
    # 键含提示词/格式指纹与思考模式后缀
    suffix = "_think" if thinking_enabled(dimension) else ""
    prompt_fp = fingerprint if fingerprint is not None else fingerprint_for_dimension(dimension)
    model_key = _cache_model_key()
    return os.path.join(AI_CACHE_DIR, f"{dimension}_{chat_hash}_{model_key}_{prompt_fp}{suffix}.json")


#: 原子写入留下的临时后缀：既认新的唯一名（`.json.{pid}.{tid}.{rand}.tmp`），
#: 也认历史遗留的死名（`.json.tmp` / `.tmp`）——升级那一刻盘上可能正躺着旧格式
#: 半成品，漏认一种就是漏删一份含聊天派生内容的文件。
_TMP_SUFFIX_RE = re.compile(r"(?:\.\d+\.\d+\.[0-9a-f]+)?\.tmp$")


def _cache_belongs_to(name: str, chat_hash: str) -> bool:
    """文件名是否属于该聊天：按 `_` 切段后**整段**比较，而不是子串匹配。

    这是隐私删除路径，子串匹配两个方向都能错：漏删（命名格式变了、子串不再出现，
    含聊天内容摘要的缓存就留在盘上，而用户以为已经清干净）与误删（哈希段恰好是
    另一个哈希的一部分，顺手删了别人的缓存）。两种缓存的命名都把 chat_hash
    作为一个完整段：维度缓存 `{dim}_{hash}_{model}_{指纹}.json`、
    图片摘要 `vision_{hash}_{key}.json`。

    先剥掉临时文件后缀（原子写入"临时文件 + os.replace"留下的半途文件——进程在两步
    之间被杀就会有一个躺在目录里）再剥 `.json`：少了这一步，`stats_{hash}.json.tmp`
    与 `manifest_{hash}.json.tmp` 的哈希段会变成 `{hash}.json.tmp`，整段匹配判假，
    于是级联清理报称"已清理"而那份含派生内容的残片原地留到 30/90 天上限。

    临时名是**唯一**的（`_{pid}.{tid}.{rand}.tmp`，见 _tmp_sibling：并发写同一键时
    固定名会互相删掉对方的半成品），所以这里用正则剥掉整段 `.数字.数字.十六进制.tmp`，
    而不是只剥一个死的 `.tmp`。少剥这一层就等于把上面那个隐私漏洞按新命名又请回来一次。
    """
    stem = _TMP_SUFFIX_RE.sub("", name)
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    return chat_hash in stem.split("_")


def _purge_chat_caches(chat_hash: str) -> int:
    """删除某聊天文件的全部缓存（跨版本/模型）。聊天源文件被删时联动调用，
    避免派生的分析结果（含聊天内容摘要）成为孤儿残留。"""
    if not chat_hash:
        return 0
    # 先记下"这个聊天刚被清掉"，再动任何一个文件。原先这一步排在函数末尾：晚到的
    # 后台线程可能已经查过守卫（那时还没有标记）、正卡在写入的中途，于是把刚清掉的
    # 结果原地复活。把标记提到最前面，检查与落盘之间就不存在"还没标记"的窗口。
    _mark_purged(chat_hash)
    # 进程内已解析的 ChatData 也一并丢弃。它的键是 (路径, mtime, size)，而这次
    # 清理只动派生缓存、**不动源文件**——所以"源文件被同名同大小重建"时会命中
    # 残留的旧对象，把上一条聊天的内容当成新的喂下去。宁可多解析一次。
    with _CHAT_CACHE_LOCK:
        _CHAT_CACHE.clear()
    removed = 0
    # 月份级缓存必须排在目录遍历**之前**：manifest_{chat_hash}.json 就躺在
    # AI_CACHE_DIR 里（生产环境 configure_month_cache 收到的就是这个目录），而它正是
    # purge_month_cache 判断"哪些月份文件只属于这个聊天"的唯一依据。若让遍历先跑，
    # 整段匹配会顺手删掉 manifest（它确实属于这个聊天），purge_month_cache 随后读到
    # 空集合，那些含聊天原句引用的月份文件就逃过本次同步回收，只能等下一次
    # sweep_orphan_month_cache（最多一小时；进程若就此停止则要等下次启动）。
    # 放在前面之后遍历仍是兜底：月份缓存关掉（_MONTH_CACHE_DIR=""）后残留的
    # manifest 依旧会被它清掉。
    removed += purge_month_cache(chat_hash)
    try:
        entries = os.listdir(AI_CACHE_DIR)
    except OSError:
        # 列不出目录不等于"这次清理到此为止"：统计缓存、图片副本与上面的月份文件
        # 都还要回收。早退会让它们（含聊天内容描述）留在盘上，而调用方与界面
        # 已经按"已清理"对外报告了。
        entries = []
    for name in entries:
        if _cache_belongs_to(name, chat_hash):
            try:
                os.remove(os.path.join(AI_CACHE_DIR, name))
                removed += 1
            except OSError as e:
                # 静默吞掉会让用户以为隐私数据已经清干净，实际磁盘上还留着
                # （Windows 上文件被占用尤其常见），所以这里必须出声。
                logger.warning("缓存文件删除失败（可能仍残留敏感内容）: %s (%s)", name, e)
    # 本地统计缓存：连同原子写入留下的临时文件一起删（那个半成品里是同一份统计结果）。
    # 临时名现在是唯一的（`stats_{hash}.json.{pid}.{tid}.{rand}.tmp`），所以必须扫目录
    # 按前缀删——只删一个死的 `.json.tmp` 会漏掉真正存在的那种，
    # 而 `_cache_belongs_to` 那边认得它们，这里不跟着改就两边口径不一致。
    stats_prefix = f"{os.path.basename(_stats_path(chat_hash))}."
    try:
        stats_entries = os.listdir(STATS_CACHE_DIR)
    except OSError:
        stats_entries = []
    for path in [_stats_path(chat_hash)] + [
        os.path.join(STATS_CACHE_DIR, n)
        for n in stats_entries
        if n.startswith(stats_prefix) and n.endswith(".tmp")
    ]:
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("统计缓存删除失败（可能仍残留敏感内容）: %s (%s)", path, e)
    # 清理图片摘要内存缓存与 uploads/media/<chat_hash>/ 图片副本
    try:
        from analyzer import vision  # 延迟导入，避免 store→vision 模块级依赖

        removed += vision.purge_memo(chat_hash)
        try:
            from analyzer import face_images

            face_images.purge_collect_memo(chat_hash)
        except Exception:
            pass
    except Exception as e:
        logger.warning("进程内图片摘要回收失败: %s", e)
    try:
        from analyzer import vision

        removed += vision.purge_session_media(chat_hash)
    except Exception as e:
        logger.warning("图片副本回收失败: %s", e)
    # 清理 logs/job_history.jsonl 中归属该 chat_hash 的记录行
    try:
        removed += purge_job_history(chat_hash)
    except Exception as e:
        logger.warning("任务历史按聊天清理失败: %s", e)
    # 清理进程内任务实时输出流（磁盘 joblog_{chat_hash}_*.json 已在上方 AI_CACHE_DIR 清扫）
    try:
        from webapp import job_stream

        job_stream.purge_chat(chat_hash)
    except Exception as e:
        logger.warning("任务输出流清理失败: %s", e)
    return removed


def _load_cache_file(path: str, *, touch: bool = True):
    """读一个维度缓存文件（不含任何回退逻辑）：读不到返回 None"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and "_created" in data and "result" in data:
        data = data["result"]
    if touch:
        try:
            os.utime(path, None)  # 命中续期 mtime（30 天滑动窗口）
        except OSError:
            pass
    return data


def _read_cache(
    dimension: str,
    chat_hash: str,
    fingerprint: "str | None" = None,
    *,
    read_only: bool = False,
):
    """直接读取当前指纹对应的维度缓存。"""
    current_fp = fingerprint or fingerprint_for_dimension(dimension)
    current_path = _cache_path(dimension, chat_hash, fingerprint=current_fp)
    return _load_cache_file(current_path, touch=not read_only)


def _write_cache(dimension: str, chat_hash: str, result, fingerprint: "str | None" = None) -> None:
    """原子写入：临时文件 + os.replace，避免进程中断留下半截 JSON。

    记录创建时间：命中读取会刷新 mtime（滑动窗口续期），若只有 mtime，
    天天查看的结果将永远不会被回收，与"保留 30 天"的隐私承诺不符。
    """
    # 写侧自查（与月份缓存/图片摘要/词频三处同一口径）：调用方 jobs 是在
    # "跑完 → 查 should_cancel → 写"三步里写的，查与写之间存在并发清理的窗口；
    # 守卫装在这里，这个窗口对维度缓存就不存在了。
    if _is_recently_purged(chat_hash):
        logger.info("该聊天的缓存刚被清理，维度结果不落盘（避免复活）")
        return
    path = _cache_path(dimension, chat_hash, fingerprint=fingerprint)
    payload = {"_created": time.time(), "result": result}
    try:
        # 目录必须在这里补建，不能只在 create_app() 里建一次就假定它永远在：
        # README 教用户"删掉 ai_cache/ 即可彻底清除数据"，而服务可能还开着——
        # 那种情况下这里不补目录，此后**每一次**写入都会静默失败，
        # 用户以为在命中缓存，实际每个月、每个维度都在重复付费（只有日志知道）。
        write_json_atomic(path, payload, mkdir=AI_CACHE_DIR)
    except OSError as e:
        logger.warning("AI 缓存写入失败: %s", e)


# ---------------------------------------------------------------------------
# 结果打包（导出/导入当前聊天）：换机器时"已付费的 AI 结果带得走"
# ---------------------------------------------------------------------------
# Bundle operations live in cache_bundle; store keeps the compatible facade.
# ---------------------------------------------------------------------------

BUNDLE_NAME_RE = cache_bundle.BUNDLE_NAME_RE
_CHAT_HASH_RE = cache_bundle.CHAT_HASH_RE
BUNDLE_MAX_ENTRIES = cache_bundle.BUNDLE_MAX_ENTRIES
BUNDLE_MAX_TOTAL_BYTES = cache_bundle.BUNDLE_MAX_TOTAL_BYTES
BUNDLE_META_NAME = cache_bundle.BUNDLE_META_NAME


def chat_bundle_files(chat_hash: str) -> dict:
    return cache_bundle.chat_bundle_files(
        chat_hash,
        ai_cache_dir=AI_CACHE_DIR,
        stats_cache_dir=STATS_CACHE_DIR,
        cache_belongs_to=_cache_belongs_to,
        is_safe_month_key=month_cache.is_safe_month_key,
        logger=logger,
    )


def write_chat_bundle(chat_hash: str, stream) -> int:
    return cache_bundle.write_chat_bundle(
        chat_hash,
        stream,
        files_for_chat=chat_bundle_files,
        ai_cache_dir=AI_CACHE_DIR,
        stats_cache_dir=STATS_CACHE_DIR,
        meta_name=BUNDLE_META_NAME,
    )


def _bundle_leaf_ok(leaf: str, head: str, declared_hash: str) -> bool:
    return cache_bundle.bundle_leaf_ok(leaf, head, declared_hash, cache_belongs_to=_cache_belongs_to)


def _bundle_payload_ok(head: str, leaf: str, raw: bytes) -> bool:
    return cache_bundle.bundle_payload_ok(raw)


def _clear_parsed_chat_cache() -> None:
    with _CHAT_CACHE_LOCK:
        _CHAT_CACHE.clear()


def read_chat_bundle(stream, confirm_overwrite: bool = False, live_hash: str = "") -> dict:
    return cache_bundle.read_chat_bundle(
        stream,
        confirm_overwrite,
        live_hash,
        ai_cache_dir=AI_CACHE_DIR,
        stats_cache_dir=STATS_CACHE_DIR,
        name_re=BUNDLE_NAME_RE,
        hash_re=_CHAT_HASH_RE,
        max_entries=BUNDLE_MAX_ENTRIES,
        max_total_bytes=BUNDLE_MAX_TOTAL_BYTES,
        meta_name=BUNDLE_META_NAME,
        leaf_ok=_bundle_leaf_ok,
        payload_ok=_bundle_payload_ok,
        write_bytes_atomic=write_bytes_atomic,
        clear_chat_cache=_clear_parsed_chat_cache,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# 自定义提问（ask）的缓存：一族独立的文件名，问题文本哈希进键。
# 命名 `ask_{chat_hash}_{model}_{指纹}_{问题哈希}.json` —— chat_hash 是完整段，
# 级联清理（_cache_belongs_to）、结果导出/导入、保留期回收全部自动覆盖它，
# 不必为 ask 再写一套生命周期（同一份隐私承诺，同一个作用域）。
# ---------------------------------------------------------------------------


_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _cache_model_key(model=None) -> str:
    """将模型名映射为单段安全文件名，避免 `/`、`\\`、`:` 等字符破坏路径。"""
    raw = str(DEEPSEEK_MODEL if model is None else model).strip()
    if (
        raw
        and ".." not in raw
        and "/" not in raw
        and "\\" not in raw
        and "\x00" not in raw
        and _SAFE_MODEL_RE.match(raw)
    ):
        return raw
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", raw)
    slug = re.sub(r"\.{2,}", ".", slug).strip(".-") or "model"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:40]}-{digest}"


def _ask_path(chat_hash: str, question: str) -> str:
    from analyzer import recap_client

    q_hash = hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:16]
    return os.path.join(
        AI_CACHE_DIR,
        f"ask_{chat_hash}_{_cache_model_key()}_{recap_client.ASK_PROMPT_FINGERPRINT}_{q_hash}.json",
    )


def ask_cache_read(chat_hash: str, question: str):
    """读某问题的既有答案（命中即续期 mtime，与维度缓存同口径）。"""
    if not chat_hash or not question.strip():
        return None
    return _load_cache_file(_ask_path(chat_hash, question))


def ask_cache_write(chat_hash: str, question: str, result) -> None:
    """落盘答案；刚被清理的聊天不复活（与 _write_cache 同一守卫）。"""
    if not chat_hash or not result or _is_recently_purged(chat_hash):
        return
    path = _ask_path(chat_hash, question)
    payload = {"_created": time.time(), "result": result}
    try:
        write_json_atomic(path, payload, mkdir=AI_CACHE_DIR)
    except OSError as e:
        logger.warning("提问缓存写入失败: %s", e)


# ---------------------------------------------------------------------------
# 任务历史落盘：分析任务的"最近记录"跨重启可查。
# 刻意只存 6 个白名单字段（时间/维度/状态/进度/聊天哈希前 12 位）——
# 错误文本可能带上传路径等环境信息，不进这份账（要细看错误去 logs/，那边有脱敏与轮转）。
# 也不做"重启自动续跑"：那等于服务一重启就静默发起付费调用，用户没点头的事不替用户做主。
# ---------------------------------------------------------------------------
# Job history storage is delegated; retain the store-level patch points.
# ---------------------------------------------------------------------------

JOB_HISTORY_MAX_LINES = job_history.MAX_LINES
JOB_HISTORY_FIELDS = job_history.FIELDS
_JOB_HISTORY_LOCK = threading.Lock()


def _job_history_path() -> str:
    return os.path.join(LOG_DIR, "job_history.jsonl")


def _prune_job_history_lines(lines: list, max_age_days: int, drop_prefixes: tuple = ()) -> list:
    return job_history.prune_lines(lines, max_age_days, drop_prefixes=drop_prefixes, now=time.time())


def purge_job_history(chat_hash: str) -> int:
    return job_history.purge_job_history(
        chat_hash,
        path=_job_history_path(),
        lock=_JOB_HISTORY_LOCK,
        prune=_prune_job_history_lines,
        write_text_atomic=write_text_atomic,
        logger=logger,
    )


def append_job_history(entry: dict, max_age_days: Optional[int] = None) -> None:
    if max_age_days is None:
        max_age_days = LOG_RETENTION_DAYS
    job_history.append_job_history(
        entry,
        log_dir=LOG_DIR,
        max_age_days=max_age_days,
        path=_job_history_path(),
        lock=_JOB_HISTORY_LOCK,
        prune=_prune_job_history_lines,
        max_lines=JOB_HISTORY_MAX_LINES,
        fields=JOB_HISTORY_FIELDS,
        write_text_atomic=write_text_atomic,
        logger=logger,
    )


def read_job_history(limit: int = 30) -> list:
    return job_history.read_job_history(_job_history_path(), limit=limit)
