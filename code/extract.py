"""Evidence ingestion: turn every participant-facing evidence file into Observations.

An Observation is one sighting of one resource in one evidence file. Extractors
only report what the file says (fields, explicit results); interpretation of
state happens in `state.py`.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from loaders import parse_ts

RESOURCE_SYSTEM = [
    ("audit_record_", "security_service"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("data_flow_", "integration_service"),
    ("backup_", "backup_service"),
    ("tombstone_", "identity_service"),
    ("marketing_", "marketing_platform"),
    ("legal_hold_", "legal_service"),
    ("cache_", "cache_service"),
    ("object_", "object_storage"),
]
RESOURCE_RE = re.compile(
    r"\b(?:audit_record|account|search|analytics|support|export|data_flow|backup|tombstone|marketing|legal_hold|cache|object)_\d+\b"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
USER_RE = re.compile(r"^user_\d+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

NULLS = {None, "", "null", "none", "None", "NULL", "[REDACTED]", "redacted", "no direct identifier"}


def system_for(resource_id: str) -> str | None:
    for prefix, system in RESOURCE_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return None


def is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in NULLS)


@dataclass
class Observation:
    evidence_id: str
    file_captured_at: object  # datetime from manifest; used for citation cutoff
    observed_at: object  # datetime of the record itself; used for state ordering
    system_id: str
    resource_id: str
    fields: dict = field(default_factory=dict)  # raw attributes from the source
    lookup_keys: set = field(default_factory=set)  # identifiers used to find this record
    result: str | None = None  # explicit result label (FOUND, NOT_FOUND, ACTIVE, ...)
    record_type: str | None = None  # explicit data category, when the source states it
    location: str | None = None  # table / replica / collection name
    source_kind: str = ""


def _scalar_fields(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_scalar_fields(v, f"{prefix}{k}."))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(_scalar_fields(item, f"{prefix}{k}.{i}."))
                else:
                    out[f"{prefix}{k}.{i}"] = item
        else:
            out[f"{prefix}{k}"] = v
    return out


# ---------------------------------------------------------------- record-lookup family
def _lookup_obs(eid, cap, ts, system, rid, record_type, result, subject, kind):
    return Observation(
        evidence_id=eid, file_captured_at=cap, observed_at=parse_ts(ts) if ts else cap,
        system_id=system or system_for(rid), resource_id=rid,
        fields={"subject_reference": subject}, lookup_keys={subject} if not is_null(subject) else set(),
        result=result, record_type=record_type, source_kind=kind,
    )


def extract_txt(eid, path, cap):
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == 6 and RESOURCE_RE.fullmatch(parts[2]):
            ts, system, rid, rtype, result, subject = parts
            obs.append(_lookup_obs(eid, cap, ts, system, rid, rtype, result, subject, "lookup_txt"))
    return obs


# ---------------------------------------------------------------- JSON
def _record_obs(eid, cap, rec: dict, system=None, ts=None, location=None, kind="json"):
    rid = rec.get("resource_id")
    if not rid:
        return None
    flat = _scalar_fields(rec)
    obs_ts = rec.get("observed_at") or rec.get("captured_at") or ts
    return Observation(
        evidence_id=eid, file_captured_at=cap, observed_at=parse_ts(obs_ts) if obs_ts else cap,
        system_id=system or system_for(rid), resource_id=rid, fields=flat,
        location=location, source_kind=kind,
    )


def extract_json(eid, path, cap):
    data = json.loads(path.read_text(encoding="utf-8"))
    obs, covered = [], []
    if "snapshots" in data:  # snapshot bundles: each snapshot fully lists one collection
        for snap in data["snapshots"]:
            if snap.get("data_store") == "account_service":
                covered.append((snap["data_store"], parse_ts(snap["captured_at"])))
            for rec in snap.get("records", []):
                if "system" in rec and "result" in rec:  # embedded record-lookup history
                    obs.append(_lookup_obs(eid, cap, rec.get("recorded_at"), rec["system"], rec["resource_id"],
                                           rec.get("record_type"), rec.get("result"), rec.get("lookup_value"),
                                           "lookup_json"))
                    continue
                o = _record_obs(eid, cap, rec, snap.get("data_store"), snap.get("captured_at"),
                                snap.get("collection"), "json_bundle")
                if o:
                    obs.append(o)
    elif "service_exports" in data:  # per-service resource exports
        for system, groups in data["service_exports"].items():
            for group, recs in groups.items():
                for rec in recs:
                    o = _record_obs(eid, cap, rec, system, data.get("exported_at"), group, "json_export")
                    if o:
                        if "profile" in rec and rec["profile"] is None:
                            o.fields["profile"] = None
                        obs.append(o)
    elif data.get("records") and "system" in data["records"][0]:  # record-lookup ledgers
        for r in data["records"]:
            obs.append(_lookup_obs(eid, cap, r.get("recorded_at"), r["system"], r["resource_id"],
                                   r.get("record_type"), r.get("result"), r.get("subject_reference"),
                                   "lookup_json"))
    else:  # single collection captures
        for rec in data.get("records", []):
            o = _record_obs(eid, cap, rec, None, data.get("captured_at"), data.get("collection"), "json_collection")
            if o:
                obs.append(o)
    return obs, covered


# ---------------------------------------------------------------- SQL
def extract_sql(eid, path, cap):
    text = path.read_text(encoding="utf-8")
    m = re.search(r"Captured at:\s*(\S+)", text)
    file_ts = parse_ts(m.group(1)) if m else cap
    con = sqlite3.connect(":memory:")
    con.executescript(text)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: parse_ts(r["captured_at"]) for r in con.execute("SELECT * FROM snapshot_scope")}
    obs, covered = [], set()
    for t in tables:
        if t == "snapshot_scope":
            continue
        for row in con.execute(f'SELECT * FROM "{t}"'):
            rec = dict(row)
            if "record_json" in rec:
                inner = json.loads(rec.pop("record_json"))
                rec = {**rec, **_scalar_fields(inner)}
            rid = rec.get("resource_id")
            system = rec.get("origin_system") or system_for(rid)
            ts = scope.get(system, file_ts)
            obs.append(Observation(
                evidence_id=eid, file_captured_at=cap, observed_at=ts, system_id=system,
                resource_id=rid, fields=rec, location=t, source_kind="sql",
            ))
    con.close()
    # Full point-in-time database snapshots let a later stage treat "not listed" as absent.
    covered = {o.system_id for o in obs} if "Database topology" in text else set()
    return obs, [(system, file_ts) for system in sorted(covered)]


# ---------------------------------------------------------------- logs
KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
LOOKUP_FIELDS = ("lookup_key", "subject_key", "scan_key", "match_key", "correlation_key",
                 "matched_value", "subject_ref", "subject_reference", "customer_reference")


def _kv(s: str) -> dict:
    out = {}
    for k, v in KV_RE.findall(s):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    return out


def parse_log_line(line: str):
    """Return (ts, service, trace_id, event, fields) for any of the supported dialects."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    ts, _, rest = line.partition(" ")
    if rest.startswith("{"):  # JSON payload
        d = json.loads(rest)
        return ts, d.get("service"), d.get("trace_id"), d.get("event"), d
    if rest.startswith("["):  # [LEVEL] [service] [trace] event: kv
        m = re.match(r"\[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+): ?(.*)", rest)
        if m:
            f = _kv(m.group(5))
            return ts, m.group(2), m.group(3), m.group(4), f
    if rest.startswith("| ") or re.match(r"\w+ \| ", rest):  # pipe-delimited
        parts = [p.strip() for p in rest.lstrip("| ").split(" | ", 4)]
        if len(parts) == 5:
            _, service, trace, event, kv = parts
            return ts, service, trace, event, _kv(kv)
    if rest.startswith("<"):  # syslog RFC5424
        m = re.match(r"<\d+>\d+ \S+ (\S+) (\S+) \S+ \S+ (.*)", rest)
        if m:
            f = _kv(m.group(3))
            return ts, m.group(1), m.group(2), f.get("event"), f
    f = _kv(rest)
    if "message" in f and "function" in f:  # cloud runtime
        inner = _kv(f["message"])
        return ts, f["function"], f.get("requestId"), inner.get("event"), inner
    return ts, f.get("service"), f.get("trace_id"), f.get("event"), f


