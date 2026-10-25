"""Score predictions against labelled cases.

Usage:
    python code/evaluation/main.py                       # sample_predictions.csv vs dataset/sample_output.csv
    python code/evaluation/main.py PRED.csv GOLD.csv [-v]
"""
from __future__ import annotations

import csv
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def key(s):
    return tuple(s[f] for f in FIELDS)


def score(pred, gold, verbose=False):
    n = len(gold)
    status_ok = exact = 0
    tp = fp = fn = 0
    field_hits = {f: 0 for f in FIELDS}
    aligned = 0
    seq_ratio = 0.0
    failures = []
    for cid, (gs, gsteps) in sorted(gold.items()):
        ps, psteps = pred.get(cid, ("MISSING", []))
        status_ok += ps == gs
        G, P = [key(s) for s in gsteps], [key(s) for s in psteps]
        exact += ps == gs and G == P
        gset, pset = set(G), set(P)
        tp += len(gset & pset)
        fp += len(pset - gset)
        fn += len(gset - pset)
        # field-level accuracy on steps aligned by (system_id, resource_id, phase)
        pidx = {(s["phase"], s["system_id"], s["resource_id"]): s for s in psteps}
        for s in gsteps:
            p = pidx.get((s["phase"], s["system_id"], s["resource_id"]))
            aligned += 1
            for f in FIELDS:
                field_hits[f] += bool(p) and p[f] == s[f]
        seq_ratio += SequenceMatcher(a=G, b=P, autojunk=False).ratio() if (G or P) else 1.0
        if ps != gs or G != P:
            failures.append((cid, gs, ps, sorted(gset - pset), sorted(pset - gset)))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    report = {
        "cases": n,
        "status_accuracy": status_ok / n,
        "exact_case_accuracy": exact / n,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": {f: field_hits[f] / aligned for f in FIELDS} if aligned else {},
        "sequence_similarity": seq_ratio / n,
    }
    if verbose:
        for cid, gs, ps, missing, extra in failures:
            print(f"--- {cid}: gold={gs} pred={ps}")
            for m in missing:
                print("    missing", m)
            for e in extra:
                print("    extra  ", e)
    return report, failures


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    pred_path = Path(args[0]) if args else ROOT / "code" / "evaluation" / "sample_predictions.csv"
    gold_path = Path(args[1]) if len(args) > 1 else ROOT / "dataset" / "sample_output.csv"
    report, _ = score(load(pred_path), load(gold_path), verbose="-v" in sys.argv)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
