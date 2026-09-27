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
"""临时文件与缓存的生命周期回收

生命周期分层（时间口径统一按 mtime / 缓存内的 _created）：
- uploads/ 与 flask_session/：24 小时（原始聊天记录，敏感，尽快清）。
  注意 uploads/ 里不止有平铺的 JSON：看图所需的图片副本在 uploads/media/<哈希>/，
  所以这一层必须**递归**回收（见 _purge_tree），否则图片副本会永远留在盘上。
- ai_cache/ 与 stats_cache/：滑动 30 天 + 绝对 90 天双上限
- 日志：按天轮转由 handler 负责，这里额外回收历史遗留的 5×5MB 式 app.log.N
  （换成按天保留后那些数字后缀文件不再被新 handler 认领，会永久占盘）。

早先清理只在 import 时跑一次，长跑进程永不回收；后来挂到上传路径，可
"长期开着不上传"的实例又漏了。现在同时由启动与每个请求（去抖）触发。
"""

import os
import time
from typing import Optional

from flask import request

from config import (
    AI_CACHE_DIR,
    CACHE_MAX_DAYS,
    CACHE_SLIDE_DAYS,
    LOG_DIR,
    LOG_RETENTION_DAYS,
    SESSION_FILE_DIR,
    STATS_CACHE_DIR,
    UPLOAD_FOLDER,
)
from analyzer.deepseek_client import sweep_orphan_month_cache
from analyzer.logger import get_logger
from webapp.security import BYPASS_ENDPOINTS
from webapp.store import read_created_at

logger = get_logger("app")


def _cache_created_at(path: str) -> float:
    """缓存的创建时间：新格式写在 _created 字段里，旧格式回退到 mtime。

    取法走 store 的有界扫描（只读文件头/尾各 8KB），不在这里整份 json.load：
    清理是挂在 before_request 上的，而 ai_cache/ 里最大的就是那些含整月结果的
    month_*.json——为了一个时间戳把它们全量解析，代价由碰巧触发清理的那个请求付。

    两个时间都取不到时返回 0.0（epoch）而不是 time.time()：方向很重要——判成
    "刚刚创建"等于让一个读不到时间的缓存文件**永远**逃过回收，而这里装的是含聊天
    内容的派生数据；判成"远古"只是让上层尝试删一次，删不掉会留下告警（见
    _purge_dir/_purge_tree 对 OSError 的兜底）。
    """
    created = read_created_at(path)
    if created is not None:
        return created
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _purge_dir(directory: str, expired, name_prefix: str = "") -> int:
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    failed = 0
    for name in entries:
        if name_prefix and not name.startswith(name_prefix):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and expired(path):
                os.remove(path)
                removed += 1
        except OSError as e:
            # 必须出声。_cache_expired 对"读不到时间"是 fail-closed（判成该删），
            # 也就是这类文件删不掉时会**每小时重试、永远删不掉、永远没人知道**；
            # 而 ai_cache/ 与 stats_cache/ 里是含聊天内容引用的派生数据，README 对它的
            # 承诺是"到期自动回收"。本文件 _cache_created_at 的注释也白纸黑字写着
            # "删不掉会留下告警（见 _purge_dir/_purge_tree 对 OSError 的兜底）"——
            # 静默 continue 等于让那句承诺与实现互相矛盾。
            # 按需清理那条路径早就做对了（store._purge_chat_caches 的
            # "缓存文件删除失败（可能仍残留敏感内容）"），这里补的是同一件事的另一半。
            failed += 1
            logger.warning("过期文件删除失败（可能仍残留敏感内容）: %s (%s)", name, e)
    if failed:
        logger.warning("目录 %s 有 %d 个过期文件未能删除，下个清理周期会重试", directory, failed)
    return removed


def _purge_tree(root: str, expired) -> int:
    """递归回收 root 下过期的普通文件，并顺带清掉回收后变空的目录。

    为什么不能用 _purge_dir：它只处理目录下的**普通文件**（os.path.isfile），
    而 uploads/media/<chat_hash>/ 是子目录——整棵子树连同里面最敏感的图片本体
    都会被跳过、永不回收，与"图片副本随 uploads/ 的 24 小时策略回收"的承诺相悖。
    """
    removed = 0
    failed = 0
    root_abs = os.path.abspath(root)
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if expired(path):
                    os.remove(path)
                    removed += 1
            except OSError as e:
                # 同上，且这一层是 uploads/ 与 flask_session/：原始聊天记录与登录态，
                # 删不掉的后果比派生缓存更直接，更不能静默。
                failed += 1
                logger.warning("过期文件删除失败（可能仍残留敏感内容）: %s (%s)", path, e)
        if os.path.abspath(dirpath) != root_abs:
            try:
                os.rmdir(dirpath)  # 只删空目录：还有未过期内容时 rmdir 自然失败
            except OSError:
                pass  # 目录非空是常态而非故障，报出来只会淹没真正的告警
    if failed:
        logger.warning("目录树 %s 有 %d 个过期文件未能删除，下个清理周期会重试", root, failed)
    return removed


