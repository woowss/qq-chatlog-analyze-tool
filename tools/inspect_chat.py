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
"""导出文件体检：拿到一份新导出（尤其是群聊）先跑它，再决定要不要传进 Web 界面

    python tools/inspect_chat.py path/to/export.json
    python tools/inspect_chat.py path/to/export.json --show-names   # 本机排查时才开

只读、不联网、不写任何文件、**不打印聊天正文**：默认连昵称也脱敏（只留首字），
因为终端输出经常会被贴进 issue 或聊天窗口。它回答四个问题：

  1. 这份导出是什么格式？—— 顶层字段、消息 type 命名（旧版 type_N vs 新版语义名）、
     时间字段形态。格式漂移是"统计悄悄变样"最常见的源头。
  2. 解析器会把它当成什么？—— 群聊判定（导出器自报 type / 发言者门槛）、成员数、
     占位 sender、无归属消息、被丢弃的消息。
  3. 本地统计算得对不对？—— 成员条数之和 + 未知条数 == 统计口径总条数（对账）、
     回复/@ 信号的覆盖率，以及耗时。
  4. 会话文件有没有可疑之处？—— 空 sender、同名成员、无 system 标记的系统消息等。

退出码：0 = 全部检查通过；1 = 有检查不通过（便于放进脚本或 CI）。
"""

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser.qq_parser as qp  # noqa: E402
from analyzer import group_stats as gs  # noqa: E402


def _mask(name: str, show: bool) -> str:
    """昵称脱敏：只留首字，其余用 * 代替（默认行为；--show-names 才显示原文）"""
    text = (name or "").strip()
    if show or not text:
        return text or "<空>"
    return text[0] + "*" * (len(text) - 1)


def _histogram(counter: collections.Counter, limit: int = 12) -> str:
    items = counter.most_common(limit)
    return "、".join(f"{k or '<空>'}×{v}" for k, v in items)


