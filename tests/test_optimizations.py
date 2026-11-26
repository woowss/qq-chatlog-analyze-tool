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
"""优化项回归测试：增量缓存、统计落盘、可选统计口径、配置防呆、指纹失效"""

import io
import json
import os
import shutil as _shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import bootstrap  # noqa: E402
from result_fixtures import emotion as valid_emotion  # noqa: E402

bootstrap()
# 月份缓存会跨用例复用同一份月份内容，使"调用次数"断言失去确定性；
# 需要它的用例会自行开启并指向临时目录。
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

# 别名与项目模块的导入都必须排在环境准备之后（提前导入会把开关读成默认值）
from webapp import cleanup as cleanupmod  # noqa: E402
from webapp import store as storemod  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.qq_parser import CST, ChatData, Message  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import local_stats as ls  # noqa: E402
import config  # noqa: E402
from analyzer import usage as usage_mod  # noqa: E402
from parser.qq_parser import split_by_month  # noqa: E402
from parser.qq_parser import load_chat  # noqa: E402


def _msg(uid: str, ts: int, text: str = "在吗") -> Message:
    return Message(
        id=str(ts),
        timestamp=ts,
        time_str=datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=uid,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


def _chat(msgs) -> ChatData:
    return ChatData(chat_name="", self_name="A", other_name="B", self_uid="u1", other_uid="u2", messages=msgs)


def _months_chat(n_months: int, pairs_per_month: int = 6) -> ChatData:
    """跨 n 个月的聊天（每月内容互不相同）"""
    msgs = []
    for m in range(n_months):
        year, month = 2025 + m // 12, (m % 12) + 1  # 按自然月推进，保证每月独立
        for i in range(pairs_per_month):
            t = datetime(year, month, 5, 20, 0, tzinfo=CST) + timedelta(minutes=17 * i)
            msgs.append(
                _msg(
                    "u1" if i % 2 else "u2", int(t.timestamp() * 1000), text=f"{year}-{month:02d} 第{i}条消息"
                )
            )
    return _chat(msgs)


class TestDailyGapFilling(unittest.TestCase):
    """日线图不能把"没聊天的日子"整段抹掉"""

    def test_gaps_are_filled_with_zero(self):
        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        day = 86400_000
        chat = _chat(
            [
                _msg("u1", base),
                _msg("u2", base + 3000),
                _msg("u1", base + 99 * day),
                _msg("u2", base + 100 * day),
            ]
        )
        daily = ls.calc_daily_counts(chat)
        self.assertEqual(len(daily), 101)
        self.assertEqual(daily[1]["self"] + daily[1]["other"], 0)
        self.assertEqual(sum(d["self"] + d["other"] for d in daily), 4)
        # 关掉补零时仍是 3 个点（保持旧行为可切换）
        self.assertEqual(len(ls.calc_daily_counts(chat, fill_gaps=False)), 3)


class TestResponsePercentiles(unittest.TestCase):
    """回复速度：均值会被单次长间隔带偏，必须给分位数"""

    def test_percentiles_expose_the_typical_value(self):
        base = int(datetime(2025, 3, 1, 9, 0, tzinfo=CST).timestamp() * 1000)
        msgs, ts = [], base
        for i in range(20):
            msgs.append(_msg("u2", ts))
            ts += 300_000 if i == 7 else 5_000  # 第 8 次隔了 5 分钟
            msgs.append(_msg("u1", ts))
            ts += 3_600_000
        rt = ls.calc_response_time(_chat(msgs))
        self.assertEqual(rt["self"]["p50"], 5.0)
        self.assertGreater(rt["self"]["avg"], 15.0)  # 均值被长尾拉高
        self.assertEqual(rt["self"]["count"], 20)
        # 兼容旧字段
        self.assertEqual(rt["self_avg_seconds"], rt["self"]["avg"])


class TestRoundDefinition(unittest.TestCase):
    """轮次加入时间约束：长时间中断后重新起一轮"""

    def test_long_gap_starts_new_round(self):
        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        day = 86400_000
        burst = _chat([_msg("u1" if i % 2 else "u2", base + i * 1000) for i in range(10)])
        self.assertEqual(ls.calc_exchange_rounds(burst), 10)
        spread = _chat([_msg("u1", base), _msg("u1", base + 5 * day), _msg("u1", base + 10 * day)])
        self.assertEqual(ls.calc_exchange_rounds(spread), 3)

    def test_initiator_stats_from_sessions(self):
        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        day = 86400_000
        chat = _chat(
            [
                _msg("u1", base),
                _msg("u2", base + 1000),  # 第 1 段：u1 先开口
                _msg("u2", base + 3 * day),
                _msg("u1", base + 3 * day + 1000),
            ]
        )
        stats = ls.calc_initiator_stats(chat)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["self_opened"], 1)
        self.assertEqual(stats["other_opened"], 1)


class TestOverviewDaysBasis(unittest.TestCase):
    """total_days（跨度）与 active_days（活跃）必须分开，日均分母不能悄悄换口径"""

    def test_computed_span_used_when_file_has_no_duration(self):
        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        chat = _chat([_msg("u1", base), _msg("u2", base + 99 * 86400_000)])
        chat.duration_days = 0
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_days"], 100)
        self.assertEqual(ov["days_basis"], "computed")
        self.assertEqual(ov["active_days"], 2)
        self.assertEqual(ov["avg_daily"], 0.0)

    def test_file_duration_wins(self):
        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        chat = _chat([_msg("u1", base)])
        chat.duration_days = 7
        ov = ls.calc_overview(chat)
        self.assertEqual(ov["total_days"], 7)
        self.assertEqual(ov["days_basis"], "file")


