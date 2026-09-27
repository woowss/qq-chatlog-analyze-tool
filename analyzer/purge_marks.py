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
""" "这个聊天的缓存刚被清掉"的进程内标记 —— 所有派生缓存的写方共用的黑名单。

为什么单独成一个模块：这份标记有两个消费方向，而且都必要——
1. webapp 层（后台统计线程、词频回写）：清理之后晚到的线程不该把结果写回盘上；
2. analyzer 层（月份缓存、manifest）：分析动辄跑几分钟，用户在中间换了文件，
   跑完的那个月照样会落盘，manifest 也会被"重建"出来 —— 于是清理报称已删的数据
   原地复活，而且此时已经没人引用它了。

analyzer 不能 import webapp（那是反向依赖，store 本来就 import analyzer），所以把
标记下沉到谁都能 import 的地方。原先它只有 `_RECENTLY_PURGED` 一份队列住在 store 里，
analyzer 那条路读不到 —— 正是上述第 2 点的缺口。

语义：只保留最近 256 个哈希（清理是低频操作，够用），命中即视为"刚被清掉，别再写"。
重新上传同一份文件会撤销标记（见 webapp.store.start_stats_job → unmark）：那是新会话
的正当计算，不是复活。
"""

import threading
from collections import deque

_MARKS: deque = deque(maxlen=256)
_LOCK = threading.Lock()


def mark(chat_hash: str) -> None:
    """记下"该聊天的派生缓存刚被清理掉"（调用方随后不该再写任何一份）"""
    if not chat_hash:
        return
    with _LOCK:
        # 去重：unmark 一次只撤一条。同一个哈希被清理两次（双击上传会触发两次
        # switching；两个会话先后换走同一内容也可能）若存成两份，重新上传时一处
        # 撤销就只剩一份残留 —— 残留标记会把该聊天之后的分析判成"已废弃"
        # （任务静默取消、缓存不落盘），正是本模块要防的症状本身。
        if chat_hash not in _MARKS:
            _MARKS.append(chat_hash)


def unmark(chat_hash: str) -> None:
    """撤销标记：用户重新上传了同一份内容，这是正当的新计算"""
    if not chat_hash:
        return
    with _LOCK:
        try:
            _MARKS.remove(chat_hash)
        except ValueError:
            pass


def is_marked(chat_hash: str) -> bool:
    with _LOCK:
        return chat_hash in _MARKS


def clear() -> None:
    """测试用：把标记表清空，避免用例之间互相污染"""
    with _LOCK:
        _MARKS.clear()
