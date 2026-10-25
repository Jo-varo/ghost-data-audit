"""Participant-built evaluation cases.

Each custom case is derived from a labelled sample case by changing one property that must
(or must not) change the answer, so its expected output follows from the sample label:

  custom_01  access/copy request from a real subject        -> NOT_APPLICABLE, []
  custom_02  same subject addressed by its current email      -> identical to the sample answer
             (the sample used the historical email)
  custom_03  German-language deletion request                 -> identical to the sample answer
  custom_04  upper-cased email + Spanish request text         -> identical to the sample answer

Usage:  python code/evaluation/custom_cases.py [--offline]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from llm import LLM  # noqa: E402
from loaders import DATASET, read_csv  # noqa: E402
from main import build_context, run  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("scorer", HERE / "main.py")
_scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scorer)
score = _scorer.score

CUSTOM = [
    ("custom_01", "case_24", {"request_text": "Could you send me a copy of all the personal data you hold about me?"},
     "NOT_APPLICABLE"),
    ("custom_02", "case_29", {"email": "noah.f4380@outlook.com"}, None),
    ("custom_03", "case_15", {"request_text": "Bitte löschen Sie mein Konto und alle damit verbundenen "
                                              "personenbezogenen Daten dauerhaft."}, None),
    ("custom_04", "case_20", {"email": "AKOSUA.GYASI@GMAIL.COM",
                              "request_text": "Solicito la eliminación permanente de mi cuenta y de mis datos."},
     None),
]


def main():
    offline = "--offline" in sys.argv
    llm = LLM(offline=offline)
    identities, evidence, activities = build_context(llm)
    sample = {r["case_id"]: r for r in read_csv(DATASET / "sample_deletion_requests.csv")}
    gold = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
            for r in read_csv(DATASET / "sample_output.csv")}
    all_requests = list(sample.values()) + read_csv(DATASET / "deletion_requests.csv")

    requests = [{**sample[base], **changes, "case_id": cid} for cid, base, changes, _ in CUSTOM]
    expected = {cid: (forced, []) if forced else gold[base] for cid, base, _, forced in CUSTOM}
    # Same execution flow as code/main.py; each variant is audited with the workflow that served its base case.
    rows = run(requests, all_requests, llm, identities, evidence, activities, label="custom",
               workflow_case={cid: base for cid, base, _, _ in CUSTOM})
    pred = {cid: (status, steps) for cid, status, steps in rows}

    report, failures = score(pred, expected, verbose=True)
    for cid, *_ in CUSTOM:
        ok = pred[cid][0] == expected[cid][0] and \
            [json.dumps(s, sort_keys=True) for s in pred[cid][1]] == [json.dumps(s, sort_keys=True) for s in expected[cid][1]]
        print(f"{cid}: expected={expected[cid][0]} predicted={pred[cid][0]} exact={'yes' if ok else 'no'}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