class TestDialogCompaction(unittest.TestCase):
    """对话行压缩：段内省略昵称与时间，但"谁说的/隔了多久"不能丢"""

    def setUp(self):
        base = datetime(2024, 1, 1, 8, 0, tzinfo=CST)
        self.msgs = []
        for i, (uid, delta_min) in enumerate([("u2", 0), ("u1", 3), ("u2", 3), ("u2", 1), ("u1", 200)]):
            t = base + timedelta(minutes=delta_min + (0 if i == 0 else 0))
            self.msgs.append(_msg(uid, int(t.timestamp() * 1000), text=f"消息{i}"))

    def test_first_line_has_time_and_name(self):
        line = dc._message_line(self.msgs[0], "B")
        self.assertIn("[01-01 08:00] B:", line)

    def test_same_speaker_continuation_has_no_prefix(self):
        line = dc._message_line(self.msgs[3], "B", prev_uid="u2", prev_ts=self.msgs[2].timestamp)
        self.assertEqual(line, "消息3")  # 同人连发：连昵称和时间都省掉

    def test_speaker_change_keeps_name_and_relative_mark(self):
        line = dc._message_line(self.msgs[1], "A", prev_uid="u2", prev_ts=self.msgs[0].timestamp)
        self.assertTrue(line.startswith("A(+3m):"), line)

    def test_large_gap_prints_absolute_time(self):
        line = dc._message_line(self.msgs[4], "A", prev_uid="u2", prev_ts=self.msgs[3].timestamp)
        self.assertIn("[", line)
        self.assertIn("A:", line)

    def test_header_feeds_local_facts(self):
        dialog = dc._build_dialog(self.msgs, "u1", "A", "B")
        head = dialog.split("\n")[0]
        self.assertIn("段对话", head)  # 对话段数（本地算）
        self.assertIn("先开口", head)  # 谁更常开启话题（本地算）
        self.assertIn("回复间隔中位数", head)

    def test_sampling_rebuilds_speaker_context(self):
        """抽样后的每次换人都必须重新打印昵称，不能沿用被抽走的前置行。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        speakers = ["u1", "u1", "u2", "u2", "u2", "u1", "u1", "u1"]
        messages = [
            _msg(uid, base + i * 1000, text=f"sample-{i}-" + "x" * 70) for i, uid in enumerate(speakers)
        ]
        selected, lines, sampled = dc._fit_message_context(messages, "u1", "A", "B", 300)
        self.assertTrue(sampled)
        self.assertEqual([m.sender_uid for m in selected], ["u1", "u2", "u1"])
        self.assertTrue(lines[0].startswith("["))
        self.assertTrue(lines[1].startswith("B:"), lines[1])
        self.assertTrue(lines[2].startswith("A:"), lines[2])
        dialog = dc._build_dialog(messages, "u1", "A", "B", max_chars=300)
        self.assertIn("B: sample-3-", dialog)
        self.assertIn("A: sample-6-", dialog)

    def test_sampling_uses_a_separate_dimension_cache_fingerprint(self):
        """只有触发新抽样格式的聊天才切到新维度缓存代次。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        messages = [_msg("u1" if i % 2 else "u2", base + i * 1000, text="x" * 100) for i in range(20)]
        chat = _chat(messages)
        current = dc.analysis_cache_fingerprint("emotion", chat)
        with mock.patch.object(dc, "MAX_DIALOG_CHARS", 300):
            sampled = dc.analysis_cache_fingerprint("emotion", chat)
            self.assertNotEqual(sampled, current)
            self.assertEqual(sampled, dc.sampled_prompt_fingerprint())

    def test_single_long_message_stays_within_budget(self):
        """只有一条超长消息时，截断后的换行也必须计入预算。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        selected, lines, sampled = dc._fit_message_context(
            [_msg("u1", base, text="x" * 1000)], "u1", "A", "B", 100
        )
        self.assertTrue(sampled)
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), 100)

    def test_sampling_does_not_collapse_around_one_long_message(self):
        """超长孤立消息不能把整个月的样本退化成末尾一小段。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        messages = [_msg("u1", base, text="x" * 1000)]
        messages.extend(
            _msg("u1" if i % 2 else "u2", base + (i + 1) * 1000, text=f"普通-{i}") for i in range(99)
        )
        selected, lines, sampled = dc._fit_message_context(messages, "u1", "A", "B", 200)
        self.assertTrue(sampled)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), 200)
        self.assertGreaterEqual(len(selected), 3)
        indexes = [messages.index(message) for message in selected]
        self.assertLess(indexes[0], 20)
        self.assertGreater(indexes[-1], 80)

    def test_mixed_long_messages_do_not_erase_all_ordinary_messages(self):
        """步长不能被多条超长消息推到总条数以上，只留下超长首条。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        sizes = [
            2000,
            20,
            60,
            10,
            10,
            60,
            10,
            60,
            20,
            3000,
            3000,
            20,
            3000,
            2000,
            10,
            2000,
            60,
            3000,
            2000,
            10,
            60,
            20,
        ]
        messages = [
            _msg(
                "u1" if i % 2 else "u2",
                base + i * 60_000,
                text="x" * size if size >= 100 else f"ordinary-{i}",
            )
            for i, size in enumerate(sizes)
        ]
        ordinary = [message for message in messages if message.text.startswith("ordinary-")]
        selected, lines, sampled = dc._fit_message_context(messages, "u1", "A", "B", 1000)
        self.assertTrue(sampled)
        self.assertEqual(selected, ordinary)
        self.assertEqual(lines, dc._render_message_lines(ordinary, "u1", "A", "B"))
        self.assertLessEqual(sum(len(line) + 1 for line in lines), 1000)

    def test_sampling_terminates_when_every_message_is_too_long(self):
        """所有候选都超过预算时也必须收敛到一条截断消息。"""
        base = int(datetime(2024, 1, 1, 8, 0, tzinfo=CST).timestamp() * 1000)
        messages = [_msg("u1" if i % 2 else "u2", base + i * 1000, text="x" * 1000) for i in range(40)]
        selected, lines, sampled = dc._fit_message_context(messages, "u1", "A", "B", 100)
        self.assertTrue(sampled)
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), 100)


class TestMonthIncrementalCache(unittest.TestCase):
    """增量分析：重新导出多了一个月时，历史月份不该再付费"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-monthcache-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)
        # 模块级状态读/写都打在 owns 它的模块上：deepseek_client 里的同名名字只是
        # 搬迁瞬间的值拷贝（见 analyzer/month_cache.py 的模块说明）
        from analyzer import month_cache as mc

        self._orig_dir = mc._MONTH_CACHE_DIR
        dc.configure_month_cache(self.tmp)
        self.addCleanup(lambda: dc.configure_month_cache(self._orig_dir))

    def _run(self, chat, calls, chat_hash):
        def fake_api(*a, **kw):
            calls.append(1)
            return valid_emotion()

        with (
            mock.patch.object(dc, "_call_api", side_effect=fake_api),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            return dc.analyze_emotion(chat, chat_hash=chat_hash)

    def test_only_new_month_is_billed(self):
        calls = []
        first = self._run(_months_chat(3), calls, "hashA")
        self.assertEqual(len(first), 3)
        self.assertEqual(len(calls), 3)

        # 同一份对话重跑：全部命中月份缓存
        calls.clear()
        again = self._run(_months_chat(3), calls, "hashA")
        self.assertEqual(len(again), 3)
        self.assertEqual(calls, [])

        # 重新导出，多了一个月：只新增的那一个月付费
        calls.clear()
        grown = self._run(_months_chat(4), calls, "hashB")
        self.assertEqual(len(grown), 4)
        self.assertEqual(len(calls), 1, "只应为新增月份调用一次 API")

        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), 4)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "manifest_hashA.json")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "manifest_hashB.json")))

    def test_purge_keeps_months_shared_with_other_chat(self):
        self._run(_months_chat(2), [], "hashA")
        self._run(_months_chat(3), [], "hashB")
        before = len([n for n in os.listdir(self.tmp) if n.startswith("month_")])
        self.assertEqual(before, 3)
        # hashB 引用了全部 3 个月（1、2 月与 hashA 共享）
        removed = dc.purge_month_cache("hashA")
        self.assertEqual(removed, 0, "共享的月份文件不能被删")
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), 3)
        # 再清 hashB：文件已无引用，但仍在宽限期内 → 不立刻删除
        removed = dc.purge_month_cache("hashB")
        self.assertEqual(removed, 0, "宽限期内不回收，留给增量分析复用")
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), 3)
        # 超过宽限期后由孤儿回收清理
        for name in os.listdir(self.tmp):
            if name.startswith("month_"):
                old = time.time() - dc.MONTH_CACHE_GRACE_SECONDS - 60
                os.utime(os.path.join(self.tmp, name), (old, old))
        self.assertEqual(dc.sweep_orphan_month_cache(), 3)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("month_")], [])

    def test_replace_flow_keeps_months_for_incremental_reuse(self):
        """上传新文件时的级联清理不能顺手删掉历史月份（否则增量分析形同虚设）"""
        self._run(_months_chat(3), [], "hashOld")
        # 模拟"同一段对话又多了几个月"：先清理旧 chat_hash（上传新文件时会这么做）
        dc.purge_month_cache("hashOld")
        self.assertEqual(
            len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), 3, "宽限期内历史月份必须留下"
        )
        # 新文件分析时，历史 3 个月应全部命中
        calls = []
        self._run(_months_chat(4), calls, "hashNew")
        self.assertEqual(len(calls), 1, "只应为新增的第 4 个月付费")


