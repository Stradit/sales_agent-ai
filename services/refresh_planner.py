"""Refresh Planner — Computes granular diff between local tenant account and platform catalog snapshot.

Strict Guarantees:
- Pure comparison logic: never alters database state during planning.
- Anti-Oracle isolation: returns only diff between target tenant data and snapshot golden record.
- Local-Edit Awareness: identifies protected fields and rows marked as modified by the tenant.
- Deterministic Plan Hash: signs the planned changes with SHA-256 for safe execution.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.tenant import schema_for, validate_slug
from services.catalog_pricing import CatalogPricing

logger = logging.getLogger("services.refresh_planner")

ACCOUNT_COMPARE_FIELDS = [
    "display_name",
    "legal_name",
    "domain",
    "website_url",
    "employee_count",
    "revenue_range",
    "headquarters_location",
    "company_type",
    "overview",
    "industries",
]

LOB_COMPARE_FIELDS = [
    "lob_name",
    "domain",
    "website_url",
    "operating_head",
    "segment_headcount",
    "overview",
    "audited_segment_revenue",
]

SUB_LOB_COMPARE_FIELDS = [
    "name",
    "legal_name",
    "domain",
    "website_url",
    "country",
    "city",
    "status",
]

PERSONA_COMPARE_FIELDS = [
    "display_name",
    "full_name",
    "title",
    "departments",
    "tier",
    "email",
    "phone",
    "linkedin_url",
    "city",
    "country",
    "skills",
    "target_kpis",
]


def _truncate_val(val: Any, max_len: int = 300) -> Tuple[Any, bool]:
    """Truncates strings to max_len chars and returns (val, truncated_flag)."""
    if isinstance(val, str) and len(val) > max_len:
        return val[:max_len] + "...", True
    return val, False


def _normalize_val(val: Any) -> Any:
    """Normalizes values for comparison (strips whitespace, handles None and collections)."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (list, tuple)):
        return sorted([str(x).strip() for x in val if x is not None])
    if isinstance(val, dict):
        return {k: _normalize_val(v) for k, v in sorted(val.items())}
    return str(val)


