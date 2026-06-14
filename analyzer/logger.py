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
"""日志记录模块 — 统一的项目日志系统"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"
LOG_LEVEL = logging.INFO


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


def setup_logger(name: str = "qq_analyzer") -> logging.Logger:
    """配置并返回项目统一的 logger 实例"""
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)

    if logger.handlers:
        return logger

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
    logger.addHandler(file_handler)

    # 2) 控制台日志（兼容 GBK）
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler = _ConsoleHandler()
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    return logger


def get_logger(name: str = "qq_analyzer") -> logging.Logger:
    """获取已配置的 logger（未配置则自动配置）"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
