"""Extract records from screenshots (PNG) and PDF documents with Claude vision / document input."""
from __future__ import annotations

import base64

from extract import Observation, system_for

SYSTEM = """You extract structured records from internal business artifacts of an e-commerce company
(admin console screenshots, support attachments, registers, reports). For every data record that has an
internal resource identifier (account_123, search_45, analytics_6, support_7, export_8, data_flow_9,
backup_10, audit_record_11, tombstone_12, legal_hold_13, marketing_14, ...) return one entry with the
facts shown for that record. Copy identifiers exactly as displayed. Use empty lists / null when a fact is
not shown. Put an internal resource identifier in resource_id only when it is printed verbatim; never
construct one. When a record is shown under another reference instead (a statement number, ticket id,
stored file name, customer key / user reference shown as the page subject), leave resource_id empty and put that exact
reference in display_reference."""

KINDS = ["account_profile", "search_document", "analytics_profile", "support_message", "support_attachment",
         "user_export", "data_flow", "backup_archive", "security_audit_record", "account_tombstone",
         "legal_hold_record", "marketing_profile", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "display_reference": {"type": "string"},
                    "record_kind": {"type": "string", "enum": KINDS},
                    "exists": {"type": "string", "enum": ["present", "absent", "unknown"]},
                    "emails": {"type": "array", "items": {"type": "string"}},
                    "phones": {"type": "array", "items": {"type": "string"}},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "user_refs": {"type": "array", "items": {"type": "string"}},
                    "email_hashes": {"type": "array", "items": {"type": "string"}},
                    "data_flow_enabled": {"type": ["boolean", "null"]},
                    "restore_allowed": {"type": ["boolean", "null"]},
                    "related_resource_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["resource_id", "display_reference", "record_kind", "exists", "emails", "phones", "names", "user_refs",
                             "email_hashes", "data_flow_enabled", "restore_allowed", "related_resource_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["records"],
    "additionalProperties": False,
}

RECORD_TYPE = {"support_attachment": "SUPPORT_ATTACHMENT", "marketing_profile": "MARKETING_PROFILE",
               "legal_hold_record": "LEGAL_HOLD_RECORD", "account_tombstone": "ACCOUNT_TOMBSTONE"}


def _content(path):
    data = base64.standard_b64encode(path.read_bytes()).decode()
    if path.suffix.lower() == ".pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    return [block, {"type": "text", "text": f"Artifact file name: {path.name}. Extract the records."}]


def source_label(eid: str, path) -> str:
    return f"{eid} {path.name}"


def make_media_extractor(llm, aliases: dict):
    """aliases: business reference shown in artifacts -> internal resource id (from the data catalog)."""
    def extract(eid, path, cap):
        result = llm.json_call("media_extraction", SYSTEM, _content(path), SCHEMA, max_tokens=8000,
                               source=source_label(eid, path))
        obs = []
        for r in result["records"]:
            rid = r["resource_id"].strip() or aliases.get(r["display_reference"].strip(), "")
            system = system_for(rid)
            if not system:
                continue
            fields = {}
            for key, values in (("email", r["emails"]), ("phone", r["phones"]), ("full_name", r["names"]),
                                ("email_hash", r["email_hashes"]), ("customer_reference", r["user_refs"])):
                for i, v in enumerate(values):
                    fields[f"{key}.{i}"] = v
            for i, other in enumerate(r["related_resource_ids"]):
                fields[f"related.{i}"] = other
            if r["data_flow_enabled"] is not None:
                fields["enabled"] = r["data_flow_enabled"]
            if r["restore_allowed"] is not None:
                fields["restore_allowed"] = r["restore_allowed"]
            elif system == "backup_service" and r["exists"] != "absent":
                # an archive listed as restorable inventory with no restore block shown can still be restored
                fields["restore_allowed"] = True
            if r["exists"] == "absent":
                fields["rows"] = 0
            record_type = RECORD_TYPE.get(r["record_kind"])
            if system == "support_platform" and path.suffix.lower() == ".pdf":
                record_type = "SUPPORT_ATTACHMENT"  # the uploaded document itself is the ticket attachment
            obs.append(Observation(
                evidence_id=eid, file_captured_at=cap, observed_at=cap, system_id=system, resource_id=rid,
                fields=fields, record_type=record_type, location=path.name,
                source_kind="media",
            ))
        return obs
    return extract
