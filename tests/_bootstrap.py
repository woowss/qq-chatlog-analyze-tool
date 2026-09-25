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
"""测试进程的前置事务：数据目录隔离 + 禁止真实 LLM 调用

**必须在任何项目模块（config / app / analyzer.*）之前执行**，所以它长成一个"越早越安全"
的引导模块：每个测试文件在 import 项目模块之前调用一次 `bootstrap()`。

以前这段代码在 16 个测试文件里各抄了一份，于是同一处改动要改 16 遍——
"忘了其中几个"只会表现为个别文件的神秘失败（下面第 2 条就是这么漏出来的）。
现在这里只有一份实现。

1. 数据目录：`QQCHAT_DATA_DIR` 指向本次进程独占的临时目录，绝不碰真实
   uploads/ai_cache/flask_session/logs。系统临时目录不可写时退到仓库内。

2. 网络护栏：**没有配置真实 API Key 时**，把 `analyzer.deepseek_client._get_client`
   换成一个"一调用就响亮失败"的哨兵。

   为什么不能只靠 tests/conftest.py：那里是 pytest 的 autouse fixture，而 CI 与
   README 用的是 `python -m unittest discover -s tests` —— unittest 从不读 conftest.py。
   于是 pytest 下有护栏、CI 下没有：一旦本机 .env 配了真 Key，跑 unittest 就可能真的出网
   花钱（项目为这类事故付过两次学费）。本模块对两种跑法一视同仁。

   为什么"没配 Key 才装"就够了：`_get_client()` 在 Key 是占位符时本来返回 None
   （见 deepseek_client._PLACEHOLDER_KEYS），调用方随即失败并报"未配置 API Key"。
   哨兵只是把那句含糊的失败换成明确指路的失败。反过来，本机配了真 Key 时**不装护栏**，
   所以"用真 Key 跑真实网络路径"的排查方式仍然可用——只是必须显式设置
   `QQCHAT_TESTS_ALLOW_REAL_LLM=1` 表示自己知道在做什么。
"""

import atexit
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent

# 让测试既能从仓库根目录 import 包（parser/analyzer/webapp）
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class BlockedLlmCall(AssertionError):
    """测试进程里试图建立真实 LLM 客户端时抛出（继承 AssertionError：unittest 判定为失败）"""


def _forbidden_client(*_args, **_kwargs):
    raise BlockedLlmCall(
        "测试中禁止真实 LLM 调用：请 mock analyzer.deepseek_client._call_api（月度维度）"
        " 或 analyzer.group_client._call_api（成员画像）；"
        "确实要走真实网络时设 QQCHAT_TESTS_ALLOW_REAL_LLM=1 并确保这是有意为之。"
    )


def install_llm_network_guard() -> bool:
    """没有真实 API Key 时切断客户端入口；返回护栏是否装上"""
    if os.getenv("QQCHAT_TESTS_ALLOW_REAL_LLM", "").strip() not in ("", "0", "false"):
        print(
            "[tests] QQCHAT_TESTS_ALLOW_REAL_LLM 已设置：真实 LLM 调用护栏已关闭，测试可能产生费用",
            file=sys.stderr,
        )
        return False

    from analyzer import deepseek_client

    # 真实 Key 已配置（本机 .env 常见）：出网是"应用真实行为"，不装作测试能覆盖它，
    # 留给用例自己 mock——但不再由本模块兜底。
    if deepseek_client.is_api_configured():
        return False

    deepseek_client._get_client = _forbidden_client
    return True


def api_configured_patcher():
    """让"API Key 已配置"成为用例的显式前提，返回可直接 start()/stop() 的 patcher

    为什么需要它：本机 .env 配了真 Key、CI 没有，于是同一批用例在两处行为不同。
    最阴的一类差异是"断言页面里有成本预估区块"——未配置 Key 时模板根本不渲染那段，
    测试就在**没有 Key 的环境（= CI）里红**，却在开发者本机常绿。项目一度有 3 个这样的用例：
    本机 448 全绿、CI 必挂，而没人发现，因为谁都不在"没有 Key"的机器上跑测试。

    用法（类级别，与其它 guard 一起 start/stop）：

        cls._api_guard = api_configured_patcher()
        cls._api_guard.start()
        cls.addClassCleanup(cls._api_guard.stop)

    三个别名都要打：视图层与 API 层都是 `from ... import is_api_configured` 直接绑定的，
    只打其中一个，另一个仍然按真实环境走。
    """
    from analyzer import deepseek_client
    from webapp import api as api_module
    from webapp import views as views_module

    return _MultiPatcher(deepseek_client, views_module, api_module)


