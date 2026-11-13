"""Catalog Presenter — Pure presentation functions and tenant-safe view models.

Guarantees:
- Strict Anti-Oracle isolation: Tenant-facing responses never contain source client
  slug, source client id, source client name, other client presence, or holder counts.
- Explicit allow-lists: Raw ORM objects and database models are never returned directly.
- Standardized typed dictionaries conforming to Phase C user experience specifications.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def build_preview_view(
    snapshot: Any,
    company: Any,
    quality_score: float,
    quality_grade: str,
    counts: Dict[str, int],
    already_exists: bool,
    existing_account_id: Optional[int],
    refresh_available: bool,
    fee_credits: int,
    estimated_credits_saved: int,
    fetch_fresh_estimate_credits: int,
    client_balance: int,
    requires_approval: bool,
    preview_token: str,
    expires_at: datetime,
) -> Dict[str, Any]:
    """Builds a tenant-safe PreviewView for GET /api/catalog/preview."""
    now = datetime.now(timezone.utc)
    taken_at_dt = snapshot.taken_at if snapshot and snapshot.taken_at else now
    if taken_at_dt.tzinfo is None:
        taken_at_dt = taken_at_dt.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - taken_at_dt).days)

    if age_days <= 15:
        freshness = "fresh"
        freshness_label = f"Fresh ({age_days}d ago)" if age_days > 0 else "Fresh (today)"
    elif age_days <= 45:
        freshness = "stale"
        freshness_label = f"Stale ({age_days}d ago)"
    else:
        freshness = "old"
        freshness_label = f"Old ({age_days}d ago)"

    # Rubric breakdown: derived deterministically from snapshot quality
    # Accounts: 30 max, LOBs: 25 max, Personas: 25 max, Signal Activity: 20 max
    acc_count = counts.get("accounts", 1)
    lobs_count = counts.get("lobs", 0)
    per_count = counts.get("personas", 0)
    sig_count = counts.get("posts", 0) + counts.get("cxo_movements", 0) + counts.get("jobs", 0)

    score_total = round(float(quality_score), 1)
    acc_pts = min(30.0, 30.0 if acc_count >= 1 else 0.0)
    lob_pts = min(25.0, round(lobs_count * 8.33, 1))
    per_pts = min(25.0, round(per_count * 5.0, 1))
    sig_pts = min(20.0, round(sig_count * 2.0, 1))
    
    # Scale points so their sum matches score_total
    raw_sum = acc_pts + lob_pts + per_pts + sig_pts
    if raw_sum > 0:
        factor = score_total / raw_sum
        acc_pts = round(acc_pts * factor, 1)
        lob_pts = round(lob_pts * factor, 1)
        per_pts = round(per_pts * factor, 1)
        sig_pts = round(score_total - (acc_pts + lob_pts + per_pts), 1)
    else:
        acc_pts = lob_pts = per_pts = sig_pts = 0.0

    rubric_breakdown = [
        {"criterion": "Core Enterprise Metadata", "points": acc_pts, "max": 30.0},
        {"criterion": "Lines of Business Hierarchy", "points": lob_pts, "max": 25.0},
        {"criterion": "Key Decision Makers & Personas", "points": per_pts, "max": 25.0},
        {"criterion": "Signal Intelligence & Activity", "points": sig_pts, "max": 20.0},
    ]

    # Categories included
    included = [
        {
            "category": "Company Profile",
            "count": counts.get("accounts", 1),
            "description": "Verified legal entity, domains, corporate structure, and industry classification",
        },
        {
            "category": "Lines of Business",
            "count": counts.get("lobs", 0),
            "description": "Operating divisions, commercial lines, domains, and revenue segments",
        },
        {
            "category": "Sub-LOBs",
            "count": counts.get("sub_lobs", 0),
            "description": "Granular business units and functional subdivisions",
        },
        {
            "category": "Executives & Personas",
            "count": counts.get("personas", 0),
            "description": "Management tier, verified seniority, department hierarchy, and public titles",
        },
        {
            "category": "Social Posts",
            "count": counts.get("posts", 0),
            "description": "Recent corporate announcements and executive social updates within 90 days",
        },
        {
            "category": "CXO Movements",
            "count": counts.get("cxo_movements", 0),
            "description": "Leadership transitions, new appointments, and executive departures",
        },
        {
            "category": "Hiring Signals",
            "count": counts.get("jobs", 0),
            "description": "Active recruitment signals and expansion drivers",
        },
    ]

    # Items regenerated on-demand for this specific tenant (from share policy)
    regenerated = [
        {
            "item": "Personalized Icebreakers",
            "label": "Account-specific opening hooks",
            "trigger": "Generated on demand when viewing contact profile",
        },
        {
            "item": "Value Propositions",
            "label": "Tailored pitch alignment",
            "trigger": "Synthesized dynamically based on your product offerings",
        },
        {
            "item": "Operational Pain Points",
            "label": "Executive challenge analysis",
            "trigger": "Generated on first view using your sales persona playbook",
        },
        {
            "item": "Key Objections & Talking Points",
            "label": "Anticipated objections & counters",
            "trigger": "Generated on demand during call preparation",
        },
        {
            "item": "Target KPIs & Metrics",
            "label": "Persona success metrics",
            "trigger": "Synthesized on first view",
        },
        {
            "item": "Communication Style & Tone",
            "label": "Behavioral outreach profile",
            "trigger": "Generated on demand",
        },
    ]

    # Sensitive items not included
    not_included = [
        {
            "item": "Direct Personal Contact Info",
            "description": "Direct personal mobile numbers and personal email addresses are not shared; enrich via your configured integrations.",
        },
        {
            "item": "CRM & Private Deal Data",
            "description": "Internal CRM notes, stage history, and opportunity pipeline data remain completely isolated.",
        },
    ]

    # Recommendation logic
    if age_days > 45:
        recommendation = "fetch_fresh"
        recommendation_reason = f"Catalog data is {age_days} days old — fetching fresh intelligence is recommended for fast-moving market signals."
    elif age_days > 15:
        recommendation = "use_verified_then_refresh"
        recommendation_reason = f"Catalog data is {age_days} days old — import verified intelligence instantly now, then queue a background freshness refresh."
    else:
        recommendation = "use_verified"
        recommendation_reason = "Catalog data is fresh and verified with high coverage — verified reuse saves significant API credits and pipeline time."

    sufficient_credits = client_balance >= fee_credits
    balance_after = max(0, client_balance - fee_credits) if sufficient_credits else client_balance
    net_savings = max(0, estimated_credits_saved - fee_credits)

    pricing = {
        "fee_credits": fee_credits,
        "reuse_fee_credits": fee_credits,
        "estimated_credits_saved": estimated_credits_saved,
        "savings_credits": estimated_credits_saved,
        "fetch_fresh_estimate_credits": fetch_fresh_estimate_credits,
        "net_savings": net_savings,
        "balance_before": client_balance,
        "balance_after": balance_after,
        "sufficient_credits": sufficient_credits,
    }

    return {
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_version": snapshot.version if snapshot else 1,
        "company": {
            "display_name": getattr(company, "display_name", None) or getattr(company, "name", "Enterprise Account") if company else "Enterprise Account",
            "canonical_key": getattr(company, "canonical_key", None) if company else None,
            "country": getattr(company, "country", None) if company else None,
        },
        "taken_at": taken_at_dt.isoformat(),
        "age_days": age_days,
        "freshness": freshness,
        "freshness_label": freshness_label,
        "quality": {
            "score": score_total,
            "grade": quality_grade,
            "rubric_breakdown": rubric_breakdown,
        },
        "rubric": {
            "total_score": score_total,
            "grade": quality_grade,
            "breakdown": rubric_breakdown,
        },
        "already_exists": bool(already_exists),
        "existing_account_id": existing_account_id,
        "refresh_available": bool(refresh_available),
        "included": included,
        "regenerated": regenerated,
        "not_included": not_included,
        "pricing": pricing,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "requires_approval": bool(requires_approval),
        "preview_token": preview_token,
        "expires_at": expires_at.isoformat(),
    }


def build_diff_view(
    account_id: int,
    company_name: str,
    snapshot_version_from: int,
    snapshot_version_to: int,
    summary: Dict[str, Any],
    details: Dict[str, List[Dict[str, Any]]],
    local_edits_present: bool,
    protected_rows: List[Dict[str, Any]],
    fee_credits: int,
    plan_hash: str,
) -> Dict[str, Any]:
    """Builds a tenant-safe DiffView for GET /api/accounts/{id}/reuse/refresh-preview."""
    total_changes = (
        summary.get("personas", {}).get("changed", 0)
        + summary.get("personas", {}).get("added", 0)
        + summary.get("lobs", {}).get("added", 0)
        + summary.get("lobs", {}).get("changed", 0)
        + len(summary.get("account_fields_changed", []))
    )

    if total_changes == 0 and not local_edits_present:
        status = "up_to_date"
    elif local_edits_present:
        status = "blocked_local_edits"
    else:
        status = "available"

    return {
        "account_id": account_id,
        "company_name": company_name,
        "summary": summary,
        "details": details,
        "local_edits": {
            "present": local_edits_present,
            "protected_rows": protected_rows,
        },
        "refresh_policy": {
            "status": status,
            "fee_credits": fee_credits,
            "snapshot_version_from": snapshot_version_from,
            "snapshot_version_to": snapshot_version_to,
        },
        "plan_hash": plan_hash,
    }


def build_request_view(
    event: Any,
    company_name: str,
    canonical_key: Optional[str],
    snapshot_version: int,
    sla_hours: int = 24,
    already_pending: bool = False,
) -> Dict[str, Any]:
    """Builds a tenant-safe RequestView for reuse request status."""
    timeline = []
    
    # 1. Submitted
    created_at_str = event.created_at.isoformat() if event.created_at else None
    timeline.append({
        "step": "submitted",
        "status": "completed",
        "timestamp": created_at_str,
        "label": "Reuse request submitted",
    })

    # 2. Review
    if event.status == "pending_approval":
        timeline.append({
            "step": "review",
            "status": "in_progress",
            "timestamp": None,
            "label": "Under review by platform administrator",
        })
    elif event.status in ("done", "approved"):
        timeline.append({
            "step": "review",
            "status": "completed",
            "timestamp": event.decided_at.isoformat() if getattr(event, "decided_at", None) else None,
            "label": "Approved by platform administrator",
        })
    elif event.status == "rejected":
        timeline.append({
            "step": "review",
            "status": "rejected",
            "timestamp": event.decided_at.isoformat() if getattr(event, "decided_at", None) else None,
            "label": f"Request rejected: {event.decision_note or event.reason or 'No reason provided'}",
        })

    # 3. Fulfillment
    if event.status == "done":
        timeline.append({
            "step": "fulfillment",
            "status": "completed",
            "timestamp": event.completed_at.isoformat() if event.completed_at else None,
            "label": "Intelligence hydrated into your workspace",
        })
    elif event.status == "failed":
        timeline.append({
            "step": "fulfillment",
            "status": "failed",
            "timestamp": event.completed_at.isoformat() if event.completed_at else None,
            "label": "Hydration failed; credits were refunded",
        })

    account_id = None
    created_ids = getattr(event, "created_ids", None)
    if created_ids and isinstance(created_ids, dict):
        accts = created_ids.get("accounts", [])
        if accts:
            account_id = accts[0]

    return {
        "request_id": getattr(event, "id", None),
        "status": getattr(event, "status", "pending_approval"),
        "created_at": created_at_str,
        "company_name": company_name,
        "canonical_key": canonical_key,
        "snapshot_version": snapshot_version,
        "mode": getattr(event, "mode", "hydrate"),
        "fee_credits": getattr(event, "fee_credits", 0),
        "reason": getattr(event, "reason", None),
        "decision_note": getattr(event, "decision_note", None),
        "account_id": account_id,
        "timeline": timeline,
        "sla_hint": f"Most requests are reviewed within {sla_hours} hours.",
        "already_pending": bool(already_pending),
    }


def build_provenance_view(
    origin_type: Optional[str],
    local_edits: bool,
    snapshot_version: Optional[int],
    hydrated_at: Optional[datetime],
    hydrated_by_name: Optional[str],
    latest_snapshot_version: Optional[int],
    snapshot_taken_at: Optional[datetime],
) -> Dict[str, Any]:
    """Builds a tenant-safe ProvenanceView for accounts list and account detail."""
    now = datetime.now(timezone.utc)
    age_days = None
    freshness = "unknown"

    if snapshot_taken_at:
        t_at = snapshot_taken_at if snapshot_taken_at.tzinfo else snapshot_taken_at.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - t_at).days)
        freshness = "fresh" if age_days <= 15 else ("stale" if age_days <= 45 else "old")

    if origin_type == "catalog":
        if local_edits:
            kind = "edited_catalog"
            label = f"Verified catalog v{snapshot_version or 1} · edited"
        else:
            kind = "catalog"
            label = f"Verified catalog v{snapshot_version or 1}"
    elif origin_type == "manual":
        kind = "manual"
        label = "Manual"
    else:
        kind = "fetched"
        label = "Fetched"

    refresh_available = False
    if origin_type == "catalog" and latest_snapshot_version and snapshot_version:
        refresh_available = latest_snapshot_version > snapshot_version

    return {
        "kind": kind,
        "label": label,
        "snapshot_version": snapshot_version,
        "hydrated_at": hydrated_at.isoformat() if hydrated_at else None,
        "hydrated_by": {"name": hydrated_by_name} if hydrated_by_name else None,
        "local_edits": bool(local_edits),
        "refresh_available": refresh_available,
        "latest_version": latest_snapshot_version,
        "age_days": age_days,
        "freshness": freshness,
    }


def build_ledger_entry_view(
    entry_id: int,
    created_at: datetime,
    entry_type: str,
    amount: int,
    balance_after: int,
    description: str,
    account_id: Optional[int] = None,
    account_name: Optional[str] = None,
    user_name: Optional[str] = None,
    ref_type: Optional[str] = None,
    ref_id: Optional[int] = None,
    estimated_credits_saved: Optional[int] = None,
    is_analyst_or_viewer: bool = False,
) -> Dict[str, Any]:
    """Builds a tenant-safe LedgerEntryView from public.credit_ledger."""
    at_str = created_at.isoformat() if created_at else None
    
    # Map type to clean tenant label
    # debit values are negative in credit_ledger, delta is signed amount
    delta = amount

    saved_vs_fresh = None
    if estimated_credits_saved is not None and delta < 0:
        saved_vs_fresh = max(0, estimated_credits_saved - abs(delta))

    # Mask user info for analyst or viewer
    display_user = None
    if user_name and not is_analyst_or_viewer:
        display_user = {"name": user_name}

    return {
        "id": entry_id,
        "at": at_str,
        "type": entry_type or "adjustment",
        "description": description,
        "account": {"id": account_id, "name": account_name} if (account_id and account_name) else None,
        "user": display_user,
        "delta": delta,
        "balance_after": balance_after,
        "ref": f"{ref_type}:{ref_id}" if (ref_type and ref_id) else None,
        "estimated_credits_saved": estimated_credits_saved,
        "saved_vs_fresh": saved_vs_fresh,
    }
