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
import re
import threading
import time
from typing import Iterable, Optional

from config import env_number
from analyzer import purge_marks
from analyzer.cache_policy import refreshing, refresh_cancelled
from analyzer.atomic_write import tmp_sibling, write_json_atomic
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

#: 合法的月份 key 形状。`_month_key` 产出的是 sha256 前 20 位十六进制，但测试与历史
#: 数据里也用过 `m1` / `k1` 这类短名，所以放宽到"安全字符集"而不是死卡 hex——**目的只有
#: 一个：绝不允许分隔符 / 上跳 / 盘符 / NUL 出现在被拼进文件名的值里**。
_MONTH_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_month_key(key) -> bool:
    """这个 key 能不能安全地拼进 `month_<key>.json`？

    为什么必须有这道闸（不是洁癖）：`months` 列表**来自磁盘上的 manifest 文件**，而
    manifest 可以由「导入结果包」写进来——包的内容不受本机控制。key 里只要出现一个
    分隔符，`os.path.join(dir, f"month_{key}.json")` 就能整段逃出缓存目录：
    `month_x/../../某文件` 归一后落在缓存目录之外。Windows 上路径是**先做词法归一**
    再交给文件系统的，所以中间那层目录不存在也照样穿越（Linux 需要中间目录真实存在，
    但本工具的主力平台是 Windows，不能指望这一点）。

    后果有两条，都实测复现过：级联清理按这个路径 `os.remove`，于是**删掉用户机器上
    任意一个 `.json`**；导出则 `zf.write` 同一路径，于是把任意 `.json` 的**内容打包
    进用户会拿去分享的结果包**。这正是 `_bundle_leaf_ok` 那段注释声称已经挡住的
    "一个不含聊天数据的 zip 不该能碰别人那份聊天"——归属校验只管文件名，没管
    manifest 里那个被当成路径使用的 key。
    """
    return isinstance(key, str) and bool(_MONTH_KEY_RE.fullmatch(key))


