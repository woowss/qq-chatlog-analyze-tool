"""Explicit opt-in acceptance with a private export in disposable runtime data.

Set QQCHAT_TEST_PRIVATE_EXPORT to a local file. Model responses are mocked and
the normal network guard remains enabled. No browser screenshots/traces are
created; logs and assertion messages do not include source data or statistics.
"""

import logging
import os
import re
import threading
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap, api_configured_patcher

bootstrap()

EXPORT = Path(os.environ["QQCHAT_TEST_PRIVATE_EXPORT"]) if os.getenv("QQCHAT_TEST_PRIVATE_EXPORT") else None


def locatable_source(chat, index, dimension):
    """Select an actual text sample without exposing it in test diagnostics."""
    from webapp.evidence import _input_lines

    for month in sorted({m.time_str[:7] for m in chat.statistical()}):
        allowed = _input_lines(chat, dimension, month)
        for message in chat.messages:
            if not message.id or len(index.ids[message.id]) != 1:
                continue
            fragments = re.split(r"""[「」『』“”"'\r\n]""", message.text)
            quote = next((part.strip()[:24] for part in fragments if part.strip()), "")
            if quote and quote in allowed.get(id(message), ""):
                return message, quote, month
    raise AssertionError("export has no locatable text source")


@unittest.skipUnless(EXPORT and EXPORT.is_file(), "private export not explicitly selected")
class TestPrivateRealExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        from analyzer import deepseek_client as dc
        from webapp import api, store
        from webapp.message_index import index_for
        from _stats import ensure_stats
        from result_fixtures import for_dimension

        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        cls.addClassCleanup(logging.disable, previous_logging)
        cls.original_digest = sha256(EXPORT.read_bytes()).digest()
        cls.export_path = EXPORT
        cls.client = app.app.test_client()
        cls.client.get("/")
        with cls.client.session_transaction() as session:
            cls.headers = {"Origin": "http://localhost:5000", "X-CSRF-Token": session["csrf_token"]}
        with EXPORT.open("rb") as stream:
            response = cls.client.post(
                "/upload", data={"file": (stream, "local-private.json")}, headers=cls.headers
            )
        if response.status_code != 302:
            raise AssertionError("private upload failed")
        with cls.client.session_transaction() as session:
            cls.filepath, cls.chat_hash = session["filepath"], session["chat_hash"]
        cls.addClassCleanup(store._purge_chat_caches, cls.chat_hash)
        cls.chat = store._load_chat_cached(cls.filepath)
        if cls.chat.mode != "private":
            raise unittest.SkipTest("selected export is not a private chat")
        cls.stats = ensure_stats(cls.filepath, cls.chat_hash)
        cls.index = index_for(cls.chat, api._browse_body, api._match_browse)
        cls.source, cls.quote, cls.month = locatable_source(cls.chat, cls.index, "emotion")

        cls.dimension_result = for_dimension("emotion")
        cls.dimension_result["self_evidence"] = "数据不足"
        cls.dimension_result["other_evidence"] = "数据不足"
        field = "self_evidence" if cls.source.sender_uid == cls.chat.self_uid else "other_evidence"
        cls.dimension_result[field] = "「" + cls.quote + "」"
        cls.cache_result = {cls.month: cls.dimension_result}
        cls.fingerprint = store.analysis_cache_fingerprint("emotion", cls.chat)

        def fake_call(*args, **kwargs):
            return for_dimension(kwargs["tag"])

        cls.model_calls = mock.Mock(side_effect=fake_call)
        for patcher in (
            api_configured_patcher(),
            mock.patch.object(dc, "_call_api", cls.model_calls),
            mock.patch.object(dc, "_vision_digest", return_value=""),
        ):
            patcher.start()
            cls.addClassCleanup(patcher.stop)

    def setUp(self):
        from webapp import store

        store._write_cache("emotion", self.chat_hash, self.cache_result, fingerprint=self.fingerprint)

    def test_parse_and_statistics_reconcile(self):
        overview = self.stats["overview"]
        self.assertTrue(
            bool(self.chat.self_uid) and bool(self.chat.other_uid), "participant identity missing"
        )
        self.assertTrue(
            overview["total_messages"] == len(self.chat.statistical()), "message accounting mismatch"
        )
        self.assertTrue(
            overview["self_count"] + overview["other_count"] == overview["total_messages"],
            "sender accounting mismatch",
        )
        self.assertTrue(self.stats["mode"] == "private", "wrong statistics mode")

    def test_index_matches_legacy_filters_and_reuses_query(self):
        from webapp import api
        from webapp.message_index import MAX_QUERY_POSITIONS

        day = self.source.time_str[:10]
        for query in (
            ("", "", "", "", ""),
            (self.quote, "", "", "", ""),
            ("", "self", self.month, "", ""),
            ("", "other", "", day, day),
            (self.quote, self.source.sender_uid, self.month, day, day),
        ):
            expected = [
                i
                for i, message in enumerate(self.chat.messages)
                if api._match_browse(message, *query, self.chat.self_uid)
            ]
            actual = self.index.query(*query)
            self.assertTrue(list(actual) == expected, "indexed filter differs from legacy behavior")
            if any(query) and len(actual) <= MAX_QUERY_POSITIONS:
                self.assertIs(actual, self.index.query(*query), "query positions were not reused")

    def test_browse_api_pagination_and_unfiltered_context(self):
        from webapp import api

        for page in (1, 2):
            response = self.client.get("/api/messages", query_string={"page": page, "per_page": 7})
            self.assertTrue(response.status_code == 200, "message page failed")
            start = (page - 1) * 7
            expected = [m.id for m in self.chat.messages[start : start + 7]]
            self.assertTrue(
                [m["id"] for m in response.json["messages"]] == expected, "page boundary mismatch"
            )
        response = self.client.get(
            "/api/messages", query_string={"q": self.quote, "around": self.source.id, "context": 1}
        )
        self.assertTrue(response.status_code == 200, "context lookup failed")
        position = self.index.ids[self.source.id][0]
        expected = [
            api._fmt_browse_message(m, self.chat.self_uid)
            for m in self.chat.messages[max(0, position - 10) : position + 11]
        ]
        self.assertTrue(response.json["messages"] == expected, "context was filtered or changed")

    def test_sources_ids_and_read_only_pages(self):
        from webapp import api
        from webapp.evidence import verify_sources

        valid = {
            "quote": "「" + self.quote + "」",
            "sender_uid": self.source.sender_uid,
            "date": self.source.time_str[:10],
            "evidence_ids": [self.source.id],
        }
        entries = verify_sources(
            self.chat, "emotion", {self.month: {"evidence": valid}}, self.index, api._fmt_browse_message
        )["entries"]
        self.assertTrue(entries[0]["status"] == "unique", "valid evidence ID not confirmed")
        invalid = dict(valid, evidence_ids=["synthetic-unknown-source-id"])
        entries = verify_sources(
            self.chat, "emotion", {self.month: {"evidence": invalid}}, self.index, api._fmt_browse_message
        )["entries"]
        self.assertTrue(entries[0]["status"] == "not_found", "unknown source ID was accepted")
        before = self.model_calls.call_count
        response = self.client.get("/api/evidence/emotion")
        self.assertTrue(response.status_code == 200 and bool(response.json["entries"]), "evidence API failed")
        self.assertTrue(
            any(entry["status"] in ("unique", "multiple") for entry in response.json["entries"]),
            "legacy quote not located",
        )
        for route in (
            "/dashboard",
            "/messages",
            "/emotion",
            "/relationship",
            "/habits",
            "/topics",
            "/profile",
            "/recap",
            "/report",
        ):
            self.assertTrue(self.client.get(route).status_code == 200, "private page failed")
        self.assertTrue(self.model_calls.call_count == before, "read-only checks dispatched a model call")
        self.assertTrue(sha256(EXPORT.read_bytes()).digest() == self.original_digest, "original file changed")

    def test_refresh_and_normal_retry_with_real_input_and_mock_model(self):
        from analyzer import month_cache as mc
        from webapp import store
        from _stats import wait_until

        previous = mc._MONTH_CACHE_DIR
        cache_dir = Path(os.environ["QQCHAT_DATA_DIR"]) / "private-acceptance-months"
        mc.configure_month_cache(str(cache_dir))
        self.addCleanup(mc.configure_month_cache, previous)

        def analyze(refresh):
            response = self.client.post(
                "/api/analyze/emotion" + ("?refresh=1" if refresh else ""), headers=self.headers
            )
            self.assertTrue(response.status_code == 200, "analysis request failed")
            if "job" in response.json:
                job = response.json["job"]
                self.assertTrue(
                    wait_until(
                        lambda: (
                            self.client.get("/api/analyze-job/" + job).json["status"]
                            in ("done", "error", "cancelled")
                        ),
                        timeout=30,
                    ),
                    "mock analysis timed out",
                )
                self.assertTrue(
                    self.client.get("/api/analyze-job/" + job).json["status"] == "done",
                    "mock analysis did not complete",
                )

        before = self.model_calls.call_count
        analyze(False)
        self.assertTrue(self.model_calls.call_count == before, "ordinary cached run made model calls")
        analyze(True)
        first = self.model_calls.call_count
        self.assertTrue(first > before, "forced refresh did not bypass cache")
        # Remove only the temporary aggregate, so an ordinary retry must use its
        # newly populated month units. No source file or user cache is touched.
        with mock.patch.object(store, "_read_cache", return_value=None):
            analyze(False)
        self.assertTrue(self.model_calls.call_count == first, "ordinary retry did not reuse month units")
        analyze(True)
        self.assertTrue(
            self.model_calls.call_count - first == first - before, "second refresh reused month units"
        )

    @unittest.skipUnless(os.getenv("QQCHAT_BROWSER_TESTS") == "1", "opt-in browser suite")
    def test_real_private_browser_without_persisting_diagnostics(self):
        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server, WSGIRequestHandler
        import app

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app.app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        errors = []
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch()
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                try:
                    page = context.new_page()
                    page.set_default_timeout(15_000)
                    page.on("pageerror", lambda _: errors.append("javascript error"))
                    page.on("requestfailed", lambda _: errors.append("request failed"))
                    page.on(
                        "response",
                        lambda response: errors.append("HTTP error") if response.status >= 500 else None,
                    )

                    def local_only(route):
                        if route.request.url.startswith((base + "/", "data:", "blob:")):
                            route.continue_()
                        else:
                            errors.append("external request")
                            route.abort()

                    context.route("**/*", local_only)
                    page.goto(base)
                    page.locator("#fileInput").set_input_files(str(self.export_path))
                    page.locator("#submitBtn").click()
                    page.wait_for_url("**/dashboard")
                    page.locator("canvas").first.wait_for(state="visible")
                    page.goto(base + "/messages")
                    page.locator("#msgList .msg-row").first.wait_for(state="visible")
                    page.locator("#q").fill(self.quote)
                    page.locator("#q").press("Enter")
                    page.locator("#msgList .msg-row").first.wait_for(state="visible")
                    page.locator("#msgList .msg-row").first.press("Enter")
                    page.locator("#contextList .msg-hit").wait_for(state="visible")
                    page.keyboard.press("Escape")
                    page.goto(base + "/emotion")
                    page.locator("#evidenceList button").first.wait_for(state="visible")
                    button = page.locator("#evidenceList button").first
                    button.press("Enter")
                    page.locator("#evidenceDialog").wait_for(state="visible")
                    if page.locator("#evidenceContext > button").count():
                        page.locator("#evidenceContext > button").first.click()
                    page.locator("#evidenceContext .msg-hit").wait_for(state="visible")
                    self.assertTrue(
                        self.quote in page.locator("#evidenceContext .msg-hit").inner_text(),
                        "browser source differs",
                    )
                    page.keyboard.press("Escape")
                    self.assertTrue(
                        button.evaluate("node => node === document.activeElement"), "focus did not return"
                    )
                    page.goto(base + "/report")
                    with page.expect_download() as pending:
                        page.get_by_role("button", name="下载 HTML").click()
                    download = pending.value
                    self.assertTrue(download.failure() is None, "report download failed")
                    self.assertTrue(
                        b"CSRF_TOKEN" not in Path(download.path()).read_bytes(), "report contains CSRF token"
                    )
                    self.assertFalse(errors, "browser runtime or network error")
                finally:
                    context.close()
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
