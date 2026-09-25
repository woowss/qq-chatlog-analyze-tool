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
import re
import threading
import time
from collections import deque
from typing import Optional

from flask import session

from config import AI_CACHE_DIR, DEEPSEEK_MODEL, STATS_CACHE_DIR
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
    calc_milestones,
)
from analyzer.group_stats import compute_group_stats
from analyzer.deepseek_client import fingerprint_for_dimension, purge_month_cache, thinking_enabled
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
STATS_SCHEMA_VERSION = 4
# 群聊统计是**另一套形状**，因此用独立的版本号与 mode 字段，而不是把私聊的 v4 往上顶：
# 顶版本号会让所有既有私聊统计缓存立刻失效（升级后第一次打开页面白等一次计算），
# 而私聊缓存的形状其实一个字都没变。mode 是权威判别字段，_v 只在同 mode 内有意义。
# g1 → g2：interaction 增加 explicit_/mention_ 矩阵与覆盖率计数（群聊轨尚未对外，无影响）
GROUP_STATS_SCHEMA_VERSION = 2

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
    with _META_LOCK:
        _RECENTLY_PURGED.append(chat_hash)
        _STATS_ERRORS.pop(chat_hash, None)


def _unmark_purged(chat_hash: str) -> None:
    with _META_LOCK:
        try:
            _RECENTLY_PURGED.remove(chat_hash)
        except ValueError:
            pass


def _is_recently_purged(chat_hash: str) -> bool:
    with _META_LOCK:
        return chat_hash in _RECENTLY_PURGED


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
    os.makedirs(STATS_CACHE_DIR, exist_ok=True)
    path = _stats_path(chat_hash)
    tmp = f"{path}.tmp"
    payload = dict(stats)
    payload.pop("_created", None)  # 不让调用方塞进来的值生效：首次创建时间只认盘上那份
    payload["mode"] = mode
    payload["_v"] = GROUP_STATS_SCHEMA_VERSION if mode == STATS_MODE_GROUP else STATS_SCHEMA_VERSION
    # 统计是确定性的，重算/补算（词频懒算后回写）都会重写同一个文件；
    # 绝对上限看的是**首次**创建时间，所以这里必须保留盘上已有的值——
    # 否则"每次查看词频就把 90 天硬上限往后推一格"，与 README 的保留承诺相反。
    created = read_created_at(path) or time.time()
    try:
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
    with _STATS_LOCK:
        if chat_hash in _STATS_THREADS:
            return

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
    """
    if stats is None or stats.get("word_freq"):
        return stats
    filepath = session.get("filepath")
    if not filepath or not os.path.exists(filepath):
        return stats
    try:
        chat = _load_chat_cached(filepath)
        stats["word_freq"] = calc_word_freq(chat, top_n=80)
        _save_stats(chat_hash, stats, mode=stats_mode_of(chat))
    except Exception as e:  # 词频失败不该拖垮页面
        logger.error("词频统计失败: %s", e)
        stats.setdefault("word_freq", {"self": [], "other": []})
    return stats


# ---------------------------------------------------------------------------
# AI 维度缓存（内容寻址 + 指纹键）
# ---------------------------------------------------------------------------


def _cache_path(dimension: str, chat_hash: str) -> str:
    # 键含提示词/格式指纹：由 SYSTEM_PROMPT_* 与对话格式化函数自动哈希而来，改了提示词或
    # 输入格式后旧缓存自动失效（不再依赖人工 bump 版本号）。**按维度取**：群聊维度用群聊
    # 指纹，私聊维度用私聊指纹——这样新增/修改群聊提示词不会让私聊缓存文件名发生变化
    # （文件名一变，用户就得为同样的分析重新付费）。
    # 键含思考模式：同一模型开关 thinking 前后的结果差异很大，必须分开存放，
    # 否则切换 LLM_THINKING(_DIMS) 后会命中另一种模式的旧结果（看起来"没区别"）。
    # 非思考模式不加后缀，保持既有缓存键兼容。
    suffix = "_think" if thinking_enabled(dimension) else ""
    fingerprint = fingerprint_for_dimension(dimension)
    return os.path.join(AI_CACHE_DIR, f"{dimension}_{chat_hash}_{DEEPSEEK_MODEL}_{fingerprint}{suffix}.json")


def _purge_chat_caches(chat_hash: str) -> int:
    """删除某聊天文件的全部缓存（跨版本/模型）。聊天源文件被删时联动调用，
    避免派生的分析结果（含聊天内容摘要）成为孤儿残留。"""
    if not chat_hash:
        return 0
    # 进程内已解析的 ChatData 也一并丢弃。它的键是 (路径, mtime, size)，而这次
    # 清理只动派生缓存、**不动源文件**——所以"源文件被同名同大小重建"时会命中
    # 残留的旧对象，把上一条聊天的内容当成新的喂下去。宁可多解析一次。
    with _CHAT_CACHE_LOCK:
        _CHAT_CACHE.clear()
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
            except OSError as e:
                # 静默吞掉会让用户以为隐私数据已经清干净，实际磁盘上还留着
                # （Windows 上文件被占用尤其常见），所以这里必须出声。
                logger.warning("缓存文件删除失败（可能仍残留敏感内容）: %s (%s)", name, e)
    # 本地统计缓存
    try:
        os.remove(_stats_path(chat_hash))
        removed += 1
    except FileNotFoundError:
        pass  # 本来就没有，属正常情形，不必报
    except OSError as e:
        logger.warning("统计缓存删除失败（可能仍残留敏感内容）: %s (%s)", _stats_path(chat_hash), e)
    # 月份级缓存：删 manifest，并回收不再被其他聊天引用的月份文件
    removed += purge_month_cache(chat_hash)
    # 看图用的图片副本（uploads/media/<chat_hash>/）：源文件都换了/没了，
    # 派生出来的图片本体必须一起走，否则它只受"24 小时 mtime 回收"约束，
    # 而在那之前一直是盘上最敏感的一批数据。
    try:
        from analyzer import vision  # 延迟导入，避免 store→vision 模块级依赖

        removed += vision.purge_session_media(chat_hash)
    except Exception as e:  # 回收失败不影响主流程
        logger.warning("图片副本回收失败: %s", e)
    # 记下这次清理：若该哈希的后台统计线程还在跑，落盘前会检查这里并放弃写入，
    # 避免把刚清掉的缓存"复活"成没人认领的孤儿。
    _mark_purged(chat_hash)
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
