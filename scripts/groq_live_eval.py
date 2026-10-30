from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.ai.contracts import AiUnavailable, GeneratedResponse, ProviderError
from remindly.ai.groq import GroqProvider
from remindly.ai.parser import SCHEMA, SYSTEM, SemanticParser, validate_result
from remindly.reminders.models import MentionKind, ParseResult, Participant, ReminderDraft
from remindly.settings import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests/fixtures/groq_live_cases.jsonl"
PERSON = Participant("Orange", MentionKind.TEXT_MENTION, user_id=7, username="orange")
ALICE = Participant("Alice", MentionKind.TEXT_MENTION, user_id=8, username="alice")


def summarize(result: ParseResult) -> dict:
    return {
        "title": result.title,
        "at": result.remind_at.isoformat() if result.remind_at else None,
        "missing": sorted(result.missing_fields),
        "complete": bool(result.title and result.remind_at and not result.missing_fields),
        "participants": sorted(p.stable_key for p in result.participants),
        "recurrence": json.loads(json.dumps(asdict(result.recurrence)))
        if result.recurrence
        else None,
        "question": result.raw.get("question"),
        "intent": result.raw.get("intent"),
    }


def differences(actual: dict, expected: dict) -> dict:
    failures = {}
    for field, wanted in expected.items():
        value = actual.get(field)
        if field == "recurrence" and wanted is not None and isinstance(value, dict):
            value = {key: value.get(key) for key in wanted}
        if field in {"missing", "participants"}:
            wanted = sorted(wanted)
        if value != wanted:
            failures[field] = {"expected": wanted, "actual": value}
    return failures


def context_for(case: dict) -> ReminderDraft | None:
    seed = case.get("seed")
    if not seed:
        return None
    return ReminderDraft(
        id="seed",
        chat_id=100,
        chat_type="private",
        creator_user_id=7,
        timezone=case["timezone"],
        source_text=seed["source"],
        title=seed["title"],
        remind_at=datetime.fromisoformat(seed["at"]) if seed.get("at") else None,
        participants=[PERSON],
        missing_fields=seed["missing"] + (["clarification"] if seed.get("condition") else []),
        parse_result={
            "grain": "ai",
            "question": seed.get("question"),
            "condition_question": seed.get("question"),
        },
    )


class RecordingGenerator:
    def __init__(self, key: str):
        self.key = key
        self.provider = GroqProvider()
        self.raw = None

    def enabled(self, user_id: int) -> bool:
        return True

    def generate(self, user_id, system, prompt, schema):
        self.raw = self.provider.generate(self.key, system, prompt, schema)
        return GeneratedResponse(self.raw, self.provider.id, self.provider.model)


def evaluate(case: dict, generator: RecordingGenerator) -> dict:
    now = datetime.fromisoformat(case["now"]).astimezone(ZoneInfo(case["timezone"]))
    participants = [PERSON, ALICE] if case.get("alice") else [PERSON]
    generator.raw = None
    local_error = None
    try:
        local = summarize(
            SemanticParser(generator).parse(
                case["text"], 7, now, participants, context=context_for(case)
            )
        )
    except AiUnavailable:
        local, local_error = None, "validation"
    raw_error = None
    try:
        raw = summarize(validate_result(generator.raw, now, participants))
    except (ValueError, TypeError, KeyError, OverflowError):
        raw, raw_error = None, "validation"
    return {
        "raw": generator.raw,
        "raw_result": raw,
        "local_result": local,
        "raw_failures": differences(raw, case["expected"]) if raw else {"error": raw_error},
        "local_failures": differences(local, case["expected"]) if local else {"error": local_error},
    }


def wait_until(deadline: float) -> None:
    while (remaining := deadline - time.time()) > 0:
        print(f"cooldown: {remaining:.0f}s remaining", flush=True)
        time.sleep(min(30, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Groq live regression evaluation; uses personal quota"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Append-only JSONL; resumes this run"
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise SystemExit("GROQ_API_KEY missing")
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    digest = hashlib.sha256(CASES.read_bytes() + SYSTEM.encode() + json.dumps(SCHEMA).encode())
    for relative in (
        "ai/groq.py",
        "ai/parser.py",
        "reminders/parser.py",
        "reminders/recurrence.py",
    ):
        digest.update((ROOT / "src/remindly" / relative).read_bytes())
    meta = {
        "event": "meta",
        "signature": digest.hexdigest(),
        "model": GroqProvider.model,
        "cases": len(cases),
        "requests": sum(case["repeats"] for case in cases),
    }
    previous = (
        [json.loads(line) for line in args.output.read_text().splitlines()]
        if args.output.exists()
        else []
    )
    if previous and previous[0] != meta:
        raise SystemExit("Suite or code changed; choose a new output path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    generator = RecordingGenerator(key)
    with args.output.open("a") as stream:

        def save(row):
            stream.write(json.dumps(row, ensure_ascii=False).replace(key, "[REDACTED]") + "\n")
            stream.flush()

        if not previous:
            save(meta)
        done = {(row["id"], row["run"]) for row in previous if row["event"] == "result"}
        if previous and previous[-1]["event"] == "retry":
            wait_until(previous[-1]["retry_at"])
        for case in cases:
            for run in range(1, case["repeats"] + 1):
                if (case["id"], run) in done:
                    continue
                while True:
                    started = time.monotonic()
                    try:
                        result = evaluate(case, generator)
                    except ProviderError as exc:
                        row = {
                            "event": "retry",
                            "id": case["id"],
                            "run": run,
                            "kind": exc.kind,
                            "cooldown": exc.cooldown,
                            "retry_at": time.time() + exc.cooldown,
                            "seconds": round(time.monotonic() - started, 3),
                        }
                        if exc.kind not in {"quota", "rate", "daily_quota"}:
                            row["event"] = "fatal"
                            save(row)
                            raise SystemExit(f"Stopped: {exc.kind}; progress saved") from None
                        save(row)
                        print(f"429 {exc.kind}: cooling down {exc.cooldown}s", flush=True)
                        wait_until(row["retry_at"])
                        continue
                    row = {
                        "event": "result",
                        "id": case["id"],
                        "run": run,
                        "source": case["source"],
                        "text": case["text"],
                        "now": case["now"],
                        "expected": case["expected"],
                        "seconds": round(time.monotonic() - started, 3),
                        **result,
                    }
                    save(row)
                    done.add((case["id"], run))
                    print(
                        f"{len(done)}/{meta['requests']} {case['id']} #{run}: "
                        f"raw={'FAIL' if result['raw_failures'] else 'PASS'} "
                        f"local={'FAIL' if result['local_failures'] else 'PASS'} "
                        f"{row['seconds']}s",
                        flush=True,
                    )
                    break


if __name__ == "__main__":
    main()
