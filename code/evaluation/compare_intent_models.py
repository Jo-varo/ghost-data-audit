"""Compare request-intent classification between two Claude models on the same request texts.

Usage:  python code/evaluation/compare_intent_models.py [BASELINE_MODEL] [CANDIDATE_MODEL]
Defaults: claude-opus-5-5 vs claude-sonnet-5-5. Texts are batched exactly as the pipeline batches them
(evaluation set, sample set), so cached pipeline responses are reused. Responses are cached.
Writes code/evaluation/intent_model_comparison.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from intent import authorizes, classify_requests_detailed  # noqa: E402
from llm import LLM  # noqa: E402
from loaders import DATASET, read_csv  # noqa: E402

SETS = {"evaluation": "deletion_requests.csv", "sample": "sample_deletion_requests.csv"}


def main():
    base_model = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5-5"
    cand_model = sys.argv[2] if len(sys.argv) > 2 else "claude-sonnet-5-5"
    base = LLM(models={"intent_classification": base_model})
    cand = LLM(models={"intent_classification": cand_model})

    summary = {"baseline": base_model, "candidate": cand_model, "sets": {}}
    for name, filename in SETS.items():
        requests = read_csv(DATASET / filename)
        texts = [r["request_text"] for r in requests]
        a = classify_requests_detailed(base, texts, name)
        b = classify_requests_detailed(cand, texts, name)
        decision_diff, type_diff = [], []
        for t in sorted(a):
            la, lb = a[t], b[t]
            if authorizes(la) != authorizes(lb):
                decision_diff.append(t)
            elif (la or {}).get("request_type") != (lb or {}).get("request_type"):
                type_diff.append(t)
        cases_by_text = {}
        for r in requests:
            cases_by_text.setdefault(r["request_text"], []).append(r["case_id"])
        not_applicable = {m: sorted(c for t, lab in labels.items() if not authorizes(lab) for c in cases_by_text[t])
                          for m, labels in ((base_model, a), (cand_model, b))}
        summary["sets"][name] = {
            "requests": len(requests), "distinct_texts": len(a),
            "same_decision_texts": len(a) - len(decision_diff),
            "same_label_texts": len(a) - len(decision_diff) - len(type_diff),
            "decision_differences": [{"text": t, "cases": cases_by_text[t], base_model: a[t], cand_model: b[t]}
                                     for t in decision_diff],
            "request_type_differences": [{"text": t, "cases": cases_by_text[t], base_model: a[t], cand_model: b[t]}
                                         for t in type_diff],
            "not_applicable_cases": not_applicable,
        }
        s = summary["sets"][name]
        print(f"[{name}] requests={s['requests']} distinct texts={s['distinct_texts']} "
              f"same deletion decision={s['same_decision_texts']}/{s['distinct_texts']} "
              f"same label={s['same_label_texts']}/{s['distinct_texts']}")
        for m, cs in not_applicable.items():
            print(f"     NOT_APPLICABLE by {m}: {len(cs)} {cs}")
        for d in s["decision_differences"] + s["request_type_differences"]:
            print(f"     {d['cases'][:3]} {d['text'][:90]!r}\n         {base_model}={d[base_model]}\n         {cand_model}={d[cand_model]}")

    summary["usage"] = {m: llm.usage_rows() for m, llm in ((base_model, base), (cand_model, cand))}
    (HERE / "intent_model_comparison.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                      encoding="utf-8")
    for m, rows in summary["usage"].items():
        for r in rows:
            print(f"  {m}: calls={r['calls']} cached={r['cached_calls']} in={r['input_tokens']} "
                  f"out={r['output_tokens']} ${r['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
