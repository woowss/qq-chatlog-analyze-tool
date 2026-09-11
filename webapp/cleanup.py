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
- uploads/ 与 flask_session/：24 小时（原始聊天记录，敏感，尽快清）
- ai_cache/ 与 stats_cache/：滑动 30 天 + 绝对 90 天双上限
- 日志：按天轮转由 handler 负责，这里额外回收历史遗留的 5×5MB 式 app.log.N
  （换成按天保留后那些数字后缀文件不再被新 handler 认领，会永久占盘）。

早先清理只在 import 时跑一次，长跑进程永不回收；后来挂到上传路径，可
"长期开着不上传"的实例又漏了。现在同时由启动与每个请求（去抖）触发。
"""
import json
import os
import time

from config import (
    AI_CACHE_DIR,
    LOG_DIR,
    LOG_RETENTION_DAYS,
    SESSION_FILE_DIR,
    STATS_CACHE_DIR,
    UPLOAD_FOLDER,
)
from analyzer.deepseek_client import sweep_orphan_month_cache
from analyzer.logger import get_logger

logger = get_logger("app")


def _cache_created_at(path: str) -> float:
    """缓存的创建时间：新格式写在 _created 字段里，旧格式回退到 mtime"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("_created"), (int, float)):
            return float(data["_created"])
    except (OSError, json.JSONDecodeError):
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _purge_dir(directory: str, expired, name_prefix: str = "") -> int:
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    for name in entries:
        if name_prefix and not name.startswith(name_prefix):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and expired(path):
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_old_files(max_age_seconds: int = 86400, cache_max_age: int = 30 * 86400,
                      cache_hard_max_age: int = 90 * 86400) -> int:
    """删除过期的临时文件与缓存；日志按天保留 LOG_RETENTION_DAYS 天"""
    now = time.time()
    cleaned = 0
    for directory in (UPLOAD_FOLDER, SESSION_FILE_DIR):
        cleaned += _purge_dir(directory, lambda p: now - os.path.getmtime(p) > max_age_seconds)
    for directory in (AI_CACHE_DIR, STATS_CACHE_DIR):
        cleaned += _purge_dir(directory, lambda p: (
            now - os.path.getmtime(p) > cache_max_age
            or now - _cache_created_at(p) > cache_hard_max_age))
    # 所有轮转出的旧日志一律按保留天数回收：既覆盖历史遗留的按大小产物
    # （app.log.1 / app.log.2…，新 handler 不认领它们），也兜住应用长期闲置
    # 时 TimedRotatingFileHandler 来不及在轮转中删掉的日期文件。
    log_cutoff = LOG_RETENTION_DAYS * 86400
    cleaned += _purge_dir(
        LOG_DIR,
        lambda p: now - os.path.getmtime(p) > log_cutoff,
        name_prefix="app.log.")
    try:
        cleaned += sweep_orphan_month_cache()
    except Exception as e:                       # 回收失败不影响主流程
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
    except Exception as e:                      # 清理失败不能影响请求
        logger.warning("定期清理失败: %s", e)


def startup_cleanup() -> None:
    """应用组装时跑一次全量回收，并把去抖计时归零"""
    cleanup_old_files(max_age_seconds=86400)
    _last_cleanup[0] = time.time()


def register(app):
    """每个请求过一遍带间隔去抖的清理：只开着不上传也不再漏回收"""
    app.before_request(lambda: maybe_cleanup())
