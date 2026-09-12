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
"""进程级"正在关闭"标志 —— 让 Ctrl+C 先停手，再退出

为什么需要它：月份级分析是"并发窗口内逐月调用 API"。进程被 Ctrl+C / SIGTERM
打断时，线程池里的月份是**已经排上队**的——不设标志的话，收到信号到进程真正
退出之间还会继续往外发新请求；这些请求的钱已经花了，结果却因为进程退出而丢在
半路（线程是 daemon，不会等）。

有了这个标志：信号处理器先置位，分析循环里所有"是否还要再启动一个新任务"的
判断都会看到它并停止派发；已经在跑的那一个月允许跑完，结果照常落盘。
三处消费点：analyzer.deepseek_client._analyze_periods（逐月）、
webapp.jobs._run_analyze_all（逐维度）、webapp.jobs._run_job（收尾判定）。

放在独立模块而不是 deepseek_client 里：webapp 层要用它，但 webapp 不该为了
一个布尔量去 import 整个 API 客户端（那会连带初始化 openai 客户端与密钥检查）。
"""

import threading

#: 置位即"别再发起新的付费调用"。用 Event 而不是 bool：跨线程可见性由它保证。
_SHUTDOWN = threading.Event()


def request_shutdown() -> bool:
    """请求关闭；返回 True 表示这是第一次请求（调用方据此决定要不要打日志）"""
    if _SHUTDOWN.is_set():
        return False
    _SHUTDOWN.set()
    return True


def shutdown_requested() -> bool:
    """是否已请求关闭（分析循环在每个派发点检查它）"""
    return _SHUTDOWN.is_set()


def reset_shutdown() -> None:
    """清除标志。只给测试用：进程内它是单向的，正常流程没有"取消关闭"这回事。"""
    _SHUTDOWN.clear()
