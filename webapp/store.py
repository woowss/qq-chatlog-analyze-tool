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
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
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
from analyzer.deepseek_client import (
    fingerprint_for_dimension,
    legacy_fingerprint_for_dimension,
    legacy_fingerprints_for_dimension,
    purge_month_cache,
    thinking_enabled,
)
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

# 统计结果的结构版本：字段形状变了就 +1，老缓存会被判为过期并重算（避免模板 500）
# v3：overview 增加 total_files / total_videos / total_forwards / total_other_media
# v4：overview 增加 image_bytes / media_bytes / unique_images（媒体体积与去重）
# v5：三个数字换了口径，老缓存里的值从此是错的，必须重算而不是继续显示：
#     ① 句长 median 此前取上中位（两句 [1,9] 报成 9），② 一条消息里的图片与文件
#     被同一份 media_bytes 双计进两个字段，③ 图片按"含图消息条数"数却标注为"张"。
#     重算只花本地 CPU（数万条约 0.2~1s，词频仍是懒算），不产生任何 API 费用，
#     所以这里宁可整族失效，也不让用户继续看错的数。
# v6：① 修 total_other_media 的枚举漏计（json/av_record 被解析被计数却进不了汇总，
#         界面"通话/卡片"恒为 0），改为差集口径；② 语音消息进入媒体口径（total_voices）；
#     ③ 新增撤回计数与"未识别元素类型"漂移探测；④ 新增 trends 时间趋势族
#         （逐月互发与句长、中断≥3天的重启与重启方、称呼变迁、首条消息原文）。
#     均为加法字段 + 一处口径修复，老缓存里"其他=0"是错的，继续显示不如重算
#     （只花本地 CPU，零 API 费用）。
# v7：trends.name_history 的键改用 strip 过的显示名。带尾空格的昵称此前会被拆成两个
#     桶（"小明" 与 "小明 "），仪表盘的「称呼变迁」于是显示一次从未发生的改名——而那张
#     卡片的语义是关系信号，假信号比缺信号更糟。老缓存里这条是错的，重算只花 CPU。
#     注意：calc_trends 同时进 recap/ask 的提示词指纹，所以这一改也让那两族缓存换代；
#     该族尚未发布（CHANGELOG 已记账），没有用户为它付过钱。
STATS_SCHEMA_VERSION = 7
# 群聊统计是**另一套形状**，因此用独立的版本号与 mode 字段，而不是把私聊的 v4 往上顶：
# 顶版本号会让所有既有私聊统计缓存立刻失效（升级后第一次打开页面白等一次计算），
# 而私聊缓存的形状其实一个字都没变。mode 是权威判别字段，_v 只在同 mode 内有意义。
# g1 → g2：interaction 增加 explicit_/mention_ 矩阵与覆盖率计数（群聊轨尚未对外，无影响）
# g2 → g3：① length_stats 的中位数改了口径（群统计复用同一个函数）；
# ② interaction 的 explicit_undirected 对角不再把自回复翻倍，并新增 self_replies 字段。
# 老缓存里的数字是错的，而重算只花本地 CPU（不产生任何 API 费用），所以宁可整族失效。
# g3 → g4：群聊 overview 复用私聊 calc_overview，随私聊 v6 一起变
# （total_other_media 差集口径、total_voices、撤回计数、未识别元素探测）。
GROUP_STATS_SCHEMA_VERSION = 4

# 统计计算挪出请求线程后的在跑任务：chat_hash -> Thread
_STATS_THREADS: dict[str, threading.Thread] = {}
_STATS_LOCK = threading.Lock()
# 等待后台统计的最长时间（首屏可以稍微等一会儿，好过把"计算中"甩给用户）
STATS_WAIT_SECONDS = 20.0
# 统计失败的哈希 -> 失败原因：异步化之后错误不再有 HTTP 响应可承载，
# 不记下来的话用户只会看到"上传成功但仪表盘又跳回首页"，无从排查。
_STATS_ERRORS: dict[str, str] = {}
# 最近被级联清理过的哈希：防止仍在跑的线程把结果"复活"成孤儿缓存。
# 表本身住在 analyzer.purge_marks（分析层与持久层都要读，谁也不能 import 谁），
# 这里只留同名包装，_STATS_ERRORS 的顺带清理仍在本模块的锁里做。
# 上面两处由后台统计线程与请求线程共同读写：GIL 让简单操作"大多不会炸"，
# 但 pop/迭代混在一起时并不可靠，统一挂到这把独立锁下（不复用 _STATS_LOCK，
# 避免与"等线程结束"的 join 路径互相等待）
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


