"""Link deletion-workflow activity to cases.

Some resources (a deleted account, an anonymized analytics profile) no longer carry
any identifier, so evidence alone cannot tie them to the subject. The deletion
workflow that served each request, however, acts on them shortly before and
after the request's `reported_at`. This module assigns such identifier-less
resources to cases using:

* the workflow window around `reported_at`;
* exclusion of resources whose evidence ties them to a different user;
* the account deletion result agreeing with the reported workflow status;
* global constraint propagation (each case has at most one resource of a kind).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from loaders import parse_ts

WINDOW = (timedelta(hours=-1.3), timedelta(hours=2.0))
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
# typical offsets (seconds from reported_at) used only to break remaining ties
TYPICAL_OFFSET = {"account": -3600, "default": -1700}


def kind(rid: str) -> str:
    return rid.rsplit("_", 1)[0]


class WorkflowLinker:
    def __init__(self, requests, activities, case_user: dict, owner: dict):
        """case_user: case_id -> user_id; owner: resource_id -> set(user_id) from evidence."""
        self.case_user = case_user
        self.owner = owner
        self.req = {r["case_id"]: r for r in requests}
        worker = [a for a in activities if a["source_system"] == "deletion_worker"]
        for a in worker:
            a["_t"] = parse_ts(a["event_time"])
        self.activity_of = defaultdict(list)
        self.workflow_owned = defaultdict(set)  # evidence-linked resources the case's workflow also touched
        cand = defaultdict(set)
        strict = defaultdict(set)
        for r in requests:
            rp = parse_ts(r["reported_at"])
            for a in worker:
                if not (WINDOW[0] <= a["_t"] - rp <= WINDOW[1]):
                    continue
                rid = a["resource_id"]
                if owner.get(rid):
                    if case_user.get(r["case_id"]) in owner[rid]:
                        self.workflow_owned[r["case_id"]].add(rid)
                    continue
                self.activity_of[(r["case_id"], rid)].append(a)
                cand[rid].add(r["case_id"])
                if a["target_system"] == "account_service" and a["event_type"] == "DELETE_RECORD":
                    if STATUS_RESULT.get(r["reported_status"]) == a["reported_result"]:
                        strict[rid].add(r["case_id"])
                else:
                    strict[rid].add(r["case_id"])
        # prefer status-consistent candidates when any exist
        self.cand = {rid: (strict[rid] or cs) for rid, cs in cand.items()}
        self.assigned = defaultdict(set)
        self._owned_kinds = defaultdict(set)
        for rid, users in owner.items():
            for c, u in case_user.items():
                if u in users:
                    self._owned_kinds[c].add(kind(rid))
        self._solve()

    def _solve(self):
        cand = {rid: set(cs) for rid, cs in self.cand.items()}
        has_kind = defaultdict(set, {c: set(k) for c, k in self._owned_kinds.items()})
        done = set()
        changed = True
        while changed:
            changed = False
            for rid in sorted(cand):
                if rid in done:
                    continue
                cs = cand[rid]
                narrowed = {c for c in cs if kind(rid) not in has_kind[c]}
                if narrowed and narrowed != cs:
                    cand[rid] = cs = narrowed
                    changed = True
                if len(cs) == 1:
                    c = next(iter(cs))
                    self.assigned[c].add(rid)
                    has_kind[c].add(kind(rid))
                    done.add(rid)
                    changed = True
        # tie-break: closest to the typical offset, then lowest case_id, one per case and kind
        for rid in sorted(r for r in cand if r not in done):
            options = [c for c in sorted(cand[rid]) if kind(rid) not in has_kind[c]]
            if not options:
                continue
            target = TYPICAL_OFFSET.get(kind(rid), TYPICAL_OFFSET["default"])

            def dist(c):
                a = self.activity_of[(c, rid)][0]
                off = (a["_t"] - parse_ts(self.req[c]["reported_at"])).total_seconds()
                return abs(off - target)

            best = min(options, key=lambda c: (dist(c), c))
            self.assigned[best].add(rid)
            has_kind[best].add(kind(rid))
        self.ambiguous = {rid for rid in cand if rid not in done}

    def resources_for(self, case_id: str) -> set:
        return self.assigned.get(case_id, set()) | self.workflow_owned.get(case_id, set())