def _tmp_sibling(path: str) -> str:
    """原子写入的临时文件名：**必须唯一**，不能用固定的 `f"{path}.tmp"`。

    口径只有 `analyzer.atomic_write.tmp_sibling` 一份（那里写着为什么）；保留这个函数名
    是因为用例把它当契约（tests/test_review_round7.py 钉"临时名唯一且仍被清理侧认出"）。
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
    from analyzer import deepseek_client as dc

    digest = hashlib.sha256()
    for part in (dc.DEEPSEEK_MODEL, fingerprint or dc.PROMPT_FINGERPRINT, system_prompt, user_content):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:20]


def migrate_month_cache(old_key: str, new_key: str) -> bool:
    """把"旧指纹写下的"月份缓存改名到新键，返回是否真的迁移了。

    读到旧键的缓存时调用（见 deepseek_client._analyze_periods）：结果本身完全可用，
    但文件名仍是旧键。**改名而不是复制**——这些文件含聊天原文引用，留两份等于把敏感
    内容的留存翻倍。改名之后调用方会把**新键**记进 manifest，否则它会成为"无引用"
    的文件，在宽限期后被孤儿回收删掉（用户为它付过钱）。

    目标已存在时删掉旧的：内容等价（同一段对话 + 同一套提示词），新的那份才是被记账的。
    """
    if not _MONTH_CACHE_DIR or not old_key or old_key == new_key:
        return False
    src, dst = month_cache_path(old_key), month_cache_path(new_key)
    try:
        if os.path.exists(dst):
            os.remove(src)
            return False
        os.replace(src, dst)
        return True
    except OSError as e:
        _warn_write_failure("月份缓存迁移", dst, e)
        return False


def month_cache_path(key: str) -> str:
    """月份文件的路径。**不合法的 key 一律返回空串**，而不是拼出一个能逃出目录的路径。

    这是唯一的路径构造点，放在这里等于把所有调用方（读、写、迁移、清理、导出）一次
    盖住：空串在每一条消费路径上都是"安全的无操作"——`os.path.getmtime("")` /
    `os.path.exists("")` 抛 OSError / 返回 False，`os.remove("")` 抛 OSError，
    全部被既有的 `except OSError` 兜住。合法 key 的行为逐字节不变。
    """
    if not is_safe_month_key(key):
        logger.warning("月份缓存键形状非法，已拒绝拼路径（可能来自被篡改的 manifest）")
        return ""
    return os.path.join(_MONTH_CACHE_DIR, f"month_{key}.json")


def _manifest_path(chat_hash: str) -> str:
    return os.path.join(_MONTH_CACHE_DIR, f"manifest_{chat_hash}.json")


def _read_month_cache(
    key: str, expect_thinking: "Optional[bool]" = None, chat_hash: str = ""
) -> Optional[dict]:
    """读月份缓存。expect_thinking 不是 None 时，思考模式对不上的那份算未命中。

    为什么不把思考模式放进键里：那会让所有既有月份缓存一次性不再命中，而用户已经为
    它们付过钱——这正是维度缓存用 `_think` 后缀、读侧再认旧键的那套迁移机制要避免的事。
    改成"文件里记一个 `_thinking` 标记"：键完全不变（零成本），从这次升级之后写下的
    每一份月份缓存都自带口径声明，切换 LLM_THINKING 之后再也不会串到另一种模式的
    结果上。老文件没有标记（当初是哪种模式已无从判断）→ 按当前模式照常消费一次
    （绝不 retroactively 收费），并**在命中时补上标记**；从这次起它也参与口径核对，
    首次切换模式会重算一次——没有补标记这一步的话，无标记文件会被任何模式永久放行，
    "切换后不再串模式"就只对升级后新写的文件成立。
    """
    if not _MONTH_CACHE_DIR or refreshing():
        return None
    path = month_cache_path(key)
    if not path:  # 非法 key：month_cache_path 拒绝拼路径，这里如实当"没有缓存"
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        created = data.pop("_created", None)  # 元数据不进调用方拿到的结果
        stamped = data.pop("_thinking", None)  # 同上：口径声明只在缓存层内部用
    else:
        return None
    if expect_thinking is not None and stamped is not None and bool(stamped) != bool(expect_thinking):
        # 口径不一致：当成没有缓存，让调用方重新发一次请求（结果会带标记落盘）
        logger.info("月份缓存是另一种思考模式算出来的，本轮重新分析: %s", key[:12])
        return None
    try:
        os.utime(path, None)  # 命中即续期，避免常用缓存被 30 天 TTL 回收
    except OSError:
        pass
    if expect_thinking is not None and stamped is None:
        _restamp_month_cache(path, data, bool(expect_thinking), created, chat_hash)
    return data


def _restamp_month_cache(path: str, data: dict, thinking: bool, created, chat_hash: str = "") -> None:
    """给升级前无口径声明的老文件补记 `_thinking`（只在读侧命中时调用）。

    补当前消费的模式不算撒谎：结果本身无从考证是哪个模式产出的，但从这一刻起
    它被当作该模式的产出对待——之后的模式切换会重算它（付一次费，口径从此正确）。
    失败不致命：这份结果已在内存里，不补只是"下轮切换还会串用一次"。

    这是**读路径上的写**，也是这一族里唯一一处"顺手落盘"的地方，所以要两道闸：

    ① `os.path.exists` 复查——读→补写之间文件可能刚被级联清理删掉（用户换了文件）；
    ② `purge_marks.is_marked(chat_hash)`——只靠 exists 复查挡不住 TOCTOU：检查通过、
       `os.replace` 之前恰好被清理，补写就把刚删掉的月份文件（含聊天原句引用）重新
       造回盘上，而且没有 manifest 引用它，只能等 24 小时宽限期后的孤儿回收。
       症状正是本仓库反复修的那一类："清理报称已删，盘上却又长出含聊天内容的文件"。
       写**新结果**的两处（deepseek_client / group_client）早就查这个标记，这里补齐。
       没传 chat_hash（老调用方/测试）时退化为只做 ①，行为与从前一致。
    """
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


def _write_month_cache(key: str, result: dict, thinking: "Optional[bool]" = None) -> None:
    if not _MONTH_CACHE_DIR or refresh_cancelled():
        return
    path = month_cache_path(key)
    if not path:  # 非法 key 不落盘（见 month_cache_path）
        return
    # _created 是"绝对 90 天"硬上限的依据（cleanup 读它）。缺了它就只能按 mtime 判，
    # 而 mtime 在每次命中时被续期（见 _read_month_cache）——含聊天原句引用的这族
    # 缓存会因此无限期留存。读侧会把它 pop 掉，调用方拿到的结果不变。
    # 放在**最前面**写：清理任务只扫文件头就能取到，不必整份解析这些最敏感的月份文件
    # （见 webapp.store.read_created_at）。
    payload = dict(result) if isinstance(result, dict) else {"result": result}
    payload.pop("_created", None)
    payload.pop("_thinking", None)  # 口径声明由本函数负责写，不接受调用方塞进来的值
    stamp = {"_thinking": bool(thinking)} if thinking is not None else {}
    try:
        # 目录可能被用户按 README 的指引删掉来"彻底清除数据"，而服务还开着：
        # 这里不补目录，月份缓存从此再也写不进去，增量分析静默失效（每月重复付费）。
        write_json_atomic(path, {"_created": time.time(), **stamp, **payload}, mkdir=_MONTH_CACHE_DIR)
    except OSError as e:
        _warn_write_failure("月份缓存", path, e)


def _record_month_usage(chat_hash: str, keys: "Iterable[str]") -> None:
    """把这一批用到的月份缓存记进该聊天的 manifest，供级联清理做引用计数。

    调用方按"一次分析"批量传入（见 _analyze_periods）：原先每完成一个月就
    「读 manifest → 改 → 写回」，24 个月就是 48 次文件 I/O，而写进去的内容
    只是同一个集合在变大。现在整个维度只读一次、写一次。
    """
    if not _MONTH_CACHE_DIR or not chat_hash:
        return
    # 刚被级联清理过的聊天不再重建 manifest。分析动辄跑几分钟，用户在中间换了文件，
    # 跑完的那一轮照样会写月份文件、并在收尾时把 manifest 重新造出来 —— 于是清理
    # 报称"已删"的数据原地复活，而且这份 manifest 还会把那批含聊天原句引用的月份文件
    # 钉成"仍被引用"，连孤儿回收都收不走。用户重新上传同一份内容时
    # （webapp.store.start_stats_job 会撤销标记）这条路径立刻恢复。
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
        # 写回时也过一遍闸：磁盘上既有的清单可能是被篡改的（见 is_safe_month_key），
        # 顺手把它一起洗干净，别让非法 key 通过"读旧 + 并新"继续留在文件里。
        merged = {k for k in merged if is_safe_month_key(k)}
        data["months"] = sorted(merged)
        data["updated"] = time.time()
        # setdefault 语义：绝对上限看的是"首次创建"，重写 manifest 不该把它续期。
        # 重排到最前面写，让清理任务只扫文件头就能取到（见 webapp.store.read_created_at）。
        data["_created"] = data.get("_created") or time.time()
        payload = {"_created": data.pop("_created"), **data}
        try:
            # 同 _write_month_cache：manifest 写不进去 = 这些月份文件会变成"无引用"，
            # 宽限期后被孤儿回收删掉，增量分析白跑。
            write_json_atomic(path, payload, mkdir=_MONTH_CACHE_DIR)
        except OSError as e:
            # manifest 写不进去同样只影响"重新导出时能否复用历史月份"，
            # 但会让增量分析静默失效（每次都全量付费），所以也要出声
            _warn_write_failure("月份缓存 manifest", path, e)
            _MANIFEST_KEYS.pop(name, None)
            # 半成品由 write_json_atomic 负责删掉，不会留在这里。留着它有两处长期的害处，
            # 都不只是"多一个垃圾文件"（这条口径的来历）：
            # ① 下面的清单扫描按 manifest_ 前缀认领活清单，这个 .tmp 会被当成一份真清单读进
            #    引用集，于是该聊天的 month_*.json（含聊天原句引用）被永久钉成"仍被引用"，
            #    purge 与孤儿回收都收不走它；
            # ② 它的哈希段粘着 .json.tmp，级联清理的整段匹配认不出来，也就删不掉。
        else:
            # getmtime 同样必须兜住：这一行原本裸在外面，而调用方 _record_month_usage
            # 是 _analyze_periods 的 **finally** 分支（见 deepseek_client 那处）。
            # 于是"`os.replace` 成功之后、`getmtime` 之前"恰好被并发的级联清理删掉
            # manifest，会让 FileNotFoundError 从 finally 里抛出去，把**已经付费跑完**的
            # 整个维度打掉——用户等了几分钟、花了钱，最后只拿到一个错误。
            # 同函数里其它每个 fs 调用都有 try 包着，只有这一行漏了。
            # 取不到 mtime 时退化成当前时间：这份清单的校验口径是"mtime 变了就重读"，
            # 记新一点只会让下次多读一次文件，方向无害；记 0 反而会被当成"已变更"。
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            _MANIFEST_KEYS[name] = (mtime, set(data["months"]))


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