class RefreshPlanner:
    """Computes difference between local tenant account entities and catalog snapshot golden record."""

    @classmethod
    def plan(
        cls,
        session: Session,
        schema: str,
        account_id: int,
        snapshot: Any,
        blob_payload: Dict[str, Any],
        is_super_admin: bool = False,
    ) -> Dict[str, Any]:
        """Generates tenant-safe diff view, protected row list, and plan hash."""
        clean_schema = schema_for(validate_slug(schema.replace("client_", "")))

        # 1. Fetch local account, origin, lobs, sub_lobs, personas
        local_acct_row = session.execute(
            text(f'SELECT * FROM "{clean_schema}"."accounts" WHERE id = :id'),
            {"id": account_id},
        ).mappings().first()

        if not local_acct_row:
            raise ValueError(f"Account {account_id} not found in schema {clean_schema}")

        local_origin = session.execute(
            text(f'SELECT * FROM "{clean_schema}"."account_origin" WHERE account_id = :id'),
            {"id": account_id},
        ).mappings().first()

        has_local_edits = bool(local_origin and local_origin["local_edits"])
        snapshot_version_from = local_origin["snapshot_version"] if local_origin and local_origin["snapshot_version"] else 1
        snapshot_version_to = snapshot.version

        local_lobs = session.execute(
            text(f'SELECT * FROM "{clean_schema}"."lobs" WHERE account_id = :id'),
            {"id": account_id},
        ).mappings().all()

        lob_ids = [l["id"] for l in local_lobs]
        local_sub_lobs = []
        if lob_ids:
            local_sub_lobs = session.execute(
                text(f'SELECT * FROM "{clean_schema}"."sub_lobs" WHERE lob_id = ANY(:lob_ids)'),
                {"lob_ids": lob_ids},
            ).mappings().all()

        local_personas = session.execute(
            text(f'SELECT * FROM "{clean_schema}"."personas" WHERE account_id = :id'),
            {"id": account_id},
        ).mappings().all()

        remote_acct = blob_payload.get("account", {})
        remote_lobs = blob_payload.get("lobs", [])
        remote_sub_lobs = blob_payload.get("sub_lobs", [])
        remote_personas = blob_payload.get("personas", [])
        remote_posts = blob_payload.get("posts", [])
        remote_cxo = blob_payload.get("cxo_movements", [])
        remote_jobs = blob_payload.get("linkedin_jobs", [])

        # Bounded diff computation guard (Phase C C6 performance & stability)
        MAX_DIFF_ROWS = 2000
        if (
            len(local_personas) > MAX_DIFF_ROWS
            or len(remote_personas) > MAX_DIFF_ROWS
            or len(local_lobs) > MAX_DIFF_ROWS
            or len(remote_lobs) > MAX_DIFF_ROWS
            or len(local_sub_lobs) > MAX_DIFF_ROWS
            or len(remote_sub_lobs) > MAX_DIFF_ROWS
        ):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail="Dataset exceeds maximum diff computation limit (2,000 rows per table). Please contact support for assisted sync.",
            )

        protected_rows: List[Dict[str, Any]] = []
        table_edits_count: Dict[str, int] = {}

        # 2. Diff Account Fields
        account_diffs = []
        account_fields_changed = []
        for field in ACCOUNT_COMPARE_FIELDS:
            loc_val = local_acct_row.get(field)
            rem_val = remote_acct.get(field)
            if _normalize_val(loc_val) != _normalize_val(rem_val):
                account_fields_changed.append(field)
                old_val, old_trunc = _truncate_val(loc_val)
                new_val, new_trunc = _truncate_val(rem_val)
                account_diffs.append({
                    "field": field,
                    "section": "Company Profile",
                    "old": old_val,
                    "new": new_val,
                    "truncated": old_trunc or new_trunc,
                    "status": "changed",
                    "is_local_edited": has_local_edits,
                })

        if has_local_edits:
            table_edits_count["accounts"] = 1

        # 3. Diff LOBs
        lob_summary = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
        lob_diffs = []

        local_lob_by_key = {
            (l.get("key") or (l.get("lob_name") or "")).strip().lower(): l
            for l in local_lobs if l.get("lob_name") or l.get("key")
        }

        matched_local_lob_keys = set()
        for r_lob in remote_lobs:
            r_key = (r_lob.get("key") or r_lob.get("lob_name") or r_lob.get("name") or "").strip().lower()
            matched_local = local_lob_by_key.get(r_key)
            if not matched_local:
                lob_summary["added"] += 1
                r_name = r_lob.get("lob_name") or r_lob.get("name")
                lob_diffs.append({
                    "key": r_lob.get("key") or r_name,
                    "label": r_name,
                    "status": "added",
                    "changes": [{"field": "lob_name", "old": None, "new": r_name, "truncated": False}],
                    "is_local_edited": False,
                })
            else:
                matched_local_lob_keys.add(r_key)
                field_diffs = []
                for f in LOB_COMPARE_FIELDS:
                    lv = matched_local.get(f)
                    rv = r_lob.get(f)
                    if _normalize_val(lv) != _normalize_val(rv):
                        old_v, o_trunc = _truncate_val(lv)
                        new_v, n_trunc = _truncate_val(rv)
                        field_diffs.append({
                            "field": f,
                            "old": old_v,
                            "new": new_v,
                            "truncated": o_trunc or n_trunc,
                        })

                is_row_edited = bool(matched_local.get("is_manually_verified") or matched_local.get("local_edited_at"))
                if is_row_edited:
                    protected_rows.append({
                        "table": "lobs",
                        "id": matched_local["id"],
                        "key": matched_local.get("key") or matched_local.get("lob_name"),
                        "label": matched_local.get("lob_name"),
                    })
                    table_edits_count["lobs"] = table_edits_count.get("lobs", 0) + 1

                if field_diffs:
                    lob_summary["changed"] += 1
                    lob_diffs.append({
                        "key": matched_local.get("key") or matched_local.get("lob_name"),
                        "label": matched_local.get("lob_name"),
                        "status": "changed",
                        "changes": field_diffs,
                        "is_local_edited": is_row_edited,
                    })
                else:
                    lob_summary["unchanged"] += 1

        # Check for unreferenced local LOBs
        for l_key, l_row in local_lob_by_key.items():
            if l_key not in matched_local_lob_keys:
                lob_summary["unchanged"] += 1

        # 4. Diff Sub-LOBs
        sub_lob_summary = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
        sub_lob_diffs = []
        local_sub_lob_by_key = {
            (s.get("name") or "").strip().lower(): s
            for s in local_sub_lobs if s.get("name")
        }
        matched_local_sub_lob_keys = set()
        for r_sub in remote_sub_lobs:
            s_name = (r_sub.get("name") or "").strip().lower()
            matched_s = local_sub_lob_by_key.get(s_name)
            if not matched_s:
                sub_lob_summary["added"] += 1
                sub_lob_diffs.append({
                    "key": s_name,
                    "label": r_sub.get("name"),
                    "status": "added",
                    "changes": [{"field": "name", "old": None, "new": r_sub.get("name"), "truncated": False}],
                    "is_local_edited": False,
                })
            else:
                matched_local_sub_lob_keys.add(s_name)
                s_diffs = []
                for sf in SUB_LOB_COMPARE_FIELDS:
                    lv = matched_s.get(sf)
                    rv = r_sub.get(sf)
                    if _normalize_val(lv) != _normalize_val(rv):
                        old_v, o_trunc = _truncate_val(lv)
                        new_v, n_trunc = _truncate_val(rv)
                        s_diffs.append({
                            "field": sf,
                            "old": old_v,
                            "new": new_v,
                            "truncated": o_trunc or n_trunc,
                        })
                is_s_edited = bool(matched_s.get("is_manually_verified") or matched_s.get("local_edited_at"))
                if is_s_edited:
                    protected_rows.append({
                        "table": "sub_lobs",
                        "id": matched_s["id"],
                        "key": s_name,
                        "label": matched_s.get("name"),
                    })
                    table_edits_count["sub_lobs"] = table_edits_count.get("sub_lobs", 0) + 1

                if s_diffs:
                    sub_lob_summary["changed"] += 1
                    sub_lob_diffs.append({
                        "key": s_name,
                        "label": matched_s.get("name"),
                        "status": "changed",
                        "changes": s_diffs,
                        "is_local_edited": is_s_edited,
                    })
                else:
                    sub_lob_summary["unchanged"] += 1

        # 5. Diff Personas
        persona_summary = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0}
        persona_diffs = []

        local_per_by_key = {}
        for p in local_personas:
            p_key = (p.get("key") or p.get("email") or p.get("display_name") or p.get("full_name") or "").strip().lower()
            if p_key:
                local_per_by_key[p_key] = p

        matched_local_persona_keys = set()
        for r_per in remote_personas:
            r_key = (r_per.get("key") or r_per.get("email") or r_per.get("display_name") or r_per.get("full_name") or r_per.get("name") or "").strip().lower()
            matched_p = local_per_by_key.get(r_key)
            if not matched_p:
                persona_summary["added"] += 1
                p_label = r_per.get("display_name") or r_per.get("full_name") or r_per.get("name")
                persona_diffs.append({
                    "key": r_key,
                    "label": p_label,
                    "title": r_per.get("title"),
                    "status": "added",
                    "changes": [{"field": "name", "old": None, "new": p_label, "truncated": False}],
                    "is_local_edited": False,
                })
            else:
                matched_local_persona_keys.add(r_key)
                p_diffs = []
                for pf in PERSONA_COMPARE_FIELDS:
                    lv = matched_p.get(pf)
                    rv = r_per.get(pf)
                    if _normalize_val(lv) != _normalize_val(rv):
                        old_v, o_trunc = _truncate_val(lv)
                        new_v, n_trunc = _truncate_val(rv)
                        p_diffs.append({
                            "field": pf,
                            "old": old_v,
                            "new": new_v,
                            "truncated": o_trunc or n_trunc,
                        })

                is_per_edited = bool(matched_p.get("is_manually_verified") or matched_p.get("local_edited_at"))
                if is_per_edited:
                    protected_rows.append({
                        "table": "personas",
                        "id": matched_p["id"],
                        "key": r_key,
                        "label": matched_p.get("display_name") or matched_p.get("full_name"),
                    })
                    table_edits_count["personas"] = table_edits_count.get("personas", 0) + 1

                if p_diffs:
                    persona_summary["changed"] += 1
                    persona_diffs.append({
                        "key": r_key,
                        "label": matched_p.get("display_name") or matched_p.get("full_name"),
                        "title": matched_p.get("title"),
                        "status": "changed",
                        "changes": p_diffs,
                        "is_local_edited": is_per_edited,
                    })
                else:
                    persona_summary["unchanged"] += 1

        summary = {
            "account_fields_changed": account_fields_changed,
            "lobs": lob_summary,
            "sub_lobs": sub_lob_summary,
            "personas": persona_summary,
            "posts": {"added": len(remote_posts)},
            "cxo_movements": {"added": len(remote_cxo)},
            "jobs": {"added": len(remote_jobs)},
        }

        details = {
            "account": account_diffs,
            "lobs": lob_diffs,
            "sub_lobs": sub_lob_diffs,
            "personas": persona_diffs,
        }

        tables_edited = [{"table": t, "rows_edited": cnt} for t, cnt in sorted(table_edits_count.items())]

        # Deterministic Plan Hash
        canonical_plan_bytes = json.dumps(
            {
                "account_id": account_id,
                "snapshot_id": snapshot.id,
                "summary": summary,
                "details": details,
                "protected_count": len(protected_rows),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        plan_hash = hashlib.sha256(canonical_plan_bytes).hexdigest()

        # Fee computation
        fee_credits = CatalogPricing.fee_for(snapshot=snapshot, mode="refresh", is_super_admin=is_super_admin)

        local_edits_present = has_local_edits or len(protected_rows) > 0

        total_changes = (
            summary["personas"]["changed"]
            + summary["personas"]["added"]
            + summary["lobs"]["added"]
            + summary["lobs"]["changed"]
            + summary["sub_lobs"]["added"]
            + summary["sub_lobs"]["changed"]
            + len(summary["account_fields_changed"])
        )

        if total_changes == 0 and not local_edits_present:
            status = "up_to_date"
        elif local_edits_present:
            status = "blocked_local_edits"
        else:
            status = "available"

        return {
            "account_id": account_id,
            "company_name": local_acct_row.get("display_name") or local_acct_row.get("legal_name") or "Account",
            "snapshot_version_from": snapshot_version_from,
            "snapshot_version_to": snapshot_version_to,
            "summary": summary,
            "details": details,
            "local_edits": {
                "present": local_edits_present,
                "tables": tables_edited,
                "protected_rows": protected_rows,
            },
            "local_edits_present": local_edits_present,
            "protected_rows": protected_rows,
            "refresh_policy": {
                "status": status,
                "fee_credits": fee_credits,
                "snapshot_version_from": snapshot_version_from,
                "snapshot_version_to": snapshot_version_to,
            },
            "fee_credits": fee_credits,
            "plan_hash": plan_hash,
        }