class TestStatsCache(unittest.TestCase):
    """统计结果落盘：重复上传同一文件不该重算，session 也不该再背着几十 KB"""

    def test_stats_roundtrip_and_schema_guard(self):
        import app as appmod

        base = int(datetime(2025, 1, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        msgs = [
            {
                "id": str(i),
                "timestamp": base + i * 60000,
                "time": "2025-01-01 20:00:00",
                "sender": {"uid": "u1" if i % 2 else "u2", "name": "A" if i % 2 else "B"},
                "content": "在吗",
            }
            for i in range(6)
        ]
        payload = json.dumps(
            {
                "chatInfo": {"name": "B", "selfUid": "u1", "selfName": "A"},
                "statistics": {"senders": [{"uid": "u1", "name": "A"}, {"uid": "u2", "name": "B"}]},
                "messages": msgs,
            },
            ensure_ascii=False,
        ).encode()
        client = appmod.app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            token = sess["csrf_token"]
        headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": token}
        r = client.post("/upload", data={"file": (io.BytesIO(payload), "c.json")}, headers=headers)
        self.assertEqual(r.status_code, 302)
        with client.session_transaction() as sess:
            chat_hash = sess["chat_hash"]
            filepath = sess["filepath"]
            self.assertNotIn("overview", sess, "统计结果不应再塞进 session")
            self.assertNotIn("daily_counts", sess)
        storemod.wait_for_stats(chat_hash)  # 统计已挪到后台线程，断言前先收口
        try:
            stats = storemod._load_stats(chat_hash)
            self.assertIsNotNone(stats)
            self.assertIn("overview", stats)
            self.assertNotIn("word_freq", stats, "词频应懒算，不该在上传时就算")

            # 结构版本不匹配时判为过期（避免旧结构把模板打 500）
            path = storemod._stats_path(chat_hash)
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            raw["_v"] = 0
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            self.assertIsNone(storemod._load_stats(chat_hash))

            # 懒算词频：访问 habits 页后应写回统计缓存
            client.get("/habits")
            stats2 = storemod._load_stats(chat_hash)
            self.assertIsNone(stats2)  # _v 已被改坏 → 但页面不该崩
        finally:
            storemod._purge_chat_caches(chat_hash)
            if filepath and os.path.exists(filepath):
                os.remove(filepath)

    def test_word_freq_is_computed_lazily_and_cached(self):
        import app as appmod
        from flask import session as flask_session
        from webapp import store as storemod

        stats = {"overview": {"total_messages": 1}}
        with appmod.app.test_request_context("/habits"):
            flask_session["filepath"] = __file__  # 存在即可
            flask_session["chat_hash"] = "hashW"
            with (
                mock.patch.object(
                    storemod,
                    "_load_chat_cached",
                    return_value=_chat([_msg("u1", 1704067200000, "今天加班到十点")]),
                ),
                mock.patch.object(storemod, "_save_stats") as saved,
            ):
                out = storemod._stats_with_word_freq(dict(stats), "hashW")
        self.assertIn("word_freq", out)
        self.assertTrue(saved.called)


class TestConfigHardening(unittest.TestCase):
    """非法配置不再让应用/任务崩在难以理解的地方"""

    def test_env_number_falls_back(self):
        # 数值解析助手已经统一到 config（原先 deepseek_client 自己实现了一份，
        # 与 config 的 env_int 逻辑相同、失败通道却不同）。
        import config

        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "0"}):
            self.assertEqual(config.env_number("LLM_CONCURRENCY", 6, 1, 64), 6)
        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "abc"}):
            self.assertEqual(config.env_number("LLM_CONCURRENCY", 6, 1, 64), 6)
        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "4"}):
            self.assertEqual(config.env_number("LLM_CONCURRENCY", 6, 1, 64), 4)

    def test_bool_parsing_is_shared_and_unchanged(self):
        """一份词表：留空取默认，1/true/yes/on 为真，其余为假

        LLM_THINKING 额外认 enabled/disabled 是**它自己**在调用前归一（见
        deepseek_client._THINKING_NORMALIZED），共用词表不因此放宽——
        否则 QQCHAT_ALLOW_MULTI_PARTY=enabled 这种写法会从"假"静默变成"真"。
        """
        import config

        with mock.patch.dict(os.environ, {"QQCHAT_T_BOOL": "yes"}):
            self.assertTrue(config.env_bool("QQCHAT_T_BOOL", False))
        with mock.patch.dict(os.environ, {"QQCHAT_T_BOOL": "enabled"}):
            self.assertFalse(config.env_bool("QQCHAT_T_BOOL", False), "共用词表不含 enabled")
        with mock.patch.dict(os.environ, {"QQCHAT_T_BOOL": ""}):
            self.assertTrue(config.env_bool("QQCHAT_T_BOOL", True), "留空取默认值")

    def test_port_validation(self):
        import config

        self.assertEqual(config.env_int("FLASK_PORT", 5000, 1, 65535), 5000)  # 当前环境未设置
        with mock.patch.dict(os.environ, {"FLASK_PORT": "abc"}):
            self.assertEqual(config.env_int("FLASK_PORT", 5000, 1, 65535), 5000)
        with mock.patch.dict(os.environ, {"FLASK_PORT": "99999"}):
            self.assertEqual(config.env_int("FLASK_PORT", 5000, 1, 65535), 5000)
        with mock.patch.dict(os.environ, {"FLASK_PORT": "5001"}):
            self.assertEqual(config.env_int("FLASK_PORT", 5000, 1, 65535), 5001)

    def test_thinking_budget_conflict_is_reported(self):
        """开了思考模式但预算不足时必须告警（这类组合会 100% 截断丢结果）"""
        with (
            mock.patch.object(dc, "THINKING_DEFAULT", True),
            mock.patch.object(dc, "THINKING_DIMS", frozenset()),
            mock.patch.object(dc, "MAX_TOKENS_BY_DIM", {"emotion": 1024, "profile": 16384}),
        ):
            warnings = dc.thinking_budget_warnings()
        self.assertTrue(any("emotion" in w for w in warnings))
        self.assertFalse(any("profile" in w for w in warnings))  # profile 预算充足

    def test_shipped_defaults_are_conflict_free(self):
        """出厂设置（准确性优先）不该再出现"开思考但预算不足"的组合"""
        self.assertEqual(dc.thinking_budget_warnings(), [])
        for dim, budget in dc.MAX_TOKENS_BY_DIM.items():
            self.assertGreaterEqual(budget, dc.THINKING_MIN_TOKENS, f"{dim} 的预算装不下思维链 + 结果")


