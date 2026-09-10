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
"""日志记录模块 — 统一的项目日志系统

所有模块的 logger 都挂在同一个包级 logger（qqchatlog）下并向上传播，
handler 只在包级配置一次：早先每个 logger 名各建一套 handler，导致
"app" 与 "deepseek" 两个 logger 各持一个指向 logs/app.log 的
RotatingFileHandler，Windows 上轮转时 os.rename 会因另一个句柄占用而
抛 PermissionError，轮转失效且日志行丢失。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"
LOG_LEVEL = logging.INFO
PACKAGE_LOGGER = "qqchatlog"


class _ConsoleHandler(logging.StreamHandler):
    """兼容 Windows GBK 终端的控制台处理器，自动替换不可打印字符"""

    def __init__(self):
        super().__init__(sys.stdout)
        self._enc = sys.stdout.encoding or "utf-8"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except UnicodeEncodeError:
            try:
                # 用 GBK 编码，不可打印字符替换为 ?
                buf = (msg + self.terminator).encode(self._enc, errors="replace")
                self.stream.write(buf.decode(self._enc))
                self.flush()
            except Exception:
                self.handleError(record)
        except Exception:
            self.handleError(record)


def _configure(base: logging.Logger) -> None:
    """给包级 logger 装一次 handler（文件 + 控制台），重复调用无副作用"""
    if base.handlers:
        return
    base.setLevel(LOG_LEVEL)
    base.propagate = False        # 不再向 root 传播，避免被第三方/默认配置重复输出

    # 1) 文件日志 — 按大小轮转，保留 5 份 × 5MB
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(file_fmt)
    base.addHandler(file_handler)

    # 2) 控制台日志（兼容 GBK）
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler = _ConsoleHandler()
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(console_fmt)
    base.addHandler(console_handler)


def get_logger(name: str = PACKAGE_LOGGER) -> logging.Logger:
    """获取子 logger（handler 只挂在包级 logger 上，各子 logger 向上传播）"""
    base = logging.getLogger(PACKAGE_LOGGER)
    _configure(base)
    clean = (name or "").strip()
    if not clean or clean in (PACKAGE_LOGGER, "qq_analyzer"):
        return base
    if clean.startswith(PACKAGE_LOGGER + "."):
        clean = clean[len(PACKAGE_LOGGER) + 1:]
    child = logging.getLogger(f"{PACKAGE_LOGGER}.{clean}")
    child.setLevel(LOG_LEVEL)
    return child


def setup_logger(name: str = "qq_analyzer") -> logging.Logger:
    """兼容旧调用：等价于 get_logger(name)"""
    return get_logger(name)
