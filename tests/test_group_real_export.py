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
"""真实导出文件的端到端验收（有文件才跑，没有就跳过）

为什么单独一个文件：合成 fixture 能覆盖边角，但覆盖不了"真实导出长什么样"——字段命名、
占位 sender、被删除的引用、同名成员、跨月的消息分布，这些只有真文件才给得出来。
本文件把真文件变成一条可重复执行的验收：解析 → 判定 → 统计对账 → 八个页面 → 报告导出 →
缓存回读，任何一步出错都会红。

文件从哪来（按顺序找）：
1. 环境变量 `QQCHAT_TEST_EXPORT` 指向的 JSON；
2. 常见导出目录里最新的 `group_*.json`（默认查 `~/Documents/QQChatExporter/exports/`）。
都找不到就 `skipTest`——CI 与别人的机器上不会因为它而失败。

**不联网**：AI 维度用假响应替换（`analyzer.deepseek_client._call_api`），
本文件只验证"真实数据能否走通整条链路"，模型答得好不好要靠人工看结果。
"""

import io
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


# 测试隔离 + 网络护栏：数据目录指向本次进程独占的临时目录，且未配置真实 API Key 时
# 禁止一切真实 LLM 调用。两者都必须在 import 项目模块（config / analyzer.*）之前完成，
# 否则 config 会把数据目录读成真实目录。实现与理由见 tests/_bootstrap.py。
from _bootstrap import api_configured_patcher  # noqa: E402
from _bootstrap import bootstrap  # noqa: E402
from _stats import ensure_stats  # noqa: E402
from result_fixtures import (  # noqa: E402
    group_dynamics as valid_group_dynamics,
    group_emotion as valid_group_emotion,
    group_topics as valid_group_topics,
    profile as valid_profile,
)

