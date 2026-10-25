"""Interpret an Observation: which data category it belongs to and what state it shows.

States:
  PRESENT   record exists and still carries direct identifiers
  CLEAN     record exists but direct identifiers are gone (redacted / anonymized)
  ABSENT    record does not exist
  ACTIVE    data flow enabled, or backup restorable
  DISABLED  data flow disabled
  BLOCKED   backup restore blocked
"""
from __future__ import annotations

import re

from extract import EMAIL_RE, is_null

# data_category -> policy treatment family
CATEGORY_BY_SYSTEM = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "object_storage": "SUPPORT_ATTACHMENT",
}

DIRECT_ID_KEYS = re.compile(r"(^|\.)(email|phone|full_name|name|display_name|email_hash|requester_\w+|owner_\w+|"
                            r"customer_email|uploaded_by|matched_value|subject_selector)$")
USER_REF_KEYS = re.compile(r"(^|\.)(user_id|customer_reference|subject_reference|subject_ref)$")

LOOKUP_STATE = {
    "FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "IDENTIFIABLE": "PRESENT", "ANONYMIZED": "CLEAN",
    "REDACTED": "CLEAN", "ACTIVE": "ACTIVE", "DISABLED": "DISABLED", "BLOCKED": "BLOCKED",
    "ABSENT": "ABSENT", "PRESENT": "PRESENT",
}


def _truthy(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, (int, bool)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "allow", "allowed"):
        return True
    if s in ("false", "0", "no", "deny", "denied"):
        return False
    return None


def category_of(o) -> str:
    if o.record_type and o.record_type != "DATA_FLOW":
        return o.record_type
    f = o.fields
    if o.system_id == "support_platform":
        events = f.get("events", "")
        if ("attachment" in events or "file_name" in f or "file" in f or f.get("storage_class") == "ticket_attachment"
                or (o.location or "") in ("ticket_attachments", "attachment_preview")):
            return "SUPPORT_ATTACHMENT"
        if "contact" in events or o.location == "support_replica":
            return "SECONDARY_IDENTIFIER"  # legacy contact index entry: a secondary copy of the contact
    if o.system_id == "analytics_platform" and ("email_opt_in" in f or "contact" in f.get("events", "")):
        return "MARKETING_PROFILE"
    return CATEGORY_BY_SYSTEM.get(o.system_id, "UNKNOWN")


def has_direct_identifiers(o, include_user_ref: bool) -> bool:
    for k, v in o.fields.items():
        if is_null(v):
            continue
        if DIRECT_ID_KEYS.search(k):
            return True
        if include_user_ref and USER_REF_KEYS.search(k):
            return True
        if k.endswith(("message_text", "message_excerpt")) and EMAIL_RE.search(str(v)):
            return True
    di = o.fields.get("direct_identifiers")
    return di is not None and str(di) not in ("0", "")


def state_of(o) -> str | None:
    f = o.fields
    if o.result:
        return LOOKUP_STATE.get(o.result.upper())
    system = o.system_id
    if system == "integration_service":
        en = _truthy(f.get("enabled"))
        if en is None:
            return None
        return "ACTIVE" if en else "DISABLED"
    if system == "backup_service":
        for key in ("restore_allowed", "eligible"):
            v = _truthy(f.get(key))
            if v is not None:
                if key == "eligible" and _truthy(f.get("deletion_block")):
                    return "BLOCKED"
                return "ACTIVE" if v else "BLOCKED"
        if "decision" in f:
            return "ACTIVE" if _truthy(f["decision"]) else "BLOCKED"
        return None
    # existence signals
    if "http_status" in f or "status_code" in f:
        code = str(f.get("http_status") or f.get("status_code"))
        if code == "404":
            return "ABSENT"
    if "rows" in f and str(f["rows"]) == "0":
        return "ABSENT"
    if "result" in f and str(f["result"]).lower() == "miss":
        return "ABSENT"
    if "hits.total" in f and str(f["hits.total"]) == "0":
        return "ABSENT"
    if "profile" in f and f["profile"] is None:
        return "ABSENT"
    if "available" in f and _truthy(f["available"]) is False:
        return "ABSENT"
    if "download_available" in f and _truthy(f["download_available"]) is False:
        return "ABSENT"
    if "marker_found" in f and _truthy(f.get("marker_found")) is True:
        return "ABSENT"
    if system in ("security_service", "legal_service"):
        return "PRESENT"
    include_ref = CATEGORY_BY_SYSTEM.get(system) == "ANALYTICS_PROFILE"
    if system in ("account_service", "search_service", "export_service"):
        return "PRESENT"
    return "PRESENT" if has_direct_identifiers(o, include_ref) else "CLEAN"