class TestPromptFingerprint(unittest.TestCase):
    """指纹必须随提示词/格式/输入预算变化——这是"忘记 bump 版本号"那个坑的根治办法"""

    def test_fingerprint_is_stable_and_content_addressed(self):
        import analyzer.deepseek_client as dc

        fp1 = dc._prompt_fingerprint()
        fp2 = dc._prompt_fingerprint()
        self.assertEqual(fp1, fp2, "同一份代码算出的指纹必须稳定")
        self.assertEqual(len(dc.PROMPT_FINGERPRINT), 12)

    def test_prompt_change_changes_fingerprint(self):
        import analyzer.deepseek_client as dc

        base = dc._prompt_fingerprint()
        with mock.patch("analyzer.prompts.SYSTEM_PROMPT_EMOTION", "完全不同的提示词"):
            self.assertNotEqual(base, dc._prompt_fingerprint(), "改了系统提示词必须换指纹")

    def test_input_budget_change_changes_fingerprint(self):
        """对话预算/时间标记口径会改变喂给模型的输入，必须反映在指纹里。

        曾经只哈希函数源码：把 MAX_DIALOG_CHARS 从 5 万调到 1 万后，
        输入内容变了但指纹没变，旧缓存会继续以"新分析"的名义返回旧结果。
        """
        import analyzer.deepseek_client as dc

        base = dc._prompt_fingerprint()
        with mock.patch.object(dc, "MAX_DIALOG_CHARS", 10_000):
            self.assertNotEqual(base, dc._prompt_fingerprint(), "改了对话字符预算必须换指纹")
        with mock.patch.object(dc, "TIME_MARK_MINUTES", 5):
            self.assertNotEqual(base, dc._prompt_fingerprint(), "改了时间标记口径必须换指纹")

    def test_sampled_fingerprint_is_distinct_from_unsampled(self):
        self.assertEqual(len(dc.sampled_prompt_fingerprint()), 12)
        self.assertNotEqual(dc.sampled_prompt_fingerprint(), dc._unsampled_prompt_fingerprint())

    def test_version_and_model_changes_rotate_keys(self):
        base = dc._prompt_fingerprint()
        with mock.patch.object(dc, "PRIVATE_CACHE_VERSION", "v999"):
            self.assertNotEqual(base, dc._prompt_fingerprint(), "版本号变更必须换指纹")
        with mock.patch.object(dc, "DEEPSEEK_MODEL", "other-model"):
            self.assertNotEqual(
                dc._month_key("SYS", "USER"),
                storemod._cache_path("emotion", "deadbeefdeadbeef"),
            )
            with mock.patch.object(storemod, "DEEPSEEK_MODEL", "other-model"):
                self.assertIn("other-model", storemod._cache_path("emotion", "deadbeefdeadbeef"))

    def test_cache_key_uses_fingerprint_not_manual_version(self):
        path = storemod._cache_path("emotion", "deadbeefdeadbeef")
        self.assertIn(dc.PROMPT_FINGERPRINT, path)
        self.assertNotIn("v2.", os.path.basename(path))


class TestCacheRetention(unittest.TestCase):
    """缓存保留：滑动 30 天 + 绝对 90 天（只看 mtime 会让常用缓存永不回收）"""

    def _patch_dirs(self, tmp):
        """把 store 与 cleanup 两个命名空间的目录都指向临时目录后返回上下文列表"""
        from webapp import cleanup as cleanupmod
        from webapp import store as storemod

        stats = os.path.join(tmp, "stats")
        up = os.path.join(tmp, "up")
        sess = os.path.join(tmp, "sess")
        for d in (stats, up, sess):
            os.makedirs(d, exist_ok=True)
        return [
            mock.patch.object(storemod, "AI_CACHE_DIR", tmp),
            mock.patch.object(storemod, "STATS_CACHE_DIR", stats),
            mock.patch.object(cleanupmod, "AI_CACHE_DIR", tmp),
            mock.patch.object(cleanupmod, "STATS_CACHE_DIR", stats),
            mock.patch.object(cleanupmod, "UPLOAD_FOLDER", up),
            mock.patch.object(cleanupmod, "SESSION_FILE_DIR", sess),
        ]

    def _start(self, patches):
        for p in patches:
            p.start()

    def _stop(self, patches):
        for p in reversed(patches):
            p.stop()

    def test_absolute_cap_removes_frequently_read_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patch_dirs(tmp)
            self._start(patches)
            try:
                storemod._write_cache("emotion", "hashOld", {"a": 1})
                path = storemod._cache_path("emotion", "hashOld")
                # 模拟"创建于 100 天前、但昨天刚被读过"（mtime 新，_created 很老）
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                payload["_created"] = time.time() - 100 * 86400
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.utime(path, None)
                cleanupmod.cleanup_old_files()
                self.assertFalse(os.path.exists(path), "超过绝对上限的缓存必须删除")
            finally:
                self._stop(patches)

    def test_sliding_window_keeps_active_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches = self._patch_dirs(tmp)
            self._start(patches)
            try:
                storemod._write_cache("emotion", "hashNew", {"a": 1})
                cleanupmod.cleanup_old_files()
                self.assertTrue(os.path.exists(storemod._cache_path("emotion", "hashNew")))
            finally:
                self._stop(patches)

    def test_legacy_size_rotated_logs_are_purged(self):
        """换按天轮转后，遗留的 app.log.1/.2 也要按保留天数回收；token_usage.json 不动"""
        from webapp import cleanup as cleanupmod

        with tempfile.TemporaryDirectory() as tmp:
            old_log = os.path.join(tmp, "app.log.1")
            with open(old_log, "w", encoding="utf-8") as f:
                f.write("legacy rotated line\n")
            keep = os.path.join(tmp, "token_usage.json")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("{}")
            old = time.time() - 30 * 86400
            os.utime(old_log, (old, old))
            os.utime(keep, (old, old))
            with mock.patch.object(cleanupmod, "LOG_DIR", tmp):
                cleanupmod.cleanup_old_files()
            self.assertFalse(os.path.exists(old_log), "过期旧日志必须删除")
            self.assertTrue(os.path.exists(keep), "用量统计文件不属于日志轮转产物，不能误删")


def _r3_msg(uid: str, ts: int, text: str) -> Message:
    return Message(
        id=f"{uid}-{ts}",
        timestamp=ts,
        time_str=datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=uid,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=False,
        is_reply=False,
    )


def _r4_msg(uid: str, ts: int, text: str = "在吗", has_image: bool = False, **kw) -> Message:
    return Message(
        id=str(ts),
        timestamp=ts,
        time_str=datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        sender_name=uid,
        sender_uid=uid,
        text=text,
        raw_text=text,
        msg_type="type_1",
        has_image=has_image,
        is_reply=False,
        **kw,
    )