# ---------------------------------------------------------------------------
# 存活会话对"某份聊天内容"的引用表
# ---------------------------------------------------------------------------
# 缓存是按**内容哈希**寻址的，而同一个内容可以被多个会话同时打开了好几份，
# 每个会话自己传自己一份（各自的 filepath 不同，内容一样 → chat_hash 一样）。
# 于是"会话 2 换文件"触发的级联清理会连带删掉会话 1 正在用的统计与**已付费**的 AI 结果：
# 会话 1 的症状是"仪表盘平白弹回首页"，而他要恢复只能重新分析（真的再付一次钱）。
# 实测过：20 个会话并发跑，7 个换文件，另外 3 个会话的页面请求就被打回首页。
# 这里记一份"谁在用哪个哈希"，级联清理只删**已经没有别的会话在用**的那些。
#
# 口径：TTL 与 flask_session/ 本身的 24 小时回收一致（见 cleanup 的 86400）——
# 会话只要还在被请求就会刷新时间戳，不再活动的会话自然过期，不会把缓存永久钉住。
_LIVE_CHAT_REFS: dict[str, dict[str, float]] = {}
_LIVE_REFS_LOCK = threading.Lock()
LIVE_CHAT_REF_TTL = 86400.0
#: (哈希, 会话) 对的硬上限。修剪原先只发生在"该哈希又被读一次"的时候，而长期运行的
#: 实例里每个上传过的哈希都只被写、之后未必再被读——(哈希, sid) 对会一直涨。
#: 这张表只用来判断"还能不能删缓存"，超限时按时间戳淘汰最旧的：误判方向只是
#: 提前删掉一份早已没人看的缓存，而不是把隐私清理永久挡住。
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

    **必须是唯一名**，不能继续用 `f"{path}.tmp"`。多浏览器并发打开同一份聊天是本
    项目明确支持的用法（见 _LIVE_CHAT_REFS 那一整段的实测记录），固定名等于让两个
    写者踩同一个临时文件：A 刚 open 完、B 把同一个文件重开覆盖，或任一方在失败分支
    里 `os.remove(tmp)` 删掉对方正在写的那份，最后 `os.replace` 就可能把半截文件
    发布成正式缓存——维度缓存丢的是**一次已付费结果**，统计缓存丢的是页面渲染时的
    数据结构。pid + 线程 id + 随机后缀保证每个写者各用各的；仍在同一目录内，
    所以 os.replace 的原子性不变，清理侧也照旧按 `.tmp` 后缀识别（见 _cache_belongs_to）。
    """
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"


def _load_stats(chat_hash: str, expect_mode: str = STATS_MODE_PRIVATE):
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
    tmp = _tmp_sibling(path)
    payload = dict(stats)
    payload.pop("_created", None)  # 不让调用方塞进来的值生效：首次创建时间只认盘上那份
    payload["mode"] = mode
    payload["_v"] = GROUP_STATS_SCHEMA_VERSION if mode == STATS_MODE_GROUP else STATS_SCHEMA_VERSION
    # 统计是确定性的，重算/补算（词频懒算后回写）都会重写同一个文件；
    # 绝对上限看的是**首次**创建时间，所以这里必须保留盘上已有的值——
    # 否则"每次查看词频就把 90 天硬上限往后推一格"，与 README 的保留承诺相反。
    created = read_created_at(path) or time.time()
    try:
        # 目录补建必须在 try 里、与 _write_cache 同一层：README 教用户"删掉 stats_cache/
        # 即可彻底清除数据"，而服务可能还开着（重建的目录随后被删、或那个位置被一个
        # 同名文件占住）。放在 try 外面时 FileExistsError 会一路穿到请求层，
        # 变成"统计页 500"这种与真实原因毫不相干的症状。
        os.makedirs(STATS_CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            # _created 放最前面，让清理任务只扫文件头就能拿到它（见 read_created_at）
            json.dump({"_created": created, **payload}, f, ensure_ascii=False)
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
    """把统计计算放进后台线程：上传请求只做「保存 + 解析校验」，尽快返回。

    50MB 记录的十项统计要 0.2~1s+，压在请求线程上纯属让浏览器干转圈；
    结果是确定性的、又要落盘复用，天然适合异步。首个页面请求经
    _current_stats() 短暂 join，用户几乎无感。
    """
    if not chat_hash:
        return
    # 用户清掉后又重新上传同一份文件：这是新会话的正当计算，撤销旧的"已清理"标记
    _unmark_purged(chat_hash)
    # 这里**故意不做**"已在跑就返回"的前置检查：判存在与登记必须一次加锁完成，
    # 否则两个并发上传会双双通过检查、各起一个线程（真正的判定挪到函数末尾的注册处）。

    def _work():
        t0 = time.time()
        mode = stats_mode_of(chat)
        try:
            if _load_stats(chat_hash, expect_mode=mode) is not None:
                return
            stats = compute_stats(chat)
            # 守卫必须放在计算之后、落盘之前复查：计算期间用户可能已清理该聊天，
            # 开算前查一次是不够的（那正是"晚到的线程复活孤儿缓存"的窗口）。
            if _is_recently_purged(chat_hash):
                logger.info("该聊天的缓存已被清理，放弃写入后台统计结果（避免孤儿）")
                return
            _save_stats(chat_hash, stats, mode=mode)
            _clear_stats_error(chat_hash)
            logger.info("本地统计完成（%.0f ms，后台线程），已落盘复用", (time.time() - t0) * 1000)
        except Exception as e:
            # 异步之后没有 HTTP 响应能承载这个错误：记在案，首页会提示用户
            _record_stats_error(chat_hash, f"{type(e).__name__}: {e}")
            logger.error("本地统计失败（该文件统计结果不可用）: %s", e)
        finally:
            with _STATS_LOCK:
                # **只摘自己**：原先这里无条件 pop，于是"同一份内容被并发上传两次"时，
                # 先结束的那个线程会把还在一起跑的第二个线程从表里抹掉。
                # 后果不是脏数据而是用户可见的故障：_current_stats() 看到 busy=False
                # 就不等 20 秒、直接拿到 None，_require_stats() 于是把人弹回首页——
                # 正是本轮 _LIVE_CHAT_REFS 想消灭的"仪表盘平白弹回首页"，从另一条路复活。
                # 比对身份（同一个 Thread 对象）才摘，别人的登记不许我替他们清。
                if _STATS_THREADS.get(chat_hash) is t:
                    _STATS_THREADS.pop(chat_hash, None)

    t = threading.Thread(target=_work, daemon=True, name=f"stats-{chat_hash[:8]}")
    # 判"有没有人在算"与登记必须一次加锁完成：原先查完就放锁、再二次进锁登记，
    # 中间那个窗口里两个并发上传会双双通过检查、各起一个线程，然后撞上
    # 上面 finally 的无条件 pop（同一份内容被两个浏览器/两台设备打开是本项目
    # 明确支持的场景，见 _LIVE_CHAT_REFS）。与登录限流那处是同一个教训。
    with _STATS_LOCK:
        existing = _STATS_THREADS.get(chat_hash)
        if existing is not None and existing.is_alive():
            return
        # 已登记但线程早已结束（异常路径没清干净时的残骸）：覆盖掉，别永久卡死
        _STATS_THREADS[chat_hash] = t
        # start() 必须在**同一把锁内**：它原先在锁外，于是"登记完、还没 start"的窗口里
        # 另一个请求会看到 busy=True、进而 join 一个尚未启动的线程——CPython 对此抛
        # `RuntimeError: cannot join thread before it is started`（实测复现），仪表盘直接
        # 500；在不抛的版本上 join 立刻返回、调用方拿到 None，症状又变回"平白弹回首页"，
        # 正是本轮 _LIVE_CHAT_REFS 要消灭的那个。start() 本身很便宜，而 _work 只在
        # finally 里取这把锁，锁内启动不会与它自锁。
        t.start()


def wait_for_stats(chat_hash: str, timeout: float = STATS_WAIT_SECONDS) -> None:
    """等某个聊天的后台统计跑完（没有在跑就直接返回）"""
    with _STATS_LOCK:
        t = _STATS_THREADS.get(chat_hash)
    # is_alive() 两道用：① 线程可能已经结束但还没被自己摘掉（见 _work 的 finally）；
    # ② 兜住任何"已登记但尚未 start"的历史窗口——对未启动的线程 join 会抛 RuntimeError。
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


def _cache_path(dimension: str, chat_hash: str, legacy: bool = False) -> str:
    # 键含提示词/格式指纹：由 SYSTEM_PROMPT_* 与对话格式化函数自动哈希而来，改了提示词或
    # 输入格式后旧缓存自动失效（不再依赖人工 bump 版本号）。**按维度取**：群聊维度用群聊
    # 指纹，私聊维度用私聊指纹——这样新增/修改群聊提示词不会让私聊缓存文件名发生变化
    # （文件名一变，用户就得为同样的分析重新付费）。
    # 键含思考模式：同一模型开关 thinking 前后的结果差异很大，必须分开存放，
    # 否则切换 LLM_THINKING(_DIMS) 后会命中另一种模式的旧结果（看起来"没区别"）。
    # 非思考模式不加后缀，保持既有缓存键兼容。
    #
    # legacy=True 给出"旧指纹公式"下的文件名：指纹改为 AST 归一之后，既有用户的缓存
    # 仍叫那个名字。_read_cache 会先按当前键找、找不到再看旧键，读到就改名（迁移），
    # 于是这次公式变更不会让任何人为同样的分析重新付费。
    suffix = "_think" if thinking_enabled(dimension) else ""
    pick = legacy_fingerprint_for_dimension if legacy else fingerprint_for_dimension
    return os.path.join(
        AI_CACHE_DIR, f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}_{pick(dimension)}{suffix}.json"
    )


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
            pass  # 本来就没有，属正常情形，不必报
        except OSError as e:
            logger.warning("统计缓存删除失败（可能仍残留敏感内容）: %s (%s)", path, e)
    # 月份级缓存已在函数开头处理（顺序原因见那里的注释）：这里不再重复调用。
    # 看图用的图片副本（uploads/media/<chat_hash>/）：源文件都换了/没了，
    # 派生出来的图片本体必须一起走，否则它只受"24 小时 mtime 回收"约束，
    # 而在那之前一直是盘上最敏感的一批数据。
    try:
        from analyzer import vision  # 延迟导入，避免 store→vision 模块级依赖

        removed += vision.purge_session_media(chat_hash)
    except Exception as e:  # 回收失败不影响主流程
        logger.warning("图片副本回收失败: %s", e)
    # 任务历史账目（logs/job_history.jsonl）里那几行 chat_hash 前缀。
    # 少了这一步，「删除本聊天」之后盘上仍然留着"某日分析过这份内容、跑了哪个维度、
    # 几个月"这张账——而 chat_hash 在本项目里就是聊天的身份（缓存全按它寻址）。
    # 它不含聊天内容，但属于"这份内容被分析过"的留存记录，不该在删除后独自留下。
    try:
        removed += purge_job_history(chat_hash)
    except Exception as e:  # 账目清不掉不影响隐私主路径，但要出声
        logger.warning("任务历史按聊天清理失败: %s", e)
    return removed


def _load_cache_file(path: str):
    """读一个维度缓存文件（不含任何回退逻辑）：读不到返回 None"""
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


def _read_cache(dimension: str, chat_hash: str):
    """读维度缓存；当前指纹下没有时，沿"历史代指纹链"逐代找并迁移到当前键。

    指纹公式从"源码原文"改成 AST 归一之后，既有用户的缓存文件名仍是旧指纹。
    不认那份的话，他们每一次分析都要重新付费——而这次公式变更与提示词、与对话
    内容都毫无关系。读到旧文件就 os.replace 到当前键：既完成迁移，又不留重复的
    敏感内容（这些缓存含聊天原文引用）。
    链式（而非单值）：未来任何一次指纹换代都只需把"上一代的当前值"压进链头，
    这里会挨代找到旧缓存并搬到新键——第三次、第四次换代的老用户同样不再付费。
    """
    current_path = _cache_path(dimension, chat_hash)
    data = _load_cache_file(current_path)
    if data is not None:
        return data
    suffix = "_think" if thinking_enabled(dimension) else ""
    for legacy_fp in legacy_fingerprints_for_dimension(dimension):
        if legacy_fp == fingerprint_for_dimension(dimension):
            continue  # 两个指纹相同（没有历史包袱的干净环境），不必多查一次
        legacy_path = os.path.join(
            AI_CACHE_DIR, f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}_{legacy_fp}{suffix}.json"
        )
        data = _load_cache_file(legacy_path)
        if data is None:
            continue
        try:
            os.replace(legacy_path, current_path)
            logger.info("命中旧指纹的维度缓存并迁移到当前键: %s", dimension)
        except OSError as e:
            # 迁移失败不影响这次的结果（已经在 data 里了）；下次读还会再看到旧文件
            logger.warning("维度缓存迁移失败（结果仍可用，只是留在旧文件名下）: %s", e)
        return data
    return None


def _write_cache(dimension: str, chat_hash: str, result) -> None:
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
    path = _cache_path(dimension, chat_hash)
    tmp = _tmp_sibling(path)
    payload = {"_created": time.time(), "result": result}
    try:
        # 目录必须在这里补建，不能只在 create_app() 里建一次就假定它永远在：
        # README 教用户"删掉 ai_cache/ 即可彻底清除数据"，而服务可能还开着——
        # 那种情况下这里不补目录，此后**每一次**写入都会静默失败，
        # 用户以为在命中缓存，实际每个月、每个维度都在重复付费（只有日志知道）。
        os.makedirs(AI_CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("AI 缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 结果打包（导出/导入当前聊天）：换机器时"已付费的 AI 结果带得走"
# ---------------------------------------------------------------------------
#: 导入文件名的白名单：安全字符集 + 只收 .json、不含任何路径成分（路径校验在
#: 解包侧按目录前缀做，这里钉死叶子名）。月份/manifest/图片摘要/维度缓存
#: （{dim}_{hash}_{model}_{指纹}[_think]）与统计文件都落在这个字符集里；
#: 模型名允许点与横线，".." 单独不可能通过"以字母数字开头且 .json 结尾"。
BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,120}\.json$")
#: meta.json 里声明的 chat_hash 的合法形状（与 _chat_hash() 同一口径：sha256 前 16 位）。
#: 归属校验的基准值必须先确认"是个哈希"，否则一个空串或 "../x" 会退化成
#: 在 _bundle_leaf_ok 里做子串匹配的依据。
_CHAT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
BUNDLE_MAX_ENTRIES = 5000
BUNDLE_MAX_TOTAL_BYTES = 512 * 1024 * 1024
BUNDLE_META_NAME = "meta.json"


def chat_bundle_files(chat_hash: str) -> dict:
    """属于该聊天的缓存文件清单：{"ai_cache": [名字…], "stats_cache": [名字…]}（不含路径）。

    月份文件（month_{key}.json）按**内容哈希**寻址、名字里没有 chat_hash，
    必须顺着 manifest 的引用收进来——否则迁到新机器后增量机制退化成整月重付费，
    恰好废掉这个项目最核心的省钱设计。
    """
    if not chat_hash:
        return {"ai_cache": [], "stats_cache": []}
    ai: list[str] = []
    month_keys: list[str] = []
    try:
        entries = os.listdir(AI_CACHE_DIR)
    except OSError:
        entries = []
    for name in entries:
        if not _cache_belongs_to(name, chat_hash):
            continue
        ai.append(name)
        if name.startswith("manifest_"):
            try:
                with open(os.path.join(AI_CACHE_DIR, name), encoding="utf-8") as f:
                    month_keys += [str(k) for k in (json.load(f).get("months") or [])]
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
    for key in month_keys:
        # key 来自磁盘上的 manifest（可能由导入的结果包写进来），而它马上要被当成
        # 路径成分用：不合法的一律丢掉。少了这一步，`month_x/../../某文件` 会归一成
        # 缓存目录之外的路径，并被 zf.write **把那个文件的内容打进用户会分享的包里**
        # ——实测复现过（见 analyzer.month_cache.is_safe_month_key）。
        if not month_cache.is_safe_month_key(key):
            logger.warning("manifest 里有形状非法的月份键，导出时已跳过")
            continue
        mname = f"month_{key}.json"
        if mname not in ai and os.path.exists(os.path.join(AI_CACHE_DIR, mname)):
            ai.append(mname)
    stats: list[str] = []
    sname = f"stats_{chat_hash}.json"
    if os.path.exists(os.path.join(STATS_CACHE_DIR, sname)):
        stats.append(sname)
    return {"ai_cache": sorted(ai), "stats_cache": stats}


def write_chat_bundle(chat_hash: str, stream: io.BytesIO) -> int:
    """把该聊天的统计与已付费结果打包成 zip（条目形如 ai_cache/xxx.json），返回文件数"""
    files = chat_bundle_files(chat_hash)
    count = 0
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            BUNDLE_META_NAME,
            json.dumps({"bundle": 1, "chat_hash": chat_hash, "created": time.time()}, ensure_ascii=False),
        )
        for logical, names in (("ai_cache", files["ai_cache"]), ("stats_cache", files["stats_cache"])):
            base = AI_CACHE_DIR if logical == "ai_cache" else STATS_CACHE_DIR
            for name in names:
                try:
                    zf.write(os.path.join(base, name), f"{logical}/{name}")
                    count += 1
                except OSError:
                    pass
    return count


def _bundle_leaf_ok(leaf: str, head: str, declared_hash: str) -> bool:
    """条目文件名是否可以落盘。

    路径成分在这一层之外已经挡掉（目录前缀白名单 + BUNDLE_NAME_RE）；这里管的是
    **"这个包凭什么写这个键"**。导出包的内容会被当作"已付费的分析结果"直接渲染，
    所以一个不含任何聊天数据的 zip 必须只能写它自己那份聊天的文件——否则任何人都能
    把 `stats_<别人当前聊天哈希>.json` 塞进包里，导入后被冒充成用户的真实统计与
    AI 结论（本项目"AI 结论必须可回溯到本地事实"的立论基础就被打穿了）。
    """
    if head == "stats_cache":
        # stats_{hash}.json —— 必须就是包里声明的那份聊天
        return leaf == f"stats_{declared_hash}.json"
    if leaf.startswith("month_"):
        # 月份缓存按**内容**寻址（month_{key}.json），名字里没有 chat_hash，
        # 靠 manifest 的引用收进包里（见 chat_bundle_files）。这一族只校验形状，
        # 不校验归属：它本就是跨聊天复用的"同样的一个月"，强行绑哈希会把
        # 正常的增量迁移整条打断。
        return True
    if leaf.startswith("manifest_"):
        return leaf == f"manifest_{declared_hash}.json"
    # 维度缓存 / 图片摘要 / 提问缓存：{dim}_{hash}_{...}.json，chat_hash 是完整段
    return _cache_belongs_to(leaf, declared_hash)


def _bundle_payload_ok(head: str, leaf: str, raw: bytes) -> bool:
    """落盘前的**安全性**校验，不是 schema 校验。

    要挡的是"根本不是 JSON / 结构上不可能被任何读取路径使用"的东西——这类文件一旦
    进入 ai_cache/，读取侧会把它当作既有结果直接渲染，形状完全陌生的对象会一路走到
    模板里才炸。所以这里只要求"可解析 + 是个非空 dict"。

    **刻意不做逐族形状判定**：盘上真实缓存的形状本来就五花八门，逐一列名单一定会把
    合法导出误拒（那比原漏洞更糟——用户带不走已付费结果，症状还是偶发的）：
    月份缓存是裸的 {期间: 结果} 映射（_read_month_cache 也接受带 result 的外层），
    图片摘要是 {"_created", "digest"}，维度缓存新旧两制分别是 {"_created","result"}
    与裸结果 dict，统计缓存顶层是 {mode,_v,overview,...}。名单每漏一族，
    那一族的迁移就静默失败。读侧对坏文件本来就是容错的（返回 None → 重新分析，
    只多花一次钱），所以从严没有收益、从宽没有风险。
    """
    if len(raw) > 32 * 1024 * 1024:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and bool(data)


def read_chat_bundle(stream, confirm_overwrite: bool = False, live_hash: str = "") -> dict:
    """导入结果包：条目必须属于包里声明的那份聊天，且落点只有两个缓存目录。

    zip 里的路径成分一律不信，落点只由"目录前缀 + 过白名单的叶子名"决定。
    返回 {"written": n, "skipped": m}，或 {"error": 人话}（超限/坏 zip/需要确认时
    早停，不把半包状态留给用户猜）。

    confirm_overwrite / live_hash：当前会话正打开着同一份聊天时，导入会覆盖**正在看**
    的统计与已付费结果。这是合法用法（换机器恢复），但也正是"用一个 zip 冒充当前
    聊天结果"最省事的攻击路径，所以要求显式二次确认才放行（见 api_import_chat）。
    """
    try:
        zf = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError):
        return {"error": "不是有效的导出包（zip 打不开）；请使用本工具的「导出」生成的文件"}
    with zf:
        infos = zf.infolist()
        if len(infos) > BUNDLE_MAX_ENTRIES:
            return {"error": f"导出包条目过多（{len(infos)}），拒绝"}

        # ① 先读 meta 并取回它声明的 chat_hash —— 这是后面所有归属校验的基准
        declared = ""
        try:
            meta_raw = zf.read(BUNDLE_META_NAME)
        except KeyError:
            return {"error": "结果包缺少 meta.json，无法确认它属于哪份聊天，已拒绝"}
        except Exception as e:  # 坏 CRC / 加密条目 / 不支持的压缩法
            return {"error": f"结果包的 meta.json 读不出来（{type(e).__name__}），已拒绝"}
        try:
            declared = str(json.loads(meta_raw.decode("utf-8")).get("chat_hash") or "").strip()
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            declared = ""
        if not _CHAT_HASH_RE.fullmatch(declared):
            return {"error": "结果包的 meta.json 缺少合法的 chat_hash，已拒绝"}

        # ② 覆盖当前正在看的聊天需要用户明确点头：这是合法用法（换机器恢复），
        # 但也是"用一个 zip 冒充当前聊天结果"最省事的攻击路径，所以要一次显式确认。
        if live_hash and live_hash == declared and not confirm_overwrite:
            return {
                "need_confirm": True,
                "error": "这份结果包属于你当前正在打开的聊天，导入会覆盖现有的统计与已付费结果。"
                "确认要继续请再点一次「导入」。",
            }

        written = skipped = total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name == BUNDLE_META_NAME:
                continue
            head, sep, leaf = name.rpartition("/")
            if not sep or head not in ("ai_cache", "stats_cache") or not BUNDLE_NAME_RE.match(leaf):
                skipped += 1
                continue
            # 归属校验：不许写别人那份聊天的键
            if not _bundle_leaf_ok(leaf, head, declared):
                skipped += 1
                continue
            total += max(0, info.file_size)
            if total > BUNDLE_MAX_TOTAL_BYTES:
                return {"error": "导出包解压体积超限，已中止（导入不完整）"}
            target_dir = AI_CACHE_DIR if head == "ai_cache" else STATS_CACHE_DIR
            path = os.path.join(target_dir, leaf)
            tmp = _tmp_sibling(path)
            try:
                with zf.open(info) as src:
                    raw = src.read()
            except Exception as e:  # 单条目坏掉不许把整个请求打成 500
                logger.warning("结果包条目 %s 读不出来（%s），已跳过", leaf, type(e).__name__)
                skipped += 1
                continue
            if not _bundle_payload_ok(head, leaf, raw):
                logger.warning("结果包条目 %s 形状不对，已跳过", leaf)
                skipped += 1
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
                with open(tmp, "wb") as dst:
                    dst.write(raw)
                os.replace(tmp, path)
                written += 1
            except OSError:
                skipped += 1
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    if written:
        # 已解析的 ChatData 与 manifest 引用缓存都可能盖着旧状态：清掉，
        # 让导入的文件从下一次读起就是权威（与级联清理同一口径）。
        with _CHAT_CACHE_LOCK:
            _CHAT_CACHE.clear()
    return {"written": written, "skipped": skipped, "chat_hash": declared[:12]}


# ---------------------------------------------------------------------------
# 自定义提问（ask）的缓存：一族独立的文件名，问题文本哈希进键。
# 命名 `ask_{chat_hash}_{model}_{指纹}_{问题哈希}.json` —— chat_hash 是完整段，
# 级联清理（_cache_belongs_to）、结果导出/导入、保留期回收全部自动覆盖它，
# 不必为 ask 再写一套生命周期（同一份隐私承诺，同一个作用域）。
# ---------------------------------------------------------------------------


def _ask_path(chat_hash: str, question: str) -> str:
    from analyzer import recap_client

    q_hash = hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:16]
    return os.path.join(
        AI_CACHE_DIR,
        f"ask_{chat_hash}_{DEEPSEEK_MODEL}_{recap_client.ASK_PROMPT_FINGERPRINT}_{q_hash}.json",
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
    tmp = _tmp_sibling(path)
    payload = {"_created": time.time(), "result": result}
    try:
        os.makedirs(AI_CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("提问缓存写入失败: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 任务历史落盘：分析任务的"最近记录"跨重启可查。
# 刻意只存 6 个白名单字段（时间/维度/状态/进度/聊天哈希前 12 位）——
# 错误文本可能带上传路径等环境信息，不进这份账（要细看错误去 logs/，那边有脱敏与轮转）。
# 也不做"重启自动续跑"：那等于服务一重启就静默发起付费调用，用户没点头的事不替用户做主。
# ---------------------------------------------------------------------------

#: 这份账目最多保留多少行（超了对折）与多少天（按条目自带时间戳判过期）。
#: 天数上限必须与 LOG_RETENTION_DAYS 同口径：README 承诺"日志按天轮转保留 N 天"，
#: 而这份账目就住在 LOG_DIR 里。清理任务的日志回收只认 `app.log.` 前缀
#: （见 webapp/cleanup.py），它**永远不会**碰到这个文件名——所以年龄回收只能自己实现，
#: 否则用户删掉聊天之后，"某人在某日用某维度分析过哈希 X 次"还会在这里留几个月。
JOB_HISTORY_MAX_LINES = 1000
JOB_HISTORY_FIELDS = ("t", "dim", "status", "done", "total", "chat")
_JOB_HISTORY_LOCK = threading.Lock()


def _job_history_path() -> str:
    return os.path.join(LOG_DIR, "job_history.jsonl")


def _prune_job_history_lines(lines: list, max_age_days: int, drop_prefixes: tuple = ()) -> list:
    """按年龄与"按聊天删除"两种口径过滤账目行。

    **看不懂的行原样保留**（坏 JSON、非 dict、缺字段一律不动）。理由不是宽容，
    而是这里做的是**重写整个文件**：读侧（read_job_history）对坏行是"跳过"，
    顶多不显示；而写侧若顺手把读不懂的行删掉，就等于让一次无关的年龄回收
    或"删A聊天"顺手销毁了B的行——账目本身是辅助信息，销毁它换不到任何收益。
    同理，缺 `t` 或 `t` 不是数字的行**不**按年龄删：判不出来就不删。
    """
    cutoff = (time.time() - max_age_days * 86400) if max_age_days and max_age_days > 0 else None
    out = []
    for line in lines:
        if not line.strip():
            continue  # 空行不是数据，收掉不算销毁
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
        # chat 存的是哈希前缀（见 jobs.append 处的 [:12]），所以按前缀比对：
        # 既能匹配截断后的值，也不会因为只存了 12 位就漏删。
        if drop_prefixes and chat and chat.startswith(drop_prefixes):
            continue
        out.append(json.dumps(entry, ensure_ascii=False))
    return out


def purge_job_history(chat_hash: str) -> int:
    """删掉属于该聊天的全部账目行（级联清理与"删除本聊天"必须带上这里）。

    为什么算隐私路径：这一行的字段是 (时间, 维度, 状态, 进度, chat_hash[:12])。
    不含聊天内容，但它是一份**按内容哈希稳定标识"这台机器分析过这份聊天"**的留存记录。
    用户点「删除本聊天」时接口承诺"上传原件 + 全部派生缓存"，而按内容哈希寻址本来就是
    本项目的身份口径（两份同名不同内容的文件是两个聊天，反之同内容就是同一份）——
    所以"这份内容被分析过"这条元信息也归该聊天所有，不该在删除后留下。
    """
    if not chat_hash:
        return 0
    path = _job_history_path()
    with _JOB_HISTORY_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return 0
        kept = _prune_job_history_lines(lines, 0, drop_prefixes=(chat_hash[:12],))
        removed = len([x for x in lines if x.strip()]) - len(kept)
        if removed <= 0:
            return 0
        try:
            tmp = _tmp_sibling(path)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in kept))
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("任务历史按聊天清理失败（可能仍残留哈希前缀）: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return 0
    return removed


def append_job_history(entry: dict, max_age_days: Optional[int] = None) -> None:
    # 默认跟着 LOG_RETENTION_DAYS 走（而不是让每个调用点各传一遍）：这份账目住在
    # LOG_DIR 里，README 对它承诺的就是"日志保留 N 天"。写在函数里还有一条好处——
    # 新增调用点不会忘记传参而悄悄拿到"永不过期"。
    if max_age_days is None:
        max_age_days = LOG_RETENTION_DAYS
    line = {k: entry.get(k) for k in JOB_HISTORY_FIELDS if entry.get(k) is not None}
    if not line:
        return
    path = _job_history_path()
    # 整段在锁内：两个分析同时收尾时，"追加 → 读回 → 对折重写"必须互斥，
    # 否则可能各自读到同一份全量、双双重写，把对方的那一行挤掉。
    with _JOB_HISTORY_LOCK:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            original = len([x for x in lines if x.strip()])
            # 有界保留：① 超行数对折到最近一半；② 超过 LOG_RETENTION_DAYS 的条目删除
            kept = _prune_job_history_lines(lines, max_age_days)
            if len(kept) > JOB_HISTORY_MAX_LINES:
                kept = kept[-(JOB_HISTORY_MAX_LINES // 2) :]
            if len(kept) == original:
                return  # 什么都没掉就不重写文件（绝大多数时候走这条）
            tmp = _tmp_sibling(path)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(x + "\n" for x in kept))
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("任务历史写入失败（不影响分析本身）: %s", e)


def read_job_history(limit: int = 30) -> list:
    """最近的历史，新→旧。文件缺失/坏行如实跳过，不抛。"""
    try:
        with open(_job_history_path(), "r", encoding="utf-8") as f:
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