def inspect(path: str, show_names: bool) -> int:
    problems: list[str] = []
    print("=" * 72)
    print(f"文件：{os.path.basename(path)}")
    print(f"体积：{os.path.getsize(path) / 1048576:.2f} MB")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    # ---------- 1. 格式 ----------
    print("\n【格式】")
    print("  顶层字段：", "、".join(sorted(raw)) or "<空>")
    # 不是导出文件就先说清楚再退出。下面的 load_chat 会抛 ValueError，而那个异常在
    # "群聊被拒收就临时放行再试一次"的分支里会被再抛一次、直接变成 Traceback——
    # 用户拿着一份不确定的文件来体检，看到的应该是一句人话。
    missing = [k for k in ("chatInfo", "messages") if k not in raw]
    if missing:
        print("\n【结论】")
        print(f"  这不是 QQChatExporter 的导出文件：缺少顶层字段 {'、'.join(missing)}")
        print("  解析器要求 chatInfo 与 messages 同时存在；请确认选对了文件或重新导出。")
        return 1
    info = raw.get("chatInfo") or {}
    print(f"  chatInfo.type = {info.get('type') or '<缺失>'}（group 表示导出器自报群聊）")
    print(
        f"  chatInfo：selfUid {'有' if info.get('selfUid') else '缺失'}，"
        f"selfName {'有' if info.get('selfName') else '缺失'}，"
        f"name 长度 {len(info.get('name') or '')}"
    )
    if not info.get("selfUid"):
        problems.append("缺少 chatInfo.selfUid：解析器会直接报错，需重新导出")

    msgs = raw.get("messages") or []
    types = collections.Counter((m.get("type") or "") for m in msgs)
    legacy = sum(v for k, v in types.items() if k.startswith("type_") and k[5:].isdigit())
    print(f"  消息 {len(msgs)} 条 | type 命名：语义名 {len(msgs) - legacy} 条 / type_N {legacy} 条")
    print("  type 分布：", _histogram(types))
    time_iso = sum(1 for m in msgs if isinstance(m.get("time"), str) and "T" in m["time"])
    print(
        f"  time 字段：ISO(UTC) {time_iso} 条 / 其它 {len(msgs) - time_iso} 条（解析器一律按 timestamp 重算）"
    )
    bad_ts = sum(
        1 for m in msgs if not isinstance(m.get("timestamp"), (int, float)) or m.get("timestamp") <= 0
    )
    if bad_ts:
        problems.append(f"{bad_ts} 条消息时间戳不可用，会被丢弃")

    elements = collections.Counter()
    for m in msgs:
        content = m.get("content")
        if isinstance(content, dict):
            for el in content.get("elements") or []:
                elements[el.get("type") or ""] += 1
    print("  元素分布：", _histogram(elements))
    print(
        f"  结构化信号：reply {elements.get('reply', 0)} 条、at {elements.get('at', 0)} 条"
        f"（这两个是群聊互动的精确信号）"
    )

    # ---------- 2. 解析与判定 ----------
    print("\n【解析与判定】")
    t0 = time.time()
    try:
        chat = qp.load_chat(path)  # 用当前配置真实跑一遍（可能抛"群聊拒收"）
        parse_note = "按当前配置解析成功"
    except ValueError as e:
        # 拒收是预期结果之一：把闸门临时打开再解析一次，才能看清这份文件的全貌。
        # 但"再试一次"也可能失败（例如双方身份无法确定），那时同样要给人话而不是堆栈。
        parse_note = f"当前配置下拒收：{e}"
        try:
            with _ready(True):
                chat = qp.load_chat(path)
        except ValueError as e2:
            print("\n【结论】")
            print(f"  这份文件无法解析：{e2}")
            return 1
    elapsed_parse = time.time() - t0
    mode = qp.group_chat_mode()
    offenders = qp._multi_party_offenders(chat)
    with _ready(True):
        action_ready = qp.multi_party_action(chat)
    print(f"  {parse_note}")
    print(f"  配置：QQCHAT_GROUP_CHAT={mode}（群聊轨未就绪时 auto/off 都按升级前行为处理）")
    speakers = len(qp._statistical_sender_counts(chat))
    print(f"  导出器自报类型：{chat.chat_type or '<缺失>'} | 统计口径发言者：{speakers} 位")
    print(f"  达到'实质发言'门槛的第三方：{len(offenders)} 位 → 群聊轨就绪后判定为：{action_ready}")
    print(
        f"  解析耗时：{elapsed_parse * 1000:.0f} ms | 消息 {len(chat.messages)} 条"
        f"（统计口径 {len(chat.statistical())} 条，丢弃 {chat.dropped_messages} 条）"
    )
    print(f"  记录跨度：{chat.duration_days} 天 | 月份：{'、'.join(chat.months()) or '<空>'}")
    if chat.dropped_messages:
        problems.append(f"{chat.dropped_messages} 条消息因时间戳无效被丢弃（未计入统计）")
    if chat.unknown_element_types:
        # 解析器没命中任何分支的元素类型（与上面"元素分布"的全量口径对账：
        # 分布里有、这里也有 = 导出器出现了解析器还不认识的新类型）。
        print(
            f"  ⚠ 未识别元素类型：{_histogram(collections.Counter(chat.unknown_element_types))}"
            "（未进入统计与分析；若含正文类类型，说明导出器格式漂移）"
        )
        problems.append(
            f"{sum(chat.unknown_element_types.values())} 个元素未被识别"
            f"（类型：{'、'.join(sorted(chat.unknown_element_types))}）"
        )

    # ---------- 3. 成员与占比 ----------
    print("\n【成员】")
    people = chat.participants()
    unknown_msgs = [m for m in chat.statistical() if gs.is_unknown_message(m)]
    print(f"  成员 {len(people)} 位（已排除占位 sender）| 无归属消息 {len(unknown_msgs)} 条")
    for p in people[:10]:
        print(f"    {_mask(p.name, show_names):<16} {'（我）' if p.is_self else ''}")
    if len(people) > 10:
        print(f"    …另有 {len(people) - 10} 位")
    names = collections.Counter(p.raw_name for p in people if p.raw_name)
    dup = [n for n, c in names.items() if c > 1]
    if dup:
        print(f"  同名成员 {len(dup)} 组（解析器已加 #uid 后缀区分）")

    # ---------- 4. 统计与对账 ----------
    print("\n【本地统计】")
    t0 = time.time()
    stats = gs.compute_group_stats(chat) if (chat.is_group_chat or action_ready == "group") else None
    if stats is None:
        print("  这份文件不是群聊，群聊统计不适用（私聊统计见 Web 界面）")
    else:
        activity = stats["member_activity"]
        total = len(chat.statistical())
        accounted = sum(a["msg_count"] for a in activity) + len(unknown_msgs)
        ov = stats["overview"]
        mi = stats["interaction"]
        print(
            f"  耗时 {(time.time() - t0) * 1000:.0f} ms | 成员 {ov['member_count']} 位"
            f" | 同时在聊高峰 {ov['peak_concurrent']['count']} 人（{ov['peak_concurrent']['at']}）"
        )
        print(
            f"  互动（推断接话）：矩阵 {len(mi['members'])}×{len(mi['members'])}"
            f"{'（已按上限截断，另有 %d 次互动未进矩阵）' % mi['dropped_replies'] if mi['truncated'] else ''}"
            f" | 无归属接话 {mi['unknown_replies']} 次"
        )
        print(
            f"  回复（精确）：标记 {mi['reply_total']} 条 = 有引用 {mi['reply_located']}"
            f" + 引用已删除 {mi['reply_no_target']}；其中有引用里 {mi['reply_resolved']} 条定位到发言人、"
            f"{mi['reply_unresolved']} 条不在本次导出"
        )
        print(
            f"              定位到的回复：{mi['reply_unknown']} 条指向占位/无归属、"
            f"{mi['reply_outside']} 条在矩阵之外，其余进矩阵"
        )
        attributed_mentions = sum(sum(r) for r in mi["mention_directed"])
        print(
            f"  @点名（精确）：{mi['mention_total']} 次 = 进矩阵 {attributed_mentions}"
            f" + 指向占位 {mi['mention_unknown']} + 矩阵之外 {mi['mention_outside']}"
            f" | @全体成员 {mi['mentions_all_count']} 条"
        )
        print(
            f"  对账：成员条数之和 {sum(a['msg_count'] for a in activity)} + 未知 {len(unknown_msgs)}"
            f" = {accounted} vs 统计口径 {total} → {'一致' if accounted == total else '不一致'}"
        )
        if accounted != total:
            problems.append(f"成员条数与总条数对不上：{accounted} vs {total}")
        if mi["reply_total"] and mi["reply_resolved"] / mi["reply_total"] < 0.5:
            problems.append("超过一半的回复引用无法解析：导出可能被裁剪过（引用的消息不在文件里）")

    print("\n" + "=" * 72)
    if problems:
        print("发现的问题：")
        for item in problems:
            print("  -", item)
        return 1
    print("检查通过：格式可解析、判定明确、统计对账一致。")
    return 0


class _ready:
    """临时把群聊轨就绪开关打开（体检工具要看全貌，不受当前灰度状态限制）"""

    def __init__(self, value: bool):
        self.value = value

    def __enter__(self):
        self.old = qp.GROUP_TRACK_READY
        qp.GROUP_TRACK_READY = self.value

    def __exit__(self, *exc):
        qp.GROUP_TRACK_READY = self.old
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="导出文件体检（只读、默认脱敏、不打印聊天正文）")
    ap.add_argument("path", help="QQChatExporter 导出的 JSON 文件")
    ap.add_argument("--show-names", action="store_true", help="显示真实昵称（默认脱敏为 首字+***）")
    args = ap.parse_args()
    if not os.path.exists(args.path):
        print(f"文件不存在：{args.path}")
        return 1
    return inspect(args.path, args.show_names)


if __name__ == "__main__":
    raise SystemExit(main())