def _r4_chat(msgs) -> ChatData:
    return ChatData(chat_name="", self_name="A", other_name="B", self_uid="u1", other_uid="u2", messages=msgs)


def _r4_months_chat(n_months: int, pairs_per_month: int = 6) -> ChatData:
    """跨 n 个月的聊天（每月内容互不相同）"""
    msgs = []
    for m in range(n_months):
        year, month = 2025 + m // 12, (m % 12) + 1
        for i in range(pairs_per_month):
            t = datetime(year, month, 5, 20, 0, tzinfo=CST) + timedelta(minutes=17 * i)
            text = f"{year}-{month:02d} 第{i}条消息"
            msgs.append(_r4_msg("u1" if i % 2 else "u2", int(t.timestamp() * 1000), text=text))
    return _r4_chat(msgs)


class TestCacheWriteFailuresAreLoud(unittest.TestCase):
    """写失败必须留痕：静默失败会变成"每次都在重复付费"且用户毫不知情"""

    def test_month_cache_write_failure_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "not-a-dir")
            with open(missing, "w", encoding="utf-8") as f:
                f.write("占位：同名文件存在时，目录创建/写入必然失败")
            dc.configure_month_cache(missing)
            # 去抖比较的是 time.monotonic()（Linux 上即开机时长）。把它当成 0.0 时，
            # 刚启动不到去抖窗口（300s）的机器上 now - 0 < 300 会成立、警告被吞掉，
            # 用例就在 GitHub runner 上偶发失败。用 -inf 表示"从未告警过"，与开机时长无关。
            dc._last_write_warning[0] = float("-inf")
            try:
                with self.assertLogs("qqchatlog.deepseek", level="WARNING") as logs:
                    dc._write_month_cache("key1", {"a": 1})
            finally:
                dc.configure_month_cache("")
            self.assertTrue(
                any("写入失败" in line for line in logs.output), "月份缓存写失败要打警告，而不是静默吞掉"
            )

    def test_usage_flush_keeps_pending_on_failure(self):
        """用量落盘失败时增量要留在内存里下次再写，而不是被清空丢掉"""
        usage_mod._PENDING["days"].clear()
        usage_mod._PENDING["dims"].clear()
        for k in usage_mod._PENDING["total"]:
            usage_mod._PENDING["total"][k] = 0
        usage_mod.record_call("deepseek-flash", "emotion", 100, 50)
        with mock.patch.object(usage_mod, "_dump", side_effect=OSError("磁盘满")):
            usage_mod.flush()  # 不应抛异常
        self.assertTrue(usage_mod._DIRTY, "失败后仍应标记为待落盘")
        self.assertEqual(usage_mod._PENDING["total"]["calls"], 1, "增量不能被清空")
        usage_mod._PENDING["days"].clear()
        usage_mod._PENDING["dims"].clear()
        for k in usage_mod._PENDING["total"]:
            usage_mod._PENDING["total"][k] = 0
        usage_mod._DIRTY = False


class TestUsageRetention(unittest.TestCase):
    """用量按天聚合，但历史不该无限累积（日志只留 7 天，这里口径应一致）"""

    def test_old_days_are_pruned_on_flush(self):
        path = os.path.join(os.environ["QQCHAT_DATA_DIR"], "usage_prune.json")
        ancient = (datetime.now(tz=usage_mod.CST) - timedelta(days=config.LOG_RETENTION_DAYS + 10)).strftime(
            "%Y-%m-%d"
        )
        recent = datetime.now(tz=usage_mod.CST).strftime("%Y-%m-%d")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "days": {
                        ancient: {"calls": 9, "prompt": 1, "completion": 1},
                        recent: {"calls": 1, "prompt": 1, "completion": 1},
                    },
                    "dims": {},
                    "total": {"calls": 10, "prompt": 2, "completion": 2},
                },
                f,
            )
        try:
            with mock.patch.object(usage_mod, "TOKEN_USAGE_FILE", path):
                usage_mod.record_call("deepseek-flash", "emotion", 10, 5)
                usage_mod.flush()
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn(ancient, saved["days"], "超过保留期的日期应被清掉")
            self.assertIn(recent, saved["days"], "保留期内的记录必须还在")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestMonthGroupingCache(unittest.TestCase):
    """split_by_month 在一次分析里会被多个维度各调一遍，不能每次都重算"""

    def test_second_call_reuses_the_same_object(self):
        chat = _r4_months_chat(3)
        first = split_by_month(chat)
        second = split_by_month(chat)
        self.assertIs(first, second, "应直接返回缓存的字典")
        self.assertEqual(list(first), ["2025-01", "2025-02", "2025-03"])

    def test_cache_lives_on_the_instance_and_is_not_part_of_equality(self):
        chat = _r4_months_chat(1)
        self.assertIsNone(chat._months_cache, "初始为 None，不该在构造时就计算")
        chat.months()
        self.assertIsNotNone(chat._months_cache)
        # 缓存字段不参与相等比较，也不该出现在 repr 里（它可能很大）
        other = _r4_months_chat(1)
        self.assertEqual(chat, other)
        self.assertNotIn("_months_cache", repr(chat))

    def test_stats_cache_is_a_declared_field(self):
        """_stats_cache 曾经是 setattr 动态挂载；现在是声明字段，读代码时看得见"""
        chat = _r4_months_chat(1)
        self.assertIsNone(chat._stats_cache)
        ls.calc_overview(chat)
        self.assertIsNotNone(chat._stats_cache)


class TestOverviewSinglePass(unittest.TestCase):
    """calc_overview 合并成单次遍历后，字段口径必须与原实现逐项一致"""

    def _reference(self, chat):
        ms = chat.statistical()
        self_count = sum(1 for m in ms if m.sender_uid == chat.self_uid)
        image_msgs = [m for m in ms if m.has_image]
        media = {}
        for m in ms:
            if m.media_kind:
                media[m.media_kind] = media.get(m.media_kind, 0) + 1
        return {
            "self_count": self_count,
            "other_count": len(ms) - self_count,
            "self_chars": sum(len(m.text) for m in ms if m.sender_uid == chat.self_uid),
            "other_chars": sum(len(m.text) for m in ms if m.sender_uid != chat.self_uid),
            "total_images": len(image_msgs),
            "total_faces": sum((len(m.face_names) or len(m.face_ids)) for m in ms),
            "image_bytes": sum(m.media_bytes for m in image_msgs),
            "other_media_bytes": sum(m.media_bytes for m in ms if m.media_kind),
            "unique_images": len({m.media_id for m in image_msgs if m.media_id}) or None,
            "media": media,
        }

    def test_fields_match_reference_implementation(self):
        base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        msgs = [
            _r4_msg("u1", base, text="图片消息", has_image=True, media_bytes=100, media_id="m1"),
            _r4_msg("u2", base + 1000, text="同一张图", has_image=True, media_bytes=100, media_id="m1"),
            _r4_msg("u1", base + 2000, text="文件", media_kind="file", media_bytes=5000, media_id="f1"),
            _r4_msg("u2", base + 3000, text="视频", media_kind="video", media_bytes=9000),
            _r4_msg("u1", base + 4000, text="表情", face_ids=[3], face_names=[]),
            _r4_msg("u2", base + 5000, text="大表情", face_ids=[], face_names=["微笑"]),
            _r4_msg("u1", base + 6000, text="没有媒体的普通消息"),
        ]
        chat = _r4_chat(msgs)
        ov = ls.calc_overview(chat)
        ref = self._reference(chat)
        for key in (
            "self_count",
            "other_count",
            "self_chars",
            "other_chars",
            "total_images",
            "total_faces",
            "image_bytes",
            "other_media_bytes",
            "unique_images",
        ):
            self.assertEqual(ov[key], ref[key], f"{key} 与参考实现不一致")
        self.assertEqual(ov["total_files"], ref["media"].get("file", 0))
        self.assertEqual(ov["total_videos"], ref["media"].get("video", 0))
        self.assertEqual(ov["unique_images"], 1, "两张同 md5 的图应算一张")

    def test_faces_prefer_names_over_ids(self):
        """商城大表情没有数字 id：face_names 与 face_ids 取两者中有的那个，不能相加"""
        base = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp() * 1000)
        chat = _r4_chat([_r4_msg("u1", base, face_ids=[1, 2], face_names=["a", "b", "c"])])
        self.assertEqual(ls.calc_overview(chat)["total_faces"], 3)


