from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ai import parsed

from remindly.ai.contracts import GeneratedResponse, ProviderError
from scripts import groq_live_eval as evaluation


class GroqLiveEvaluationTest(unittest.TestCase):
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

    def test_quota_cooldown_and_resume_do_not_repeat_completed_calls(self):
        case = self.case()
        calls = []

        class FakeGenerator:
            raw = None

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
