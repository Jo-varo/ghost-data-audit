"""Per-case deletion audit: identity resolution, evidence linking, state reconstruction, step planning."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from extract import EMAIL_RE, RESOURCE_RE, is_null, system_for
from loaders import DATASET, parse_ts
from operations import OperationCatalog
from policy import Policy
from state import CATEGORY_BY_SYSTEM, category_of, state_of

FAR_FUTURE = parse_ts("2100-01-01T00:00:00Z")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_TOKEN_RE = re.compile(r"\buser_\d+\b")
PHONE_KEYS = re.compile(r"(^|\.)(phone|\w+_phone)$")


OPERATIONS = OperationCatalog.load()


def norm_email(s: str) -> str:
    return s.strip().lower()


def norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s)


# ------------------------------------------------------------------ identity
@dataclass
class Identity:
    user_id: str
    name: str
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    hashes: set = field(default_factory=set)


class IdentityIndex:
    def __init__(self, identities: list[dict], audit_log: list[dict]):
        self.users: dict[str, Identity] = {}
        self.audit_by_user = defaultdict(list)
        for r in identities:
            ident = Identity(r["user_id"], r["name"])
            ident.emails.add(norm_email(r["email"]))
            ident.phones.add(norm_phone(r["phone"]))
            ident.hashes.add(r["email_hash"].lower())
            self.users[r["user_id"]] = ident
        for a in audit_log:
            ident = self.users.get(a["user_id"])
            if not ident:
                continue
            self.audit_by_user[a["user_id"]].append(a)
            for v in (a["old_value"], a["new_value"]):
                if is_null(v):
                    continue
                if a["field_name"] == "email":
                    ident.emails.add(norm_email(v))
                elif a["field_name"] == "phone":
                    ident.phones.add(norm_phone(v))
                elif a["field_name"] == "email_hash":
                    ident.hashes.add(v.lower())
        self.by_email = {}
        # current emails take precedence over historical ones
        for ident in self.users.values():
            for e in ident.emails:
                self.by_email.setdefault(e, ident.user_id)
        for r in identities:
            self.by_email[norm_email(r["email"])] = r["user_id"]

    def resolve(self, email: str) -> Identity | None:
        uid = self.by_email.get(norm_email(email))
        return self.users.get(uid) if uid else None


# ------------------------------------------------------------------ observation tokens
def obs_tokens(o) -> tuple[set, set]:
    """Return (identifier tokens, referenced resource ids) carried by an observation."""
    tokens, refs = set(), set()
    values = list(o.lookup_keys)
    for k, v in o.fields.items():
        if is_null(v):
            continue
        values.append(v)
        if PHONE_KEYS.search(k):
            tokens.add("phone:" + norm_phone(str(v)))
    for v in values:
        s = str(v)
        for e in EMAIL_RE.findall(s):
            tokens.add("email:" + e.lower())
        for u in USER_TOKEN_RE.findall(s):
            tokens.add("user:" + u)
        for h in HASH_RE.findall(s.lower()):
            tokens.add("hash:" + h)
        for r in RESOURCE_RE.findall(s):
            if r != o.resource_id:
                refs.add(r)
    return tokens, refs


def identity_tokens(ident: Identity) -> set:
    t = {"user:" + ident.user_id}
    t |= {"email:" + e for e in ident.emails}
    t |= {"phone:" + p for p in ident.phones if len(p) >= 7}
    t |= {"hash:" + h for h in ident.hashes}
    return t


class EvidenceIndex:
    def __init__(self, observations, coverage):
        self.obs = observations
        self.coverage = coverage
        self.by_resource = defaultdict(list)
        self.by_token = defaultdict(list)
        for o in observations:
            o.category = category_of(o)
            o.state = None if o.source_kind == "activity_link" else state_of(o)
            toks, refs = obs_tokens(o)
            o.tokens, o.refs = toks, refs
            self.by_resource[o.resource_id].append(o)
            for t in toks:
                self.by_token[t].append(o)

    def linked_resources(self, ident: Identity, cutoff, since=None) -> dict[str, list]:
        """Resources tied to the identity by evidence available at cutoff -> list of linking observations.
        `since` drops evidence already covered by an earlier audit of the same subject."""
        links = defaultdict(list)
        for t in identity_tokens(ident):
            for o in self.by_token.get(t, []):
                if o.file_captured_at <= cutoff and (since is None or o.file_captured_at > since):
                    links[o.resource_id].append(o)
        # one hop through explicit resource references (e.g. search doc -> source account)
        for rid, os_ in list(links.items()):
            for o in os_:
                for r in o.refs:
                    if r not in links and system_for(r):
                        links[r].append(o)
        return links

    def owners(self, identities: "IdentityIndex") -> dict[str, set]:
        """resource_id -> users that any evidence ties it to (used only to exclude other subjects)."""
        tok2user = defaultdict(set)
        for u in identities.users.values():
            for t in identity_tokens(u):
                tok2user[t].add(u.user_id)
        owner = defaultdict(set)
        for o in self.obs:
            for t in o.tokens:
                owner[o.resource_id] |= tok2user.get(t, set())
        return {k: v for k, v in owner.items() if v}

    def absence_proof(self, rid: str, cutoff):
        """Latest full database snapshot at cutoff that covers the resource's system but does not list it."""
        system = system_for(rid)
        listed = {o.evidence_id for o in self.by_resource.get(rid, [])}
        best = None
        for eid, cap, sys_, snap_ts in self.coverage:
            if cap <= cutoff and sys_ == system and eid not in listed:
                if best is None or (snap_ts, cap) > best[0]:
                    best = ((snap_ts, cap), eid)
        return best[1] if best else None