class TestMonthManifestBatching(unittest.TestCase):
    """manifest 从"每月读一次写一次"改成"每个维度读一次写一次" """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-manifest-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)
        # 模块级状态读/写都打在 owns 它的模块上（deepseek_client 的重导出只是值拷贝）
        from analyzer import month_cache as mc

        self._orig_dir = mc._MONTH_CACHE_DIR
        dc.configure_month_cache(self.tmp)
        self.addCleanup(lambda: dc.configure_month_cache(self._orig_dir))

    def _run(self, chat, chat_hash):
        def fake_api(*a, **kw):
            return valid_emotion()

        with (
            mock.patch.object(dc, "_call_api", side_effect=fake_api),
            mock.patch.object(dc, "CALL_MIN_INTERVAL", 0.0),
        ):
            return dc.analyze_emotion(chat, chat_hash=chat_hash)

    def test_manifest_written_once_per_dimension(self):
        """3 个月的维度：_record_month_usage 只应被调用 1 次（而不是 3 次）"""
        real = dc._record_month_usage
        calls = []

        def spy(chat_hash, keys):
            calls.append(set(keys))
            return real(chat_hash, keys)

        with mock.patch.object(dc, "_record_month_usage", side_effect=spy):
            result = self._run(_r4_months_chat(3), "hashBatch")
        self.assertEqual(len(result), 3)
        self.assertEqual(len(calls), 1, "应当只写一次 manifest")
        self.assertEqual(len(calls[0]), 3, "三个月份的键都要记进 manifest")

        with open(os.path.join(self.tmp, "manifest_hashBatch.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["months"]), 3)

    def test_manifest_accumulates_across_runs(self):
        self._run(_r4_months_chat(2), "hashGrow")
        self._run(_r4_months_chat(3), "hashGrow")
        with open(os.path.join(self.tmp, "manifest_hashGrow.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["months"]), 3, "第二次运行应把新月份并进同一个 manifest")

    def test_referenced_keys_uses_mtime_cache(self):
        """引用键集合带进程级缓存：manifest 没被改过就不该再读一遍文件"""
        dc._record_month_usage("hashCache", {"k1", "k2"})
        with (
            mock.patch.object(dc, "_MONTH_CACHE_LOCK", dc._MONTH_CACHE_LOCK),
            mock.patch.object(dc.json, "load", side_effect=AssertionError("不该重新解析 manifest")),
        ):
            keys = dc._referenced_keys_locked()
        self.assertEqual(keys, {"k1", "k2"})

    def test_mtime_change_invalidates_cache(self):
        dc._record_month_usage("hashMtime", {"k1"})
        path = os.path.join(self.tmp, "manifest_hashMtime.json")
        # 绕过写路径直接改文件（模拟外部修改）：mtime 变了就必须重读
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"months": ["k9"]}, f)
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(dc._referenced_keys_locked(), {"k9"})

    def test_purge_ignores_other_manifests(self):
        """purge 走的仍是"别人引用的不能删"这套语义（缓存不能把它改坏）"""
        self._run(_r4_months_chat(2), "hashA")
        self._run(_r4_months_chat(3), "hashB")
        before = len([n for n in os.listdir(self.tmp) if n.startswith("month_")])
        self.assertEqual(dc.purge_month_cache("hashA"), 0, "共享的月份文件不能被删")
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.startswith("month_")]), before)


class TestComputeStatsNoDoubleWork(unittest.TestCase):
    """compute_stats 不再把轮次算两遍（calc_overview 内部已经算过一次）"""

    def test_exchange_rounds_reused_from_overview(self):
        base = datetime(2024, 1, 1, 8, 0, tzinfo=CST)
        msgs = [
            _r3_msg(uid, int((base + timedelta(minutes=5 * i)).timestamp() * 1000), f"m{i}")
            for i, uid in enumerate(["uA", "uB", "uA", "uB", "uA"])
        ]
        chat = ChatData(
            chat_name="c",
            self_name="A",
            other_name="B",
            self_uid="uA",
            other_uid="uB",
            messages=msgs,
        )
        stats = storemod.compute_stats(chat)
        self.assertEqual(stats["exchange_rounds"], stats["overview"]["exchange_rounds"])

    def test_exchange_rounds_computed_only_once(self):
        """用调用计数钉住"只算一次"，避免以后又被加回去。

        调用点是 local_stats.calc_exchange_rounds（由 calc_overview 内部调用），
        所以盯的是 local_stats 命名空间里的那个引用。
        """
        from analyzer import local_stats as ls

        base = datetime(2024, 1, 1, 8, 0, tzinfo=CST)
        msgs = [
            _r3_msg(uid, int((base + timedelta(minutes=5 * i)).timestamp() * 1000), f"m{i}")
            for i, uid in enumerate(["uA", "uB", "uA"])
        ]
        chat = ChatData(
            chat_name="c",
            self_name="A",
            other_name="B",
            self_uid="uA",
            other_uid="uB",
            messages=msgs,
        )
        real = ls.calc_exchange_rounds
        calls = []

        def counting(c):
            calls.append(1)
            return real(c)

        with mock.patch.object(ls, "calc_exchange_rounds", counting):
            storemod.compute_stats(chat)
        self.assertEqual(len(calls), 1, f"轮次只应算一次，实际 {len(calls)} 次")


