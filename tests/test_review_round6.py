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
"""第六轮审查的回归测试：缓存生命周期与付费结果的一致性

这一轮修的是"清理说了不算"与"残缺结果冒充完成"两类问题。**关键回归逐条注明反向验证**
怎么做（把修复改回去，这条必须变红）；其余标注"反向保护/既有行为钉"的用例按定义在
旧实现下也应为绿——两类口径不同，别混着数。提交前复核又抓出一条假绿
（logout 引用释放的断言把被测对象自己过滤掉了）与一条恒真（TTL 用例不带 now），
已修正并补了十来个新主题的回归。

1. 换文件时的级联清理不再以"上一份上传文件还在盘上"为前提（views.upload）；
2. 清理之后，晚到的写回一律不复活：维度缓存（调用点 + 写侧自查两层）、月份文件、
   manifest、图片摘要（含 memo 与落盘解耦）、词频；
3. 标记时机：清理标记必须排在删除动作**之前**（否则守卫存在 TOCTOU 窗口），且**去重**
   （双份标记 = 重传一次撤不干净）；
4. 原子写入留下的 .tmp：既不能躲过清理，也不能把别人的月份文件钉成"仍被引用"；
5. 同一份内容被两个会话同时打开时，一方换文件不得删掉另一方的统计与已付费结果；
   退出登录必须真的松开引用；一键全量被清理打断时终态如实 cancelled；
6. 配额中止只成功几个月 → 整个维度如实失败，残缺结果不进维度缓存，月份缓存保住已付部分；
7. 月份缓存核对思考模式口径，但**不改键**：老文件首次消费照常命中（不 retroactively
   收费），命中时补上标记，此后切换模式不再串用（读侧补写不许复活已删文件）；
8. 解析器坏形状面：数字 uid、messages 非数组（必须报错而不是静默吞）、statistics 非
   dict、senders 混 null、JSON Infinity；
9. 统计口径：中位数必须先排序（n≥3 乱序才有鉴别力）、多文件消息的字节逐个记全；
10. 导出快照：剥脚本、剥表单里的 hidden csrf_token、冻结底色取卡片底。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import api_configured_patcher, bootstrap  # noqa: E402

bootstrap()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import month_cache as mc  # noqa: E402
from analyzer import purge_marks  # noqa: E402
from parser.qq_parser import Message  # noqa: E402
from webapp import jobs  # noqa: E402
from webapp import store  # noqa: E402

BASE_MS = 1_700_000_000_000  # 2023-11-14，月份分组为 2023-11


def chat_payload(text="内容", months=1, msgs_per_month=4):
    """两人私聊导出；months>1 时消息跨多个月（配额中止的用例需要多个月份）"""
    msgs = []
    k = 0
    for m in range(months):
        for i in range(msgs_per_month):
            mine = i % 2 == 0
            msgs.append(
                {
                    "id": f"m{k}",
                    "timestamp": BASE_MS + m * 30 * 86400 * 1000 + i * 60000,
                    "time": "2023-11-14 22:13:20",
                    "sender": {
                        "uid": "u_self" if mine else "u_other",
                        "name": "我" if mine else "对方",
                    },
                    "type": "text",
                    "content": {"text": f"{text} 第 {k} 条"},
                }
            )
            k += 1
    return json.dumps(
        {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {
                "totalMessages": len(msgs),
                "senders": [
                    {"uid": "u_self", "name": "我"},
                    {"uid": "u_other", "name": "对方"},
                ],
            },
            "messages": msgs,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def ok_resp(content='{"self_intensity": 5}'):
    r = mock.MagicMock()
    r.choices = [mock.MagicMock()]
    r.choices[0].finish_reason = "stop"
    r.choices[0].message.content = content
    r.usage = None
    return r


def http_err(status, msg):
    e = Exception(msg)
    e.status_code = status
    return e


class FakeCreate:
    """假 client 的 create：第 fail_from 次起吃 402（真实额度耗尽的形状），并累计调用次数

    用带 __call__ 的对象而不是往 Mock 上挂属性：`fake.chat.completions.create` 是
    自动创建的 Mock，函数对象传进去会被包成子 Mock，挂上去的属性就读不到了。
    """

    def __init__(self, fail_from=None, content='{"self_intensity": 5}'):
        self.n = 0
        self.fail_from = fail_from
        self.content = content

    def __call__(self, **_kw):
        self.n += 1
        if self.fail_from is not None and self.n >= self.fail_from:
            raise http_err(402, "Insufficient Balance")
        return ok_resp(self.content)


def make_create(fail_from=None, content='{"self_intensity": 5}'):
    return FakeCreate(fail_from=fail_from, content=content)


class CacheLifecycleCase(unittest.TestCase):
    """脚手架：把 ai_cache / stats_cache 指到本用例独占目录，并复位进程内状态"""

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.app = appmod.app
        cls._api = api_configured_patcher()
        cls._api.start()

    @classmethod
    def tearDownClass(cls):
        cls._api.stop()

    def setUp(self):
        self.ai_dir = tempfile.mkdtemp(prefix="r6-ai-")
        self.stats_dir = tempfile.mkdtemp(prefix="r6-stats-")
        self._patches = [
            mock.patch.object(store, "AI_CACHE_DIR", self.ai_dir),
            mock.patch.object(store, "STATS_CACHE_DIR", self.stats_dir),
        ]
        for p in self._patches:
            p.start()
        mc.configure_month_cache(self.ai_dir)
        store.clear_live_chat_refs()
        purge_marks.clear()
        self._orig_interval = dc.CALL_MIN_INTERVAL
        dc.CALL_MIN_INTERVAL = 0.0

    def tearDown(self):
        dc.CALL_MIN_INTERVAL = self._orig_interval
        mc.configure_month_cache("")
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.ai_dir, ignore_errors=True)
        shutil.rmtree(self.stats_dir, ignore_errors=True)
        store.clear_live_chat_refs()
        purge_marks.clear()

    def ai_names(self):
        return sorted(os.listdir(self.ai_dir))

    def month_files(self):
        return [n for n in self.ai_names() if n.startswith("month_")]

    def new_client(self):
        self.app.config["TESTING"] = True
        return self.app.test_client()

    def upload(self, client, payload):
        client.get("/")
        with client.session_transaction() as s:
            token = s.get("csrf_token", "")
        return client.post(
            "/upload",
            data={"file": (io.BytesIO(payload), "chat.json"), "csrf_token": token},
            headers={"Origin": "http://127.0.0.1:5000"},
        )

    def session_of(self, client):
        with client.session_transaction() as s:
            return s.get("chat_hash", ""), s.get("filepath", ""), getattr(s, "sid", "")


class TestPurgeWithoutOldFile(CacheLifecycleCase):
    """断言 1：级联清理不能以"上一份上传文件还在"为前提"""

    def test_purge_runs_when_previous_upload_was_recycled(self):
        """旧上传文件被 24 小时回收后再换文件，上一条聊天的派生缓存照样要删

        反向验证：把 views.upload 里 old_hash 的取法改回
        `store._chat_hash(old_path)`（依赖旧文件可读），本条立刻变红 ——
        那时 old_hash 取不到，整段清理被跳过，而界面已经按"换文件即清理"报过信。
        """
        c = self.new_client()
        self.assertEqual(self.upload(c, chat_payload("聊天甲")).status_code, 302)
        h_first, path_first, _sid = self.session_of(c)
        store.wait_for_stats(h_first, timeout=30)

        # 这个聊天的两类派生结果：维度缓存（含聊天原句引用）与本地统计
        store._write_cache("emotion", h_first, {"quote": "甲的原句"})
        self.assertIsNotNone(store._read_cache("emotion", h_first))
        self.assertTrue(os.path.exists(store._stats_path(h_first)))

        os.remove(path_first)  # 模拟 uploads/ 的 24 小时回收（会话仍在，每个请求都续期）

        self.upload(c, chat_payload("聊天乙"))
        self.assertIsNone(store._read_cache("emotion", h_first), "旧聊天的付费结果必须随换文件删除")
        self.assertFalse(os.path.exists(store._stats_path(h_first)), "旧聊天的统计缓存必须随换文件删除")

    def test_same_content_reupload_keeps_cache(self):
        """重传同一份内容：哈希相同 → 不清理（省钱，这条既有行为不能被改坏）"""
        c = self.new_client()
        payload = chat_payload("同一份")
        self.upload(c, payload)
        h, _p, _s = self.session_of(c)
        store.wait_for_stats(h, timeout=30)
        store._write_cache("emotion", h, {"quote": "同一份的原句"})
        self.upload(c, payload)
        self.assertIsNotNone(store._read_cache("emotion", h))


class TestLiveSessionReferences(CacheLifecycleCase):
    """断言 5：同一份内容被两个会话打开时，一方换文件不得删另一方的数据"""

    def test_other_session_keeps_its_paid_results(self):
        """会话 2 换文件 → 会话 1 的统计与付费结果都还在，仪表盘不弹回首页

        反向验证：删掉 views.upload 里对 store.other_live_sessions() 的判断
        （恢复"按内容哈希无条件清理"），本条变红：c1 的维度缓存被删、/dashboard 回 302。
        """
        payload = chat_payload("两个浏览器同一份")
        c1 = self.new_client()
        self.upload(c1, payload)
        h, _p1, _sid1 = self.session_of(c1)
        store.wait_for_stats(h, timeout=30)
        store._write_cache("emotion", h, {"quote": "共同聊天的原句"})
        self.assertEqual(c1.get("/dashboard").status_code, 200)

        c2 = self.new_client()
        self.upload(c2, payload)
        h2, _p2, _sid2 = self.session_of(c2)
        self.assertEqual(h2, h, "同一份内容必须是同一个哈希（前提）")
        c2.get("/dashboard")  # 会话 2 真实在用，引用被刷新

        self.upload(c2, chat_payload("会话2换的另一份"))

        self.assertIsNotNone(store._read_cache("emotion", h), "会话 1 的已付费结果不该被删")
        self.assertTrue(os.path.exists(store._stats_path(h)), "会话 1 的统计不该被删")
        self.assertEqual(c1.get("/dashboard").status_code, 200, "会话 1 不该被弹回首页")

    def test_purge_happens_after_last_holder_leaves(self):
        """最后一个持有者也换走之后，清理照常发生（引用不能把缓存永久钉住）"""
        payload = chat_payload("两个会话")
        c1, c2 = self.new_client(), self.new_client()
        self.upload(c1, payload)
        h, _p, _s = self.session_of(c1)
        self.upload(c2, payload)
        store._write_cache("emotion", h, {"quote": "原句"})

        self.upload(c1, chat_payload("另一份1"))  # 还有 c2 在用 → 不该删
        self.assertIsNotNone(store._read_cache("emotion", h))
        self.upload(c2, chat_payload("另一份2"))  # 没人用了 → 该删
        self.assertIsNone(store._read_cache("emotion", h))

    def test_reference_ttl_expires(self):
        """引用有效期与会话文件本身的 24 小时回收同口径，不会永久挡住隐私清理"""
        with mock.patch.object(store, "LIVE_CHAT_REF_TTL", 60.0):
            store.note_live_chat("hStale", "sidA", now=1000.0)
            self.assertEqual(store.other_live_sessions("hStale", exclude_sid="", now=2000.0), [])
        store.note_live_chat("hFresh", "sidB", now=1000.0)
        self.assertEqual(store.other_live_sessions("hFresh", exclude_sid="", now=1001.0), ["sidB"])
        # exclude 必须**带 now 打在同一条时间轴上**才有效：不带 now 时条目按真实时钟
        # 早已过期（1000.0 = 1970 年），"排除自己"的分支根本不会被执行，断言恒真。
        self.assertEqual(store.other_live_sessions("hFresh", exclude_sid="sidB", now=1001.0), [])
        self.assertEqual(store.other_live_sessions("hFresh", exclude_sid="sidC", now=1001.0), ["sidB"])

    def test_logout_releases_reference(self):
        """退出登录就是明确的"不再使用"，不该留一条幽灵引用挡住清理

        断言必须 exclude_sid=""：幽灵引用正是登出会话**自己**那条，若像最初那样
        exclude_sid=sid，等于把被测对象过滤掉再断言"没有"——forget_live_chat 删成
        no-op 这条也照样绿（提交前复核抓出的假绿用例）。真正要钉的症状是别的会话
        换文件时 other_live_sessions(old_hash, exclude_sid=别的sid) 看见这条幽灵、
        把级联清理挡最多 24 小时。
        反向验证：删掉 webapp/security.py logout 里的 store.forget_live_chat 一行，本条变红。
        """
        payload = chat_payload("退出前打开")
        c = self.new_client()
        self.upload(c, payload)
        h, _p, sid = self.session_of(c)
        self.assertTrue(h, "上传必须成功（本用例的前提）")
        self.assertTrue(store.other_live_sessions(h, exclude_sid=""), "上传后应有引用")
        self.assertIn(sid, store.other_live_sessions(h, exclude_sid=""))

        c.get("/")
        with c.session_transaction() as s:
            token = s["csrf_token"]
        c.post("/logout", data={"csrf_token": token}, headers={"Origin": "http://127.0.0.1:5000"})

        self.assertEqual(store.other_live_sessions(h, exclude_sid=""), [], "登出后该会话的引用必须消失")

    def test_live_ref_table_is_bounded(self):
        """引用表必须有硬上限（长期运行的实例里，每个上传过的哈希都会被记一笔）

        反向验证：删掉 note_live_chat 里的 _prune_live_refs_locked 调用，本条变红
        （50 笔引用全部留在表里）。修剪原先只发生在"该哈希又被读一次"的时候，而每个
        上传过的哈希都是只写不读的老条目，于是这张表只增不减。
        """
        with mock.patch.object(store, "LIVE_CHAT_REFS_MAX", 8):
            for i in range(50):
                store.note_live_chat(f"hBound{i}", f"sid{i}", now=1000.0 + i)
            total = sum(len(holders) for holders in store._LIVE_CHAT_REFS.values())
            self.assertLessEqual(total, 8, f"引用表超上限了：{total} 笔")
            newest = store.other_live_sessions("hBound49", exclude_sid="", now=1049.0)
            self.assertEqual(newest, ["sid49"], "最新的一笔不该被淘汰（淘汰从最旧开始）")


class TestResurrectionGuards(CacheLifecycleCase):
    """断言 2/3：清理之后任何晚到的写回都不能让数据复活"""

    def test_mark_is_set_before_any_deletion(self):
        """标记必须排在删除之前，否则"先查守卫再落盘"的线程仍能写回来

        反向验证：把 _mark_purged(chat_hash) 挪回 _purge_chat_caches 末尾，本条变红
        （purge_month_cache 被调用时标记还不存在）。
        """
        seen = {}
        real = mc.purge_month_cache

        def spy(chat_hash):
            seen["marked"] = purge_marks.is_marked(chat_hash)
            return real(chat_hash)

        with mock.patch.object(store, "purge_month_cache", spy):
            store._purge_chat_caches("hashMarkOrder")
        self.assertTrue(seen.get("marked"), "开始删除时就必须已经标记")

    def test_ai_job_does_not_write_dimension_cache_after_purge(self):
        """分析跑到一半用户换了文件：这一轮的结果不写回，任务如实记为 cancelled

        反向验证：去掉 jobs._stop_requested 里的 purge_marks 判据，本条变红
        （任务置 done 并把结果落盘，清理报称已删的数据原地复活）。
        """
        c = self.new_client()
        self.upload(c, chat_payload("跑到一半换文件"))
        h, filepath, sid = self.session_of(c)

        def fake_analyze(_chat, on_progress=None, should_cancel=None, chat_hash=""):
            store._purge_chat_caches(chat_hash)  # 中途被清理
            return {"2023-11": {"self_intensity": 5}}

        job_id = jobs._get_or_create_job(sid, "emotion", h, total=1)[0]
        with mock.patch.object(jobs, "analyze_func_for", return_value=fake_analyze):
            jobs._run_job(job_id, "emotion", filepath, h)

        self.assertIsNone(store._read_cache("emotion", h), "清理后晚到的结果不该落盘")
        with jobs.JOBS_LOCK:
            status = jobs.JOBS.get(job_id, {}).get("status")
        self.assertEqual(status, "cancelled", f"应如实记为 cancelled，实际 {status}")

    def test_word_freq_writeback_is_guarded(self):
        """词频回写是第四条复活路径，也要查守卫

        反向验证：去掉 _stats_with_word_freq 里的 _is_recently_purged 判断，本条变红。
        """
        c = self.new_client()
        self.upload(c, chat_payload("词频复活用例"))
        h, filepath, _sid = self.session_of(c)
        store.wait_for_stats(h, timeout=30)
        stats = {"overview": {"total_messages": 4}}

        with self.app.test_request_context("/habits"):
            from flask import session as fsession

            fsession["chat_hash"] = h
            fsession["filepath"] = filepath
            fsession["chat_mode"] = "private"
            store._purge_chat_caches(h)
            self.assertTrue(store._is_recently_purged(h))
            store._stats_with_word_freq(stats, h)

        self.assertIsNotNone(stats.get("word_freq"), "页面照样该拿到词频（只是不落盘）")
        self.assertFalse(os.path.exists(store._stats_path(h)), "被清掉的统计不该被词频回写复活")

    def test_reupload_clears_stale_purge_mark(self):
        """重新上传必须先撤销"刚被清理"标记，否则后续分析会被静默取消

        这条路只在"清理标记生效、统计文件却没删掉"时才走到（Windows 上文件被占用是
        常见情形）：此时重新上传会**命中统计缓存**，于是 start_stats_job 不执行、它里面
        那处撤销也不会执行，标记一直生效 —— 用户看到的是"一键全量永远停在取消状态"，
        没有报错、没有任何落盘。所以撤销要挂在"上传成功"这个事实上，而不是挂在
        "统计需要重算"这个副作用上。
        反向验证：删掉 views.upload 里的 store._unmark_purged(new_hash)，本条变红。
        """
        c = self.new_client()
        payload = chat_payload("标记残留")
        self.upload(c, payload)
        h, filepath, sid = self.session_of(c)
        store.wait_for_stats(h, timeout=30)

        # 造出"标记在、统计文件也还在"的状态（等价于清理时统计文件删不掉）
        store._save_stats(h, {"overview": {"total_messages": 4}})
        store._mark_purged(h)
        self.assertTrue(store._is_recently_purged(h))

        self.upload(c, payload)  # 统计命中缓存 → 不会走 start_stats_job
        self.assertFalse(store._is_recently_purged(h), "重新上传后标记必须撤销")
        # 重新上传会写一份新的上传副本并删掉旧的（同一内容、同一个哈希），
        # 所以文件路径要重新取一次，不能沿用第一次那份
        h2, filepath2, _sid2 = self.session_of(c)
        self.assertEqual(h2, h, "同一份内容必须是同一个哈希（本用例的前提）")

        job_id = jobs._get_or_create_job(sid, "emotion", h, total=1)[0]
        with mock.patch.object(
            jobs, "analyze_func_for", return_value=lambda *a, **k: {"2023-11": {"self_intensity": 5}}
        ):
            jobs._run_job(job_id, "emotion", filepath2, h)
        with jobs.JOBS_LOCK:
            status = jobs.JOBS.get(job_id, {}).get("status")
        self.assertEqual(status, "done", f"不该被残留标记静默取消，实际 {status}")
        self.assertIsNotNone(store._read_cache("emotion", h), "正常跑完的结果应当落盘")

    def test_manifest_is_not_rebuilt_after_purge(self):
        """收尾的 manifest 记账也要受守卫，否则月份文件被重新钉成「仍被引用」"""

        purge_marks.mark("hashManifest")
        mc._record_month_usage("hashManifest", {"someMonthKey"})
        self.assertNotIn("manifest_hashManifest.json", self.ai_names())
        purge_marks.unmark("hashManifest")
        mc._record_month_usage("hashManifest", {"someMonthKey"})
        self.assertIn("manifest_hashManifest.json", self.ai_names(), "撤销标记后必须恢复正常记账")

    def test_month_files_are_not_written_after_purge(self):
        """月份文件本身也不许复活：清理之后跑完的这一轮，盘上不该长出 month_*"""
        months = {
            "2023-11": [
                Message(
                    "m1",
                    BASE_MS,
                    "2023-11-14 22:13:20",
                    "我",
                    "u_self",
                    "一月的话",
                    "一月的话",
                    "text",
                    False,
                    False,
                )
            ]
        }
        purge_marks.mark("hMonthPurge")
        fake = mock.Mock()
        fake.chat.completions.create.return_value = ok_resp()
        with mock.patch.object(dc, "_get_client", return_value=fake):
            out = dc._analyze_periods(
                months, "sys", lambda p, m: f"对话 {p}", max_tokens=1024, chat_hash="hMonthPurge"
            )
        self.assertEqual(sorted(out), ["2023-11"], "结果照常返回给这一轮")
        self.assertEqual(self.month_files(), [], "但月份缓存不许落盘")

    def test_write_cache_self_check_after_purge(self):
        """维度缓存的写方也要自查守卫：jobs 的 should_cancel 查在写之前，查→写之间有窗口

        本轮给月份/图片/词频装的都是"落盘点自查"，唯独 _write_cache 还停留在调用点守卫；
        并发清理正好落进那半步，清掉的维度缓存就复活。
        反向验证：删掉 store._write_cache 开头的 _is_recently_purged 判断，本条变红。
        """
        store._mark_purged("hDimGuard")
        store._write_cache("emotion", "hDimGuard", {"q": "原句"})
        self.assertEqual([n for n in self.ai_names() if n.startswith("emotion_")], [], "清理后不许落盘")
        purge_marks.unmark("hDimGuard")
        store._write_cache("emotion", "hDimGuard", {"q": "原句"})
        self.assertTrue([n for n in self.ai_names() if n.startswith("emotion_")], "撤销标记后恢复正常")

    def test_double_mark_needs_only_one_reupload(self):
        """标记必须去重：unmark 一次只撤一条，双份标记 = 重传一次撤不干净

        场景真实存在：双击上传触发两次 switching，旧哈希被清理两次；之后重新上传该
        聊天时只有 views.upload 一处无条件撤销（统计命中缓存则 start_stats_job 不执行），
        残留的那份标记会把此后所有分析判成"已废弃"——静默取消、不落盘，正是这套守卫
        自己制造出来的症状。
        反向验证：去掉 purge_marks.mark 的 `not in _MARKS` 去重，本条变红。
        """
        purge_marks.mark("hDedupe")
        purge_marks.mark("hDedupe")
        purge_marks.unmark("hDedupe")
        self.assertFalse(purge_marks.is_marked("hDedupe"), "两次 mark 只算一份标记，一次撤销即清")

    def test_analyze_all_reports_cancelled_when_chat_purged(self):
        """一键全量在跑到一半被清理：终态如实 cancelled，不许标成 done

        收尾判据在 JOBS_LOCK 里不能调 _stop_requested()（不可重入），但 chat_hash 与
        purge_marks 都现成可判——漏了第三条，循环因清理而 break 的任务会被记成 done，
        界面上显示"全量分析完成"而结果什么都没写。
        反向验证：去掉终态判断里的 purge_marks.is_marked 一支，本条变红（状态成 done）。
        """
        c = self.new_client()
        self.assertEqual(self.upload(c, chat_payload("全量跑到一半清理")).status_code, 302)
        h, filepath, sid = self.session_of(c)

        def fake(_chat, on_progress=None, should_cancel=None, chat_hash=""):
            store._purge_chat_caches(chat_hash)  # 第一个维度开工即被清理
            return {"2023-11": {"k": 1}}

        job_id = jobs._get_or_create_job(sid, "all", h, total=5)[0]
        with mock.patch.object(jobs, "analyze_func_for", return_value=fake):
            jobs._run_analyze_all(job_id, filepath, h, refresh=False)
        with jobs.JOBS_LOCK:
            status = jobs.JOBS.get(job_id, {}).get("status")
        self.assertEqual(status, "cancelled", f"清理打断的全量必须记 cancelled，实际 {status}")
        self.assertEqual(
            [n for n in self.ai_names() if n.startswith(("emotion_", "topics_", "relationship_"))],
            [],
            "任何维度都不许落盘",
        )

    def test_vision_digest_not_written_after_purge(self):
        """图片摘要是第四条复活路径（本轮 docstring 列了它，之前整段没人测）"""
        import analyzer.vision as vision

        img = [{"key": "vkey-1", "path": "resources/images/a.jpg", "w": 900, "h": 700}]
        with (
            mock.patch.object(vision, "available", return_value=True),
            mock.patch.object(vision, "pick_images", return_value=img),
            mock.patch.object(dc, "_call_vision", return_value="一张截图"),
            mock.patch.object(vision, "AI_CACHE_DIR", self.ai_dir),
            mock.patch.object(vision, "_MEMO", {}),
        ):
            purge_marks.mark("hVision")
            out = vision.digest([], chat_hash="hVision", label="2023-11")
            self.assertEqual(out, "一张截图", "已付费拿到的摘要照常返回给这一轮")
            self.assertEqual([n for n in self.ai_names() if n.startswith("vision_")], [], "但摘要不许落盘")

            # 反向验证：把守卫写成"连 memo 一起跳过"（本轮最初的实现），下面这段变红——
            # 同一批图在多个维度间复用，拒收内存缓存等于转头再为同一批付一次费。
            n_calls = dc._call_vision.call_count if hasattr(dc._call_vision, "call_count") else None
            out2 = vision.digest([], chat_hash="hVision", label="2023-11 第二次")
            self.assertEqual(out2, "一张截图")
            if n_calls is not None:
                self.assertEqual(dc._call_vision.call_count, 1, "第二次必须吃到 memo，不再调用 API")

            purge_marks.unmark("hVision")
            img2 = [{"key": "vkey-2", "path": "resources/images/b.jpg", "w": 900, "h": 700}]
            with mock.patch.object(vision, "pick_images", return_value=img2):
                vision.digest([], chat_hash="hVision", label="重传后")
            self.assertTrue(
                [n for n in self.ai_names() if n.startswith("vision_")],
                "撤销标记后（重新上传同一份内容）恢复正常落盘",
            )


class TestTmpCleanup(CacheLifecycleCase):
    """断言 4：原子写入留下的 .tmp"""

    def test_belongs_to_matches_tmp_siblings(self):
        for name in (
            "stats_abcd1234.json",
            "stats_abcd1234.json.tmp",
            "manifest_abcd1234.json.tmp",
            "emotion_abcd1234_model_fp.json.tmp",
            "vision_abcd1234_key.json.tmp",
        ):
            self.assertTrue(store._cache_belongs_to(name, "abcd1234"), f"该认得 {name}")
        self.assertFalse(store._cache_belongs_to("stats_abcd12345.json", "abcd1234"), "不许误删别人")
        self.assertFalse(store._cache_belongs_to("month_deadbeef.json", "abcd1234"))

    def test_purge_removes_stats_tmp_sibling(self):
        """统计缓存的半成品必须一起删：那里面就是同一份统计结果（含词频=聊天原词）"""
        h = "hashTmpStats"
        store._save_stats(h, {"overview": {}})
        tmp = f"{store._stats_path(h)}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"overview": {"secret_word": "体检"}}, f)
        store._purge_chat_caches(h)
        self.assertFalse(os.path.exists(store._stats_path(h)))
        self.assertFalse(os.path.exists(tmp), "含派生内容的半成品不该在「已清理」之后留在盘上")

    def test_manifest_write_failure_removes_its_tmp(self):
        """写失败分支要自己收尾（与 _write_month_cache、_save_stats 一致）"""
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            mc._record_month_usage("hashManifestFail", {"k1"})
        self.assertNotIn("manifest_hashManifestFail.json.tmp", self.ai_names(), "留下的 .tmp 会被当成活清单")

    def test_tmp_manifest_does_not_pin_month_files(self):
        """一份半途的 manifest 不该把含聊天原句的月份文件永久钉住

        反向验证：去掉 _referenced_keys_locked 里的 `and n.endswith(".json")`，本条变红
        —— .tmp 被当成活清单，月份文件算"仍被引用"，purge 与孤儿回收都收不走它。
        """
        key = "1213654df6f771dfa309"
        month_path = mc.month_cache_path(key)
        with open(month_path, "w", encoding="utf-8") as f:
            json.dump({"quote": "[01-02 03:04] 对方: 一月的话"}, f, ensure_ascii=False)
        old = time.time() - 7 * 86400  # 早已超过宽限期
        os.utime(month_path, (old, old))

        ghost = Path(mc._manifest_path("hashGhost"))
        ghost.write_text(json.dumps({"months": [key]}), encoding="utf-8")
        os.replace(ghost, Path(str(ghost) + ".tmp"))  # 半途文件：前缀还是 manifest_

        self.assertGreaterEqual(mc.sweep_orphan_month_cache(), 1, "半途 manifest 不该钉住月份文件")
        self.assertFalse(os.path.exists(month_path), "含聊天原句引用的月份文件必须被回收")


class TestQuotaPartialResult(CacheLifecycleCase):
    """断言 6：配额中止只成功几个月时，残缺结果绝不能当成功"""

    def test_partial_periods_raise(self):
        """第 2 个月吃到 402 → 整个维度上报失败，而不是返回"看起来正常"的残缺字典"""
        months = {
            f"2024-{m + 1:02d}": [
                Message(
                    f"m{m}",
                    BASE_MS + m * 30 * 86400 * 1000,
                    "t",
                    "我",
                    "u_self",
                    f"第{m}月的话",
                    f"第{m}月的话",
                    "text",
                    False,
                    False,
                )
            ]
            for m in range(3)
        }
        mc.configure_month_cache("")  # 本条只看"抛不抛"，月份缓存落不落盘由下一条管
        fake = mock.Mock()
        fake.chat.completions.create.side_effect = make_create(fail_from=2)
        with mock.patch.object(dc, "_get_client", return_value=fake):
            with self.assertRaises(dc.QuotaExhaustedError):
                dc._analyze_periods(months, "sys", lambda p, m: f"对话 {p}", max_tokens=1024, tag="emotion")

    def test_partial_result_not_cached_job_reports_error(self):
        """完整链路：任务记为 error、维度缓存不写、月份缓存保住已付费的那几个月

        反向验证：把 _analyze_periods 末尾改回 `if not results: raise`（有结果就当成功），
        本条变红 —— 任务 done、维度缓存里只有 1 个月，而用户之后每次都命中这份残缺缓存，
        缺掉的月份永远不会再补（这正是 _stop_requested 文档为取消路径立的规矩）。
        """
        c = self.new_client()
        self.upload(c, chat_payload("配额中途耗尽", months=3, msgs_per_month=4))
        h, filepath, sid = self.session_of(c)
        store.wait_for_stats(h, timeout=30)

        fake = mock.Mock()
        fake.chat.completions.create.side_effect = make_create(fail_from=2)
        job_id = jobs._get_or_create_job(sid, "emotion", h, total=3)[0]
        with mock.patch.object(dc, "_get_client", return_value=fake):
            jobs._run_job(job_id, "emotion", filepath, h)

        with jobs.JOBS_LOCK:
            job = jobs.JOBS.get(job_id, {})
        self.assertEqual(job.get("status"), "error", f"应如实失败，实际 {job.get('status')}")
        self.assertIn("402", job.get("error", "") + str(job), "错误信息要能看出是额度问题")
        self.assertIsNone(store._read_cache("emotion", h), "残缺结果绝不能进维度缓存")
        self.assertGreaterEqual(
            len(self.month_files()), 1, "已经付费的那几个月必须留在月份缓存里（重跑不重复付费）"
        )


class TestMonthCacheThinkingScope(CacheLifecycleCase):
    """断言 7：月份缓存核对思考模式口径，但不改键（老缓存不失效、不重新付费）"""

    def test_read_rejects_other_mode(self):
        mc._write_month_cache("kMode00001", {"who": "思考模式产出"}, thinking=True)
        self.assertIsNone(mc._read_month_cache("kMode00001", expect_thinking=False), "口径不同必须未命中")
        got = mc._read_month_cache("kMode00001", expect_thinking=True)
        self.assertEqual(got, {"who": "思考模式产出"})
        self.assertNotIn("_thinking", got, "口径声明不许泄进调用方拿到的结果")

    def test_legacy_unstamped_file_still_accepted_then_stamped(self):
        """升级前的月份文件没有标记：首次按当前模式照常消费（绝不 retroactively 收费），
        但命中时当场补上当前模式的标记——不补的话这类文件会被任何模式永久放行，
        "切换后不再串模式"就只对升级后新写的文件成立，配置依旧静默失效。

        反向验证：①删掉 _read_month_cache 里 expect_thinking 的核对，第二段变红；
        ②删掉补标记的 _restamp_month_cache 调用，"切换后必须未命中"那一段变红
        （无标记文件继续被任何模式放行 = 本条要防的静默失效）。
        """
        for mode in (True, False):
            key = f"kLegacy{int(mode)}0001"
            p = mc.month_cache_path(key)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"_created": time.time(), "who": "升级前的结果"}, f, ensure_ascii=False)
            got = mc._read_month_cache(key, expect_thinking=mode)
            self.assertEqual(
                (got or {}).get("who"), "升级前的结果", f"老文件首次消费必须命中（thinking={mode}）"
            )
            self.assertNotIn("_thinking", got or {}, "补上的标记不许泄进调用方结果")
            self.assertIsNone(
                mc._read_month_cache(key, expect_thinking=not mode),
                f"补标记（{mode}）之后再切到 {not mode}：必须未命中，这才是口径生效",
            )
            self.assertIsNotNone(mc._read_month_cache(key, expect_thinking=mode), "同模式继续命中（零成本）")
            with open(p, encoding="utf-8") as f:
                self.assertEqual(bool(json.load(f).get("_thinking")), mode, "标记必须真的写进了文件")

    def test_restamp_does_not_resurrect_purged_file(self):
        """补标记也是写盘：文件在"读出→补写"之间被级联清理删掉时，绝不把它重新造出来"""
        key = "kRestampRace"
        p = mc.month_cache_path(key)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"_created": time.time(), "who": "将被清掉"}, f, ensure_ascii=False)
        data = mc._read_month_cache(key, expect_thinking=True)  # 先命中并补标记
        self.assertIsNotNone(data)
        os.remove(p)  # 模拟清理删掉文件（标记之外的删除路径，这里直接删）
        mc._restamp_month_cache(p, data, False, time.time())
        self.assertFalse(os.path.exists(p), "已删文件不许被补标记复活")

    def test_mode_switch_reissues_call_instead_of_reusing(self):
        """切换 LLM_THINKING 之后重跑：必须真的重发调用，而不是吃另一种模式的结果

        反向验证：把 _read_month_cache 里的 expect_thinking 校验删掉，本条变红
        （第二次跑出 0 次调用，用户以为自己关了思考，其实拿的还是思考模式的产出）。
        """
        c = self.new_client()
        self.upload(c, chat_payload("思考开关切换"))
        h, filepath, sid = self.session_of(c)

        fake = mock.Mock()
        creator = make_create()
        fake.chat.completions.create.side_effect = creator
        with mock.patch.object(dc, "_get_client", return_value=fake):
            with mock.patch.object(dc, "THINKING_DEFAULT", True):
                jobs._run_job(jobs._get_or_create_job(sid, "emotion", h, total=1)[0], "emotion", filepath, h)
            self.assertEqual(creator.n, 1, "第一轮该发一次调用")
            with mock.patch.object(dc, "THINKING_DEFAULT", False):
                jobs._run_job(jobs._get_or_create_job(sid, "emotion", h, total=1)[0], "emotion", filepath, h)
            self.assertEqual(creator.n, 2, "口径切换后必须重新分析（月份不许串用）")

    def test_month_key_formula_is_unchanged(self):
        """这次修复不许改动任何缓存键：改键 = 所有既有月份缓存作废 = 用户重新付费

        反向验证：把 thinking 塞进 _month_key 的哈希输入，本条变红。
        """
        self.assertEqual(mc._month_key("SYS", "USER"), mc._month_key("SYS", "USER", dc.PROMPT_FINGERPRINT))
        with mock.patch.object(dc, "THINKING_DEFAULT", True):
            on = mc._month_key("SYS", "USER")
            dim_on = store._cache_path("emotion", "hKey")
        with mock.patch.object(dc, "THINKING_DEFAULT", False):
            off = mc._month_key("SYS", "USER")
            dim_off = store._cache_path("emotion", "hKey")
        self.assertEqual(on, off, "月份键必须与思考模式无关（口径靠文件内的标记核对）")
        self.assertTrue(dim_on.endswith("_think.json"), "维度缓存分模式存放是既有行为")
        self.assertFalse(dim_off.endswith("_think.json"))


class TestParserRobustness(unittest.TestCase):
    """断言 B1/B2：占位 sender 不再冒充"对方"或把私聊判成群聊；坏形状不再抛"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="r6-parse-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def load(self, messages, senders=None, stats_extra=None, chat_info=None):
        payload = {
            "chatInfo": chat_info or {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {
                "totalMessages": len(messages),
                "senders": senders
                if senders is not None
                else [
                    {"uid": "u_self", "name": "我"},
                    {"uid": "u_other", "name": "对方"},
                ],
            },
            "messages": messages,
        }
        if stats_extra:
            payload["statistics"].update(stats_extra)
        p = Path(self.dir) / "case.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        from parser.qq_parser import load_chat

        return load_chat(str(p))

    @staticmethod
    def msg(uid, name, i, text="说话", **extra):
        m = {
            "id": f"{uid}-{i}",
            "timestamp": BASE_MS + i * 60000,
            "time": "2023-11-14 22:13:20",
            "sender": {"uid": uid, "name": name},
            "type": "text",
            "content": {"text": f"{text}{i}"},
        }
        m.update(extra)
        return m

    @staticmethod
    def phantom(i):
        """导出器给系统类消息安排的占位 sender，且**没有** system 标记（实测会漏标）"""
        return {
            "id": f"ph{i}",
            "timestamp": BASE_MS + (500 + i) * 60000,
            "time": "2023-11-14 23:00:00",
            "sender": {"uid": "未知uid未知", "name": "系统消息"},
            "type": "text",  # 不是被过滤的 type_23/system，is_statistical 会放行
            "content": {"text": "[消息提示]"},
        }

    def test_two_party_with_three_unmarked_phantoms_stays_private(self):
        """3 条未标记的占位消息不该把两人私聊判成群聊

        反向验证：去掉 _multi_party_offenders 里的 is_placeholder_sender 过滤，本条变红
        ——门槛只有 3 条，于是 mode 变成 group、other_uid 被置空、response_time 不再算。
        """
        msgs = [self.msg("u_self", "我", i) for i in range(30)]
        msgs += [self.msg("u_other", "对方", 100 + i) for i in range(28)]
        msgs += [self.phantom(i) for i in range(3)]
        chat = self.load(msgs)
        self.assertFalse(chat.is_group_chat, "占位 sender 不该被算成第三方")
        self.assertEqual(chat.mode, "private")
        self.assertEqual(chat.other_uid, "u_other")

    def test_off_mode_does_not_reject_private_with_phantoms(self):
        """QQCHAT_GROUP_CHAT=off 下同一份私聊文件也不该被当作群聊拒收"""
        msgs = [self.msg("u_self", "我", i) for i in range(20)]
        msgs += [self.msg("u_other", "对方", 100 + i) for i in range(20)]
        msgs += [self.phantom(i) for i in range(4)]
        with mock.patch.dict(os.environ, {"QQCHAT_GROUP_CHAT": "off"}):
            chat = self.load(msgs)  # 旧实现会在这里抛 ValueError
        self.assertFalse(chat.is_group_chat)

    def test_real_third_party_still_detected(self):
        """反向保护：真人第三方必须照样判成群聊（别把修复做成"永远不当群聊"）"""
        msgs = [self.msg("u_self", "我", i) for i in range(20)]
        msgs += [self.msg("u_other", "对方", 100 + i) for i in range(20)]
        msgs += [self.msg("uC", "阿强", 200 + i) for i in range(6)]
        chat = self.load(msgs)
        self.assertTrue(chat.is_group_chat, "真第三方被漏判的话，多人导出又会退回两分类")

    def test_other_name_follows_other_uid(self):
        """other_name 与 other_uid 必须指向同一个人（名字此前按文件顺序取，会指错）

        反向验证：把 load_chat 结尾按 uid 重算名字的三行删掉，本条变红 ——
        statistics.senders 里占位条目排在前面的导出，"对方"会被写成"系统消息"，
        而仪表盘、日志、以及喂给模型的每一行非我方发言都跟着错署名。
        """
        msgs = [self.msg("u_self", "我", i) for i in range(10)]
        msgs += [self.msg("uB", "真人对方", 50 + i) for i in range(8)]
        chat = self.load(
            msgs,
            senders=[
                {"uid": "未知uid未知", "name": "系统消息"},  # 占位排在最前
                {"uid": "uB", "name": "真人对方"},
                {"uid": "u_self", "name": "我"},
            ],
        )
        self.assertEqual(chat.other_uid, "uB")
        self.assertEqual(chat.other_name, "真人对方", f"名字与 uid 指错了人：{chat.other_name!r}")

    def test_duration_days_string_does_not_kill_stats(self):
        """durationDays 写成字符串：归一为整数，不能让整份文件的统计崩掉

        反向验证：把 load_chat 结尾的 _to_int(...) 改回直接采信文件值，本条变红 ——
        "31" 会一路带进 calc_overview 的除法，抛 TypeError，症状只是"上传成功却被
        踢回首页"，而文件本身是完全合法的。
        """
        msgs = [self.msg("u_self", "我", i) for i in range(6)]
        msgs += [self.msg("u_other", "对方", 100 + i) for i in range(6)]
        chat = self.load(
            msgs,
            stats_extra={"timeRange": {"start": "2023-11-01", "end": "2023-12-01", "durationDays": "31"}},
        )
        self.assertEqual(chat.duration_days, 31)
        from analyzer.local_stats import calc_overview

        ov = calc_overview(chat)
        self.assertEqual(ov["total_days"], 31)
        self.assertGreater(ov["avg_daily"], 0)

    def test_negative_duration_days_falls_back_to_computed(self):
        """负数跨度夹成 0 → 按首末消息自己算，日均不该是负数"""
        msgs = [self.msg("u_self", "我", i) for i in range(6)]
        msgs += [self.msg("u_other", "对方", 100 + i) for i in range(6)]
        chat = self.load(msgs, stats_extra={"timeRange": {"durationDays": -5}})
        self.assertEqual(chat.duration_days, 0)
        from analyzer.local_stats import calc_overview

        ov = calc_overview(chat)
        self.assertGreaterEqual(ov["avg_daily"], 0, "日均消息数不该是负的")
        self.assertEqual(ov["days_basis"], "computed")

    def test_null_shapes_do_not_raise(self):
        """六种实测出现过的坏形状：跳过坏条目，而不是把整份导出判死

        反向验证：把这些 `or {}` / isinstance 检查删掉，本条会在前四种形状上抛
        AttributeError/TypeError —— 而上传接口的症状是回一句
        "解析失败: 'NoneType' object has no attribute 'get'"，用户既不知道哪里坏、
        也不知道能不能修（同函数对 content 已有 isinstance 阶梯，这两侧不一致才是要修的）。
        """
        base = [self.msg("u_self", "我", i) for i in range(4)] + [
            self.msg("u_other", "对方", 100 + i) for i in range(4)
        ]
        cases = {
            "sender 为 null": ("msgs", lambda ms: ms.__setitem__(1, {**ms[1], "sender": None})),
            "elements 为 null": (
                "msgs",
                lambda ms: ms.__setitem__(1, {**ms[1], "content": {"text": "", "elements": None}}),
            ),
            "单个元素为 null": (
                "msgs",
                lambda ms: ms.__setitem__(1, {**ms[1], "content": {"elements": [None]}}),
            ),
            "整条消息为 null": ("msgs", lambda ms: ms.insert(0, None)),
            "text 元素内容为 null": (
                "msgs",
                lambda ms: ms.__setitem__(
                    1, {**ms[1], "content": {"elements": [{"type": "text", "data": {"text": None}}]}}
                ),
            ),
            "statistics 为 null": ("stats", lambda d: d.__setitem__("statistics", None)),
            "timeRange 为 null": ("stats", lambda d: d.__setitem__("timeRange", None)),
            "senders 为 null": ("stats", lambda d: d.__setitem__("senders", None)),
        }
        from parser.qq_parser import load_chat

        for label, (kind, mutate) in cases.items():
            msgs = json.loads(json.dumps(base))
            stats = {"totalMessages": len(msgs), "senders": [{"uid": "u_self", "name": "我"}]}
            if kind == "msgs":
                mutate(msgs)
            else:
                mutate(stats)
            payload = {
                "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
                "statistics": stats,
                "messages": msgs,
            }
            p = Path(self.dir) / "shape.json"
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.subTest(case=label):
                chat = load_chat(str(p))  # 不许抛
                self.assertTrue(chat.messages or chat.dropped_messages, "至少要如实报告处理了什么")

    def test_numeric_sender_uid_parses_instead_of_crashing(self):
        """数字型 uid（异版导出器/手工编辑写成 JSON number）不许崩，也不许静默错归属

        这是本轮新引入的回归：占位判定复用进 load_chat 主路径后，
        is_placeholder_sender 的 .strip() 会抛 'int' object has no attribute 'strip'，
        上传收到 400 + 一句 Python 内部错误——正是本轮宣称要消灭的症状（旧代码
        只对 uid 做相等比较，反而不崩）。selfUid 与发言者 uid 两处归一都要钉：
        只归一发言者、不归一 selfUid 的话，"我"的每一条都会因 int!=str 被判给
        对方——不崩，但归属全错，比崩隐蔽。
        反向验证：删掉 qq_parser 的 sender_uid str()、selfUid str() 或
        group_identity.is_placeholder_sender 的入参归一，各自都会让本条变红。
        """
        chat = self.load(
            [self.msg(111, "我", 0), self.msg(12345, "阿明", 1)],  # 数字 uid 进消息
            senders=[{"uid": 111, "name": "我"}, {"uid": 12345, "name": "阿明"}],  # 也进汇总
            chat_info={"name": "阿明", "selfUid": 111, "selfName": "我"},  # 也进 chatInfo
        )
        self.assertEqual(chat.self_uid, "111", "chatInfo.selfUid 同样要归一")
        self.assertEqual(chat.other_uid, "12345", "数字 uid 归一成字符串后照常认定「对方」")
        self.assertEqual(chat.other_name, "阿明")
        self.assertEqual(len(chat.messages), 2)
        self.assertEqual(chat.messages[0].sender_uid, "111", "消息侧的 self 归属不许因类型错判")

    def test_messages_not_a_list_raises_instead_of_swallowing(self):
        """messages 是 dict/str：整份文件会被静默吞掉（逐 key 迭代全计入 dropped），
        症状是"共 N 条消息、0 条可分析"而 total_count 照抄 statistics——比崩难发现。
        现在必须 ValueError 报错，且文案是人话而不是 Python 内部错误。

        反向验证：删掉 isinstance(raw.get("messages"), list) 检查，本条变红（不再抛）。
        """
        payload = {
            "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
            "statistics": {"totalMessages": 5, "senders": []},
            "messages": {"0": {"id": "x"}},
        }
        p = Path(self.dir) / "badmessages.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        from parser.qq_parser import load_chat

        with self.assertRaises(ValueError) as cm:
            load_chat(str(p))
        self.assertIn("messages 不是一个数组", str(cm.exception))

    def test_statistics_wrong_shape_and_null_senders_do_not_raise(self):
        """statistics 写成数组、senders 里混 null：都是"键在、形状不对"，不许崩

        反向验证：删掉 statistics 的 isinstance 守卫或"找回自己"循环的 isinstance，
        两段各自变红（前者 'list' object has no attribute 'get'，后者 None.get）。
        """
        p = Path(self.dir) / "badstats.json"
        p.write_text(
            json.dumps(
                {
                    "chatInfo": {"name": "对方", "selfUid": "u_self", "selfName": "我"},
                    "statistics": [{"uid": "u_self"}],  # 非 dict
                    "messages": [self.msg("u_self", "我", 0)],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        from parser.qq_parser import load_chat

        chat = load_chat(str(p))
        self.assertEqual(len(chat.messages), 1)

        # 缺 selfUid、senders 里混着 null：靠"按名字找回自己"的循环，守卫必须齐
        p2 = Path(self.dir) / "nullsenders.json"
        p2.write_text(
            json.dumps(
                {
                    "chatInfo": {"name": "对方", "selfName": "我"},
                    "statistics": {"senders": [None, {"uid": "u_self", "name": "我"}]},
                    "messages": [self.msg("u_self", "我", 0)],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        chat2 = load_chat(str(p2))
        self.assertEqual(chat2.self_uid, "u_self")

    def test_infinity_does_not_raise(self):
        """JSON 的 Infinity 字面量（json.loads 默认接受）：_to_int/_parse_timestamp
        只捕 TypeError/ValueError，int(float(inf)) 抛的是 OverflowError，没接住就崩。

        反向验证：把两处 except 元组里的 OverflowError 删掉，本条变红。
        """
        chat = self.load(
            [
                self.msg("u_self", "我", 0),
                self.msg("u_other", "对方", 1, timestamp=float("inf"), time=""),  # 数值不可用→丢弃
            ],
            stats_extra={"timeRange": {"durationDays": float("inf"), "start": "", "end": ""}},
        )
        self.assertEqual(chat.duration_days, 0, "Infinity 天数按 0 处理（calc 会改按首末消息算跨度）")
        self.assertEqual(len(chat.messages), 1)
        self.assertEqual(chat.dropped_messages, 1)

    def test_bad_message_counted_as_dropped(self):
        """整条不是对象：计入 dropped_messages，日志会如实报「跳过 N 条」"""
        msgs = (
            [self.msg("u_self", "我", i) for i in range(4)]
            + [None, "字符串"]
            + [self.msg("u_other", "对方", 100 + i) for i in range(4)]
        )
        chat = self.load(msgs)
        self.assertEqual(chat.dropped_messages, 2)
        self.assertEqual(len(chat.messages), 8)


class TestStatsConventions(unittest.TestCase):
    """断言 B3/B4/B5：媒体分家、中位数口径唯一、同名成员与自回复"""

    @staticmethod
    def _chat(items):
        """items: [(uid, name, text, kwargs...)] 直接构造 Message，绕开解析器"""
        from parser.qq_parser import ChatData, Message

        msgs = []
        for i, (uid, name, text, kw) in enumerate(items):
            m = Message(
                f"m{i}",
                BASE_MS + i * 60000,
                "2023-11-14 22:13:20",
                name,
                uid,
                text,
                text,
                "text",
                bool(kw.pop("has_image", False)),
                False,
                **kw,
            )
            msgs.append(m)
        return ChatData("c", "我", "对方", "u_self", "u_other", messages=msgs, total_count=len(msgs))

    def test_image_and_file_in_one_message_not_double_counted(self):
        """一条消息同时有图片与文件：字节必须分家，不能两边各记一份合计

        反向验证：把 calc_overview 里的 img_bytes/other_bytes 分家改回
        `image_bytes += m.media_bytes` / `other_media_bytes += m.media_bytes`，本条变红
        （仪表盘并排显示"图片 6144 · 文件/视频 6144"，而真实发送量只有 6144）。
        """
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [
                (
                    "u_self",
                    "我",
                    "看图说话",
                    {
                        "has_image": True,
                        "media_bytes": 2048 + 4096,
                        "media_kind": "file",
                        "image_bytes": 2048,
                        "image_count": 1,
                        "image_ids": ["IMG"],
                        "media_id": "IMG",
                    },
                ),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["image_bytes"], 2048, "图片体积只该算图片那一份")
        self.assertEqual(ov["other_media_bytes"], 4096, "非图片媒体只算剩下的那一份")
        self.assertEqual(ov["image_bytes"] + ov["other_media_bytes"], 6144, "合计不许重复计")

    def test_images_counted_per_picture_not_per_message(self):
        """一条消息发 3 张图 → "图片总数"该是 3（标签写的是张，此前按消息条数算）"""
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [
                (
                    "u_self",
                    "我",
                    "三连",
                    {
                        "has_image": True,
                        "media_bytes": 3000,
                        "image_bytes": 3000,
                        "image_count": 3,
                        "image_ids": ["A", "B", "C"],
                        "media_id": "A",
                    },
                ),
            ]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 3)
        self.assertEqual(ov["unique_images"], 3, "去重也按张，一条消息里的三张不该塌成一张")

    def test_legacy_message_without_image_split_still_counts(self):
        """手工构造的旧式 Message（没有分家字段）退回旧口径，不许把图片算成 0"""
        from analyzer.local_stats import calc_overview

        chat = self._chat(
            [("u_other", "对方", "图", {"has_image": True, "media_bytes": 100, "media_id": "x"})]
        )
        ov = calc_overview(chat)
        self.assertEqual(ov["total_images"], 1)
        self.assertEqual(ov["image_bytes"], 100)
        self.assertEqual(ov["other_media_bytes"], 0)

    def test_p50_survives_chronological_gaps(self):
        """中位数必须先排序：arr 是按对话时间序 append 的，"取中间那条"不是中位数

        反向验证：把 _stats 里的 _median(sorted(arr)) 改回 _median(arr)，本条变红——
        间隔按时间序 [10,100,20,30]，真中位数 25.0，未排序会报 (100+20)/2=60.0，
        等于把最长的一段对话当成"典型回复速度"，而 p50 恰恰是关系页的主展示指标。
        （为什么非要 n≥3 且乱序：n=2 时取中间两数平均与顺序无关；离群值若排在
        后半段，中间位置照样落进普通值——本轮两条 p50 用例都栽在这个盲区里。）
        """
        from analyzer.local_stats import calc_response_time
        from parser.qq_parser import ChatData, Message

        def m(i, uid, ts_s):
            return Message(
                f"m{i}",
                BASE_MS + ts_s * 1000,
                "2023-11-14 22:13:20",
                "我" if uid == "u_self" else "对方",
                uid,
                "话",
                "话",
                "text",
                False,
                False,
            )

        # 时间序上交替发言；self 侧回复间隔依次是 10、100、20、30 秒（乱序）
        timeline = [
            ("u_other", 0),
            ("u_self", 10),
            ("u_other", 15),
            ("u_self", 115),
            ("u_other", 120),
            ("u_self", 140),
            ("u_other", 145),
            ("u_self", 175),
        ]
        chat = ChatData(
            "c",
            "我",
            "对方",
            "u_self",
            "u_other",
            messages=[m(i, u, t) for i, (u, t) in enumerate(timeline)],
            total_count=len(timeline),
        )
        rt = calc_response_time(chat)
        self.assertEqual(rt["self"]["count"], 4, "self 侧应配对出 4 个间隔")
        self.assertEqual(rt["self"]["p50"], 25.0, f"[10,100,20,30] 的中位数是 25.0，实际 {rt['self']['p50']}")
        self.assertEqual(rt["self"]["avg"], 40.0)

    def test_median_of_even_sample_is_not_the_upper_middle(self):
        """句长中位数：偶数样本必须取中间两个的平均，而不是上中位

        反向验证：把 _median 改回 s[n//2]，本条变红 —— 两句 [1,9] 会被报成
        "中位句长 9"（真值 5.0），等于把最长的那句当成中位数。
        """
        from analyzer.local_stats import calc_message_length_stats

        chat = self._chat(
            [
                ("u_self", "我", "x", {}),
                ("u_self", "我", "y" * 9, {}),
                ("u_other", "对方", "a", {}),
                ("u_other", "对方", "b" * 100, {}),
            ]
        )
        st = calc_message_length_stats(chat)
        self.assertEqual(st["self"]["median"], 5.0, f"两句的中位数应是 5.0，实际 {st['self']['median']}")
        self.assertEqual(st["other"]["median"], 50.5)

    def test_p50_and_median_share_one_implementation(self):
        """同一个模块里"中位数"只许有一种算法（口径唯一是本地统计的立身之本）

        反向验证：让 p50 重新走 `_percentile(arr, 0.5)`（那是 int(round(0.5*(n-1)))，
        偶数样本取**下**中位），第二组断言立刻与第一组矛盾。
        """
        from analyzer.local_stats import _median

        self.assertEqual(_median([1, 9]), 5.0)
        self.assertEqual(_median([1, 2, 9]), 2.0)
        self.assertEqual(_median([]), 0.0)
        self.assertEqual(_median([7]), 7.0)

        # p50 走的就是它：造两个间隔 1s / 99s 的配对，仪表盘该报 50.0 而不是 1.0
        from analyzer.local_stats import calc_response_time

        msgs = [
            Message("a", BASE_MS, "t", "我", "u_self", "在", "在", "text", False, False),
            Message("b", BASE_MS + 1000, "t", "对方", "u_other", "嗯", "嗯", "text", False, False),
            Message("c", BASE_MS + 61000, "t", "我", "u_self", "在", "在", "text", False, False),
            Message("d", BASE_MS + 160000, "t", "对方", "u_other", "嗯", "嗯", "text", False, False),
        ]
        from parser.qq_parser import ChatData

        got = calc_response_time(
            ChatData("c", "我", "对方", "u_self", "u_other", messages=msgs, total_count=4)
        )
        self.assertEqual(got["other"]["p50"], 50.0, f"两次间隔的中位数应是 50s，实际 {got['other']['p50']}")

    def test_same_second_reply_is_not_reported_as_zero_speed(self):
        """秒级时间戳下同秒跨人配对不该算出"平均回复 0 秒"

        反向验证：把 calc_response_time 的 `gap <= 0` 判断去掉，本条变红
        （self: avg 0.0 / p50 0.0，界面显示的是一件物理上不可能的事）。
        """
        from analyzer.local_stats import calc_response_time

        chat = self._chat(
            [
                ("u_self", "我", "甲", {}),
                ("u_other", "对方", "乙", {}),
            ]
        )
        # 人为制造同毫秒的跨人配对
        chat.messages[0].timestamp = BASE_MS
        chat.messages[1].timestamp = BASE_MS
        got = calc_response_time(chat)
        self.assertEqual(got["other"]["count"], 0, "0 间隔不该被当成一次响应")
        self.assertEqual(got["other"]["avg"], 0.0)

    def test_duplicate_names_with_numeric_uids_are_unique(self):
        """真实 QQ 号前 4 位相同也必须消歧（此前 #uid4 会让两个"小明"合成一个人）

        反向验证：把 unique_display_names 的后缀改回 uid[:4]，本条变红。
        """
        from parser.group_identity import Participant, unique_display_names

        parts = [
            Participant(uid="1000000001", name="小明"),
            Participant(uid="1000000042", name="小明"),
        ]
        names = [p.name for p in unique_display_names(parts)]
        self.assertEqual(len(set(names)), 2, f"显示名冲突：{names}")
        self.assertTrue(all(n.startswith("小明#") for n in names), names)

    def test_anonymous_members_are_distinguishable(self):
        """两个匿名成员也不能撞名"""
        from parser.group_identity import Participant, unique_display_names

        parts = [Participant(uid="1000000001", name=""), Participant(uid="1000000042", name="")]
        names = [p.name for p in unique_display_names(parts)]
        self.assertEqual(len(set(names)), 2, f"匿名成员撞名：{names}")

    def test_self_reply_not_doubled_in_undirected_matrix(self):
        """自回复（引用自己发的消息）在无向矩阵对角上不该翻倍

        反向验证：把 _symmetrize 的对角分支改成与 off-diagonal 同样的相加，本条变红
        （3 次自回复报成 6 次，而矩阵与图例、总数从此对不上账）。
        """
        from analyzer.group_stats import calc_interaction_matrix
        from parser.qq_parser import load_chat

        msgs = []
        for i in range(4):
            msgs.append(
                {
                    "id": f"seed{i}",
                    "timestamp": BASE_MS + i * 60000,
                    "time": "2023-11-14 22:13:20",
                    "sender": {"uid": "uA", "name": "甲"},
                    "type": "text",
                    "content": {"text": f"甲原话{i}"},
                }
            )
        for i in range(3):
            msgs.append(
                {
                    "id": f"sr{i}",
                    "timestamp": BASE_MS + (400 + i) * 60000,
                    "time": "2023-11-14 23:00:00",
                    "sender": {"uid": "uA", "name": "甲"},
                    "type": "text",
                    "content": {
                        "text": f"自回复{i}",
                        "elements": [{"type": "reply", "data": {"referencedMessageId": f"seed{i}"}}],
                    },
                }
            )
        for i in range(3):
            msgs.append(
                {
                    "id": f"b{i}",
                    "timestamp": BASE_MS + (800 + i) * 60000,
                    "time": "2023-11-14 23:30:00",
                    "sender": {"uid": "uB", "name": "乙"},
                    "type": "text",
                    "content": {"text": f"乙{i}"},
                }
            )
        payload = {
            "chatInfo": {"name": "测试群", "selfUid": "uA", "selfName": "甲", "type": "group"},
            "statistics": {"totalMessages": len(msgs), "senders": [{"uid": "uA", "name": "甲"}]},
            "messages": msgs,
        }
        tmp = Path(tempfile.mkdtemp(prefix="r6-sr-")) / "selfreply.json"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            chat = load_chat(str(tmp))
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        inter = calc_interaction_matrix(chat)
        idx = [m["uid"] for m in inter["members"]].index("uA")
        self.assertEqual(inter["explicit_directed"][idx][idx], 3)
        self.assertEqual(inter["explicit_undirected"][idx][idx], 3, "无向对角不该翻倍")
        self.assertEqual(inter["self_replies"], 3, "自回复总数要能对账（关系图里没有自环）")


class TestFrontendGuards(unittest.TestCase):
    """断言 C1/C2/C3：前端三处用静态断言守住（与 test_review_fixes 的同类手法一致）"""

    ROOT = Path(__file__).resolve().parent.parent
    CHARTS = ROOT / "web" / "static" / "js" / "group_charts.js"
    EMOTION = ROOT / "web" / "templates" / "group_emotion.html"
    TOPICS = ROOT / "web" / "templates" / "group_topics.html"
    ASSETS = ROOT / "web" / "templates" / "_report_assets.html"
    GROUP_REPORT = ROOT / "web" / "templates" / "group_report.html"

    @staticmethod
    def _code_of(text):
        """只留代码行：注释里会引用旧写法作为反面教材，混进来会把断言变成永真"""
        return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))

    def _heatmap_body(self):
        src = self._code_of(self.CHARTS.read_text(encoding="utf-8"))
        return src.split("function renderInteractionHeatmap")[1].split("\nfunction ")[0]

    def test_heatmap_tooltip_matches_axis_convention(self):
        """tooltip 的「谁先说 / 谁接话」必须与坐标轴、数据三方一致

        服务端是 directed[先说者][接话者]，图里压成 [列, 行, 值] → x=列=接话者、
        y=行=先说者。写反之后卡片标题与两条坐标轴都是对的，只有鼠标气泡说反，
        而用户信的是气泡。
        反向验证：把「说完」那一方从 value[1] 改回 value[0]，本条变红。
        """
        body = self._heatmap_body()
        # 绑定与使用要一起看：气泡里的「说完」必须取自 value[1]（行 = 先说的人）。
        # 只匹配一整句字符串的话，改个变量名就能骗过这条用例。
        self.assertRegex(
            body, r"var\s+row\s*=\s*esc\(names\[p\.value\[1\]\]\)", "row 必须绑定到 value[1]（行）"
        )
        self.assertRegex(
            body, r"var\s+col\s*=\s*esc\(names\[p\.value\[0\]\]\)", "col 必须绑定到 value[0]（列）"
        )
        self.assertRegex(body, r"row\s*\+\s*' 说完", "「说完」的一方必须是 row")
        self.assertNotIn("col + ' 说完", body, "把 col 说成「先开口的人」就是本轮修的那个反义 bug")
        self.assertIn("接了", body, "接话次数仍要在气泡里报出来")

    def test_mention_matrix_uses_its_own_wording(self):
        """@点名矩阵复用同一张热力图时，文案与行列语义都要换一套

        反向验证：删掉 mention 分支（回到只会说「说完/接了」），本条变红。
        """
        body = self._heatmap_body()
        self.assertIn("mention", body, "函数需要区分接话矩阵与 @ 矩阵")
        self.assertIn("@了", body, "@矩阵要说 @，不能沿用「说完/接了」")
        self.assertIn("mode: 'mention'", self.TOPICS.read_text(encoding="utf-8"), "调用点要显式声明模式")

    def test_emotion_page_guards_missing_container(self):
        """未配 Key 时 #sec-emotion 不渲染，但回调仍会拿到旧结果 —— 必须判空

        反向验证：去掉模板里的 `if (box)`、恢复直接把 getElementById 传进渲染函数，
        本条变红。那不只丢 AI 小节：同一个回调里排在后面的 mountChart('emotionTrend')
        会被一个 TypeError 一起带走，本地统计的走势图同样空掉。
        """
        src = self._code_of(self.EMOTION.read_text(encoding="utf-8"))
        self.assertNotIn(
            "renderGroupEmotion(document.getElementById(",
            src,
            "容器判空前不要直接把 getElementById 传进渲染函数",
        )
        self.assertIn("if (box)", src, "AI 小节要判空后再渲染")
        self.assertLess(
            src.index("if (box)"),
            src.index("mountChart('emotionTrend')"),
            "判空要在走势图之前，否则一个 null 把本地图也带走",
        )

    def test_report_snapshot_freezes_charts_before_stripping_scripts(self):
        """导出快照必须先把 canvas 冻结成图片，再剥脚本

        canvas 的像素不参与 outerHTML 序列化，而画图的代码又被剥掉了 —— 顺序反了
        或根本没冻结，分享出去的群报告就是四个全白的图表区域。
        反向验证：删掉 `freezeCharts(clone)` 这一行，本条变红。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn("function freezeCharts", code, "导出快照要有图表冻结步骤")
        self.assertIn("getInstanceByDom", code, "要用活实例取快照")
        self.assertIn("getDataURL", code)
        # 断言必须落在**调用点**上。第一版写的是 index("freezeCharts(clone)")，
        # 而这串字符同样出现在 `function freezeCharts(clone) {` 那一行里 —— 删掉调用
        # 也照样"绿"，是一条没有鉴别力的假守卫（反向验证脚本把它抓出来了）。
        self.assertRegex(code, r"\n\s*freezeCharts\(clone\);", "buildReportSnapshot 里要真的调用冻结")
        call = code.index("freezeCharts(clone);")
        self.assertGreater(call, code.index("cloneNode(true)"), "冻结要作用在克隆体上")
        self.assertLess(call, code.index("/static/js/"), "必须先冻结再剥脚本（脚本没了就画不出来了）")

    def test_report_snapshot_drops_live_render_scripts_but_keeps_theme(self):
        """分享版剥掉「活渲染块」与导出机制，但必须留住首屏的主题初始化

        反向验证：把内联脚本整体保留（旧行为只删 CSRF 那一块），收件人打开导出件
        会在第一行 ReferenceError；而误删主题初始化会让分享件打开时先闪一下浅色。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn("window\\.CSRF_TOKEN", code, "含 token 的脚本仍要按内容精确剔除")
        self.assertIn("data-theme", code, "主题初始化必须留在分享版里")
        self.assertIn("{ s.remove(); return; }", code, "非主题的脚本要整体摘掉，而不是留一份跑不动的")

    def test_report_snapshot_strips_csrf_input_and_uses_surface_bg(self):
        """剥了脚本不许漏剥表单里的 hidden csrf_token；冻结底色取卡片底而不是页面底

        反向验证：①删掉摘 input[name=csrf_token]/form.nav-logout 的那一行，第一段变红
        （登出表单在 base.html 里带 hidden token，与 window.CSRF_TOKEN 同值，README
        承诺"导出剥掉 CSRF token"就落空）；②把 freezeCharts 的取色改回 --bs-body-bg，
        第二段变红（图表都在 .card 里，暗色主题下按页面底填色，每张导出图自带一块
        比卡片更深的矩形）。
        """
        code = self._code_of(self.ASSETS.read_text(encoding="utf-8"))
        self.assertIn('input[name="csrf_token"]', code, "hidden csrf_token 要整体摘掉")
        self.assertIn("form.nav-logout", code, "登出表单连壳一起摘，别只留个空 form")
        self.assertIn("--surface", code, "冻结底色取卡片底")
        self.assertNotIn("--bs-body-bg", code, "页面底不是图表的容身色")


if __name__ == "__main__":
    unittest.main()
