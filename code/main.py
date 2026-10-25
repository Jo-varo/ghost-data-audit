"""Ghost Data Still Exists - deletion-audit agent.

Usage:
    python code/main.py                      # audit dataset/deletion_requests.csv -> output.csv
    python code/main.py --sample             # audit the 32 sample requests -> code/evaluation/sample_predictions.csv
    python code/main.py --offline            # use only cached LLM responses (no API calls)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import EvidenceIndex, IdentityIndex, audit_case, check_plan  # noqa: E402
from extract import Observation, extract_all  # noqa: E402
from intent import authorizes as authorizes_deletion, classify_requests_detailed, unclassified  # noqa: E402
from llm import LLM  # noqa: E402
from loaders import DATASET, REPO_ROOT, parse_ts, read_csv  # noqa: E402
from media import make_media_extractor  # noqa: E402
from policy import Policy  # noqa: E402
from workflow import WorkflowLinker  # noqa: E402


LINK_RE = re.compile(r"\b([a-z_]+_\d+) \(([^)]+)\)")


def catalog_aliases(activities) -> dict:
    """Data-catalog links name the business reference a resource is displayed under."""
    aliases = {}
    for a in activities:
        m = LINK_RE.search(a["details"]) if a["event_type"] == "LINK_RESOURCE_IDENTITY" else None
        if m and m.group(1) != m.group(2):
            aliases[m.group(2)] = m.group(1)
    return aliases


def activity_link_observations(activities):
    """Catalog activities that tie a resource to a subject are identity links (they carry no state)."""
    obs = []
    for a in activities:
        if a["event_type"] != "LINK_RESOURCE_IDENTITY":
            continue
        t = parse_ts(a["event_time"])
        obs.append(Observation(evidence_id=a["activity_id"], file_captured_at=t, observed_at=t,
                               system_id=a["target_system"], resource_id=a["resource_id"],
                               fields={"details": a["details"]}, source_kind="activity_link"))
    return obs


def case_sort_key(case_id: str):
    """Natural order: case_33 < case_100 (plain string order would put case_100 first)."""
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (case_id, -1)


def serialize(steps) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_context(llm: LLM):
    activities = read_csv(DATASET / "system_activity.csv")
    media = make_media_extractor(llm, catalog_aliases(activities))
    observations, coverage = extract_all(DATASET, media_extractor=media)
    observations += activity_link_observations(activities)
    identities = IdentityIndex(read_csv(DATASET / "user_identities.csv"), read_csv(DATASET / "user_audit_log.csv"))
    evidence = EvidenceIndex(observations, coverage)
    return identities, evidence, activities


REPORT_PATH = REPO_ROOT / "code" / "evaluation" / "evaluation_report.md"
UNCLASSIFIED_START = "<!-- unclassified-intents:start -->"
UNCLASSIFIED_END = "<!-- unclassified-intents:end -->"


def report_unclassified(cases: list[dict], run_label: str, update_report: bool) -> None:
    """Make requests whose intent could not be classified visible: console always, report on full runs."""
    if cases:
        print(f"WARNING: {len(cases)} request(s) got no intent classification after retry; "
              f"audited as deletion requests (fail-safe):")
        for c in cases:
            print(f"  {c['case_id']}: {c['request_text']!r}")
    if not update_report or not REPORT_PATH.exists():
        return
    if cases:
        body = ("| case_id | request_text | handling |\n|---|---|---|\n" + "".join(
            f"| {c['case_id']} | {c['request_text'].replace('|', '\\|')} | audited as a deletion request "
            f"(fail-safe) |\n" for c in cases))
    else:
        body = "None: every request received an intent classification (after at most one retry).\n"
    section = (f"{UNCLASSIFIED_START}\n## Unclassified intents (last {run_label} run)\n\n"
               "Requests the intent model left unanswered even after one retry of only the missing texts. "
               "They are not dropped: they are audited as deletion requests and listed here for review.\n\n"
               f"{body}{UNCLASSIFIED_END}")
    text = REPORT_PATH.read_text(encoding="utf-8")
    if UNCLASSIFIED_START in text:
        head, rest = text.split(UNCLASSIFIED_START, 1)
        text = head + section + rest.split(UNCLASSIFIED_END, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + section + "\n"
    REPORT_PATH.write_text(text, encoding="utf-8")


def run(requests, all_requests, llm: LLM, identities, evidence, activities, label: str = "",
        update_report: bool = False, workflow_case: dict | None = None):
    """Audit `requests`. `all_requests` are the real requests the deletion workflow served (used for workflow
    linking and earlier-audit scoping). `workflow_case` maps a request that is not in `all_requests` (e.g. a
    synthetic evaluation variant) to the real case whose workflow served it."""
    workflow_case = workflow_case or {}
    labels = classify_requests_detailed(llm, [r["request_text"] for r in requests], label)
    missing = set(unclassified(labels))
    report_unclassified([r for r in sorted(requests, key=lambda r: case_sort_key(r["case_id"])) if r["request_text"] in missing],
                        label, update_report)
    authorizes = {t: authorizes_deletion(lab) for t, lab in labels.items()}
    policy = Policy.load(DATASET / "deletion_rules.csv")
    check_plan(policy)
    case_user = {}
    for r in all_requests + [r for r in requests if r["case_id"] in workflow_case]:
        ident = identities.resolve(r["email"])
        if ident:
            case_user[r["case_id"]] = ident.user_id
    served = {r["case_id"] for r in all_requests}
    workflow = WorkflowLinker(all_requests, activities, {c: u for c, u in case_user.items() if c in served},
                              evidence.owners(identities))
    # When a subject filed an earlier request, evidence up to that audit belongs to the earlier case.
    audits_by_user = {}
    for r in all_requests:
        if r["case_id"] in case_user:
            audits_by_user.setdefault(case_user[r["case_id"]], []).append(parse_ts(r["audit_at"]))
    rows = []
    for req in sorted(requests, key=lambda r: case_sort_key(r["case_id"])):
        if not authorizes[req["request_text"]]:
            rows.append((req["case_id"], "NOT_APPLICABLE", []))
            continue
        cutoff = parse_ts(req["audit_at"])
        earlier = [t for t in audits_by_user.get(case_user.get(req["case_id"]), []) if t < cutoff]
        res = audit_case(req, identities, evidence,
                         workflow.resources_for(workflow_case.get(req["case_id"], req["case_id"])),
                         previous_audit=max(earlier) if earlier else None, policy=policy)
        rows.append((req["case_id"], res.status, res.steps))
    return rows


def write_output(rows, path: Path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for case_id, status, steps in rows:
            w.writerow([case_id, status, serialize(steps)])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="audit the public sample requests instead")
    ap.add_argument("--offline", action="store_true", help="use cached LLM responses only")
    ap.add_argument("--output", type=Path, help="output CSV path")
    args = ap.parse_args(argv)

    started = time.time()
    llm = LLM(offline=args.offline)
    identities, evidence, activities = build_context(llm)
    sample = read_csv(DATASET / "sample_deletion_requests.csv")
    evaluation = read_csv(DATASET / "deletion_requests.csv")
    requests = sample if args.sample else evaluation
    out = args.output or (REPO_ROOT / "code" / "evaluation" / "sample_predictions.csv" if args.sample
                          else REPO_ROOT / "output.csv")
    rows = run(requests, sample + evaluation, llm, identities, evidence, activities,
               label="sample" if args.sample else "evaluation", update_report=not args.sample)
    write_output(rows, out)

    usage = {"requests": len(requests), "seconds": round(time.time() - started, 1), "rows": llm.usage_rows()}
    usage_path = REPO_ROOT / "code" / "evaluation" / ("usage_sample.json" if args.sample else "usage_full.json")
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    counts = {}
    for _, status, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    print(f"wrote {len(rows)} rows to {out} {counts}")
    for r in usage["rows"]:
        print(f"  {r['purpose']:<22} {r['model']}: calls={r['calls']} cached={r['cached_calls']} "
              f"in={r['input_tokens']} out={r['output_tokens']} ${r['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
