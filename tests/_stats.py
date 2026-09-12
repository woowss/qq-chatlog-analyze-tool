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
"""渲染型用例的统计落盘辅助：把"统计已可用"从时序问题变成同步保证。

为什么需要它：上传后统计由后台线程算并落盘，而 `store.wait_for_stats` 只在**确实
有线程在跑**时才 join。当同一份 fixture 在多个用例类之间被上传、缓存又被别的用例
清掉时，它会立刻返回而磁盘上没有统计——页面随即因为"会话无统计数据"跳回首页，
整类用例一起红（CI 负载高时必现，本机几乎撞不上）。

所以这里的做法是：等一次，等不到就**当场同步补算**。本组用例考的是页面渲染，
统计管线本身由 test_optimizations / test_review_round* 里那些专门用例钉住，
不该让它们互相污染。
"""

import time

from webapp import store


def ensure_stats(filepath: str, chat_hash: str, timeout: float = 5.0) -> dict:
    """保证 (filepath, chat_hash) 的统计数据已落盘并返回它"""
    store.wait_for_stats(chat_hash, timeout=timeout)
    chat = store._load_chat_cached(filepath)
    mode = store.stats_mode_of(chat)
    stats = store._load_stats(chat_hash, expect_mode=mode)
    if stats is None:
        # 后台线程没跑/跑完没落盘/结果被清理窗口丢掉：这里同步算一次，确定性优先
        stats = store.compute_stats(chat)
        store._save_stats(chat_hash, stats, mode=mode)
        # 仍读不回来说明落盘本身有问题，这里直接暴露，而不是让上层页面 302
        assert store._load_stats(chat_hash, expect_mode=mode) is not None
    return stats


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待某个条件成立（给"最终一致"的断言用，避免写死 sleep）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