bootstrap()
os.environ.setdefault("QQCHAT_MONTH_CACHE", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import parser.qq_parser as qp  # noqa: E402
from parser.group_identity import is_placeholder_sender  # noqa: E402
from analyzer import deepseek_client as dc  # noqa: E402
from analyzer import group_client as gc  # noqa: E402
from analyzer import group_stats as gs  # noqa: E402
from webapp import store  # noqa: E402

EXPORT_ENV = "QQCHAT_TEST_EXPORT"
DEFAULT_DIRS = (
    Path.home() / "Documents" / "QQChatExporter" / "exports",
    Path.home() / "QQChatExporter" / "exports",
)


def find_export() -> Path | None:
    """找一份真实导出：优先环境变量，其次常见导出目录里最新的 group_*.json"""
    explicit = (os.getenv(EXPORT_ENV) or "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    candidates: list[Path] = []
    for directory in DEFAULT_DIRS:
        try:
            candidates += [p for p in directory.glob("*.json") if p.is_file()]
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


EXPORT = find_export()


def _fake_call(system_prompt, user_content, max_tokens=2048, retry=2, tpm_wait=None, tag="unknown", dim=None):
    """假响应：形状与真实契约一致（字段齐全），用来驱动页面与渲染路径"""
    if tag == "member_profiles":
        return valid_profile(group=True)
    if tag == "group_topics":
        return valid_group_topics()
    if tag == "group_emotion":
        return valid_group_emotion()
    return valid_group_dynamics()


@unittest.skipIf(EXPORT is None, f"没有真实导出文件（可设 {EXPORT_ENV} 指定路径）")
class TestRealExport(unittest.TestCase):
    """一份真实导出的完整验收"""

    @classmethod
    def setUpClass(cls):
        import app as appmod

        cls.appmod = appmod
        cls.client = appmod.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as sess:
            cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": sess["csrf_token"]}
        cls.guards = [
            mock.patch.object(qp, "GROUP_TRACK_READY", True),
            mock.patch.object(dc, "_call_api", side_effect=_fake_call),
            mock.patch.object(gc, "_call_api", side_effect=_fake_call),
            # 成本预估区块只在"已配置 API Key"时渲染：本机 .env 有真 Key、CI 没有。
            # 不显式打这个补丁，本类用例就只在本机绿（原因见 tests/_bootstrap.py）。
            api_configured_patcher(),
        ]
        for guard in cls.guards:
            guard.start()
        with open(EXPORT, "rb") as f:
            payload = f.read()
        cls.source_digest = hashlib.sha256(payload).digest()
        resp = cls.client.post(
            "/upload", data={"file": (io.BytesIO(payload), "real.json")}, headers=cls.headers
        )
        assert resp.status_code == 302, f"真实文件上传失败：{resp.status_code}"
        with cls.client.session_transaction() as sess:
            cls.chat_hash = sess.get("chat_hash")
        store.wait_for_stats(cls.chat_hash)
        # 同步保证统计已落盘：等不到后台线程（缓存被别的用例清掉等）就当场补算，
        # 否则整类用例会一起因为"没有统计数据"而红。详见 tests/_stats.py。
        cls.stats = ensure_stats(sess["filepath"], cls.chat_hash)
        cls.chat = store._load_chat_cached(sess["filepath"])
        cls.payload = json.loads(payload.decode("utf-8"))

    @classmethod
    def tearDownClass(cls):
        for guard in cls.guards:
            guard.stop()
        store._purge_chat_caches(cls.chat_hash)
        assert hashlib.sha256(EXPORT.read_bytes()).digest() == cls.source_digest, "original export changed"

    # ---------------------------------------------------------------- 解析与判定

    def test_parsed_as_group(self):
        self.assertTrue(self.chat.is_group_chat)
        self.assertEqual(self.chat.mode, "group")
        self.assertEqual(self.chat.other_uid, "")
        self.assertEqual(self.chat.dropped_messages, 0, "真实文件不该有被丢弃的消息")
        self.assertGreater(len(self.chat.participants()), 2)

    def test_no_placeholder_leaked_into_members(self):
        for p in self.chat.participants():
            self.assertFalse(
                is_placeholder_sender(p.uid, p.raw_name), f"占位 sender 混进了成员列表：{p.raw_name}"
            )

    def test_member_names_are_unique(self):
        names = [p.name for p in self.chat.participants()]
        self.assertEqual(len(names), len(set(names)), "显示名必须唯一（同名成员要加 #uid 后缀）")

    # ---------------------------------------------------------------- 统计与对账

    def test_stats_cached_in_group_mode(self):
        self.assertIsNotNone(self.stats, "群聊统计没有落盘")
        self.assertEqual(self.stats["mode"], store.STATS_MODE_GROUP)
        self.assertIsNone(store._load_stats(self.chat_hash), "群聊结果不得被私聊口径读走")

    def test_member_count_reconciles_with_total(self):
        activity = self.stats["member_activity"]
        unknown = sum(1 for m in self.chat.statistical() if gs.is_unknown_message(m))
        self.assertEqual(sum(a["msg_count"] for a in activity) + unknown, len(self.chat.statistical()))

    def test_reply_accounting_is_closed(self):
        mi = self.stats["interaction"]
        self.assertEqual(mi["reply_total"], mi["reply_located"] + mi["reply_no_target"])
        self.assertEqual(mi["reply_located"], mi["reply_resolved"] + mi["reply_unresolved"])
        self.assertEqual(
            mi["mention_total"],
            sum(sum(row) for row in mi["mention_directed"]) + mi["mention_unknown"] + mi["mention_outside"],
        )

    def test_explicit_signals_present_in_real_data(self):
        """真实导出里有精确回复与 @ 点名：这是本项目相对旧版最重要的信号升级"""
        mi = self.stats["interaction"]
        self.assertGreater(mi["reply_located"], 0, "没解析到精确回复，说明 reply 元素处理有问题")
        self.assertGreater(mi["mention_total"], 0, "没解析到 @ 点名")

    def test_matrix_is_consistent(self):
        mi = self.stats["interaction"]
        n = len(mi["members"])
        self.assertEqual(len(mi["directed"]), n)
        for i in range(n):
            self.assertEqual(len(mi["directed"][i]), n)
            self.assertEqual(mi["directed"][i][i], 0, "对角线必须为 0（自己不接自己的话）")
            self.assertEqual(mi["undirected"][i][i], 0)

    def test_overview_fields(self):
        ov = self.stats["overview"]
        self.assertTrue(ov["is_group"])
        self.assertEqual(ov["other_name"], "")
        self.assertEqual(ov["member_count"], len(self.chat.participants()))
        self.assertIsNone(ov["exchange_rounds"], "群聊不提供对话轮次")
        self.assertGreaterEqual(ov["peak_concurrent"]["count"], 2)

    # ---------------------------------------------------------------- 页面与报告

    def test_all_pages_render(self):
        for path in (
            "/",
            "/dashboard",
            "/emotion",
            "/relationship",
            "/habits",
            "/topics",
            "/profile",
            "/report",
        ):
            with self.subTest(page=path):
                resp = self.client.get(path)
                body = resp.get_data(as_text=True)
                self.assertEqual(resp.status_code, 200, f"{path} 返回 {resp.status_code}")
                self.assertNotIn("Traceback", body)

    def test_report_keeps_vendor_sri(self):
        import base64
        import hashlib

        body = self.client.get("/report").get_data(as_text=True)
        vendor = Path(self.appmod.__file__).resolve().parent / "web" / "static" / "vendor"
        for path in sorted(p for p in vendor.iterdir() if p.suffix in (".css", ".js")):
            digest = base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()
            with self.subTest(vendor=path.name):
                self.assertIn(f"sha384-{digest}", body)

    def test_cost_plan_matches_months_and_members(self):
        """成本预提示必须等于"月份 × 3 + 成员画像人数"（群聊的计费结构）"""
        import config as configmod

        body = self.client.get("/dashboard").get_data(as_text=True)
        months = len(self.chat.months())
        members = min(len(self.chat.participants()), configmod.GROUP_AI_MAX_MEMBERS)
        self.assertIn(f"本次预计调用 <strong>{months * 3 + members}</strong>", body)

    # ---------------------------------------------------------------- AI 路径（假响应）

    def test_group_dimension_runs_end_to_end(self):
        resp = self.client.post("/api/analyze/group_dynamics", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        job = resp.get_json().get("job")
        import time

        for _ in range(400):
            state = self.client.get(f"/api/analyze-job/{job}").get_json()
            if state.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        self.assertEqual(state.get("status"), "done", state.get("error"))
        self.assertEqual(sorted(state["result"]), sorted(self.chat.months()), "每个月份都要有结果")
        # 结果已落盘，且能按群聊口径读回
        cached = self.client.get("/api/analysis/group_dynamics")
        self.assertEqual(cached.status_code, 200)
        self.assertTrue(cached.get_json().get("cached"))

    def test_private_dimension_is_rejected_for_real_group(self):
        resp = self.client.post("/api/analyze/emotion", headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不适用于当前记录", resp.get_json()["error"])

    def test_p1_index_filters_and_context_match_original_timeline(self):
        from webapp import api
        from webapp.message_index import index_for

        index = index_for(self.chat, api._browse_body, api._match_browse)
        message = next(m for m in self.chat.messages if m.text and m.id and len(index.ids[m.id]) == 1)
        day, month = message.time_str[:10], message.time_str[:7]
        for query in ((message.text[:8].strip(), "", month, "", ""), ("", message.sender_uid, "", day, day)):
            expected = [
                i
                for i, m in enumerate(self.chat.messages)
                if api._match_browse(m, *query, self.chat.self_uid)
            ]
            self.assertTrue(list(index.query(*query)) == expected, "group index filter mismatch")
        response = self.client.get(
            "/api/messages", query_string={"q": message.text[:8], "around": message.id, "context": 1}
        )
        self.assertTrue(response.status_code == 200, "group context request failed")
        position = index.ids[message.id][0]
        expected = [
            api._fmt_browse_message(m, self.chat.self_uid)
            for m in self.chat.messages[max(0, position - 10) : position + 11]
        ]
        self.assertTrue(response.json["messages"] == expected, "group context differs from original timeline")

    def test_p1_sources_validate_current_group_and_input(self):
        from webapp import api
        from webapp.evidence import verify_sources
        from webapp.message_index import index_for
        from test_private_real_export import locatable_source

        index = index_for(self.chat, api._browse_body, api._match_browse)
        source, quote, month = locatable_source(self.chat, index, "group_emotion")
        claim = {
            "quote": "「" + quote + "」",
            "sender_uid": source.sender_uid,
            "date": source.time_str[:10],
            "evidence_ids": [source.id],
        }
        result = {month: {"group_evidence": claim}}
        entries = verify_sources(self.chat, "group_emotion", result, index, api._fmt_browse_message)[
            "entries"
        ]
        self.assertTrue(entries[0]["status"] == "unique", "group source ID was not confirmed")
        claim["evidence_ids"] = ["synthetic-unknown-group-source"]
        entries = verify_sources(self.chat, "group_emotion", result, index, api._fmt_browse_message)[
            "entries"
        ]
        self.assertTrue(entries[0]["status"] == "not_found", "unknown group source ID was accepted")

    def test_p1_member_refresh_with_real_sample_and_mock_model(self):
        import tempfile
        from analyzer import month_cache as mc
        from analyzer.cache_policy import content_cache_policy

        previous = mc._MONTH_CACHE_DIR
        member = gc.select_ai_members(self.chat)[0]
        with tempfile.TemporaryDirectory() as directory:
            mc.configure_month_cache(directory)
            try:
                with (
                    mock.patch.object(gc, "_call_api", side_effect=_fake_call) as call,
                    mock.patch.object(gc, "_vision_digest", return_value=""),
                ):

                    def run():
                        return gc._analyze_member(
                            self.chat,
                            member,
                            "test",
                            "{display_name}\n{dialog}\n{context}",
                            1000,
                            "member_profiles",
                        )

                    self.assertTrue(bool(run()), "group member analysis failed")
                    run()
                    self.assertTrue(call.call_count == 1, "group member unit was not reused")
                    with content_cache_policy(True):
                        self.assertTrue(bool(run()), "group member refresh failed")
                    self.assertTrue(call.call_count == 2, "group member refresh did not bypass cache")
            finally:
                mc.configure_month_cache(previous)

    @unittest.skipUnless(os.getenv("QQCHAT_BROWSER_TESTS") == "1", "opt-in browser suite")
    def test_p1_group_browser_without_persisting_diagnostics(self):
        from webapp import api
        from webapp.message_index import index_for
        from test_private_real_export import TestPrivateRealExport, locatable_source

        self.export_path = EXPORT
        index = index_for(self.chat, api._browse_body, api._match_browse)
        source, self.quote, month = locatable_source(self.chat, index, "group_emotion")
        result = valid_group_emotion()
        result["group_evidence"] = "「" + self.quote + "」"
        fingerprint = store.analysis_cache_fingerprint("group_emotion", self.chat)
        store._write_cache("group_emotion", self.chat_hash, {month: result}, fingerprint=fingerprint)
        TestPrivateRealExport.test_real_private_browser_without_persisting_diagnostics(self)


if __name__ == "__main__":
    unittest.main(verbosity=2)
