"""Compare image/PDF extraction between two Claude models on the same evidence files.

Usage:  python code/evaluation/compare_media_models.py [BASELINE_MODEL] [CANDIDATE_MODEL]
Defaults: claude-opus-5-5 vs claude-sonnet-5-5. Responses are cached, so reruns are free.
Writes code/evaluation/media_model_comparison.json with per-file differences.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from extract import load_manifest  # noqa: E402
from llm import LLM  # noqa: E402
from loaders import DATASET  # noqa: E402
from media import SCHEMA, SYSTEM, _content, source_label  # noqa: E402

FIELDS = ["record_kind", "exists", "emails", "phones", "names", "user_refs", "email_hashes",
          "data_flow_enabled", "restore_allowed", "related_resource_ids"]


def normalize(record: dict) -> tuple[str, dict]:
    key = record["resource_id"].strip() or f"ref:{record['display_reference'].strip()}"
    norm = {}
    for f in FIELDS:
        v = record[f]
        norm[f] = sorted({str(x).strip().lower() for x in v}) if isinstance(v, list) else v
    return key, norm


def extract(llm: LLM, eid: str, path: Path) -> dict:
    result = llm.json_call("media_extraction", SYSTEM, _content(path), SCHEMA, max_tokens=8000,
                           source=source_label(eid, path))
    return dict(normalize(r) for r in result["records"])


def main():
    base_model = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5-5"
    cand_model = sys.argv[2] if len(sys.argv) > 2 else "claude-sonnet-5-5"
    base = LLM(models={"media_extraction": base_model})
    cand = LLM(models={"media_extraction": cand_model})
    files = [e for e in load_manifest(DATASET) if Path(e["file_path"]).suffix.lower() in (".png", ".pdf")]

    report, same_files, total_records, same_records = [], 0, 0, 0
    for e in files:
        path = DATASET / e["file_path"]
        a, b = extract(base, e["file_id"], path), extract(cand, e["file_id"], path)
        diffs = {"only_" + base_model: sorted(set(a) - set(b)), "only_" + cand_model: sorted(set(b) - set(a)),
                 "field_differences": {}}
        for key in sorted(set(a) & set(b)):
            fd = {f: {base_model: a[key][f], cand_model: b[key][f]} for f in FIELDS if a[key][f] != b[key][f]}
            if fd:
                diffs["field_differences"][key] = fd
        keys = set(a) | set(b)
        agree = len(keys) - len(diffs["only_" + base_model]) - len(diffs["only_" + cand_model]) \
            - len(diffs["field_differences"])
        total_records += len(keys)
        same_records += agree
        identical = agree == len(keys)
        same_files += identical
        report.append({"file_id": e["file_id"], "file": e["file_path"], "records": len(keys),
                       "identical": identical, **({} if identical else diffs)})
        print(f"{e['file_id']} {Path(e['file_path']).name:<45} records={len(keys):<3} "
              f"{'IDENTICAL' if identical else 'DIFFERENT'}")
        if not identical:
            for k in ("only_" + base_model, "only_" + cand_model):
                if diffs[k]:
                    print(f"     {k}: {diffs[k]}")
            for key, fd in diffs["field_differences"].items():
                print(f"     {key}: {json.dumps(fd, ensure_ascii=False)}")

    usage = {m: llm.usage_rows() for m, llm in ((base_model, base), (cand_model, cand))}
    summary = {"baseline": base_model, "candidate": cand_model, "files": len(files), "identical_files": same_files,
               "records": total_records, "identical_records": same_records, "usage": usage, "per_file": report}
    out = HERE / "media_model_comparison.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nfiles identical: {same_files}/{len(files)}   records identical: {same_records}/{total_records}")
    for m, rows in usage.items():
        for r in rows:
            print(f"  {m}: calls={r['calls']} cached={r['cached_calls']} in={r['input_tokens']} "
                  f"out={r['output_tokens']} ${r['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