class _MultiPatcher:
    """把同一个"已配置"返回值同时装到多个模块上的最小 patcher（有 start/stop 接口）"""

    def __init__(self, *modules):
        from unittest import mock

        self._patchers = [
            mock.patch.object(module, "is_api_configured", return_value=True) for module in modules
        ]

    def start(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def stop(self):
        for patcher in reversed(self._patchers):
            patcher.stop()


def _make_data_dir() -> str:
    """建一个本次进程独占的数据目录

    优先系统临时目录；%TEMP% 不可写（只读沙箱/受限容器）时退到仓库内
    `tests/.tmp_baseline/`——那里一定是可写的，因为仓库本身就是可写的。
    这是"鸡生蛋"的绕法：不再让数据目录本身依赖 %TEMP% 可用。
    """
    try:
        return tempfile.mkdtemp(prefix="qqchatlog-test-")
    except OSError:  # 只读/不可用的 %TEMP% 会抛 PermissionError / FileNotFoundError 等
        fallback_root = _TESTS_DIR / ".tmp_baseline"
        fallback_root.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="qqchatlog-test-", dir=str(fallback_root))


def _drop_data_dir(path: str) -> None:
    """跑完删掉自己建的数据目录

    先关日志：否则本清理先跑、logging 的 shutdown 又把 app.log 写回来，留下一堆空目录。
    """
    logging.shutdown()
    shutil.rmtree(path, ignore_errors=True)


#: 这些环境变量会改变**被测行为**，而 CI 上它们一个都不存在。开发机的 shell 里若留着
#: 其中一个（例如为了本地跑带口令的实例而 export ACCESS_PASSWORD），整套用例会跟着变：
#: 实测过一次——shell 里留着 `ACCESS_PASSWORD=s3cret`，505 条里红了 78 条，清一色是
#: "页面被跳到登录页"，与任何代码改动都无关，很容易被误读成"这轮改动把应用改坏了"。
#: 这里统一清掉，让"本机 = CI"成为**前提**，而不是靠开发者记得 unset。
#: 需要非默认值的用例请显式打桩（登录/限流相关用例本来就是这么做的），
#: 这样"这条用例依赖什么前提"写在用例里，而不是藏在环境里。
_AMBIENT_KEYS_TO_CLEAR = (
    "ACCESS_PASSWORD",  # 设了口令 → 所有页面都要登录
    "ALLOWED_ORIGINS",  # 影响 POST 的 Origin 校验
    "FLASK_HOST",  # 回环与否决定：非回环+无口令直接 503、Secure cookie 的 auto 判定
    "QQCHAT_COOKIE_SECURE",  # Secure cookie 的显式覆盖
    "LOG_REDACT_NAMES",  # 日志脱敏（有源码级守卫用例依赖默认开）
    "QQCHAT_GROUP_CHAT",  # 私聊轨 / 群聊轨 / 两方归并——会整类改变断言
    "QQCHAT_ALLOW_MULTI_PARTY",  # 上面那个的旧别名
    "QQCHAT_FACE_IMAGES",  # 打开会影响习惯页/报告页的渲染
    "QQCHAT_MEDIA_DIR",  # 影响"图片理解是否可用"
    "QQCHAT_LOGIN_MAX_ATTEMPTS",  # 限流用例写死了默认值
    "QQCHAT_LOGIN_WINDOW_SECONDS",
    "QQCHAT_MONTH_CACHE",  # 下面会 setdefault 成 0（增量缓存会破坏"调用次数"断言的确定性）
)


def neutralize_ambient_env() -> list:
    """清掉会改变被测行为的"环境类"变量；返回实际清掉的名字（供用例断言）

    必须在 import config / webapp / analyzer **之前**调用：那些模块在 import 期就把值读走了
    （这正是每个测试文件都要先 `from _bootstrap import bootstrap; bootstrap()` 的原因）。
    """
    return [name for name in _AMBIENT_KEYS_TO_CLEAR if os.environ.pop(name, None) is not None]


def bootstrap() -> str:
    """准备数据目录并装护栏；返回本次进程的数据目录

    幂等：同一进程内多次调用只做一次（unittest discover 会 import 多个测试文件）。
    """
    neutralize_ambient_env()

    data_dir = os.environ.get("QQCHAT_DATA_DIR", "").strip()
    if not data_dir:
        data_dir = _make_data_dir()
        os.environ["QQCHAT_DATA_DIR"] = data_dir
        atexit.register(_drop_data_dir, data_dir)  # 只清理自己建的；外部指定的目录一律不动

    # 本进程的临时目录统一挪到数据目录下，两个好处：
    # 1) %TEMP% 只读受限的环境里 tempfile.* 不再直接 PermissionError；
    # 2) 用例产生的临时 json/图片/表情包随数据目录一起回收，不在 %TEMP% 留垃圾。
    tmp_root = os.path.join(data_dir, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tempfile.tempdir = tmp_root

    # 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
    # 需要它的用例会自行开启并指向临时目录。
    # （用赋值而不是 setdefault：neutralize_ambient_env 已经把它清掉，这里要把值**定死**，
    # 否则开发机 .env 里的 QQCHAT_MONTH_CACHE=1 会让同一批用例在本机红。）
    os.environ["QQCHAT_MONTH_CACHE"] = "0"

    install_llm_network_guard()
    return data_dir
