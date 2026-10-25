"""Decide whether each request_text authorizes deletion (multilingual), using Claude in batches."""
from __future__ import annotations

SYSTEM = """You classify customer privacy requests for a data-deletion audit.
A request AUTHORIZES DELETION only if the person asks to delete, erase, remove or close their account
or personal data (in any language). Follow-ups about an earlier deletion also authorize it: asking to
verify, audit or confirm that a deletion is complete, or reporting that deleted data still appears, is a
DELETION request because the person wants their data gone. Requests to access, download, export or copy data, to correct or
update details, to change an email or phone, to stop marketing / unsubscribe, to reset a login, or to
get invoices do NOT authorize deletion, even if they mention the account or personal data.
Return one result per input id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "request_type": {"type": "string", "enum": [
                        "DELETION", "ACCESS", "EXPORT", "CORRECTION", "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "OTHER"]},
                    "authorizes_deletion": {"type": "boolean"},
                },
                "required": ["id", "request_type", "authorizes_deletion"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

BATCH = 40


RETRIES = 1  # extra attempts for texts the model left out of its batch answer


def _classify_batch(llm, chunk: list[str], source: str) -> dict[str, dict]:
    listing = "\n".join(f"[{i}] {t}" for i, t in enumerate(chunk))
    result = llm.json_call("intent_classification", SYSTEM, [{"type": "text", "text": listing}], SCHEMA,
                           max_tokens=8000, source=source)
    by_id = {r["id"].strip("[] "): r for r in result["results"]}
    return {t: by_id[str(i)] for i, t in enumerate(chunk) if str(i) in by_id}


def classify_requests_detailed(llm, texts: list[str], label: str = "") -> dict[str, dict | None]:
    """Map each distinct request text to the model's {request_type, authorizes_deletion}.
    Texts the model leaves out are retried in their own batch; None means still unclassified.
    `label` names the request set (for the cache index)."""
    prefix = f"{label} " if label else ""
    unique = sorted(set(texts))
    out: dict[str, dict | None] = {}
    batches = range(0, len(unique), BATCH)
    for n, start in enumerate(batches, 1):
        chunk = unique[start:start + BATCH]
        out.update(_classify_batch(llm, chunk, f"{prefix}request texts batch {n}/{len(batches)} ({len(chunk)} texts)"))
    for attempt in range(1, RETRIES + 1):
        missing = [t for t in unique if t not in out]
        if not missing:
            break
        for start in range(0, len(missing), BATCH):
            chunk = missing[start:start + BATCH]
            out.update(_classify_batch(llm, chunk, f"{prefix}request texts retry {attempt} ({len(chunk)} texts)"))
    return {t: out.get(t) for t in unique}


def unclassified(labels: dict[str, dict | None]) -> list[str]:
    return sorted(t for t, lab in labels.items() if lab is None)


def authorizes(label: dict | None) -> bool:
    # Fail safe toward auditing: a text still unclassified after retries is audited as a deletion request
    # (and reported by the caller via `unclassified`).
    return True if label is None else bool(label["authorizes_deletion"] and label["request_type"] == "DELETION")


def classify_requests(llm, texts: list[str], label: str = "") -> dict[str, bool]:
    """Map each distinct request text to True when it authorizes deletion."""
    return {t: authorizes(lab) for t, lab in classify_requests_detailed(llm, texts, label).items()}