def extract_log(eid, path, cap):
    obs = []
    traces: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_log_line(line)
        if not parsed:
            continue
        ts, service, trace, event, f = parsed
        if event == "record_lookup" and f.get("resource_id"):  # record-lookup family in log form
            obs.append(_lookup_obs(eid, cap, ts, f.get("system"), f["resource_id"], f.get("record_type"),
                                   f.get("result"), f.get("subject_reference"), "lookup_log"))
            continue
        key = trace or f"{service}:{ts}"
        traces.setdefault(key, []).append((ts, service, event, f))
    for trace, events in traces.items():
        keys = set()
        for _, _, _, f in events:
            for k in LOOKUP_FIELDS:
                if k in f and not is_null(f[k]) and not RESOURCE_RE.fullmatch(str(f[k])):
                    keys.add(f[k])
        by_res: dict = {}
        for ts, service, event, f in events:
            rid = f.get("resource_id")
            if not rid:
                continue
            entry = by_res.setdefault(rid, {"ts": ts, "service": service, "fields": {}, "events": []})
            entry["fields"].update({k: v for k, v in f.items() if k not in ("instance", "pod", "region")})
            entry["events"].append(event)
            entry["ts"] = ts
        for rid, e in by_res.items():
            e["fields"]["events"] = ",".join(e["events"])
            obs.append(Observation(
                evidence_id=eid, file_captured_at=cap, observed_at=parse_ts(e["ts"]),
                system_id=system_for(rid) or e["service"], resource_id=rid, fields=e["fields"],
                lookup_keys=set(keys), location=e["service"], source_kind="log_trace",
            ))
    return obs


# ---------------------------------------------------------------- driver
def load_manifest(dataset: Path):
    with open(dataset / "evidence_files.csv", newline="", encoding="utf-8") as fh:
        return [{**r, "captured_at": parse_ts(r["captured_at"])} for r in csv.DictReader(fh)]


def extract_all(dataset: Path, media_extractor=None):
    """Return (observations, coverage); coverage lists (evidence_id, file_captured_at, system, snapshot_at)
    for snapshots that fully list a system, so that "not listed" can be read as absent."""
    observations, coverage = [], []
    for entry in load_manifest(dataset):
        eid, cap, path = entry["file_id"], entry["captured_at"], dataset / entry["file_path"]
        suffix = path.suffix.lower()
        if suffix == ".sql":
            obs, covered = extract_sql(eid, path, cap)
            coverage += [(eid, cap, system, ts) for system, ts in covered]
        elif suffix == ".json":
            obs, covered = extract_json(eid, path, cap)
            coverage += [(eid, cap, system, ts) for system, ts in covered]
        elif suffix == ".log":
            obs = extract_log(eid, path, cap)
        elif suffix == ".txt":
            obs = extract_txt(eid, path, cap)
        elif suffix in (".png", ".pdf") and media_extractor:
            obs = media_extractor(eid, path, cap)
        else:
            obs = []
        observations.extend(obs)
    return observations, coverage
