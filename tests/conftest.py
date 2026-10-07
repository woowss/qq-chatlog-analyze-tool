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
"""测试全局护栏：默认不许测试真的调用 LLM API

为什么需要它（两次真实教训，都花了钱）：
1. mock 打在 `group_client._call_api` 上，而三个群级维度走的是 `_analyze_periods`，
   它内部的 `_call_api` 取自 `deepseek_client` 的模块全局——补丁对月度维度无效，
   一次"零成本验证"实际发出十几次调用（数十万 tokens）；
2. 用例里 `POST /api/analyze/<dim>` 会**起后台任务**，请求返回 200 之后任务照跑，
   于是单元测试也能真的出网（数千 tokens）。

本文件用 autouse fixture 换掉 `_get_client`：没有 client，`_call_api` / `_call_vision`
连一次请求都发不出去（漏网时**响亮失败**，而不是悄悄花钱）。只有显式设置
`QQCHAT_TESTS_ALLOW_REAL_LLM=1` 才会关闭本 fixture；这时测试可能产生费用。

只拦这一个入口是刻意的：`_request_with_retry` 是"重试骨架"本身，仓库里有若干用例靠它
配一个假 client 来验证 429 / 402 / 截断等分支——把骨架也换掉，那些用例就失去了被测对象。
需要真实 client 的用例自己在用例内 patch `_get_client` 即可覆盖本 fixture。
"""

import os
import sys

import pytest


@pytest.fixture(autouse=True)
def block_real_llm_calls(monkeypatch):
    """切断真实 API 客户端，避免测试静默消耗用户的额度

    导入必须**放在 fixture 里**（延迟到用例执行时）：pytest 会先收集所有测试文件再跑用例，
    而本文件如果在模块顶层 import 项目模块，`config` 就会在测试设置 `QQCHAT_DATA_DIR`
    之前被加载并缓存——数据目录随即指错地方（会话文件、缓存全落到别处）。
    """
    if os.getenv("QQCHAT_TESTS_ALLOW_REAL_LLM", "").strip() == "1":
        print(
            "[tests] QQCHAT_TESTS_ALLOW_REAL_LLM 已设置：真实 LLM 调用护栏已关闭，测试可能产生费用",
            file=sys.stderr,
        )
        yield
        return

    from analyzer import deepseek_client as dc

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "测试中禁止真实 LLM 调用：请 mock analyzer.deepseek_client._call_api（月度维度）"
            " 或 analyzer.group_client._call_api（成员画像）"
        )

    monkeypatch.setattr(dc, "_get_client", _forbidden)
    yield