def _older_than(now: float, seconds: float):
    """返回"该文件是否早于 now-seconds"的判定函数。

    显式把 now 冻进闭包，而不是让 lambda 直接引用外层的循环变量：读的人不必
    再去确认 now 在后面有没有被改写（原先两个 lambda 共享同一个 now，加一处
    赋值就会同时改变两处的判定口径）。
    """

    def expired(path: str) -> bool:
        return now - os.path.getmtime(path) > seconds

    return expired


def cleanup_old_files(
    max_age_seconds: int = 86400,
    cache_max_age: Optional[int] = None,
    cache_hard_max_age: Optional[int] = None,
) -> int:
    """删除过期的临时文件与缓存；日志按天保留 LOG_RETENTION_DAYS 天

    缓存双上限默认取 QQCHAT_CACHE_SLIDE_DAYS / QQCHAT_CACHE_MAX_DAYS（30/90 天）。
    此前这两个数字写死在函数默认参数里、没有任何配置出口——"30 天没浏览就删掉
    已付费的 AI 结果"对低频使用者是真伤害，而调长/关闭是用户的正当权利
    （数据始终只在本机）。0 = 对应规则永不过期；两条都关时启动横幅会提示
    "已偏离 README 的到期回收承诺"。显式传参的调用方（测试）不受配置影响。
    """
    if cache_max_age is None:
        cache_max_age = CACHE_SLIDE_DAYS * 86400
    if cache_hard_max_age is None:
        cache_hard_max_age = CACHE_MAX_DAYS * 86400
    never_expire = cache_max_age <= 0 and cache_hard_max_age <= 0
    now = time.time()
    cleaned = 0
    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR):
        cleaned += _purge_tree(directory, _older_than(now, max_age_seconds))

    def _cache_expired(path: str) -> bool:
        """缓存的双上限：滑动（按 mtime，命中即续期）+ 绝对（按 _created）

        取不到 mtime 时按"该回收"处理（fail-closed）：这两个目录装的是含聊天内容的
        派生数据，判不出来的正确方向是尝试删掉并留一行日志，而不是当作新文件永远
        留下。删不掉由调用方兜住（_purge_dir/_purge_tree 都捕获 OSError 并跳过）。
        例外：两条规则都被显式关掉时，"判不出时间"不再是删除的理由——用户要的就是
        "永不过期"，宁可留下也不删。
        """
        if never_expire:
            return False
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return True
        if cache_max_age > 0 and now - mtime > cache_max_age:
            return True
        if cache_hard_max_age > 0:
            return now - _cache_created_at(path) > cache_hard_max_age
        return False

    for directory in (AI_CACHE_DIR, STATS_CACHE_DIR):
        cleaned += _purge_dir(directory, _cache_expired)
    # 所有轮转出的旧日志一律按保留天数回收：既覆盖历史遗留的按大小产物
    # （app.log.1 / app.log.2…，新 handler 不认领它们），也兜住应用长期闲置
    # 时 TimedRotatingFileHandler 来不及在轮转中删掉的日期文件。
    log_cutoff = LOG_RETENTION_DAYS * 86400
    cleaned += _purge_dir(LOG_DIR, _older_than(now, log_cutoff), name_prefix="app.log.")
    try:
        cleaned += sweep_orphan_month_cache()
    except Exception as e:  # 回收失败不影响主流程
        logger.warning("月份缓存回收失败: %s", e)
    if cleaned:
        logger.info("已清理 %d 个过期文件", cleaned)
    return cleaned


_last_cleanup = [0.0]


def maybe_cleanup(interval_seconds: int = 3600) -> None:
    """长跑进程也要清理：按间隔去抖，避免每个请求都全目录扫描"""
    now = time.time()
    if now - _last_cleanup[0] < interval_seconds:
        return
    _last_cleanup[0] = now
    try:
        cleanup_old_files(max_age_seconds=86400)
    except Exception as e:  # 清理失败不能影响请求
        logger.warning("定期清理失败: %s", e)


def startup_cleanup() -> None:
    """应用组装时跑一次全量回收，并把去抖计时归零"""
    cleanup_old_files(max_age_seconds=86400)
    _last_cleanup[0] = time.time()


def cleanup_hook():
    """before_request 钩子：健康探针不触发清理（它可能每秒被调一次）"""
    if request.endpoint in BYPASS_ENDPOINTS:
        return None
    return maybe_cleanup()


def register(app):
    """每个请求过一遍带间隔去抖的清理：只开着不上传也不再漏回收"""
    app.before_request(cleanup_hook)
