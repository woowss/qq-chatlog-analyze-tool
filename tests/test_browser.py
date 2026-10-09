"""Opt-in Chromium flows against a loopback server, synthetic data and fake LLM."""

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap, api_configured_patcher

bootstrap()


@unittest.skipUnless(os.getenv("QQCHAT_BROWSER_TESTS") == "1", "opt-in browser suite")
class TestBrowserFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server
        import app
        from analyzer import deepseek_client as dc, group_client as gc
        from result_fixtures import for_dimension

        def fake_api(*args, **kwargs):
            result = for_dimension(kwargs["tag"])
            if kwargs["tag"] == "emotion":
                result["turning_point"] = "未找到「<img src=x onerror=alert(1)>」"
            if kwargs["tag"] == "group_emotion":
                result["group_evidence"] = "「今晚开黑吗」"
            return result

        cls.calls = mock.Mock(side_effect=fake_api)
        cls.patchers = [
            api_configured_patcher(),
            mock.patch.object(dc, "_call_api", cls.calls),
            mock.patch.object(gc, "_call_api", cls.calls),
        ]
        for patcher in cls.patchers:
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        cls.server = make_server("127.0.0.1", 0, app.app, threaded=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.thread.join, 5)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        cls.browser = cls.runtime.chromium.launch()
        cls.addClassCleanup(cls.browser.close)
        cls.artifacts = Path(os.getenv("QQCHAT_BROWSER_ARTIFACTS", "output/playwright"))
        cls.artifacts.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1366, "height": 900})
        self.addCleanup(self.context.close)
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.errors, self.diagnostics = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on(
            "console", lambda message: self.diagnostics.append(f"console:{message.type}:{message.text}")
        )
        self.page.on(
            "requestfailed", lambda request: self.errors.append(f"request:{request.url}:{request.failure}")
        )

        def response(result):
            if result.status >= 400:
                self.diagnostics.append(f"http:{result.status}:{result.url}")
                # A missing optional analysis cache is an expected initial state.
                if not (result.status == 404 and "/api/analysis/" in result.url):
                    self.errors.append(f"http:{result.status}:{result.url}")

        self.page.on("response", response)

        def local_only(route):
            if route.request.url.startswith((self.base + "/", "data:", "blob:")):
                route.continue_()
            else:
                self.errors.append("unexpected external request: " + route.request.url)
                route.abort()

        self.context.route("**/*", local_only)

    def tearDown(self):
        # Always retain reproducible trace/logs, including assertion failures.
        name = self.id().rsplit(".", 1)[-1]
        self.page.screenshot(path=str(self.artifacts / (name + ".png")), full_page=True)
        self.context.tracing.stop(path=str(self.artifacts / (name + ".zip")))
        (self.artifacts / (name + ".log")).write_text(
            "\n".join(self.diagnostics + self.errors), encoding="utf-8"
        )

    def upload(self, contents):
        from playwright.sync_api import expect

        self.page.goto(self.base)
        self.page.locator("#fileInput").set_input_files(
            {"name": "synthetic.json", "mimeType": "application/json", "buffer": contents}
        )
        self.page.locator("#submitBtn").click()
        self.page.wait_for_url("**/dashboard")
        expect(self.page.locator("canvas").first).to_be_visible()

    def test_private_upload_search_task_evidence_and_report(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        expect(self.page.locator("#msgList .msg-row")).to_have_count(100)
        self.page.locator("#nextPage").click()
        expect(self.page.locator("#msgList .msg-row")).to_have_count(20)
        self.page.locator("#q").fill("在吗")
        self.page.locator("#q").press("Enter")
        expect(self.page.locator("#msgList .msg-row")).to_have_count(2)
        self.page.locator("#msgList .msg-row").first.press("Enter")
        expect(self.page.locator("#contextDialog")).to_be_visible()
        expect(self.page.locator("#contextList")).to_contain_text("在的")
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#q")).to_have_value("在吗")
        self.page.goto(self.base + "/emotion")
        self.page.locator("#inPageAnalyze").click()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成", timeout=20_000)
        expect(self.page.locator("#emotionLineChart canvas")).to_be_visible()
        expect(self.page.locator("#evidenceList")).to_contain_text("存在多个候选")
        missing = self.page.locator("#evidenceList > div").filter(has_text="未找到一致来源")
        expect(missing).to_have_count(1)
        expect(missing.locator("button")).to_have_count(0)
        expect(self.page.locator("#evidenceList img")).to_have_count(0)
        candidate = self.page.get_by_role("button", name="查看候选（2）")
        candidate.press("Enter")
        expect(self.page.locator("#evidenceDialog")).to_be_visible()
        expect(self.page.locator("#evidenceContext > button")).to_have_count(2)
        self.page.locator("#evidenceContext > button").first.click()
        expect(self.page.locator("#evidenceContext .msg-hit")).to_contain_text("在吗")
        expect(self.page.locator("#evidenceContext")).to_contain_text("在的")
        self.page.keyboard.press("Escape")
        expect(candidate).to_be_focused()
        self.page.get_by_role("button", name="查看原文", exact=True).click()
        expect(self.page.locator("#evidenceContext .msg-hit")).to_contain_text("在的")
        self.page.set_viewport_size({"width": 390, "height": 844})
        expect(self.page.locator("#evidenceClose")).to_be_visible()
        bounds = self.page.locator("#evidenceDialog").bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        self.page.screenshot(path=str(self.artifacts / "private-context-mobile.png"))
        self.page.locator("#evidenceClose").click()
        self.page.set_viewport_size({"width": 1366, "height": 900})
        before = self.calls.call_count
        self.page.locator("#inPageAnalyze").click()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成")
        self.assertEqual(self.calls.call_count, before)
        self.page.locator(".analysis-options summary").click()
        self.page.locator("#inPageRefresh").check()
        self.page.locator("#inPageAnalyze").click()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成", timeout=20_000)
        self.assertEqual(self.calls.call_count, before + 1)
        self.page.screenshot(path=str(self.artifacts / "private-evidence.png"), full_page=True)
        self.page.goto(self.base + "/report")
        expect(self.page.locator("#evidencePanel")).to_have_count(0)
        with self.page.expect_download() as download:
            self.page.get_by_role("button", name="下载 HTML").click()
        downloaded = download.value
        self.assertTrue(downloaded.suggested_filename.endswith(".html"))
        report_path = self.artifacts / "synthetic-report.html"
        downloaded.save_as(report_path)
        report = report_path.read_text(encoding="utf-8")
        self.assertNotIn("evidenceDialog", report)
        self.assertNotIn("CSRF_TOKEN", report)
        self.assertFalse(self.errors, self.errors)

    def test_group_upload_and_source_context(self):
        from playwright.sync_api import expect

        self.upload((Path(__file__).parent / "fixtures/group_5p.json").read_bytes())
        self.page.goto(self.base + "/emotion")
        # Group pages use their shared controls, rather than private in-page controls.
        self.page.get_by_role("button", name="分析群聊情绪", exact=True).click()
        expect(self.page.locator("#evidenceList")).to_contain_text("原文唯一匹配", timeout=20_000)
        self.page.get_by_role("button", name="查看原文", exact=True).first.click()
        expect(self.page.locator("#evidenceContext .msg-hit")).to_contain_text("今晚开黑吗")
        expect(self.page.locator("#evidenceContext .msg-who").first).not_to_be_empty()
        self.page.screenshot(path=str(self.artifacts / "group-context.png"))
        self.page.keyboard.press("Escape")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
