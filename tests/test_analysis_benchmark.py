import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _bootstrap import bootstrap

bootstrap()

from tools.analysis_benchmark import (  # noqa: E402
    DEFAULT_DATASET,
    _build_prompt_messages,
    make_review_template,
    compare_runs,
    execute_real_plan,
    validate_dataset,
    validate_plan,
    validate_review,
    validate_run,
)


class AnalysisBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
        cls.dataset_hash = hashlib.sha256(DEFAULT_DATASET.read_bytes()).hexdigest()
        cls.scenario = cls.dataset["scenarios"][0]

    def _run(self, *, evaluation_type="simulated", records=None, **overrides):
        run = {
            "run_id": "synthetic-test-run",
            "dataset_id": self.dataset["dataset_id"],
            "dataset_sha256": self.dataset_hash,
            "condition": "offline-fixture",
            "evaluation_type": evaluation_type,
            "model": "simulated-only",
            "prompt_version": "fixture-v1",
            "input_strategy": {"name": "all-scene-messages", "version": "1"},
            "records": records
            if records is not None
            else [
                {
                    "scenario_id": self.scenario["id"],
                    "input_message_ids": [item["id"] for item in self.scenario["messages"]],
                    "claims": [
                        {
                            "type": "fact",
                            "text": "青禾负责标题。",
                            "speaker": "青禾",
                            "evidence_ids": [self.scenario["messages"][0]["id"]],
                            "quote": "今天的海报我先排了标题。",
                        }
                    ],
                    "dimension": "ask",
                    "model_result": {"answer": "青禾负责标题。", "confidence": "medium", "evidence": []},
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "latency_ms": 250,
                }
            ],
        }
        run.update(overrides)
        return run

    def _write_run(self, run, root):
        path = Path(root) / "run.json"
        path.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        return path

    def test_fixed_dataset_covers_all_eight_categories(self):
        report = validate_dataset()
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["scenario_count"], 24)
        self.assertEqual(len(report["category_counts"]), 8)
        self.assertTrue(all(count == 3 for count in report["category_counts"].values()))

    def test_valid_run_checks_structure_citations_and_cost_only_when_configured(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._write_run(self._run(), root)
            report = validate_run(path)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["sample_coverage"], round(1 / 24, 4))
        self.assertEqual(report["mechanical_metrics"]["citation_coverage"], 1.0)
        self.assertIsNone(report["usage"]["estimated_cost"])

    def test_bad_evidence_and_invalid_result_structure_are_rejected(self):
        run = self._run()
        run["records"][0]["claims"][0]["evidence_ids"] = ["outside-message"]
        run["records"][0]["model_result"] = {"answer": 7, "confidence": "perhaps", "evidence": []}
        with tempfile.TemporaryDirectory() as root:
            report = validate_run(self._write_run(run, root))
        self.assertFalse(report["valid"])
        self.assertTrue(any("场景外消息" in error for error in report["errors"]))
        self.assertTrue(any("model_result" in error for error in report["errors"]))

    def test_quote_must_match_an_input_evidence_message(self):
        run = self._run()
        run["records"][0]["claims"][0]["quote"] = "没有出现过的原文"
        with tempfile.TemporaryDirectory() as root:
            report = validate_run(self._write_run(run, root))
        self.assertFalse(report["valid"])
        self.assertTrue(any("quote" in error for error in report["errors"]))

    def test_candidate_prompt_hides_labels_and_uses_opaque_message_ids(self):
        record = {"input_message_ids": [message["id"] for message in self.scenario["messages"]]}
        plan = {"system_prompt": "谨慎分析。", "user_prompt": "概括对话。"}
        messages, _, _, _ = _build_prompt_messages(plan, self.scenario, record)
        request_text = "\n".join(message["content"] for message in messages)
        prompt_payload = json.loads(request_text.split("输入消息（JSON 数组；按时间顺序）：\n", 1)[1])

        self.assertNotIn(self.scenario["id"], request_text)
        self.assertNotIn(self.scenario["category"], request_text)
        self.assertEqual(
            [message["id"] for message in prompt_payload],
            [f"m{i}" for i in range(1, len(prompt_payload) + 1)],
        )
        self.assertTrue({message["id"] for message in prompt_payload}.isdisjoint(record["input_message_ids"]))

    def test_real_run_requires_explicit_limits_and_complete_usage(self):
        run = self._run(evaluation_type="real")
        with tempfile.TemporaryDirectory() as root:
            report = validate_run(self._write_run(run, root))
        self.assertFalse(report["valid"])
        self.assertTrue(any("真实评测必须记录" in error for error in report["errors"]))

    def test_price_estimate_requires_prices_and_complete_usage(self):
        run = self._run(prices={"input_per_million": 2.0, "output_per_million": 8.0, "currency": "CNY"})
        with tempfile.TemporaryDirectory() as root:
            report = validate_run(self._write_run(run, root))
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["usage"]["estimated_cost"], 0.00036)

    def test_review_template_uses_condition_and_empty_independent_scores(self):
        run = self._run()
        with tempfile.TemporaryDirectory() as root:
            run_path = self._write_run(run, root)
            output = Path(root) / "review.json"
            make_review_template(run_path, output)
            review = json.loads(output.read_text(encoding="utf-8"))
        item = review["reviews"][0]
        self.assertEqual(item["condition"], "offline-fixture")
        self.assertIsNone(item["evidence_support"])
        self.assertIn("key_fact_coverage", item)
        self.assertTrue(item["messages"][0]["included_in_model_input"])
        self.assertEqual(review["dataset_sha256"], self.dataset_hash)

    def test_compare_runs_pairs_same_scenes_and_never_calls_simulated_quality_evidence(self):
        baseline = self._run()
        candidate = self._run()
        baseline.update(run_id="baseline-run", condition="baseline")
        candidate.update(
            run_id="candidate-run",
            condition="candidate",
            prompt_version="context-v2",
            input_strategy={"name": "reply-neighbors", "version": "2"},
        )
        candidate["records"][0]["prompt_tokens"] = 130
        with tempfile.TemporaryDirectory() as root:
            baseline_path = self._write_run(baseline, root)
            candidate_path = Path(root) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            report = compare_runs(baseline_path, candidate_path)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["dataset"]["paired_scenario_count"], 1)
        self.assertEqual(report["usage"]["prompt_tokens"]["delta_candidate_minus_baseline"], 30)
        self.assertFalse(report["quality_evidence"])
        self.assertIn("模拟或离线", report["quality_note"])

    def test_real_comparison_requires_matching_full_coverage_and_complete_reviews(self):
        def real_run(run_id, condition, prompt_version):
            records = []
            total_chars = 0
            for scenario in self.dataset["scenarios"]:
                input_ids = [message["id"] for message in scenario["messages"]]
                total_chars += sum(len(message["text"]) for message in scenario["messages"])
                records.append(
                    {
                        "scenario_id": scenario["id"],
                        "input_message_ids": input_ids,
                        "claims": [],
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "latency_ms": 250,
                    }
                )
            run = self._run(evaluation_type="real", records=records)
            run.update(
                run_id=run_id,
                condition=condition,
                prompt_version=prompt_version,
                limits={
                    "max_calls": len(records),
                    "max_input_chars": total_chars,
                    "max_total_prompt_tokens": len(records) * 100,
                    "max_output_tokens": 20,
                    "cost_budget": 1.0,
                },
                prices={"input_per_million": 2.0, "output_per_million": 8.0, "currency": "CNY"},
            )
            return run

        with tempfile.TemporaryDirectory() as root:
            baseline_path = self._write_run(real_run("real-a", "A", "p1"), root)
            candidate_path = Path(root) / "candidate.json"
            candidate_path.write_text(
                json.dumps(real_run("real-b", "B", "p2"), ensure_ascii=False), encoding="utf-8"
            )
            baseline_review_path = Path(root) / "review-a.json"
            candidate_review_path = Path(root) / "review-b.json"
            make_review_template(baseline_path, baseline_review_path)
            make_review_template(candidate_path, candidate_review_path)
            for review_path, score in ((baseline_review_path, 3), (candidate_review_path, 4)):
                review = json.loads(review_path.read_text(encoding="utf-8"))
                review["reviewer"] = "reviewer-1"
                for item in review["reviews"]:
                    for field in (
                        "evidence_support",
                        "key_fact_coverage",
                        "interpretation_calibration",
                        "uncertainty_calibration",
                    ):
                        item[field] = score
                review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(validate_review(baseline_review_path, baseline_path)["valid"])
            report = compare_runs(
                baseline_path,
                candidate_path,
                baseline_review_path=baseline_review_path,
                candidate_review_path=candidate_review_path,
            )
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["dataset"]["scenario_coverage"], 1.0)
        self.assertTrue(report["quality_evidence"])
        for metric in report["human_review"]["metrics"].values():
            self.assertEqual(metric["paired_count"], 24)
            self.assertEqual(metric["mean_delta_candidate_minus_baseline"], 1.0)
            self.assertEqual(metric["paired_wins"], 24)

    def test_real_plan_preflight_enforces_calls_input_tokens_output_and_worst_case_cost(self):
        input_ids = [message["id"] for message in self.scenario["messages"]]
        source_chars = sum(len(message["text"]) for message in self.scenario["messages"])
        plan = {
            "run_id": "planned-real-run",
            "dataset_id": self.dataset["dataset_id"],
            "dataset_sha256": self.dataset_hash,
            "condition": "candidate-context-v2",
            "evaluation_type": "real",
            "model": "configured-model",
            "prompt_version": "profile-v3",
            "system_prompt": "请根据聊天消息谨慎分析，并区分事实与推断。",
            "user_prompt": "概括本场景中的关键事实，说明不确定之处。",
            "input_strategy": {"name": "reply-neighbors", "version": "2"},
            "records": [{"scenario_id": self.scenario["id"], "input_message_ids": input_ids}],
            "limits": {
                "max_calls": 1,
                "max_input_chars": 10_000,
                "max_total_prompt_tokens": 10_000,
                "max_output_tokens": 20,
                "cost_budget": 0.021,
            },
            "prices": {"input_per_million": 2.0, "output_per_million": 8.0, "currency": "CNY"},
        }
        with tempfile.TemporaryDirectory() as root:
            plan_path = Path(root) / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            report = validate_plan(plan_path)
            plan["limits"]["cost_budget"] = 0.0003
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            over_budget = validate_plan(plan_path)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["planned_call_count"], 1)
        self.assertGreater(report["estimated_prompt_tokens"], 0)
        self.assertGreater(report["input_prompt_chars"], source_chars)
        self.assertEqual(report["worst_case_cost"], 0.02016)
        self.assertFalse(over_budget["valid"])
        self.assertTrue(any("最坏费用" in error for error in over_budget["errors"]))

    def test_real_plan_preflight_uses_full_prompt_and_rejects_each_exceeded_cap(self):
        plan = self._real_plan(scenario_count=2)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "plan.json"
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            baseline = validate_plan(path)
            self.assertTrue(baseline["valid"], baseline["errors"])

            plan["limits"]["max_calls"] = 1
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            calls = validate_plan(path)
            plan["limits"]["max_calls"] = 2

            plan["limits"]["max_input_chars"] = baseline["input_prompt_chars"] - 1
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            chars = validate_plan(path)
            plan["limits"]["max_input_chars"] = 100_000

            plan["limits"]["max_total_prompt_tokens"] = baseline["estimated_prompt_tokens"] - 1
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            tokens = validate_plan(path)

        self.assertFalse(calls["valid"])
        self.assertTrue(any("max_calls" in error for error in calls["errors"]))
        self.assertFalse(chars["valid"])
        self.assertTrue(any("max_input_chars" in error for error in chars["errors"]))
        self.assertFalse(tokens["valid"])
        self.assertTrue(any("max_total_prompt_tokens" in error for error in tokens["errors"]))

    def test_nonfinite_prices_and_cost_budgets_are_rejected(self):
        run = self._run(
            prices={"input_per_million": float("inf"), "output_per_million": 1.0, "currency": "CNY"}
        )
        with tempfile.TemporaryDirectory() as root:
            run_report = validate_run(self._write_run(run, root))
            plan_path = Path(root) / "plan.json"
            plan = self._real_plan()
            plan["limits"]["cost_budget"] = float("nan")
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            plan_report = validate_plan(plan_path)
        self.assertFalse(run_report["valid"])
        self.assertTrue(any("prices" in error for error in run_report["errors"]))
        self.assertFalse(plan_report["valid"])
        self.assertTrue(any("cost_budget" in error for error in plan_report["errors"]))

    def _real_plan(self, scenario_count=1):
        scenarios = self.dataset["scenarios"][:scenario_count]
        records = [
            {
                "scenario_id": scenario["id"],
                "input_message_ids": [message["id"] for message in scenario["messages"]],
            }
            for scenario in scenarios
        ]
        return {
            "run_id": "mocked-real-evaluation",
            "dataset_id": self.dataset["dataset_id"],
            "dataset_sha256": self.dataset_hash,
            "condition": "candidate",
            "evaluation_type": "real",
            "model": "mock-model",
            "prompt_version": "prompt-v1",
            "system_prompt": "请根据聊天消息谨慎分析。",
            "user_prompt": "概括事实并说明不确定之处。",
            "input_strategy": {"name": "all-selected-messages", "version": "1"},
            "records": records,
            "limits": {
                "max_calls": scenario_count,
                "max_input_chars": 100_000,
                "max_total_prompt_tokens": 100_000,
                "max_output_tokens": 32,
                "cost_budget": 1.0,
            },
            "prices": {"input_per_million": 1.0, "output_per_million": 1.0, "currency": "CNY"},
        }

    def test_real_runner_requires_explicit_opt_in_and_never_retries(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                from types import SimpleNamespace

                marker = "输入消息（JSON 数组；按时间顺序）：\n"
                prompt_messages = json.loads(kwargs["messages"][1]["content"].split(marker, 1)[1])
                evidence_message = prompt_messages[0]
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "answer": "青禾负责标题。",
                                        "claims": [
                                            {
                                                "type": "fact",
                                                "text": "青禾负责标题。",
                                                "speaker": "青禾",
                                                "evidence_ids": [evidence_message["id"]],
                                                "quote": evidence_message["text"],
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=200, completion_tokens=20),
                )

        with tempfile.TemporaryDirectory() as root:
            plan_path = Path(root) / "plan.json"
            output_path = Path(root) / "run.json"
            plan_path.write_text(json.dumps(self._real_plan(), ensure_ascii=False), encoding="utf-8")
            completions = FakeCompletions()
            client = type(
                "FakeClient",
                (),
                {"chat": type("Chat", (), {"completions": completions})()},
            )()
            client_factory = mock.Mock(return_value=client)
            with self.assertRaisesRegex(ValueError, "execute_real=True"):
                execute_real_plan(
                    plan_path,
                    output_path,
                    api_key="synthetic-key",
                    client_factory=client_factory,
                )
            self.assertFalse(output_path.exists())
            client_factory.assert_not_called()

            report = execute_real_plan(
                plan_path,
                output_path,
                execute_real=True,
                api_key="synthetic-key",
                base_url="https://api.example.test/v1",
                client_factory=client_factory,
            )
            run = json.loads(output_path.read_text(encoding="utf-8"))
            output_text = output_path.read_text(encoding="utf-8")

        self.assertTrue(report["valid"], report)
        self.assertEqual(run["execution_status"], "complete")
        self.assertEqual(run["execution"]["calls_attempted"], 1)
        self.assertEqual(len(completions.calls), 1)
        self.assertEqual(completions.calls[0]["max_tokens"], 32)
        request_text = "\n".join(item["content"] for item in completions.calls[0]["messages"])
        self.assertNotIn(self.scenario["id"], request_text)
        self.assertNotIn(self.scenario["category"], request_text)
        self.assertNotIn(self.scenario["messages"][0]["id"], request_text)
        self.assertEqual(run["records"][0]["claims"][0]["evidence_ids"], [self.scenario["messages"][0]["id"]])
        self.assertEqual(
            run["records"][0]["model_result"]["claims"][0]["evidence_ids"],
            [self.scenario["messages"][0]["id"]],
        )
        self.assertNotIn("synthetic-key", output_text)

    def test_unknown_candidate_evidence_alias_fails_and_stops_the_run(self):
        from types import SimpleNamespace

        class UnknownAliasCompletions:
            def __init__(self, unknown_id):
                self.unknown_id = unknown_id
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps(
                                    {
                                        "answer": "合成回答",
                                        "claims": [
                                            {
                                                "type": "fact",
                                                "text": "合成事实",
                                                "evidence_ids": [self.unknown_id],
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
                )

        with tempfile.TemporaryDirectory() as root:
            plan_path = Path(root) / "plan.json"
            output_path = Path(root) / "run.json"
            plan_path.write_text(
                json.dumps(self._real_plan(scenario_count=2), ensure_ascii=False), encoding="utf-8"
            )
            completions = UnknownAliasCompletions(self.scenario["messages"][0]["id"])
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            report = execute_real_plan(
                plan_path,
                output_path,
                execute_real=True,
                api_key="synthetic-key",
                client_factory=lambda **_kwargs: client,
            )
            run = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertFalse(report["valid"])
        self.assertEqual(run["execution_status"], "incomplete")
        self.assertEqual(run["execution"]["stop_reason"], "invalid_response_or_usage")
        self.assertEqual(len(completions.calls), 1)
        self.assertIn("未知输入消息别名", run["records"][0]["execution_error"])

    def test_real_runner_stops_after_actual_prompt_token_limit_is_exceeded(self):
        from types import SimpleNamespace

        plan = self._real_plan(scenario_count=2)
        with tempfile.TemporaryDirectory() as root:
            plan_path = Path(root) / "plan.json"
            output_path = Path(root) / "partial-run.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            estimate = validate_plan(plan_path)["estimated_prompt_tokens"]
            plan["limits"]["max_total_prompt_tokens"] = estimate
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            completions = mock.Mock(
                create=mock.Mock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(message=SimpleNamespace(content='{"answer":"ok","claims":[]}'))
                        ],
                        usage=SimpleNamespace(prompt_tokens=estimate + 1, completion_tokens=1),
                    )
                )
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            report = execute_real_plan(
                plan_path,
                output_path,
                execute_real=True,
                api_key="synthetic-key",
                client_factory=lambda **_kwargs: client,
            )
            run = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertFalse(report["valid"])
        self.assertEqual(report["execution_status"], "incomplete")
        self.assertEqual(completions.create.call_count, 1)
        self.assertEqual(run["execution"]["stop_reason"], "max_total_prompt_tokens_exceeded")
        self.assertEqual(run["execution"]["calls_attempted"], 1)

    def test_real_runner_passes_output_cap_and_stops_if_provider_exceeds_it(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as root:
            plan_path = Path(root) / "plan.json"
            output_path = Path(root) / "over-output.json"
            plan_path.write_text(json.dumps(self._real_plan(), ensure_ascii=False), encoding="utf-8")
            completions = mock.Mock(
                create=mock.Mock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(message=SimpleNamespace(content='{"answer":"ok","claims":[]}'))
                        ],
                        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=33),
                    )
                )
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            report = execute_real_plan(
                plan_path,
                output_path,
                execute_real=True,
                api_key="synthetic-key",
                client_factory=lambda **_kwargs: client,
            )
            run = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertFalse(report["valid"])
        self.assertEqual(completions.create.call_args.kwargs["max_tokens"], 32)
        self.assertEqual(run["execution_status"], "incomplete")
        self.assertEqual(run["execution"]["stop_reason"], "max_output_tokens_exceeded")

    def test_compare_runs_rejects_different_scenario_sets_and_unpaired_reviews(self):
        baseline = self._run()
        candidate = self._run()
        baseline.update(run_id="baseline", condition="baseline")
        candidate.update(run_id="candidate", condition="candidate")
        candidate["records"][0]["scenario_id"] = self.dataset["scenarios"][1]["id"]
        with tempfile.TemporaryDirectory() as root:
            baseline_path = self._write_run(baseline, root)
            candidate_path = Path(root) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            report = compare_runs(
                baseline_path,
                candidate_path,
                baseline_review_path=baseline_path,
            )
        self.assertFalse(report["valid"])
        self.assertTrue(any("相同且非空" in error for error in report["errors"]))
        self.assertTrue(any("必须同时提供" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
