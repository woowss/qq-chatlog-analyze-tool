"""Synthetic regressions for refresh, indexed browsing and citation sources."""

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap, api_configured_patcher

bootstrap()

import app  # noqa: E402
from analyzer import deepseek_client as dc, group_client as gc, month_cache as mc  # noqa: E402
from analyzer.cache_policy import content_cache_policy  # noqa: E402
from parser.qq_parser import ChatData, Message, CST, load_chat  # noqa: E402
from webapp import api, jobs, store  # noqa: E402
from webapp.evidence import verify_sources  # noqa: E402
from webapp.message_index import index_for, MAX_QUERY_POSITIONS, MAX_QUERIES  # noqa: E402
from result_fixtures import emotion, habits, profile  # noqa: E402


def message(pos, text=None, day=1, uid=None):
    uid = uid or ("self" if pos % 2 == 0 else "other")
    time = datetime(2025, 1, day, 12, tzinfo=CST) + timedelta(seconds=pos)
    text = text if text is not None else f"合成消息 {pos}"
    return Message(
        str(pos),
        int(time.timestamp() * 1000),
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "我" if uid == "self" else "对方",
        uid,
        text,
        text,
        "text",
        False,
        False,
    )


def chat(messages=None):
    return ChatData(
        "测试对话", "我", "对方", "self", "other", messages or [message(0, "在吗"), message(1, "在的")]
    )


def payload(count=120):
    messages = []
    for pos in range(count):
        msg = message(pos, "在吗" if pos in (0, 4) else "在的" if pos == 1 else None)
        messages.append(
            {
                "id": msg.id,
                "timestamp": msg.timestamp,
                "sender": {"uid": msg.sender_uid, "name": msg.sender_name},
                "type": "text",
                "content": {"text": msg.text, "elements": [{"type": "text", "data": {"text": msg.text}}]},
            }
        )
    return json.dumps(
        {"chatInfo": {"name": "测试对话", "selfUid": "self", "selfName": "我"}, "messages": messages},
        ensure_ascii=False,
    ).encode()