class TestCleanupClosureBinding(unittest.TestCase):
    """清理判定函数把 now 显式冻进闭包，不依赖外层变量的后续取值"""

    def test_older_than_expired(self):
        mtime = os.path.getmtime(__file__)
        self.assertTrue(cleanupmod._older_than(mtime + 1000, 100)(__file__))

    def test_older_than_fresh(self):
        mtime = os.path.getmtime(__file__)
        self.assertFalse(cleanupmod._older_than(mtime + 10, 100)(__file__))

    def test_cleanup_still_purges_expired_upload(self):
        """端到端：过期的 uploads 文件仍会被回收（别人的 now 改动不该漏掉它）"""
        import time

        with tempfile.TemporaryDirectory(prefix="qqchatlog-cleanup-") as tmp:
            uploads = os.path.join(tmp, "uploads")
            os.makedirs(uploads, exist_ok=True)
            stale = os.path.join(uploads, "stale.json")
            with open(stale, "w", encoding="utf-8") as f:
                f.write("{}")
            old = time.time() - 3 * 86400
            os.utime(stale, (old, old))

            with (
                mock.patch.object(cleanupmod, "UPLOAD_FOLDER", uploads),
                mock.patch.object(cleanupmod, "SESSION_FILE_DIR", os.path.join(tmp, "session")),
                mock.patch.object(cleanupmod, "AI_CACHE_DIR", os.path.join(tmp, "ai")),
                mock.patch.object(cleanupmod, "STATS_CACHE_DIR", os.path.join(tmp, "stats")),
                mock.patch.object(cleanupmod, "LOG_DIR", os.path.join(tmp, "logs")),
                mock.patch.object(cleanupmod, "sweep_orphan_month_cache", lambda: 0),
            ):
                cleanupmod.cleanup_old_files(max_age_seconds=86400)
            self.assertFalse(os.path.exists(stale), "过期的上传文件应被清理")


_F7_BASE = int(datetime(2025, 3, 1, 20, 0, tzinfo=CST).timestamp())


