from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ai import parsed

from remindly.ai.contracts import GeneratedResponse, ProviderError
from scripts import groq_live_eval as evaluation


class GroqLiveEvaluationTest(unittest.TestCase):
    def test_new_live_recordings_replay_without_unnecessary_questions(self):
        for case_file, recording_file, count in (
            ("groq_live_cases.jsonl", "groq_live_results_2026-11-04.jsonl", 52),
            (
                "groq_exploration_2026-10-31.jsonl",
                "groq_exploration_results_2026-11-04.jsonl",
                30,
            ),
            (
                "groq_exploration_2026-10-31.jsonl",
                "groq_targeted_drift_results_2026-11-04.jsonl",
                4,
            ),
        ):
            cases = {
                case["id"]: case
                for case in map(
                    json.loads, evaluation.CASES.with_name(case_file).read_text().splitlines()
                )
            }
            rows = [
                row
                for row in map(
                    json.loads,
                    evaluation.CASES.with_name(recording_file).read_text().splitlines(),
                )
                if row["event"] == "result"
            ]
            self.assertEqual(count, len(rows))
            for row in rows:
                with self.subTest(recording=recording_file, case=row["id"], run=row["run"]):
                    self.assertEqual(cases[row["id"]]["expected"], row["expected"])
                    generator = evaluation.RecordingGenerator("fake-key")
                    with patch.object(generator.provider, "generate", return_value=row["raw"]):
                        result = evaluation.evaluate(cases[row["id"]], generator)
                    self.assertEqual({}, result["local_failures"])
                    self.assertEqual(row["raw"], result["raw"])
                    self.assertEqual(row["raw_failures"], result["raw_failures"])

    def test_exploration_inputs_and_expectations_remain_frozen(self):
        fixture = evaluation.CASES.with_name("groq_exploration_2026-10-31.jsonl")
        self.assertEqual(
            "7cfc8ba038fe77bd42046ffc005ead263b84fd14ac2d4d004f1fc86ff4b5be9b",
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
        )
        cases = list(map(json.loads, fixture.read_text().splitlines()))
        existing = list(map(json.loads, evaluation.CASES.read_text().splitlines()))
        self.assertEqual(20, len(cases))
        self.assertEqual(30, sum(case["repeats"] for case in cases))
        self.assertFalse({case["text"] for case in cases} & {case["text"] for case in existing})
        for case in cases:
            self.assertEqual("human-designed-synthetic", case["origin"])
            self.assertIn("intent", case["expected"])
            self.assertIn("route", case["expected"])

    def case(self):
        return {
            "id": "test",
            "source": "test",
            "text": "提醒我明天下午三點買湯",
            "now": "2026-10-30T12:00:00+08:00",
            "timezone": "Asia/Taipei",
            "repeats": 1,
            "expected": {
                "title": "買湯",
                "at": "2026-10-31T15:00:00+08:00",
                "missing": [],
                "complete": True,
                "participants": ["user:7"],
                "recurrence": None,
                "intent": "create",
                "route": "confirmation",
            },
        }

    def test_suite_has_distinct_sources_and_repeats_drift_cases(self):
        cases = [json.loads(line) for line in evaluation.CASES.read_text().splitlines()]
        self.assertEqual(36, len(cases))
        self.assertEqual(36, len({case["id"] for case in cases}))
        self.assertEqual(52, sum(case["repeats"] for case in cases))
        self.assertEqual(8, sum(case["repeats"] == 3 for case in cases))
        for case in cases:
            source_file, source_id = case["source"].split("#")
            source = [
                json.loads(line)
                for line in (evaluation.CASES.parent / source_file).read_text().splitlines()
            ]
            self.assertIn(source_id, {row["id"] for row in source})

    def test_raw_failure_is_preserved_when_local_rules_repair_it(self):
        generator = evaluation.RecordingGenerator("fake-key")
        with patch.object(
            generator.provider,
            "generate",
            return_value=parsed(title="模型改寫", date="2027-01-01", time="15:00"),
        ):
            result = evaluation.evaluate(self.case(), generator)
        self.assertIn("title", result["raw_failures"])
        self.assertIn("at", result["raw_failures"])
        self.assertEqual({}, result["local_failures"])
        self.assertEqual("模型改寫", result["raw"]["title"])

    def test_wrong_intent_cannot_pass_with_correct_reminder_fields(self):
        generator = evaluation.RecordingGenerator("fake-key")
        with patch.object(
            generator.provider,
            "generate",
            return_value=parsed(
                intent="not_reminder", title="買湯", date="2026-10-31", time="15:00"
            ),
        ):
            result = evaluation.evaluate(self.case(), generator)
        self.assertIn("intent", result["raw_failures"])
        self.assertIn("route", result["raw_failures"])

    def test_actual_router_ignores_chat_and_catches_invented_reminder(self):
        case = {
            **self.case(),
            "text": "多謝啦",
            "expected": {
                "intent": "not_reminder",
                "route": "ignore",
                "complete": False,
            },
        }
        generator = evaluation.RecordingGenerator("fake-key")
        ignored = parsed(
            intent="not_reminder", title=None, date=None, time=None, participant_indices=[]
        )
        with patch.object(generator.provider, "generate", return_value=ignored):
            result = evaluation.evaluate(case, generator)
        self.assertEqual({}, result["off_failures"])
        self.assertEqual({}, result["local_failures"])
        with patch.object(
            generator.provider,
            "generate",
            return_value=parsed(
                title="買湯",
                date="2026-10-31",
                time="15:00",
            ),
        ):
            result = evaluation.evaluate(case, generator)
        self.assertIn("route", result["local_failures"])
        self.assertEqual("confirmation", result["local_result"]["route"])

    def test_recorded_model_added_examples_remain_correct_offline(self):
        cases = {
            case["id"]: case
            for case in map(
                json.loads,
                evaluation.CASES.with_name("groq_exploration_2026-10-31.jsonl")
                .read_text()
                .splitlines(),
            )
        }
        analysis = json.loads(
            evaluation.CASES.with_name("groq_exploration_ablation_2026-10-31.json").read_text()
        )
        added = {row["id"] for row in analysis["summary"]["attributable_gains"]}
        self.assertEqual(7, len(added))
        records = map(
            json.loads,
            evaluation.CASES.with_name("groq_exploration_results_2026-10-31.jsonl")
            .read_text()
            .splitlines(),
        )
        covered = set()
        for row in records:
            if row["event"] != "result" or row["id"] not in added:
                continue
            with self.subTest(case=row["id"], run=row["run"]):
                generator = evaluation.RecordingGenerator("fake-key")
                with patch.object(generator.provider, "generate", return_value=row["raw"]):
                    result = evaluation.evaluate(cases[row["id"]], generator)
                self.assertEqual({}, result["local_failures"])
                covered.add(row["id"])
        self.assertEqual(added, covered)

    def test_guards_preserve_all_recorded_correct_exploration_results(self):
        cases = {
            case["id"]: case
            for case in map(
                json.loads,
                evaluation.CASES.with_name("groq_exploration_2026-10-31.jsonl")
                .read_text()
                .splitlines(),
            )
        }
        target_ids = {"quarter-to-three", "ten-past-eight", "dusk", "tomorrow-de-evening"}
        covered = set()
        checked = 0
        for row in map(
            json.loads,
            evaluation.CASES.with_name("groq_exploration_results_2026-10-31.jsonl")
            .read_text()
            .splitlines(),
        ):
            if row["event"] != "result" or row["raw_failures"]:
                continue
            with self.subTest(case=row["id"], run=row["run"]):
                generator = evaluation.RecordingGenerator("fake-key")
                with patch.object(generator.provider, "generate", return_value=row["raw"]):
                    result = evaluation.evaluate(cases[row["id"]], generator)
                self.assertEqual({}, result["raw_failures"])
                self.assertEqual({}, result["local_failures"])
                self.assertEqual(row["raw"], result["raw"])
                checked += 1
                if row["id"] in target_ids:
                    covered.add(row["id"])
        self.assertEqual(target_ids, covered)
        self.assertEqual(28, checked)

    def test_recorded_weekday_and_new_reminder_intent_are_constrained(self):
        cases = {
            case["id"]: case
            for case in map(
                json.loads,
                evaluation.CASES.with_name("groq_exploration_2026-10-31.jsonl")
                .read_text()
                .splitlines(),
            )
        }
        covered = set()
        for row in map(
            json.loads,
            evaluation.CASES.with_name("groq_exploration_results_2026-10-31.jsonl")
            .read_text()
            .splitlines(),
        ):
            if row["event"] != "result" or row["id"] not in {"this-sunday", "replace-draft"}:
                continue
            with self.subTest(case=row["id"]):
                generator = evaluation.RecordingGenerator("fake-key")
                with patch.object(generator.provider, "generate", return_value=row["raw"]):
                    result = evaluation.evaluate(cases[row["id"]], generator)
                self.assertEqual({}, result["local_failures"])
                self.assertEqual(row["raw"], result["raw"])
                self.assertTrue(result["raw_failures"])
                covered.add(row["id"])
        self.assertEqual({"this-sunday", "replace-draft"}, covered)

    def test_recorded_live_responses_satisfy_local_regression_expectations(self):
        cases = {
            case["id"]: case for case in map(json.loads, evaluation.CASES.read_text().splitlines())
        }
        recording = evaluation.CASES.with_name("groq_live_results_2026-10-30.jsonl")
        rows = [
            row
            for row in map(json.loads, recording.read_text().splitlines())
            if row["event"] == "result"
        ]
        self.assertEqual(52, len(rows))
        self.assertEqual(52, len({(row["id"], row["run"]) for row in rows}))
        for row in rows:
            with self.subTest(case=row["id"], run=row["run"]):
                generator = evaluation.RecordingGenerator("fake-key")
                with patch.object(generator.provider, "generate", return_value=row["raw"]):
                    result = evaluation.evaluate(cases[row["id"]], generator)
                self.assertEqual({}, result["local_failures"])
                if row["id"] == "reminder-title-period":
                    # 私聊關閉 AI 時，沒有「提醒」關鍵字的文字不會建立草稿。
                    self.assertEqual("ignore", result["off_result"]["route"])
                else:
                    self.assertEqual({}, result["off_failures"])

    def test_quota_cooldown_and_resume_do_not_repeat_completed_calls(self):
        case = self.case()
        calls = []

        class FakeGenerator:
            raw = None

            def enabled(self, user_id):
                return True

            def generate(self, *args, **kwargs):
                calls.append(True)
                if len(calls) == 1:
                    raise ProviderError("quota", 120)
                self.raw = parsed(title="買湯", date="2026-10-31", time="15:00")
                return GeneratedResponse(self.raw, "groq", "test")

        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "cases.jsonl"
            suite.write_text(json.dumps(case) + "\n")
            output = Path(directory) / "results.jsonl"
            with (
                patch.object(evaluation, "CASES", suite),
                patch.object(evaluation, "RecordingGenerator", return_value=FakeGenerator()),
                patch.object(evaluation, "wait_until") as wait,
                patch.dict("os.environ", {"GROQ_API_KEY": "fake-key"}),
                patch("sys.argv", ["eval", "--output", str(output)]),
            ):
                evaluation.main()
                evaluation.main()
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(["meta", "retry", "result"], [row["event"] for row in rows])
            self.assertEqual(120, rows[1]["cooldown"])
            wait.assert_called_once_with(rows[1]["retry_at"])
            self.assertEqual(2, len(calls))
            self.assertNotIn("fake-key", output.read_text())