class TestForceRefresh(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        mc.configure_month_cache(temporary.name)
        self.addCleanup(mc.configure_month_cache, "")
        self.chat = chat()

    def test_month_workers_refresh_and_normal_retry_reuses(self):
        def run():
            return dc._analyze_periods(
                {"2025-01": [], "2025-02": []},
                "sys",
                lambda p, _: p,
                1000,
                tag="emotion",
                chat_hash="refresh-months",
            )

        with mock.patch.object(dc, "_call_api", side_effect=lambda *a, **k: emotion()) as call:
            run()
            self.assertEqual(call.call_count, 2)
            run()
            self.assertEqual(call.call_count, 2)
            with content_cache_policy(True):
                run()
            self.assertEqual(call.call_count, 4)
            run()
            self.assertEqual(call.call_count, 4)

    def test_person_refresh_and_failed_or_cancelled_refresh_keeps_old_result(self):
        def run():
            return dc._analyze_person(
                "sys", 10, self.chat.messages, "测试成员", "{dialog}", 1000, tag="habits"
            )

        with mock.patch.object(dc, "_call_api", side_effect=lambda *a, **k: habits()) as call:
            run()
            run()
            self.assertEqual(call.call_count, 1)
            with content_cache_policy(True):
                run()
            self.assertEqual(call.call_count, 2)
        with content_cache_policy(True), mock.patch.object(dc, "_call_api", return_value=None):
            self.assertIsNone(run())
        new = habits()
        new["signature_moment"] = "新版结果"
        with content_cache_policy(True, lambda: True), mock.patch.object(dc, "_call_api", return_value=new):
            run()
        with mock.patch.object(dc, "_call_api", side_effect=AssertionError("must retain old cache")):
            self.assertEqual(run()["signature_moment"], "「在吗」")

    def test_group_member_refresh(self):
        group = load_chat(Path(__file__).parent / "fixtures/group_5p.json")
        member = gc.select_ai_members(group)[0]

        def run():
            return gc._analyze_member(
                group, member, "sys", "{display_name}\n{dialog}\n{context}", 1000, "member_profiles"
            )

        with mock.patch.object(gc, "_call_api", side_effect=lambda *a, **k: profile(group=True)) as call:
            run()
            run()
            self.assertEqual(call.call_count, 1)
            with content_cache_policy(True):
                run()
            self.assertEqual(call.call_count, 2)

    def test_policy_isolated_between_concurrent_tasks(self):
        key = mc._month_key("sys", "isolation")
        mc._write_month_cache(key, {"old": True})

        def read(refresh):
            with content_cache_policy(refresh):
                return mc._read_month_cache(key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            refresh, normal = list(pool.map(read, (True, False)))
        self.assertIsNone(refresh)
        self.assertEqual(normal, {"old": True})
        self.assertEqual(mc._read_month_cache(key), normal)

    def test_single_and_all_jobs_propagate_refresh_and_keep_aggregate_on_failure(self):
        client = app.app.test_client()
        client.get("/")
        with client.session_transaction() as session:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
        client.post("/upload", data={"file": (io.BytesIO(payload(4)), "synthetic.json")}, headers=headers)
        with client.session_transaction() as session:
            filepath, chat_hash, sid = session["filepath"], session["chat_hash"], session.sid
        loaded = store._load_chat_cached(filepath)
        fp = store.analysis_cache_fingerprint("emotion", loaded)

        def runner(chat, **kwargs):
            return dc._analyze_periods(
                {"2025-01": []}, "sys", lambda p, _: "job-month", 1000, tag="emotion", chat_hash=chat_hash
            )

        try:
            with mock.patch.dict(jobs.ANALYZE_FUNCS, {"emotion": runner}):
                with mock.patch.object(dc, "_call_api", side_effect=lambda *a, **k: emotion()) as call:
                    jid, _, _ = jobs._get_or_create_job(sid, "emotion", chat_hash, 0)
                    jobs._run_job(jid, "emotion", filepath, chat_hash)
                    self.assertEqual(call.call_count, 1)
                    jid, _, _ = jobs._get_or_create_job(sid, "emotion", chat_hash, 0, refresh=True)
                    jobs._run_job(jid, "emotion", filepath, chat_hash, True)
                    self.assertEqual(call.call_count, 2)
                    with mock.patch.object(jobs, "dimensions_for_mode", return_value=["emotion"]):
                        jid, _, _ = jobs._get_or_create_job(sid, "all", chat_hash, 1, refresh=True)
                        jobs._run_analyze_all(jid, filepath, chat_hash, True)
                    self.assertEqual(call.call_count, 3)
                before = store._read_cache("emotion", chat_hash, fp)
                with mock.patch.object(dc, "_call_api", return_value=None):
                    jid, _, _ = jobs._get_or_create_job(sid, "emotion", chat_hash, 0, refresh=True)
                    jobs._run_job(jid, "emotion", filepath, chat_hash, True)
                    self.assertEqual(jobs.JOBS[jid]["status"], "error")
                self.assertEqual(store._read_cache("emotion", chat_hash, fp), before)
            configured = api_configured_patcher()
            configured.start()
            try:
                with mock.patch.object(api.threading, "Thread") as thread:
                    client.post("/api/analyze/emotion?refresh=1", headers=headers)
                    self.assertTrue(thread.call_args.kwargs["args"][-1])
            finally:
                configured.stop()
        finally:
            jobs.JOBS.clear()
            store._purge_chat_caches(chat_hash)


class TestRefreshJobDedup(unittest.TestCase):
    """An in-flight cache-reusing job cannot fulfill a forced refresh request."""

    def setUp(self):
        configured = api_configured_patcher()
        configured.start()
        self.addCleanup(configured.stop)

    def requests(self, dimension, is_group):
        client = app.app.test_client()
        client.get("/")
        with client.session_transaction() as session:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
        contents = (Path(__file__).parent / "fixtures/group_5p.json").read_bytes() if is_group else payload(4)
        response = client.post(
            "/upload", data={"file": (io.BytesIO(contents), "synthetic.json")}, headers=headers
        )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            chat_hash, sid = session["chat_hash"], session.sid

        def cleanup():
            with jobs.JOBS_LOCK:
                for jid in [jid for jid, job in jobs.JOBS.items() if job.get("sid") == sid]:
                    jobs.JOBS.pop(jid)
            store._purge_chat_caches(chat_hash)

        self.addCleanup(cleanup)
        endpoint = "/api/analyze-all" if dimension == "all" else f"/api/analyze/{dimension}"

        def post(refresh):
            return client.post(endpoint + ("?refresh=1" if refresh else ""), headers=headers)

        return post

    def check_conflict_and_retry(self, dimension, is_group, existing_refresh):
        post = self.requests(dimension, is_group)
        # Hold the worker before it starts, without depending on thread timing.
        with (
            mock.patch.object(api.threading, "Thread") as thread,
            mock.patch.object(store, "_read_cache", return_value=None),
        ):
            first = post(existing_refresh)
            self.assertEqual(first.status_code, 200)
            jid = first.json["job"]
            before = deepcopy(jobs.JOBS[jid])
            rejected = post(not existing_refresh)
            self.assertEqual(rejected.status_code, 409)
            self.assertIn("刷新模式", rejected.json["error"])
            self.assertNotIn("job", rejected.json)
            self.assertEqual(jobs.JOBS[jid], before)
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(thread.return_value.start.call_count, 1)
            self.assertEqual(before["refresh"], existing_refresh)
            with jobs.JOBS_LOCK:
                jobs.JOBS[jid]["status"] = "done"
            retry = post(not existing_refresh)
            self.assertEqual(retry.status_code, 200)
            self.assertNotEqual(retry.json["job"], jid)
            self.assertEqual(jobs.JOBS[retry.json["job"]]["refresh"], not existing_refresh)
            worker_args = thread.call_args.kwargs["args"]
            self.assertEqual(worker_args[3 if dimension == "all" else 4], not existing_refresh)
            self.assertEqual(thread.call_count, 2)

    def test_dimension_refresh_mode_conflicts_and_can_retry_after_completion(self):
        for dimension, is_group in (("emotion", False), ("member_profiles", True)):
            for existing_refresh in (False, True):
                with self.subTest(dimension=dimension, refresh=existing_refresh):
                    self.check_conflict_and_retry(dimension, is_group, existing_refresh)

    def test_all_refresh_mode_conflicts_and_can_retry_after_completion(self):
        for is_group in (False, True):
            for existing_refresh in (False, True):
                with self.subTest(group=is_group, refresh=existing_refresh):
                    self.check_conflict_and_retry("all", is_group, existing_refresh)

    def test_same_refresh_mode_reuses_dimension_and_all_jobs(self):
        for dimension, is_group in (
            ("emotion", False),
            ("member_profiles", True),
            ("all", False),
            ("all", True),
        ):
            for refresh in (False, True):
                with self.subTest(dimension=dimension, group=is_group, refresh=refresh):
                    post = self.requests(dimension, is_group)
                    with (
                        mock.patch.object(api.threading, "Thread") as thread,
                        mock.patch.object(store, "_read_cache", return_value=None),
                    ):
                        first, repeated = post(refresh), post(refresh)
                        self.assertEqual(first.status_code, 200)
                        self.assertEqual(repeated.status_code, 200)
                        self.assertEqual(first.json["job"], repeated.json["job"])
                        self.assertTrue(repeated.json["reused"])
                        self.assertEqual(jobs.JOBS[first.json["job"]]["refresh"], refresh)
                        self.assertEqual(thread.call_count, 1)
                        self.assertEqual(thread.return_value.start.call_count, 1)


class TestMessageIndex(unittest.TestCase):
    def test_filters_match_old_behavior_including_media_and_paging(self):
        messages = [message(i, day=1 + i % 3) for i in range(30)]
        messages[2].text = ""
        messages[2].media_kind, messages[2].media_label = "file", "TEST.pdf"
        item = chat(sorted(messages, key=lambda m: m.timestamp))
        index = index_for(item, api._browse_body, api._match_browse)
        for query in (
            ("", "", "", "", ""),
            ("test", "", "", "", ""),
            ("", "self", "2025-01", "2025-01-02", "2025-01-03"),
            ("合成", "other", "", "", ""),
            ("", "other", "2025-99", "", ""),
            ("", "missing", "", "", ""),
            ("", "", "", "2025-01-03", "2025-01-01"),
        ):
            with self.subTest(query=query):
                expected = [
                    i for i, m in enumerate(item.messages) if api._match_browse(m, *query, item.self_uid)
                ]
                self.assertEqual(list(index.query(*query)), expected)

    def test_large_chat_date_lookup_and_query_reuse_are_bounded(self):
        item = chat([message(i, day=1 + i // 2000) for i in range(50_000)])
        matcher = mock.Mock(wraps=api._match_browse)
        index = index_for(item, api._browse_body, matcher)
        first = index.query(side="self", dt_from="2025-01-12", dt_to="2025-01-12")
        # Only that day's candidate positions are visited, rather than all 50k messages.
        self.assertLess(matcher.call_count, 3_000)
        calls = matcher.call_count
        self.assertIs(first, index.query(side="self", dt_from="2025-01-12", dt_to="2025-01-12"))
        self.assertEqual(matcher.call_count, calls)
        self.assertEqual(index.ids["25000"], [25000])
        for i in range(30):
            index.query(q=str(i))
        self.assertLessEqual(len(index.queries), MAX_QUERIES)
        self.assertLessEqual(index.cached_positions, MAX_QUERY_POSITIONS)

    def test_file_replacement_and_purge_discard_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(payload(2))
            first = store._load_chat_cached(str(path))
            old_index = index_for(first, api._browse_body, api._match_browse)
            path.write_bytes(payload(4))
            second = store._load_chat_cached(str(path))
            self.assertIsNot(first, second)
            self.assertIsNot(old_index, index_for(second, api._browse_body, api._match_browse))
            store._purge_chat_caches(store._chat_hash(str(path)))
            self.assertFalse(store._CHAT_CACHE)


class TestEvidenceSources(unittest.TestCase):
    def setUp(self):
        self.chat = chat(
            [
                message(0, "共同短句"),
                message(1, "原句唯一"),
                message(2, "共同短句"),
                message(3, "前缀部分原文后缀"),
                message(4, "<script>不执行</script>"),
            ]
        )
        self.index = index_for(self.chat, api._browse_body, api._match_browse)

    def verify(self, value, dimension="emotion"):
        with mock.patch.object(
            dc, "_call_api", side_effect=AssertionError("local checks must not call model")
        ):
            return verify_sources(self.chat, dimension, value, self.index, api._fmt_browse_message)["entries"]

    def test_unique_duplicate_missing_partial_and_insufficient(self):
        result = {
            "2025-01": {
                "self_evidence": "「共同短句」",
                "other_evidence": "「原句唯一」",
                "evidence": ["「编造内容」", "「部分原文」", "数据不足"],
            }
        }
        entries = self.verify(result)
        self.assertEqual(
            [x["status"] for x in entries], ["multiple", "unique", "not_found", "unique", "insufficient"]
        )
        self.assertEqual(entries[0]["candidate_count"], 2)

    def test_wrong_speaker_date_and_unknown_or_inconsistent_id(self):
        values = [
            {"quote": "「原句唯一」", "sender": "我"},
            {"quote": "「原句唯一」", "date": "2025-01-02"},
            {"quote": "「原句唯一」", "evidence_ids": ["another-chat-id"]},
            {"quote": "「原句唯一」", "evidence_ids": ["0"]},
            {"quote": "「原句唯一」", "evidence_ids": ["1"]},
        ]
        entries = self.verify({"2025-01": {"evidence": values}})
        self.assertEqual([x["status"] for x in entries], ["not_found"] * 4 + ["unique"])

    def test_nested_speaker_constraints_cannot_override_analysis_subject(self):
        result = {"2025-01": {"self_evidence": {"sender": "对方", "quote": "原句唯一"}}}
        self.assertEqual(self.verify(result)[0]["status"], "not_found")
        result = {"self": {"name": "对方", "evidence": "原句唯一"}}
        self.assertEqual(self.verify(result, "profile")[0]["status"], "not_found")

    def test_nested_date_cannot_override_analysis_month(self):
        result = {"2025-02": {"other_evidence": {"date": "2025-01-01", "quote": "原句唯一"}}}
        self.assertEqual(self.verify(result)[0]["status"], "not_found")
        result = {"2025-02": {"other_evidence": "2025-01-01「原句唯一」"}}
        self.assertEqual(self.verify(result)[0]["status"], "not_found")
        result = {"2025-01": {"other_evidence": {"date": "2025-01-01", "quote": "原句唯一"}}}
        self.assertEqual(self.verify(result)[0]["status"], "unique")

    def test_topic_name_is_not_a_sender_claim(self):
        result = {"2025-01": {"topics": [{"name": "日常问候", "evidence": "原句唯一"}]}}
        self.assertEqual(self.verify(result, "topics")[0]["status"], "unique")

    def test_nested_quote_keeps_declared_ids_and_rejects_invalid_ids(self):
        for ids in (["wrong-id"], None, 1):
            with self.subTest(ids=ids):
                result = {
                    "2025-01": {"other_evidence": {"evidence_ids": ids, "quote": {"text": "「原句唯一」"}}}
                }
                self.assertEqual(self.verify(result)[0]["status"], "not_found")

                result["2025-01"]["other_evidence"]["quote"]["evidence_ids"] = ["1"]
                self.assertEqual(self.verify(result)[0]["status"], "not_found")

    def test_duplicate_id_is_not_confirmed(self):
        self.chat.messages[2].id = "0"
        self.chat._message_index = None
        self.index = index_for(self.chat, api._browse_body, api._match_browse)
        entry = self.verify({"2025-01": {"self_evidence": {"quote": "共同短句", "evidence_ids": ["0"]}}})[0]
        self.assertEqual(entry["status"], "not_found")

    def test_legacy_missing_or_duplicate_ids_do_not_offer_wrong_context(self):
        for ambiguous_id in ("", "0"):
            with self.subTest(message_id=ambiguous_id):
                self.chat.messages[1].id = ambiguous_id
                self.chat._message_index = None
                self.index = index_for(self.chat, api._browse_body, api._match_browse)
                entry = self.verify({"2025-01": {"other_evidence": "原句唯一"}})[0]
                self.assertEqual(entry["status"], "insufficient")
                self.assertFalse(entry["candidates"])

    def test_chinese_adjacent_date_is_a_source_constraint(self):
        result = {"2025-01": {"other_evidence": "日期2025-01-02原句「原句唯一」"}}
        self.assertEqual(self.verify(result)[0]["status"], "not_found")

    def test_person_sample_checks_identity_and_truncated_quote(self):
        from analyzer.dialog import _message_line

        self.chat = chat([message(i * 2, "原句唯一尾部内容") for i in range(12)])
        for msg in self.chat.messages:
            msg.time_str = self.chat.messages[0].time_str
        self.index = index_for(self.chat, api._browse_body, api._match_browse)
        line = _message_line(self.chat.messages[-1], self.chat.self_name)
        result = {"self": {"evidence": {"quote": "原句唯一", "evidence_ids": ["0"]}}}
        with mock.patch.object(dc, "MAX_DIALOG_CHARS", len(line) + 1):
            self.assertEqual(self.verify(result, "profile")[0]["status"], "not_found")
            result["self"]["evidence"]["evidence_ids"] = [self.chat.messages[-1].id]
            self.assertEqual(self.verify(result, "profile")[0]["status"], "unique")
        with mock.patch.object(dc, "MAX_DIALOG_CHARS", len(line) - len("尾部内容")):
            self.assertEqual(self.verify(result, "profile")[0]["status"], "unique")
            result["self"]["evidence"]["quote"] = "尾部内容"
            self.assertEqual(self.verify(result, "profile")[0]["status"], "not_found")

    def test_id_outside_sample_or_recalled_input_is_not_confirmed(self):
        self.chat.messages[1].recalled = True
        result = {"2025-01": {"other_evidence": {"quote": "原句唯一", "evidence_ids": ["1"]}}}
        self.assertEqual(self.verify(result)[0]["status"], "not_found")
        self.chat.messages[1].recalled = False
        with mock.patch.object(dc, "MAX_DIALOG_CHARS", 1):
            self.assertEqual(self.verify(result)[0]["status"], "not_found")

    def test_profile_quotes_legacy_results_and_month_restriction(self):
        result = {
            "other": {"name": "对方", "personality_analysis": {"strengths": ["表达清楚（原句：'原句唯一'）"]}}
        }
        before = deepcopy(result)
        self.assertEqual(self.verify(result, "profile")[0]["status"], "unique")
        self.assertEqual(result, before)
        self.assertEqual(self.verify({"2025-02": {"other_evidence": "原句唯一"}})[0]["status"], "not_found")

    def test_group_member_and_multiple_quotes_remain_conservative(self):
        group = load_chat(Path(__file__).parent / "fixtures/group_5p.json")
        msg = next(m for m in group.messages if m.text and not m.system and not m.recalled)
        index = index_for(group, api._browse_body, api._match_browse)
        result = {msg.sender_uid: {"evidence": {"quote": msg.text, "evidence_ids": [msg.id]}}}
        entry = verify_sources(group, "member_profiles", result, index, api._fmt_browse_message)["entries"][0]
        self.assertEqual(entry["status"], "unique")
        entry = self.verify({"2025-01": {"other_evidence": "「原句唯一」及「共同短句」"}})[0]
        self.assertEqual(entry["status"], "insufficient")


class TestEvidenceAPI(unittest.TestCase):
    def test_current_session_cache_guards_and_local_only_read(self):
        client = app.app.test_client()
        client.get("/")
        self.assertEqual(client.get("/api/evidence/emotion").status_code, 400)
        with client.session_transaction() as session:
            headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
        client.post("/upload", data={"file": (io.BytesIO(payload(4)), "synthetic.json")}, headers=headers)
        with client.session_transaction() as session:
            filepath, chat_hash = session["filepath"], session["chat_hash"]
        try:
            loaded = store._load_chat_cached(filepath)
            fingerprint = store.analysis_cache_fingerprint("emotion", loaded)
            self.assertEqual(client.get("/api/evidence/emotion").status_code, 404)
            result = {"2025-01": emotion()}
            store._write_cache("emotion", chat_hash, result, fingerprint=fingerprint)
            with mock.patch.object(dc, "_call_api", side_effect=AssertionError("must be local")):
                response = client.get("/api/evidence/emotion")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([entry["status"] for entry in response.json["entries"]], ["unique", "unique"])
            self.assertEqual(store._read_cache("emotion", chat_hash, fingerprint), result)
            self.assertEqual(app.app.test_client().get("/api/evidence/emotion").status_code, 400)
            self.assertEqual(client.get("/api/evidence/group_emotion").status_code, 400)
            self.assertEqual(client.get("/api/evidence/unknown").status_code, 400)
            # Replacing the session's chat must not expose the prior citations.
            client.post(
                "/upload", data={"file": (io.BytesIO(payload(8)), "replacement.json")}, headers=headers
            )
            self.assertEqual(client.get("/api/evidence/emotion").status_code, 404)
            with client.session_transaction() as session:
                store._purge_chat_caches(session["chat_hash"])
        finally:
            store._purge_chat_caches(chat_hash)


if __name__ == "__main__":
    unittest.main()