def _f7_text(msg_id, uid, name, ts_s, text):
    return {
        "id": str(msg_id),
        "timestamp": int(ts_s * 1000),
        "time": datetime.fromtimestamp(ts_s, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "sender": {"uid": uid, "name": name},
        "type": "text",
        "content": {"text": text, "elements": [{"type": "text", "data": {"text": text}}]},
    }


def _f7_load(msgs, self_uid="uA"):
    payload = json.dumps(
        {
            "chatInfo": {"name": "对方", "selfUid": self_uid, "selfName": "我"},
            "statistics": {
                "senders": [{"uid": "uA", "name": "我"}, {"uid": "uB", "name": "对方"}],
                "totalMessages": len(msgs),
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        return load_chat(path)
    finally:
        os.remove(path)


def _f7_touch_cache(name: str, content: str = '{"_created": 1}', age_days: int = 0) -> str:
    path = os.path.join(storemod.AI_CACHE_DIR, name)
    os.makedirs(storemod.AI_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


class TestStatsVersionPin(unittest.TestCase):
    def test_bumped(self):

        # 旧缓存里"其他=0"是错的，必须失效重算（只花本地 CPU，零 API 费用）。
        self.assertGreaterEqual(storemod.STATS_SCHEMA_VERSION, 6)
        self.assertGreaterEqual(storemod.GROUP_STATS_SCHEMA_VERSION, 4)


class TestRetentionSwitch(unittest.TestCase):
    """QQCHAT_CACHE_SLIDE_DAYS / QQCHAT_CACHE_MAX_DAYS 决定派生缓存的命运；0 = 不过期。"""

    def _aged(self, name: str, days: int):
        return _f7_touch_cache(name, json.dumps({"_created": time.time() - days * 86400}), age_days=days)

    def test_default_still_collects(self):
        p = self._aged("emotion_hash_x.json", 40)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 30), mock.patch.object(cm, "CACHE_MAX_DAYS", 90):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertFalse(os.path.exists(p))

    def test_zero_means_never_expire(self):
        p = self._aged("emotion_hash_y.json", 400)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 0), mock.patch.object(cm, "CACHE_MAX_DAYS", 0):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertTrue(os.path.exists(p), "两条规则都关掉后，再老也不许删（用户显式要永久保留）")
        os.remove(p)

    def test_slide_off_hard_still_applies(self):
        p = self._aged("emotion_hash_z.json", 100)
        from webapp import cleanup as cm

        with mock.patch.object(cm, "CACHE_SLIDE_DAYS", 0), mock.patch.object(cm, "CACHE_MAX_DAYS", 90):
            cm.cleanup_old_files(max_age_seconds=86400)
        self.assertFalse(os.path.exists(p), "关掉滑动不关掉绝对上限")


class TestStopwordsExternal(unittest.TestCase):
    """停用词外部化（评审遗留 #18）：内置词表永在，文件只做加法；改了文件词云要重算。"""

    def _freq(self, words):
        from datetime import timedelta

        msgs = []
        t = _F7_BASE
        for w in words:
            msgs.append(_f7_text(len(msgs) + 1, "uA", "我", t, w))
            t += int(timedelta(minutes=1).total_seconds())
        return ls.calc_word_freq(_f7_load(msgs))

    def test_builtin_never_disappears(self):
        out = self._freq(["因为 因为"])
        self.assertNotIn("因为", {x["word"] for x in out["self"]}, "内置停用词永远生效")

    def test_external_file_is_additive_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stop.txt")
            base_fp = ls.stopwords_fingerprint()
            with open(path, "w", encoding="utf-8") as f:
                f.write("# 我们的口头禅，从词云里摘掉\n加班\n")
            ls._STOP_CACHE.update(stamp="force-reload", words=frozenset(), text="")
            with mock.patch.dict(os.environ, {"QQCHAT_STOPWORD_FILE": path}):
                fp2 = ls.stopwords_fingerprint()
                self.assertNotEqual(fp2, base_fp, "文件改了，指纹必须变（词云才会重算）")
                out = self._freq(["加班 加班", "加班"])
                self.assertNotIn("加班", {x["word"] for x in out["self"]})
                self.assertEqual(out["stop_fp"], fp2)
            ls._STOP_CACHE.update(stamp="force-reload", words=frozenset(), text="")
            self.assertEqual(ls.stopwords_fingerprint(), base_fp, "撤掉文件回到内置口径")

    def test_freq_result_carries_fingerprint(self):
        self.assertIn("stop_fp", self._freq(["你好"]), "词频要带停用词指纹，懒算缓存靠它判新旧")

    def test_lazy_cache_recomputes_on_fingerprint_mismatch(self):
        """旧词频带着过期 stop_fp → _stats_with_word_freq 必须重算并盖上新指纹。"""
        import app as appmod
        from flask import session as flask_session
        from webapp import store as storemod

        stale = {
            "overview": {"total_messages": 1},
            "word_freq": {"self": [], "other": [], "stop_fp": "STALE00000"},
        }
        with appmod.app.test_request_context("/habits"):
            flask_session["filepath"] = __file__
            flask_session["chat_hash"] = "hashStop"
            with (
                mock.patch.object(
                    storemod,
                    "_load_chat_cached",
                    return_value=_f7_load([_f7_text(1, "uA", "我", _F7_BASE, "你好")]),
                ),
                mock.patch.object(storemod, "_save_stats"),
            ):
                out = storemod._stats_with_word_freq(dict(stale), "hashStop")
        self.assertNotEqual(out["word_freq"]["stop_fp"], "STALE00000")

    def test_lazy_cache_hit_when_fresh(self):
        """指纹一致：命中缓存、绝不再分词（外部化不能把懒算的省钱效果打回去）。"""
        import app as appmod
        from flask import session as flask_session
        from webapp import store as storemod

        fresh_fp = ls.stopwords_fingerprint()
        stats = {
            "overview": {"total_messages": 1},
            "word_freq": {"self": [{"word": "你好", "count": 1}], "other": [], "stop_fp": fresh_fp},
        }
        with appmod.app.test_request_context("/habits"):
            flask_session["filepath"] = __file__  # 路径真实存在，唯一能短路的就是 stop_fp 命中
            with mock.patch.object(storemod, "_load_chat_cached", side_effect=AssertionError("不该重算")):
                out = storemod._stats_with_word_freq(dict(stats), "hashStop")
        self.assertEqual(out["word_freq"]["self"], [{"word": "你好", "count": 1}])


class TestConversationStatsMedian(unittest.TestCase):
    """给模型看的回复间隔中位数与本地统计保持同口径。"""

    def test_median_now_matches_local_stats_algorithm(self):
        """给模型看的"回复间隔中位数"与界面卡用的是同一个算法（口径唯一）。

        就地 `s[n//2]` 在偶数样本上取的是**上中位**：[10,20,30,40] 会报成 30（真值 25），
        两句长度 [1,9] 报成 9（真值 5）。local_stats 为同一个毛病专门顶过
        STATS_SCHEMA_VERSION，而模型侧这句漏了——同一个词在两处算出两个数。
        """
        from analyzer import local_stats as ls
        from analyzer.dialog import _conversation_stats
        from parser.qq_parser import Message

        def mk(i, uid, ts):
            return Message(
                id=str(i),
                timestamp=ts,
                time_str="x",
                sender_uid=uid,
                sender_name=uid,
                text="t",
                raw_text="t",
                msg_type="text",
                has_image=False,
                is_reply=False,
                media_kind="",
                media_label="",
                media_bytes=0,
                media_id="",
                media_path="",
                media_w=0,
                media_h=0,
                face_url="",
                face_ids=[],
                face_names=[],
                image_bytes=0,
                image_count=0,
                image_ids=[],
                recalled=False,
                system=False,
                reply_to_id="",
                reply_to_uid="",
                mentions=[],
                mentions_all=False,
            )

        # 换人且都在同一段内 → gaps = [10, 20, 30, 40]，偶数样本
        msgs = [
            mk(0, "uA", 1_700_000_000_000),
            mk(1, "uB", 1_700_000_010_000),
            mk(2, "uA", 1_700_000_030_000),
            mk(3, "uB", 1_700_000_060_000),
            mk(4, "uA", 1_700_000_100_000),
        ]
        got = _conversation_stats(msgs, "uA")["median_gap"]
        self.assertEqual(got, 25.0, f"偶数样本必须取中间两数平均，实际 {got}（30 = 上中位，就是原来的错法）")
        self.assertEqual(got, ls._median(sorted([10.0, 20.0, 30.0, 40.0])), "与 local_stats 同口径")
        # 奇数样本两种取法本来就一致，顺手钉住别把它改坏
        odd = _conversation_stats(msgs[:4], "uA")["median_gap"]
        self.assertEqual(odd, ls._median(sorted([10.0, 20.0, 30.0])))


class TestCleanupTriggeredWithoutUpload(unittest.TestCase):
    """清理不再只挂在上传路径：任何请求都会按小时去抖触发"""

    def test_get_request_triggers_maybe_cleanup(self):
        import app as appmod
        from webapp import cleanup as cleanupmod

        with (
            mock.patch.object(cleanupmod, "_last_cleanup", [0.0]),
            mock.patch.object(cleanupmod, "cleanup_old_files") as co,
        ):
            appmod.app.test_client().get("/")
            self.assertTrue(co.called, "GET / 也必须触发过期回收")

    def test_debounce_prevents_rescan(self):
        from webapp import cleanup as cleanupmod

        with (
            mock.patch.object(cleanupmod, "_last_cleanup", [time.time()]),
            mock.patch.object(cleanupmod, "cleanup_old_files") as co,
        ):
            cleanupmod.maybe_cleanup(3600)
            self.assertFalse(co.called, "一小时内重复请求不该反复扫描目录")

    def test_concurrent_maybe_cleanup_runs_once(self):
        import threading
        from webapp import cleanup as cleanupmod

        barrier = threading.Barrier(8)
        calls = []

        def slow_cleanup(**_kw):
            calls.append(1)
            time.sleep(0.05)
            return 0

        with (
            mock.patch.object(cleanupmod, "_last_cleanup", [0.0]),
            mock.patch.object(cleanupmod, "cleanup_old_files", side_effect=slow_cleanup),
        ):
            threads = [
                threading.Thread(target=lambda: (barrier.wait(timeout=5), cleanupmod.maybe_cleanup(3600)))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(len(calls), 1, "并发请求超过清理窗口时只应由一个线程执行清理")


class TestFingerprintRobustness(unittest.TestCase):
    """PROMPT_CACHE_SALT 可手动换键；非法宽限期配置不崩。"""

    def test_salt_changes_fingerprint(self):
        import analyzer.deepseek_client as dc

        with mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "2026-08-a"}):
            fp1 = dc._prompt_fingerprint()
            fp2 = dc._prompt_fingerprint()
        with mock.patch.dict(os.environ, {"PROMPT_CACHE_SALT": "2026-08-b"}):
            fp3 = dc._prompt_fingerprint()
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertNotEqual(fp1, dc.PROMPT_FINGERPRINT)

    def test_grace_hours_env_invalid_does_not_crash(self):
        import config

        with mock.patch.dict(os.environ, {"LLM_MONTH_CACHE_GRACE_HOURS": "abc"}):
            val = config.env_number("LLM_MONTH_CACHE_GRACE_HOURS", 24, 0, 720)
        self.assertEqual(val, 24)  # 回退默认而不是 ValueError 崩在 import


class TestUsageBatchWrite(unittest.TestCase):
    """50 次调用合并成一次落盘；读接口永远能立刻看到最新值"""

    def test_batching_and_read_your_write(self):
        import analyzer.usage as usage

        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(tmp)
        usage.flush()  # 清掉别的测试可能留下的未决增量
        orig_file, orig_dump = usage.TOKEN_USAGE_FILE, usage._dump
        writes = []

        def counting_dump(d):
            writes.append(1)
            orig_dump(d)

        usage.TOKEN_USAGE_FILE = tmp
        usage._dump = counting_dump
        try:
            for _ in range(50):
                usage.record_call("deepseek-flash", "emotion", 100, 20)
            self.assertEqual(len(writes), 0, "增量不该逐次落盘")
            u = usage.get_usage()
            self.assertEqual(u["total"]["calls"], 50)
            self.assertEqual(u["dims"]["emotion|deepseek-flash"]["calls"], 50)
            self.assertEqual(len(writes), 1, "读前应恰好冲刷一次")
            usage.get_usage()
            self.assertEqual(len(writes), 1, "无增量的读不该再写盘")
        finally:
            usage._dump = orig_dump
            usage.TOKEN_USAGE_FILE = orig_file
            usage.flush()
            if os.path.exists(tmp):
                os.remove(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
