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
        base = datetime(2025, 9, 16, 21, 0, tzinfo=CST)
        self.msgs = []
        for i, (uid, delta_min) in enumerate([("u2", 0), ("u1", 3), ("u2", 3), ("u2", 1), ("u1", 200)]):
            t = base + timedelta(minutes=delta_min + (0 if i == 0 else 0))
            self.msgs.append(_msg(uid, int(t.timestamp() * 1000), text=f"消息{i}"))

    def test_first_line_has_time_and_name(self):
        line = dc._message_line(self.msgs[0], "B")
        self.assertIn("[09-16 21:00] B:", line)

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


class TestMonthIncrementalCache(unittest.TestCase):
    """增量分析：重新导出多了一个月时，历史月份不该再付费"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqchatlog-monthcache-")
        self.addCleanup(_shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_dir = dc._MONTH_CACHE_DIR
        dc.configure_month_cache(self.tmp)
        self.addCleanup(lambda: dc.configure_month_cache(self._orig_dir))

    def _run(self, chat, calls, chat_hash):
        def fake_api(*a, **kw):
            calls.append(1)
            return {"self_emotion": "平静", "other_emotion": "平静"}

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
                    return_value=_chat([_msg("u1", 1758031009000, "今天加班到十点")]),
                ),
                mock.patch.object(storemod, "_save_stats") as saved,
            ):
                out = storemod._stats_with_word_freq(dict(stats), "hashW")
        self.assertIn("word_freq", out)
        self.assertTrue(saved.called)


class TestConfigHardening(unittest.TestCase):
    """非法配置不再让应用/任务崩在难以理解的地方"""

    def test_env_number_falls_back(self):
        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "0"}):
            self.assertEqual(dc._env_number("LLM_CONCURRENCY", 6, 1, 64), 6)
        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "abc"}):
            self.assertEqual(dc._env_number("LLM_CONCURRENCY", 6, 1, 64), 6)
        with mock.patch.dict(os.environ, {"LLM_CONCURRENCY": "4"}):
            self.assertEqual(dc._env_number("LLM_CONCURRENCY", 6, 1, 64), 4)

    def test_port_validation(self):
        import config

        self.assertEqual(config._env_int("FLASK_PORT", 5000, 1, 65535), 5000)  # 当前环境未设置
        with mock.patch.dict(os.environ, {"FLASK_PORT": "abc"}):
            self.assertEqual(config._env_int("FLASK_PORT", 5000, 1, 65535), 5000)
        with mock.patch.dict(os.environ, {"FLASK_PORT": "99999"}):
            self.assertEqual(config._env_int("FLASK_PORT", 5000, 1, 65535), 5000)
        with mock.patch.dict(os.environ, {"FLASK_PORT": "5001"}):
            self.assertEqual(config._env_int("FLASK_PORT", 5000, 1, 65535), 5001)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
