"""Offline test of the intent-classification retry and the unclassified-intent reporting.

A fake LLM answers the batched intent prompts but drops one request text:
  1. dropped only in the first batch  -> the silent retry recovers it, nothing is reported;
  2. dropped on every attempt         -> a console WARNING names the case and the report lists it.
No API calls are made and the real evaluation_report.md is not touched (a temporary copy is used).

Usage:  python code/evaluation/test_intent_fallback.py
"""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import intent  # noqa: E402
import main  # noqa: E402
from loaders import DATASET, read_csv  # noqa: E402

ACCESS_TEXT = "Can you tell me what personal information you hold about me?"


class FakeLLM:
    """Answers intent batches locally; omits `dropped` from the first answer, or from every answer."""

    def __init__(self, dropped: str, drop_always: bool):
        self.dropped, self.drop_always, self.sources = dropped, drop_always, []

    def json_call(self, purpose, system, content, schema, max_tokens=8000, effort="low", source=""):
        assert purpose == "intent_classification"
        self.sources.append(source)
        results = []
        for line in content[0]["text"].split("\n"):
            idx, text = re.match(r"\[(\d+)\] (.*)", line).groups()
            if text == self.dropped and (self.drop_always or "retry" not in source):
                continue
            access = text == ACCESS_TEXT
            results.append({"id": idx, "request_type": "ACCESS" if access else "DELETION",
                            "authorizes_deletion": not access})
        return {"results": results}


def run_scenario(requests, dropped, drop_always, report_path):
    llm = FakeLLM(dropped, drop_always)
    labels = intent.classify_requests_detailed(llm, [r["request_text"] for r in requests], "test")
    missing = set(intent.unclassified(labels))
    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        main.report_unclassified([r for r in requests if r["request_text"] in missing], "test", update_report=True)
    return llm, labels, console.getvalue(), report_path.read_text(encoding="utf-8")


def main_test() -> int:
    requests = read_csv(DATASET / "sample_deletion_requests.csv")[:5]
    requests[2] = {**requests[2], "request_text": ACCESS_TEXT}
    dropped_case = requests[0]
    failures = []

    def check(cond, msg):
        print(("  PASS " if cond else "  FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "evaluation_report.md"
        shutil.copy(main.REPORT_PATH, report)
        real_report = main.REPORT_PATH
        main.REPORT_PATH = report  # never write the real report
        try:
            print("scenario 1: text dropped only in the first batch")
            llm, labels, console, text = run_scenario(requests, dropped_case["request_text"], False, report)
            check(any("retry 1" in s for s in llm.sources), "a retry batch was sent")
            check(not intent.unclassified(labels), "every text is classified after the retry")
            check("WARNING" not in console, "nothing printed to the console")
            check("None: every request received an intent classification" in text, "report section says None")
            check(not intent.authorizes(labels[ACCESS_TEXT]), "access request stays NOT_APPLICABLE")

            print("scenario 2: text dropped on every attempt")
            llm, labels, console, text = run_scenario(requests, dropped_case["request_text"], True, report)
            check(sum("retry" in s for s in llm.sources) == intent.RETRIES, "retried exactly RETRIES time(s)")
            check(intent.unclassified(labels) == [dropped_case["request_text"]], "the dropped text stays unclassified")
            check("WARNING" in console and dropped_case["case_id"] in console, "console warning names the case")
            check(f"| {dropped_case['case_id']} |" in text, "report table lists the case")
            check(intent.authorizes(labels[dropped_case["request_text"]]), "unclassified case is audited (fail-safe)")
        finally:
            main.REPORT_PATH = real_report
    print("OK" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_test())
