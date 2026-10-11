"""Opt-in Chromium flows against a loopback server, synthetic data and fake LLM."""

import json
import os
import threading
import time
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
                expected = (result.status == 404 and "/api/analysis/" in result.url) or (
                    result.status == 409 and "/api/analyze-plan" in result.url
                )
                if not expected:
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
        # 图表可见时缓存 XHR 可能还未完成；等待真实界面状态，避免跳转取消正常请求。
        expect(self.page.locator('[data-analysis-empty][data-state="checking"]')).to_have_count(0)

    def confirm_plan(self):
        from playwright.sync_api import expect

        dialog = self.page.locator("#analysisPlanDialog")
        expect(dialog).to_be_visible()
        dialog.locator(".analysis-plan-footer .btn-primary").click()

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
        self.confirm_plan()
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
        self.confirm_plan()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成")
        self.assertEqual(self.calls.call_count, before)
        self.page.locator(".analysis-options summary").click()
        self.page.locator("#inPageRefresh").check()
        self.page.locator("#inPageAnalyze").click()
        self.confirm_plan()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成", timeout=20_000)
        self.assertEqual(self.calls.call_count, before + 1)
        self.page.screenshot(path=str(self.artifacts / "private-evidence.png"), full_page=True)
        self.page.goto(self.base + "/report")
        expect(self.page.locator("#evidencePanel")).to_have_count(0)
        with self.page.expect_download() as download:
            self.page.get_by_role("button", name="下载完整 HTML（未脱敏）").click()
        downloaded = download.value
        self.assertTrue(downloaded.suggested_filename.endswith(".html"))
        report_path = self.artifacts / "synthetic-report.html"
        downloaded.save_as(report_path)
        report = report_path.read_text(encoding="utf-8")
        self.assertNotIn("evidenceDialog", report)
        self.assertNotIn("CSRF_TOKEN", report)

        # The share copy is generated once and the exact same HTML drives preview,
        # download, and print. The default options must not call an AI endpoint.
        before_share = self.calls.call_count
        self.page.goto(self.base + "/report/share")
        self.page.get_by_role("button", name="生成预览").click()
        expect(self.page.locator("#shareStatus")).to_contain_text("预览已生成")
        frame = self.page.locator("#sharePreview")
        share_html = frame.get_attribute("srcdoc")
        self.assertNotIn("在吗", share_html)
        self.assertNotIn("CSRF_TOKEN", share_html)
        self.assertNotIn("<script", share_html.lower())
        self.assertNotIn("href=", share_html.lower())
        self.assertNotIn("src=", share_html.lower())
        self.assertIn("统计数字、图表、表格与互动矩阵已隐藏", share_html)
        self.assertEqual(self.calls.call_count, before_share)
        self.page.evaluate(
            "() => { window.__sharePrinted = false; "
            "document.querySelector('#sharePreview').contentWindow.print = "
            "() => { window.__sharePrinted = true; }; }"
        )
        self.page.get_by_role("button", name="打印 / 导出 PDF").click()
        self.assertTrue(self.page.evaluate("window.__sharePrinted"))
        with self.page.expect_download() as share_download:
            self.page.get_by_role("button", name="下载分享 HTML").click()
        share_file = share_download.value
        self.assertEqual(share_file.suggested_filename, "聊天分析分享报告.html")
        share_path = self.artifacts / "synthetic-share-report.html"
        share_file.save_as(share_path)
        self.assertEqual(share_path.read_text(encoding="utf-8"), share_html)

        self.page.locator('input[name="hide_original_text"]').uncheck()
        self.page.get_by_role("button", name="生成预览").click()
        expect(self.page.locator("#shareStatus")).to_contain_text("预览已生成")
        opted_in_html = frame.get_attribute("srcdoc")
        self.assertIn("在吗", opted_in_html)
        self.assertNotIn("2025-01-01", opted_in_html)
        self.assertNotIn("<script", opted_in_html.lower())
        self.assertEqual(self.calls.call_count, before_share)
        self.assertFalse(self.errors, self.errors)

    def test_group_upload_and_source_context(self):
        from playwright.sync_api import expect
        import app

        # 慢响应会让仪表盘先画出图表、缓存读取仍在途，覆盖离页取消请求的 CI 竞态。
        read_result = app.app.view_functions["api_analysis_result"]

        def slow_result(dimension):
            if dimension == "group_topics":
                time.sleep(1)
            return read_result(dimension)

        with mock.patch.dict(app.app.view_functions, api_analysis_result=slow_result):
            self.upload((Path(__file__).parent / "fixtures/group_5p.json").read_bytes())
            self.page.goto(self.base + "/emotion")
        # Group pages use their shared controls, rather than private in-page controls.
        self.page.get_by_role("button", name="分析群聊情绪", exact=True).click()
        self.confirm_plan()
        expect(self.page.locator("#evidenceList")).to_contain_text("原文唯一匹配", timeout=20_000)
        self.page.get_by_role("button", name="查看原文", exact=True).first.click()
        expect(self.page.locator("#evidenceContext .msg-hit")).to_contain_text("今晚开黑吗")
        expect(self.page.locator("#evidenceContext .msg-who").first).not_to_be_empty()
        self.page.screenshot(path=str(self.artifacts / "group-context.png"))
        self.page.keyboard.press("Escape")
        self.assertFalse(self.errors, self.errors)

    def test_old_person_profile_cache_is_marked_stale(self):
        from playwright.sync_api import expect
        from test_p1 import payload
        from result_fixtures import for_dimension

        self.upload(payload())
        identity = self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY")

        def stale_result(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"result": for_dimension("profile"), "stale_strategy": True, **identity},
                    ensure_ascii=False,
                ),
            )

        self.page.route("**/api/analysis/profile", stale_result)
        self.page.route(
            "**/api/evidence/profile",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"sources": []}),
            ),
        )
        self.page.goto(self.base + "/profile")
        warning = self.page.locator("#loadWarnBox[data-stale-strategy='true']")
        expect(warning).to_contain_text("旧版人物画像结果")
        expect(self.page.get_by_role("button", name="运行人物锐评")).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    def test_plan_preview_partial_selection_and_revalidation_message(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        raw = json.loads(payload().decode("utf-8"))
        for index, message in enumerate(raw["messages"]):
            month = 1 if index < len(raw["messages"]) // 2 else 2
            timestamp = 1735732800000 if month == 1 else 1738411200000
            message["timestamp"] = timestamp + index * 1000
        self.upload(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        self.page.locator(".analysis-options summary").click()
        before = self.calls.call_count
        with self.page.expect_response(
            lambda response: "/api/analysis-plan" in response.url
        ) as plan_response:
            self.page.get_by_role("button", name="情绪分析").click()
        dialog = self.page.locator("#analysisPlanDialog")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".analysis-plan-table tbody tr")).to_have_count(2)
        expect(dialog).to_contain_text("预计费用")
        self.assertEqual(self.calls.call_count, before)

        dialog.locator(".analysis-plan-table tbody tr").nth(1).locator("input").uncheck()

        preview_plan = plan_response.value.json()
        first_submit = [True]

        def changed_plan_once(route):
            if first_submit[0]:
                first_submit[0] = False
                route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "changed": True,
                            "error": "测试计划已变化，请检查更新后的计划。",
                            "plan": preview_plan,
                        },
                        ensure_ascii=False,
                    ),
                )
            else:
                route.continue_()

        self.page.route("**/api/analyze-plan", changed_plan_once)
        dialog.locator(".analysis-plan-footer .btn-primary").click()
        expect(dialog).to_contain_text("测试计划已变化")
        dialog.locator(".analysis-plan-footer .btn-primary").click()
        expect(self.page.locator("#analyzeStatus")).to_contain_text("未标记为完整维度结果", timeout=20_000)
        expect(self.page.locator("#analysisPlanDialog")).to_have_count(0)
        self.assertEqual(self.calls.call_count, before + 1)
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('ai_emotion')"))
        self.assertFalse(self.errors, self.errors)

    def test_local_period_comparison_for_private_and_group_chats(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        before = self.calls.call_count
        self.upload(payload())
        self.page.locator("#periodAFrom").fill("2025-01-02")
        self.page.locator("#periodATo").fill("2025-01-02")
        self.page.locator("#periodBFrom").fill("2025-01-01")
        self.page.locator("#periodBTo").fill("2025-01-01")
        self.page.locator("#comparePeriods").click()
        expect(self.page.locator("#periodCompareResult")).to_contain_text("平均消息长度")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("基期为 0")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("样本不足")

        self.upload((Path(__file__).parent / "fixtures/group_5p.json").read_bytes())
        self.page.locator("#periodAFrom").fill("2000-01-01")
        self.page.locator("#periodATo").fill("2099-12-31")
        self.page.locator("#periodBFrom").fill("2000-01-01")
        self.page.locator("#periodBTo").fill("2099-12-31")
        self.page.locator("#comparePeriods").click()
        expect(self.page.locator("#periodCompareResult")).to_contain_text("活跃成员")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("A：")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("B：")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("成员发言占比")
        expect(self.page.locator("#periodCompareResult")).to_contain_text("活跃小时（UTC+8）")
        self.assertEqual(self.calls.call_count, before)
        self.assertFalse(self.errors, self.errors)

    def test_keyword_timeline_filters_hits_and_opens_original_context(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        self.page.locator("#q").fill("在吗")
        self.page.locator("#btnTimeline").click()
        expect(self.page.locator("#timelineSummary")).to_contain_text("共命中 2 条消息")
        expect(self.page.locator("#timelineSummary")).to_contain_text("字面出现 2 次")
        expect(self.page.locator("#timelineSegments .timeline-segment")).to_have_count(1)
        self.page.locator("#timelineSegments .timeline-segment").click()
        expect(self.page.locator("#resultMeta")).to_contain_text("共 2 条")
        self.page.locator("#msgList .msg-row").first.press("Enter")
        expect(self.page.locator("#contextDialog")).to_be_visible()
        expect(self.page.locator("#contextList .msg-hit")).to_contain_text("在吗")
        self.assertFalse(self.errors, self.errors)

    def test_scope_editor_and_selected_message_fragment(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        expect(self.page.locator("#msgList .msg-row")).to_have_count(100)
        self.page.locator("#msgList .msg-scope-check").first.check()
        expect(self.page.locator("#selectedMessageCount")).to_contain_text("已选 1 条")
        self.page.locator("#useSelectedMessages").click()
        self.page.wait_for_url("**/dashboard")
        expect(self.page.locator("#analysisScopeButton")).to_contain_text("已限定")
        expect(self.page.locator("#analysisScopeSummary")).to_contain_text("选中片段 1 条")

        self.page.locator("#analysisScopeButton").click()
        expect(self.page.locator("#analysisScopeDialog")).to_be_visible()
        expect(self.page.locator("#scopeFragmentSummary")).to_contain_text("1 条选中消息")
        self.page.locator("#scopeCancel").click()

        plan = self.page.evaluate(
            "() => fetch('/api/analysis-plan?dimensions=emotion').then(response => response.json())"
        )
        self.assertEqual(plan["summary"]["unit_count"], 1)
        self.assertIn("选中片段 1 条", plan["scope_summary"])
        self.assertIn("不补入范围外上下文", plan["scope_summary"])
        self.assertFalse(self.errors, self.errors)

    def test_scope_save_navigates_initiator_and_refreshes_other_tab_once(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        other_tab = self.context.new_page()
        other_tab.goto(self.base + "/emotion")
        expect(other_tab.locator("#analysisScopeButton")).to_contain_text("全量对话")

        initiator_navigations = []
        other_navigations = []
        self.page.on(
            "framenavigated",
            lambda frame: initiator_navigations.append(frame.url) if frame == self.page.main_frame else None,
        )
        other_tab.on(
            "framenavigated",
            lambda frame: other_navigations.append(frame.url) if frame == other_tab.main_frame else None,
        )

        self.page.locator("#msgList .msg-scope-check").first.check()
        self.page.locator("#useSelectedMessages").click()
        self.page.wait_for_url("**/dashboard")
        expect(self.page.locator("#analysisScopeButton")).to_contain_text("已限定")
        expect(other_tab.locator("#analysisScopeButton")).to_contain_text("已限定")
        self.page.wait_for_timeout(200)

        self.assertEqual(initiator_navigations.count(self.base + "/dashboard"), 1)
        self.assertEqual(other_navigations.count(self.base + "/emotion"), 1)
        self.assertFalse(self.errors, self.errors)

    def test_scoped_analysis_does_not_reuse_legacy_session_result_after_scope_clear(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        upload_payload = json.loads(payload())
        upload_payload["messages"][0]["content"]["text"] = "隔离回归专用消息"
        upload_payload["messages"][0]["content"]["elements"][0]["data"]["text"] = "隔离回归专用消息"
        self.upload(json.dumps(upload_payload, ensure_ascii=False).encode())
        self.page.evaluate(
            "() => sessionStorage.setItem('ai_emotion', JSON.stringify({turning_point: '旧的全量结果'}))"
        )
        self.page.goto(self.base + "/messages")
        self.page.locator("#msgList .msg-scope-check").first.check()
        self.page.locator("#useSelectedMessages").click()
        self.page.wait_for_url("**/dashboard")

        self.page.goto(self.base + "/emotion")
        expect(self.page.locator('[data-analysis-empty][data-state="empty"]')).to_be_visible()
        self.assertIsNone(self.page.evaluate("() => sessionStorage.getItem('ai_emotion')"))

        self.page.locator("#analysisScopeButton").click()
        with (
            self.page.expect_navigation(wait_until="domcontentloaded"),
            self.page.expect_response(
                lambda response: "/api/analysis-scope" in response.url and response.request.method == "GET"
            ) as scope_response,
        ):
            self.page.locator("#scopeClear").click()
        self.assertFalse(scope_response.value.json()["active"])
        expect(self.page.locator('[data-analysis-empty][data-state="empty"]')).to_be_visible()
        self.assertIsNone(self.page.evaluate("() => sessionStorage.getItem('ai_emotion')"))
        self.assertFalse(self.errors, self.errors)

    def test_review_regression_escape_restores_plan_trigger(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        before = self.calls.call_count
        button = self.page.locator("#btn-analyze-all")
        for _ in range(2):
            button.click()
            expect(self.page.locator("#analysisPlanDialog")).to_be_visible()
            expect(button).to_be_disabled()
            self.page.keyboard.press("Escape")
            expect(self.page.locator("#analysisPlanDialog")).to_have_count(0)
            expect(button).to_be_enabled()
        self.assertEqual(self.calls.call_count, before)
        self.assertFalse(self.errors, self.errors)

    def test_review_regression_checkbox_space_preserves_row_keyboard_action(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        checkbox = self.page.locator("#msgList .msg-scope-check").first
        checkbox.focus()
        checkbox.press("Space")
        expect(checkbox).to_be_checked()
        expect(self.page.locator("#selectedMessageCount")).to_contain_text("已选 1 条")
        expect(self.page.locator("#contextDialog")).not_to_be_visible()
        checkbox.press("Space")
        expect(checkbox).not_to_be_checked()
        expect(self.page.locator("#selectedMessageCount")).to_contain_text("已选 0 条")
        self.page.locator("#msgList .msg-row").first.press("Space")
        expect(self.page.locator("#contextDialog")).to_be_visible()
        self.assertFalse(self.errors, self.errors)

    def test_review_regression_scoped_recap_completion_cannot_become_full_result(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        contents = json.loads(payload())
        contents["messages"][0]["content"]["text"] = "合成范围总括回归"
        contents["messages"][0]["content"]["elements"][0]["data"]["text"] = "合成范围总括回归"
        self.upload(json.dumps(contents, ensure_ascii=False).encode())
        self.page.goto(self.base + "/messages")
        self.page.locator("#msgList .msg-scope-check").first.check()
        self.page.locator("#useSelectedMessages").click()
        self.page.wait_for_url("**/dashboard")
        self.page.goto(self.base + "/recap")
        self.page.locator("#btn-recap").click()
        self.confirm_plan()
        expect(self.page.locator("#recapResult")).to_be_visible(timeout=20_000)
        self.assertIsNone(self.page.evaluate("() => sessionStorage.getItem('ai_recap')"))
        self.page.locator("#analysisScopeButton").click()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator("#scopeClear").click()
        expect(self.page.locator('[data-analysis-empty][data-state="empty"]')).to_be_visible()
        expect(self.page.locator("#recapResult")).not_to_be_visible()
        self.assertFalse(self.errors, self.errors)

    def test_plan_summary_tracks_selected_units_without_starting_model_calls(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        before = self.calls.call_count
        self.page.locator(".analysis-options summary").click()
        self.page.locator('.btn-analyze[data-dim="profile"]').click()
        dialog = self.page.locator("#analysisPlanDialog")
        expect(dialog).to_be_visible()
        checkboxes = dialog.locator(".analysis-plan-table input[type=checkbox]")
        expect(checkboxes).to_have_count(2)
        expect(dialog.locator(".analysis-plan-summary")).to_contain_text("当前选中 2 个单元")
        checkboxes.last.uncheck()
        expect(dialog.locator(".analysis-plan-summary")).to_contain_text("当前选中 1 个单元")
        dialog.locator(".btn-close").click()
        self.assertEqual(self.calls.call_count, before)
        self.assertFalse(self.errors, self.errors)

    def test_share_option_change_invalidates_pending_preview_success_and_failure(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/report/share")
        self.page.evaluate(
            """() => {
                const original = $.ajax;
                window.__shareRequests = [];
                $.ajax = function(options) {
                    if (options.url !== '/api/report/share') return original.apply(this, arguments);
                    const deferred = $.Deferred();
                    window.__shareRequests.push(deferred);
                    return deferred.promise();
                };
            }"""
        )

        self.page.get_by_role("button", name="生成预览").click()
        self.page.wait_for_function("window.__shareRequests.length === 1")
        self.page.locator('input[name="hide_original_text"]').uncheck()
        self.page.evaluate(
            "window.__shareRequests[0].resolve({html:'<p>OLD_SENSITIVE_SYNTHETIC_EXCERPT</p>',warnings:[]})"
        )
        frame = self.page.locator("#sharePreview")
        self.assertNotIn("OLD_SENSITIVE", frame.get_attribute("srcdoc"))
        expect(self.page.locator("#downloadShare")).to_be_disabled()
        expect(self.page.locator("#printShare")).to_be_disabled()
        expect(self.page.locator("#shareStatus")).to_contain_text("选项已变化")

        self.page.get_by_role("button", name="生成预览").click()
        self.page.wait_for_function("window.__shareRequests.length === 2")
        self.page.locator('input[name="hide_nicknames"]').uncheck()
        self.page.evaluate(
            "() => { window.__shareRequests[1].reject({responseJSON:{error:'OLD_SYNTHETIC_FAILURE'}}); }"
        )
        self.assertNotIn("OLD_SENSITIVE", frame.get_attribute("srcdoc"))
        expect(self.page.locator("#shareStatus")).to_contain_text("选项已变化")
        expect(self.page.locator("#shareStatus")).not_to_contain_text("OLD_SYNTHETIC_FAILURE")
        expect(self.page.locator("#downloadShare")).to_be_disabled()
        expect(self.page.locator("#printShare")).to_be_disabled()
        self.assertFalse(self.errors, self.errors)

    def test_message_and_timeline_search_ignore_out_of_order_responses(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/messages")
        expect(self.page.locator("#msgList .msg-row")).to_have_count(100)
        samples = self.page.evaluate(
            """async () => ({
                messages: await fetch('/api/messages').then(r => r.json()),
                timeline: await fetch('/api/messages/timeline?q=在吗').then(r => r.json())
            })"""
        )
        self.page.evaluate(
            """() => {
                window.__searchOriginalGet = $.get;
                const original = window.__searchOriginalGet;
                window.__searchRequests = [];
                $.get = function(url, data, success, dataType) {
                    if (url !== '/api/messages' && url !== '/api/messages/timeline')
                        return original.apply(this, arguments);
                    if (typeof data === 'function') { success = data; data = undefined; }
                    const deferred = $.Deferred();
                    if (success) deferred.done(success);
                    window.__searchRequests.push({url: url, deferred: deferred});
                    return deferred.promise();
                };
            }"""
        )

        self.page.locator("#q").fill("旧查询")
        self.page.locator("#btnSearch").click()
        self.page.wait_for_function("window.__searchRequests.length === 1")
        self.page.locator("#q").fill("新查询")
        self.page.locator("#btnSearch").click()
        self.page.wait_for_function("window.__searchRequests.length === 2")

        old_messages = json.loads(json.dumps(samples["messages"], ensure_ascii=False))
        new_messages = json.loads(json.dumps(samples["messages"], ensure_ascii=False))
        old_messages["messages"] = old_messages["messages"][:1]
        new_messages["messages"] = new_messages["messages"][:1]
        old_messages["messages"][0]["text"] = "OLD_QUERY_RESULT_SENTINEL"
        new_messages["messages"][0]["text"] = "NEW_QUERY_RESULT_SENTINEL"
        self.page.evaluate("data => window.__searchRequests[1].deferred.resolve(data)", new_messages)
        expect(self.page.locator("#msgList")).to_contain_text("NEW_QUERY_RESULT_SENTINEL")
        self.page.evaluate("data => window.__searchRequests[0].deferred.resolve(data)", old_messages)
        expect(self.page.locator("#msgList")).to_contain_text("NEW_QUERY_RESULT_SENTINEL")
        expect(self.page.locator("#msgList")).not_to_contain_text("OLD_QUERY_RESULT_SENTINEL")

        self.page.locator("#q").fill("旧失败查询")
        self.page.locator("#btnSearch").click()
        self.page.wait_for_function("window.__searchRequests.length === 3")
        self.page.locator("#q").fill("最新查询")
        self.page.locator("#btnSearch").click()
        self.page.wait_for_function("window.__searchRequests.length === 4")
        newest = json.loads(json.dumps(samples["messages"], ensure_ascii=False))
        newest["messages"] = newest["messages"][:1]
        newest["messages"][0]["text"] = "LATEST_QUERY_RESULT_SENTINEL"
        self.page.evaluate("data => window.__searchRequests[3].deferred.resolve(data)", newest)
        self.page.evaluate(
            "() => { window.__searchRequests[2].deferred.reject({responseJSON:{"
            "error:'OLD_QUERY_FAILURE_SENTINEL'}}); }"
        )
        expect(self.page.locator("#msgList")).to_contain_text("LATEST_QUERY_RESULT_SENTINEL")
        expect(self.page.locator("#msgWarn")).not_to_contain_text("OLD_QUERY_FAILURE_SENTINEL")

        # Begin with a real, completed timeline. Subsequent Search clicks can
        # then launch overlapping timeline refreshes through the actual UI path.
        self.page.evaluate("() => { $.get = window.__searchOriginalGet; }")
        self.page.locator("#q").fill("在吗")
        self.page.locator("#btnTimeline").click()
        expect(self.page.locator("#timelineSummary")).to_contain_text("共命中")
        expect(self.page.locator("#btnTimeline")).to_be_enabled()
        self.page.evaluate(
            """() => {
                const original = window.__searchOriginalGet;
                window.__searchRequests = [];
                $.get = function(url, data, success, dataType) {
                    if (url !== '/api/messages' && url !== '/api/messages/timeline')
                        return original.apply(this, arguments);
                    if (typeof data === 'function') { success = data; data = undefined; }
                    const deferred = $.Deferred();
                    if (success) deferred.done(success);
                    window.__searchRequests.push({url: url, deferred: deferred});
                    return deferred.promise();
                };
            }"""
        )

        def wait_for_timeline_count(expected):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                count = self.page.evaluate(
                    "() => window.__searchRequests.filter(r => r.url === '/api/messages/timeline').length"
                )
                if count == expected:
                    return
                self.page.wait_for_timeout(10)
            self.fail("timed out waiting for %d mocked timeline requests" % expected)

        self.page.locator("#q").fill("旧时间线")
        self.page.locator("#btnSearch").click()
        wait_for_timeline_count(1)
        self.page.locator("#q").fill("新时间线")
        self.page.locator("#btnSearch").click()
        wait_for_timeline_count(2)
        requests = self.page.evaluate("window.__searchRequests.map((r, i) => ({i, url:r.url}))")
        timeline_entries = [entry for entry in requests if entry["url"] == "/api/messages/timeline"]
        timeline_indices = [entry["i"] for entry in timeline_entries]
        old_timeline = dict(samples["timeline"], query="OLD_TIMELINE_SENTINEL", matched_messages=8)
        new_timeline = dict(samples["timeline"], query="NEW_TIMELINE_SENTINEL", matched_messages=3)
        resolve_request = "args => window.__searchRequests[args[0]].deferred.resolve(args[1])"
        self.page.evaluate(resolve_request, [timeline_indices[1], new_timeline])
        expect(self.page.locator("#timelineSummary")).to_contain_text("NEW_TIMELINE_SENTINEL")
        self.page.evaluate(resolve_request, [timeline_indices[0], old_timeline])
        expect(self.page.locator("#timelineSummary")).to_contain_text("NEW_TIMELINE_SENTINEL")
        expect(self.page.locator("#timelineSummary")).not_to_contain_text("OLD_TIMELINE_SENTINEL")

        self.page.locator("#q").fill("旧时间线失败")
        self.page.locator("#btnSearch").click()
        wait_for_timeline_count(3)
        self.page.locator("#q").fill("最新时间线")
        self.page.locator("#btnSearch").click()
        wait_for_timeline_count(4)
        requests = self.page.evaluate("window.__searchRequests.map((r, i) => ({i, url:r.url}))")
        timeline_entries = [entry for entry in requests if entry["url"] == "/api/messages/timeline"]
        timeline_indices = [entry["i"] for entry in timeline_entries]
        latest_timeline = dict(samples["timeline"], query="LATEST_TIMELINE_SENTINEL", matched_messages=2)
        self.page.evaluate(resolve_request, [timeline_indices[3], latest_timeline])
        self.page.evaluate(
            "args => { window.__searchRequests[args].deferred.reject({responseJSON:{"
            "error:'OLD_TIMELINE_FAILURE_SENTINEL'}}); }",
            timeline_indices[2],
        )
        expect(self.page.locator("#timelineSummary")).to_contain_text("LATEST_TIMELINE_SENTINEL")
        expect(self.page.locator("#timelineSummary")).not_to_contain_text("OLD_TIMELINE_FAILURE_SENTINEL")
        expect(self.page.locator("#btnTimeline")).to_be_enabled()

        self.page.locator("#q").fill("重置前时间线")
        self.page.locator("#btnSearch").click()
        wait_for_timeline_count(5)
        requests = self.page.evaluate("window.__searchRequests.map((r, i) => ({i, url:r.url}))")
        timeline_entries = [entry for entry in requests if entry["url"] == "/api/messages/timeline"]
        self.page.locator("#btnReset").click()
        self.page.evaluate(
            "args => window.__searchRequests[args[0]].deferred.resolve(args[1])",
            [timeline_entries[4]["i"], old_timeline],
        )
        expect(self.page.locator("#timelineSummary")).to_have_text("输入关键词后点“关键词时间线”。")
        expect(self.page.locator("#btnTimeline")).to_be_enabled()
        self.page.evaluate("() => { $.get = window.__searchOriginalGet; }")
        # The upload helper first visits the dashboard, whose read-only
        # bootstrap GETs can be cancelled when this test navigates to /messages
        # on slower CI workers. Ignore only those explicit Chromium aborts;
        # HTTP failures and unrelated failed requests must still fail the test.
        self.errors = [
            error
            for error in self.errors
            if not (
                error.startswith("request:")
                and "ERR_ABORTED" in error
                and any(
                    endpoint in error
                    for endpoint in (
                        "/api/jobs/history",
                        "/api/usage",
                        "/api/analysis-scope",
                    )
                )
            )
        ]
        self.assertFalse(self.errors, self.errors)

    def test_analysis_completion_and_get_cache_use_server_chat_scope_identity(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/emotion")
        identity = self.page.evaluate(
            "fetch('/api/analysis-scope').then(r => r.json()).then(({chat_hash, scope_id}) => "
            "({chat_hash, scope_id}))"
        )
        key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            identity,
        )
        self.page.locator("#inPageAnalyze").click()
        self.confirm_plan()
        expect(self.page.locator("#inPageStatus")).to_contain_text("分析完成", timeout=20_000)
        entry = self.page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", key)
        self.assertEqual(entry["chat_hash"], identity["chat_hash"])
        self.assertEqual(entry["scope_id"], identity["scope_id"])
        self.assertTrue(any("turning_point" in month for month in entry["result"].values()))
        self.assertIsNone(self.page.evaluate("() => sessionStorage.getItem('ai_emotion')"))

        # A subsequent GET cache hit follows the same identity-bearing path.
        self.page.reload()
        expect(self.page.locator("#emotionVisuals")).to_be_visible()
        entry_after_get = self.page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", key)
        self.assertEqual(entry_after_get["chat_hash"], identity["chat_hash"])
        self.assertEqual(entry_after_get["scope_id"], identity["scope_id"])
        self.assertFalse(self.errors, self.errors)

    def test_old_chat_or_scope_result_never_falls_back_into_current_analysis_page(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        full_identity = self.page.evaluate(
            "fetch('/api/analysis-scope').then(r => r.json()).then(({chat_hash, scope_id}) => "
            "({chat_hash, scope_id}))"
        )
        old_key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            full_identity,
        )
        self.page.evaluate(
            """({key, identity}) => {
                sessionStorage.setItem(key, JSON.stringify({
                    chat_hash: identity.chat_hash,
                    scope_id: identity.scope_id,
                    result: {turning_point: 'OLD_SCOPE_SYNTHETIC_RESULT'}
                }));
                sessionStorage.setItem('ai_emotion', JSON.stringify({
                    turning_point: 'LEGACY_SYNTHETIC_RESULT'
                }));
            }""",
            {"key": old_key, "identity": full_identity},
        )
        self.page.evaluate(
            """async () => {
                const token = window.CSRF_TOKEN;
                const response = await fetch('/api/analysis-scope', {
                    method: 'POST', headers: {'Content-Type':'application/json', 'X-CSRF-Token':token},
                    body: JSON.stringify({scope:{message_positions:[0]}})
                });
                if (!response.ok) throw new Error('scope setup failed: ' + response.status);
            }"""
        )
        scoped_identity = self.page.evaluate(
            "fetch('/api/analysis-scope').then(r => r.json()).then(({chat_hash, scope_id}) => "
            "({chat_hash, scope_id}))"
        )
        self.assertEqual(scoped_identity["chat_hash"], full_identity["chat_hash"])
        self.assertNotEqual(scoped_identity["scope_id"], full_identity["scope_id"])
        self.page.route(
            "**/api/analysis/emotion",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps(
                    {
                        "error": "暂无该维度的分析结果",
                        "chat_hash": scoped_identity["chat_hash"],
                        "scope_id": scoped_identity["scope_id"],
                    }
                ),
            ),
        )
        self.page.goto(self.base + "/emotion")
        expect(self.page.locator('[data-analysis-empty][data-state="empty"]')).to_be_visible()
        expect(self.page.locator("#emotionVisuals")).not_to_be_visible()
        self.assertIsNone(self.page.evaluate("() => sessionStorage.getItem('ai_emotion')"))
        scoped_key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            scoped_identity,
        )
        self.assertIsNone(self.page.evaluate("key => sessionStorage.getItem(key)", scoped_key))
        self.assertTrue(self.page.evaluate("key => !!sessionStorage.getItem(key)", old_key))

        # Even a cache-shaped response from a different chat is unusable on the
        # active scoped page. This proves fallback is bound to both identity
        # components rather than only the selected-message scope.
        from result_fixtures import for_dimension

        foreign_identity = {
            "chat_hash": "foreign-chat-synthetic",
            "scope_id": scoped_identity["scope_id"],
        }
        self.page.unroute("**/api/analysis/emotion")
        self.page.route(
            "**/api/analysis/emotion",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "result": {"2025-01": for_dimension("emotion")},
                        **foreign_identity,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        foreign_key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            foreign_identity,
        )
        self.page.reload()
        expect(self.page.locator('[data-analysis-empty][data-state="error"]')).to_be_visible()
        expect(self.page.locator("#emotionVisuals")).not_to_be_visible()
        self.assertIsNone(self.page.evaluate("key => sessionStorage.getItem(key)", foreign_key))
        self.page.unroute("**/api/analysis/emotion")
        self.errors = [
            error for error in self.errors if not ("/api/analysis-scope" in error and "ERR_ABORTED" in error)
        ]
        self.assertFalse(self.errors, self.errors)

    def test_job_result_after_another_tab_changes_scope_reloads_before_render(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/emotion")
        old_identity = self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY")
        self.assertTrue(old_identity and old_identity["chat_hash"])

        other_tab = self.context.new_page()
        other_tab.goto(self.base + "/dashboard")
        other_tab.evaluate(
            """async () => {
                const response = await fetch('/api/analysis-scope', {
                    method: 'POST', headers: {
                        'Content-Type':'application/json', 'X-CSRF-Token':window.CSRF_TOKEN
                    },
                    body: JSON.stringify({scope:{message_positions:[0]}})
                });
                if (!response.ok) throw new Error('scope update failed: ' + response.status);
            }"""
        )
        new_identity = other_tab.evaluate(
            "fetch('/api/analysis-scope').then(r => r.json()).then(({chat_hash, scope_id}) => "
            "({chat_hash, scope_id}))"
        )
        self.assertEqual(new_identity["chat_hash"], old_identity["chat_hash"])
        self.assertNotEqual(new_identity["scope_id"], old_identity["scope_id"])
        # A valid full-scope GET may have populated its own cache before the
        # second tab changes scope. Remove it so this assertion isolates the
        # stale task result rather than a legitimate earlier cache hit.
        old_key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            old_identity,
        )
        self.page.evaluate("key => sessionStorage.removeItem(key)", old_key)
        # The other tab bypasses the UI broadcast; the old document must keep its
        # frozen identity until result validation detects the server-side change.
        self.assertEqual(self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY"), old_identity)
        expect(self.page.locator("#analysisScopeButton")).to_contain_text("全量对话")

        self.page.locator("#inPageAnalyze").click()
        self.confirm_plan()
        expect(self.page.locator("#analysisScopeButton")).to_contain_text("已限定", timeout=20_000)
        expect(self.page.locator("#inPageStatus")).not_to_contain_text("分析完成")
        expect(self.page.locator("#emotionVisuals")).to_be_visible(timeout=20_000)
        current_identity = self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY")
        self.assertEqual(current_identity, new_identity)
        scoped_key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            new_identity,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.page.evaluate(
            "key => !!sessionStorage.getItem(key)", scoped_key
        ):
            self.page.wait_for_timeout(10)
        self.assertTrue(self.page.evaluate("key => !!sessionStorage.getItem(key)", scoped_key))
        self.assertIsNone(self.page.evaluate("key => sessionStorage.getItem(key)", old_key))
        # The stale document's API read can be aborted by the deliberate
        # identity-triggered full-page reload.
        self.errors = [error for error in self.errors if "/api/analysis-scope" not in error]
        self.assertFalse(self.errors, self.errors)

    def test_analysis_404_and_network_fallback_require_matching_page_identity(self):
        from playwright.sync_api import expect
        from result_fixtures import for_dimension
        from test_p1 import payload

        self.upload(payload())
        identity = self.page.evaluate(
            "fetch('/api/analysis-scope').then(r => r.json()).then(({chat_hash, scope_id}) => "
            "({chat_hash, scope_id}))"
        )
        key = self.page.evaluate(
            "id => 'ai:v2:' + encodeURIComponent(id.chat_hash) + ':' + "
            "encodeURIComponent(id.scope_id) + ':emotion'",
            identity,
        )
        cached = {"2025-01": for_dimension("emotion")}
        cached["2025-01"]["turning_point"] = "IDENTITY_MATCHED_SYNTHETIC_CACHE"
        self.page.evaluate(
            "args => cacheAnalysisResult('emotion', args.result, args.identity)",
            {"result": cached, "identity": identity},
        )
        self.page.route(
            "**/api/analysis/emotion",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"error": "暂无该维度的分析结果", **identity}),
            ),
        )
        self.page.route(
            "**/api/evidence/emotion",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"sources": []}),
            ),
        )
        self.page.goto(self.base + "/emotion")
        expect(self.page.locator("#emotionVisuals")).to_be_visible()
        expect(self.page.locator("#emotionDetails")).to_contain_text("IDENTITY_MATCHED_SYNTHETIC_CACHE")

        self.page.unroute("**/api/analysis/emotion")
        self.page.route("**/api/analysis/emotion", lambda route: route.abort())
        self.page.reload()
        expect(self.page.locator("#emotionVisuals")).to_be_visible()
        expect(self.page.locator("#emotionDetails")).to_contain_text("IDENTITY_MATCHED_SYNTHETIC_CACHE")

        self.page.evaluate("key => sessionStorage.removeItem(key)", key)
        self.page.reload()
        expect(self.page.locator('[data-analysis-empty][data-state="error"]')).to_be_visible()
        expect(self.page.locator("#emotionVisuals")).not_to_be_visible()
        # The one aborted request above is intentional; all other browser errors remain actionable.
        self.errors = [error for error in self.errors if "/api/analysis/emotion" not in error]
        self.assertFalse(self.errors, self.errors)

    def test_scope_boot_get_cannot_replace_server_rendered_page_identity(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        page_identity = self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY")
        self.assertTrue(page_identity and page_identity["chat_hash"])
        requests = [0]
        navigations = []
        self.page.on(
            "framenavigated",
            lambda frame: navigations.append(frame.url) if frame == self.page.main_frame else None,
        )

        def delayed_identity(route):
            requests[0] += 1
            identity = dict(page_identity)
            if requests[0] == 1:
                identity["scope_id"] = "synthetic-stale-document-scope"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        **identity,
                        "scope": {},
                        "active": False,
                        "summary": "",
                        "warning": "",
                        "senders": [],
                    }
                ),
            )

        self.page.route("**/api/analysis-scope", delayed_identity)
        self.page.route(
            "**/api/analysis/emotion",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"error": "暂无该维度的分析结果", **page_identity}, ensure_ascii=False),
            ),
        )
        self.page.goto(self.base + "/emotion")
        expect(self.page.locator("#analysisScopeButton")).to_contain_text("全量对话")
        expect(self.page.locator('[data-analysis-empty][data-state="empty"]')).to_be_visible()
        self.assertGreaterEqual(requests[0], 2)
        self.assertGreaterEqual(len(navigations), 2)
        self.assertEqual(self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY"), page_identity)
        self.errors = [
            error for error in self.errors if not ("/api/analysis-scope" in error and "ERR_ABORTED" in error)
        ]
        self.assertFalse(self.errors, self.errors)

    def test_older_scope_identity_response_cannot_replace_newer_request(self):
        from playwright.sync_api import expect
        from test_p1 import payload

        self.upload(payload())
        self.page.goto(self.base + "/emotion")
        expect(self.page.locator("#analysisScopeButton")).to_be_visible()
        current = self.page.evaluate("fetch('/api/analysis-scope').then(r => r.json())")
        identity = {"chat_hash": current["chat_hash"], "scope_id": current["scope_id"]}
        self.page.evaluate(
            """() => {
                window.__scopeGetRequests = [];
                window.__scopeGetResults = [];
                window.__scopeOriginalGet = $.get;
                $.get = function(url) {
                    if (url !== '/api/analysis-scope') {
                        return window.__scopeOriginalGet.apply(this, arguments);
                    }
                    const deferred = $.Deferred();
                    window.__scopeGetRequests.push(deferred);
                    return deferred.promise();
                };
                window.refreshAnalysisIdentity((value, error) =>
                    window.__scopeGetResults.push({value, error}));
                window.refreshAnalysisIdentity((value, error) =>
                    window.__scopeGetResults.push({value, error}));
            }"""
        )

        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline and self.page.evaluate("() => window.__scopeGetRequests.length") < 2
        ):
            self.page.wait_for_timeout(10)
        self.assertEqual(self.page.evaluate("() => window.__scopeGetRequests.length"), 2)
        newest = {**current, "summary": "CURRENT_SYNTHETIC_SCOPE"}
        stale = {**current, "scope_id": "older-synthetic-scope", "summary": "STALE_SYNTHETIC_SCOPE"}
        self.page.evaluate("data => { window.__scopeGetRequests[1].resolve(data); }", newest)
        self.page.evaluate("data => { window.__scopeGetRequests[0].resolve(data); }", stale)
        outcomes = self.page.evaluate("() => window.__scopeGetResults")
        self.assertEqual(outcomes[0]["value"], identity)
        self.assertIsNone(outcomes[0]["error"])
        self.assertIsNone(outcomes[1]["value"])
        self.assertIn("较新的请求取代", outcomes[1]["error"])
        self.assertEqual(self.page.evaluate("window.ANALYSIS_PAGE_IDENTITY"), identity)
        self.assertEqual(self.page.url, self.base + "/emotion")
        self.page.evaluate("() => { $.get = window.__scopeOriginalGet; }")
        self.assertFalse(self.errors, self.errors)


if __name__ == "__main__":
    unittest.main()