# ------------------------------------------------------------------ state reconstruction
def resource_state(observations, cutoff):
    """Latest state of a resource at cutoff. Returns (state, category, evidence_id) or None."""
    usable = [o for o in observations if o.file_captured_at <= cutoff and o.state]
    if not usable:
        return None
    latest_ts = max(o.observed_at for o in usable)
    latest = [o for o in usable if o.observed_at == latest_ts]
    rank = {"PRESENT": 5, "ACTIVE": 5, "CLEAN": 3, "DISABLED": 2, "BLOCKED": 2, "ABSENT": 1}
    best = max(latest, key=lambda o: (rank.get(o.state, 0), o.evidence_id))
    explicit = [o.category for o in usable if o.record_type]
    category = explicit[-1] if explicit else best.category
    return best.state, category, best.evidence_id, best


# policy action -> (remediation, verification) for records kept with identifiers removed
KEEP_WITHOUT_IDENTIFIERS = {
    "REDACT": ("REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
}


def plan_resource(policy: Policy, state, category):
    """Return (finding_op, remediation_op, verify_op, compliant) for one record under the supplied policy."""
    if category == "DATA_FLOW":
        # Not a stored data category: an enabled flow is a recreation path and must be disabled.
        if state == "ACTIVE":
            return "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", False
        return None, None, "VERIFY_DATA_FLOW_DISABLED", True
    action = policy.action(category)
    if action == "DELETE":
        if state in ("PRESENT", "CLEAN"):
            return "REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", False
        return None, None, "VERIFY_RECORD_ABSENT", True
    if action in KEEP_WITHOUT_IDENTIFIERS:
        remediate, verify = KEEP_WITHOUT_IDENTIFIERS[action]
        if state == "PRESENT":
            return "REPORT_EXPOSED_PII", remediate, verify, False
        if state == "ABSENT":
            return None, None, "VERIFY_RECORD_ABSENT", True
        return None, None, verify, True
    if action == "RETAIN":
        if policy.restorable(category):
            # kept, but restore access must be blocked so deleted data cannot come back
            if state == "ACTIVE":
                return "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", False
            return None, None, "VERIFY_RESTORE_BLOCKED", True
        return None, None, "VERIFY_RECORD_PRESENT", True
    return None, None, None, True


def check_plan(policy: Policy, catalog: OperationCatalog = OPERATIONS) -> None:
    """Fail fast if the planner can emit an operation the catalog does not allow, or in the wrong phase."""
    states = ("PRESENT", "CLEAN", "ABSENT", "ACTIVE", "DISABLED", "BLOCKED")
    for category in list(policy.by_category) + ["DATA_FLOW"]:
        for state in states:
            f_op, r_op, v_op, _ = plan_resource(policy, state, category)
            for op, phase in ((f_op, "FINDING"), (r_op, "REMEDIATION"), (v_op, "VERIFICATION")):
                if op is not None and catalog.phase(op) != phase:
                    raise ValueError(f"{op} is catalogued as {catalog.phase(op)}, planner uses it as {phase}")


def step(system_id, rid, op, evidence):
    """One output step; phase and state come from the operations catalog."""
    return {"step_no": 0, "phase": OPERATIONS.phase(op), "system_id": system_id, "resource_id": rid,
            "operation": op, "state": OPERATIONS.state(op), "evidence_id": evidence}


def order_steps(steps):
    steps.sort(key=lambda s: (*OPERATIONS.sort_key(s["operation"]), s["system_id"], s["resource_id"]))
    for i, s in enumerate(steps, 1):
        s["step_no"] = i
    return steps


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    notes: list = field(default_factory=list)


_DEFAULT_POLICY = None


def default_policy() -> Policy:
    global _DEFAULT_POLICY
    if _DEFAULT_POLICY is None:
        _DEFAULT_POLICY = Policy.load(DATASET / "deletion_rules.csv")
        check_plan(_DEFAULT_POLICY)
    return _DEFAULT_POLICY


def choose_account(candidates: list[str], workflow: set, evidence: "EvidenceIndex", ident: Identity, cutoff):
    """A subject has one primary account. Prefer the one the deletion workflow acted on, then the one
    whose evidence positively carries the subject's identifiers, then the most recently observed."""
    if len(candidates) <= 1:
        return candidates
    ident_toks = identity_tokens(ident)

    def score(rid):
        obs = [o for o in evidence.by_resource.get(rid, []) if o.file_captured_at <= cutoff]
        positive = any(o.state == "PRESENT" and (o.tokens & ident_toks) for o in obs)
        latest = max((o.observed_at for o in obs), default=None)
        return (rid in workflow, positive, latest is not None, latest or cutoff, rid)

    return [max(candidates, key=score)]


def audit_case(req, identities: IdentityIndex, evidence: EvidenceIndex, workflow_resources=(),
               previous_audit=None, policy: Policy | None = None) -> CaseResult:
    policy = policy or default_policy()
    retained_systems = {sys_ for sys_, cat in CATEGORY_BY_SYSTEM.items() if policy.permanently_retained(cat)}
    cutoff = parse_ts(req["audit_at"])
    ident = identities.resolve(req["email"])
    notes = []
    if not ident:
        return CaseResult(req["case_id"], "COMPLETE", [], ["identity not resolved"])
    workflow_resources = set(workflow_resources)
    links = evidence.linked_resources(ident, cutoff, since=previous_audit)
    for rid in workflow_resources:
        links.setdefault(rid, [])
    # Retained records never change state, so a later capture still identifies the record that must be kept.
    for rid in evidence.linked_resources(ident, FAR_FUTURE, since=previous_audit):
        if system_for(rid) in retained_systems:
            links.setdefault(rid, [])
    accounts = choose_account(sorted(r for r in links if system_for(r) == "account_service"),
                              workflow_resources, evidence, ident, cutoff)
    rids = sorted(r for r in links if system_for(r) != "account_service") + accounts

    findings, remediation, verification = [], [], []
    compliant_all = True
    for rid in rids:
        system_id = system_for(rid)
        st = resource_state(evidence.by_resource[rid], cutoff)
        if not st:
            if system_id == "account_service":
                st = ("ABSENT", "USER_PROFILE", evidence.absence_proof(rid, cutoff) or "", None)
            elif system_id in retained_systems:
                st = ("PRESENT", CATEGORY_BY_SYSTEM[system_id], "", None)
            else:
                notes.append(f"{rid}: no state")
                continue
        state, category, eid, _ = st
        f_op, r_op, v_op, ok = plan_resource(policy, state, category)
        notes.append(f"{rid}: {category} {state} @{eid}")
        if not v_op:
            continue
        if ok:
            verification.append((system_id, rid, v_op, eid))
        else:
            compliant_all = False
            findings.append(step(system_id, rid, f_op, eid))
            remediation.append(step(system_id, rid, r_op, ""))
            verification.append((system_id, rid, v_op, ""))
    status = "COMPLETE" if compliant_all else "INCOMPLETE"
    if status == "COMPLETE":
        # A completed deletion is only claimed for checks backed by evidence available at audit_at.
        verification = [v for v in verification if v[3]]
    verify_steps = [step(s, r, op, e if status == "COMPLETE" else "") for s, r, op, e in verification]
    return CaseResult(req["case_id"], status, order_steps(findings + remediation + verify_steps), notes)
