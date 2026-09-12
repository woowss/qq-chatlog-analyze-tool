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
"""群聊参与者身份 —— 只回答"谁在群里说话、叫什么、怎么唯一区分"

设计约束（与 parser/qq_parser.py 的分工）：
- 本模块是**纯函数层**：输入已经过统计口径过滤的消息列表与自己的 UID，输出 Participant。
  它不解析 JSON、不读配置、不导入 qq_parser（否则与 qq_parser 互相导入）。
- Participant **只承载身份，不承载统计快照**（没有 msg_count/char_count）。条数一律由
  统计层（analyzer/group_stats.py）按同一口径现算：解析期算一遍、统计层再算一遍，
  两处数字迟早会对不上，而界面与 prompt 都引用它——这正是本项目反复强调的"口径唯一"。

"谁算一位参与者"的口径由调用方决定：本模块的 collect_participants 不设门槛
（有一条统计口径消息就算一位），"是不是群聊"由 qq_parser 的多人防线按门槛判定。
两者必须分开：门槛用于**判定**（避免把混入占位 sender 的私聊误判成群聊），
而成员列表用于**归属**（漏掉谁，谁的消息就会凭空消失）。
"""

from dataclasses import dataclass, field
from typing import Optional

#: 显示名去重时追加的 UID 后缀长度（够区分同群重名，又不至于刷屏）
UID_SUFFIX_LEN = 4

#: 占位 sender 的显示名（导出器给系统类消息安排的假发言人）
PLACEHOLDER_NAMES = frozenset({"系统消息", "未知用户", "未知成员", "系统提示", "群系统消息"})
#: 占位 sender 的 UID 前缀（实测导出器写 "未知uid未知" 这类）
PLACEHOLDER_UID_PREFIXES = ("未知", "unknown", "system")


def is_placeholder_sender(uid: str, name: str = "") -> bool:
    """这个 sender 是不是导出器给系统类消息安排的"占位发言人"。

    为什么必须单独判一次：占位 sender 的 system 标记并不齐全。实测某份 数万条私聊导出里
    上百条占位消息中就有 1 条没有 system 标记；群聊导出里占位 sender 上百条
    且**全部**带标记。只要有一条漏标，它就会在群成员列表里变成一个"只说过一句话的幽灵成员"，
    进而在成员画像里被 AI 认真分析一遍——那比数字少一条难看得多。

    判据只认导出器的稳定约定（UID 前缀 + 名字），不做启发式猜测：宁可把可疑的算作成员
    （用户能在界面上看到它并反馈），也不要凭空把真人塞进"未知"桶。
    """
    u = (uid or "").strip()
    n = (name or "").strip()
    if n in PLACEHOLDER_NAMES:
        return True
    low = u.lower()
    return any(low.startswith(p) for p in PLACEHOLDER_UID_PREFIXES)


@dataclass
class Participant:
    """群聊参与者身份。

    name 是**唯一显示名**：同名成员会被追加 `#uid4`（见 unique_display_names），
    因此 prompt、统计表、模板可以放心用 name 当键——否则群里两个"小明"会在模型
    与界面里合并成同一个人。raw_name 保留导出文件里的原样显示名，供界面如实展示。
    """

    uid: str
    name: str
    raw_name: str = ""
    is_self: bool = False

    def __post_init__(self) -> None:
        if not self.raw_name:
            self.raw_name = self.name


@dataclass
class _Acc:
    """聚合中间态：按 UID 累计条数与出现过的显示名"""

    uid: str
    count: int = 0
    names: dict = field(default_factory=dict)  # 显示名 -> 出现次数
    first_seen: int = 0

    def add(self, name: str, order: int) -> None:
        self.count += 1
        if name:
            self.names[name] = self.names.get(name, 0) + 1
        if not self.first_seen:
            self.first_seen = order

    def display_name(self) -> str:
        """该 UID 的显示名：用得最多的那个（同一人可能改过昵称/群名片）"""
        if not self.names:
            return ""
        # 次数降序 + 名字升序：同一人多个昵称时结果确定（不随消息顺序漂移）
        return sorted(self.names.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def collect_participants(messages: list, self_uid: str) -> list[Participant]:
    """按统计口径消息收集参与者，返回**按发言条数降序**的 Participant 列表。

    调用方必须传入已过滤（is_statistical）的消息；以下两类消息**不计入任何成员**：
    - sender_uid 为空（没有归属，属于"未知"桶，由统计层单独统计）；
    - 占位 sender（见 is_placeholder_sender）。
    两者都归"未知"桶之后，成员条数之和 + 未知条数 == 总条数 这条对账才成立——
    对不上账的报表没人敢信。

    排序键是 (条数降序, uid 升序)：uid 只用于打破平手，保证同一份文件每次得到相同的
    名单与顺序（模板与缓存都依赖这个确定性）。
    """
    accs: dict[str, _Acc] = {}
    for order, m in enumerate(messages):
        uid = getattr(m, "sender_uid", "") or ""
        if not uid:
            continue
        if is_placeholder_sender(uid, getattr(m, "sender_name", "")):
            continue
        acc = accs.get(uid)
        if acc is None:
            acc = accs[uid] = _Acc(uid=uid)
        acc.add((getattr(m, "sender_name", "") or "").strip(), order)

    ranked = sorted(accs.values(), key=lambda a: (-a.count, a.uid))
    return [
        Participant(
            uid=a.uid,
            name=a.display_name() or a.uid[:8],
            raw_name=a.display_name(),
            is_self=a.uid == self_uid,
        )
        for a in ranked
    ]


def unique_display_names(participants: list[Participant]) -> list[Participant]:
    """就地保证显示名唯一，返回同一列表。

    规则（只在群聊轨使用，私聊轨不经过这里，因此私聊的"我方/对方"文案不受影响）：
    - 空名 → 用 uid 前 8 位兜底（导出文件偶有 sender 缺 name 的条目）；
    - 重名 → 双方都追加 `#uid4`，而不是只改后来者：只改一个会让读者分不清
      "带后缀的那个"和"没带后缀的那个"谁是谁。
    """
    seen: dict[str, int] = {}
    for p in participants:
        key = p.name or p.uid[:8]
        seen[key] = seen.get(key, 0) + 1
    for p in participants:
        base = p.name or p.uid[:8]
        p.name = f"{base}#{p.uid[:UID_SUFFIX_LEN]}" if seen.get(base, 0) > 1 else base
    return participants


def participant_map(participants: list[Participant]) -> dict:
    """uid -> Participant（统计层按 uid 给图表标注显示名时用）"""
    return {p.uid: p for p in participants}


def find_participant(participants: list[Participant], uid: str) -> Optional[Participant]:
    for p in participants:
        if p.uid == uid:
            return p
    return None
