"""Credits & Billing Router — Multi-Tenant Credit Transparency, Ledgers, Summaries, and Audited Exports.

Endpoints:
- GET /api/credits/ledger: Paginated and filterable credit transactions from public.credit_ledger
- GET /api/credits/summary: Multi-period aggregate usage, balance, savings from verified reuse, and daily series
- GET /api/credits/ledger/export.csv: Formula-injection safe streaming CSV export for client administrators
- GET /api/credits/platform-summary: Platform administrator view of global credit consumption and catalog reuse metrics
"""

import csv
from datetime import datetime, timedelta, timezone
import io
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth
from core.tenant_context import (
    PlatformRequestContext,
    TenantRequestContext,
    get_platform_db,
    platform_context,
    tenant_context,
)
from db.models.client import Client
from services.catalog_presenter import build_ledger_entry_view, sanitize_csv_cell

logger = logging.getLogger(__name__)

credit_router = APIRouter(prefix="/api/credits", tags=["Credits & Billing"])


def _classify_ledger_type(reason: str, delta: int) -> str:
    r_lower = (reason or "").lower()
    if "refresh" in r_lower:
        return "refresh_fee"
    if "reuse" in r_lower or "catalog_reuse" in r_lower:
        return "reuse_fee"
    if delta > 0 and ("refund" in r_lower or "rollback" in r_lower):
        return "refund"
    if delta > 0 and ("top_up" in r_lower or "grant" in r_lower or "allocation" in r_lower):
        return "top_up"
    if "reserve:run" in r_lower or "settle:" in r_lower or "pipeline" in r_lower:
        return "pipeline_run"
    return "adjustment"


def _clean_description(reason: str, entry_type: str) -> str:
    if entry_type == "reuse_fee":
        return "Verified intelligence catalog reuse fee"
    if entry_type == "refresh_fee":
        return "Verified intelligence catalog refresh fee"
    if entry_type == "refund":
        return "Credit refund / transaction rollback"
    if entry_type == "top_up":
        return "Credit allocation / top-up"
    if entry_type == "pipeline_run":
        return "Autonomous pipeline enrichment"
    return reason or "Credit adjustment"


@credit_router.get("/ledger")
def get_credits_ledger(
    from_date: Optional[str] = Query(None, alias="from", description="ISO start date filter"),
    to_date: Optional[str] = Query(None, alias="to", description="ISO end date filter"),
    type: Optional[str] = Query(None, description="Transaction type filter (reuse_fee, refresh_fee, refund, pipeline_run, adjustment)"),
    account_id: Optional[int] = Query(None, description="Optional account ID filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Page size"),
    cursor: Optional[int] = Query(None, description="Keyset cursor: return records with id < cursor"),
    ctx: TenantRequestContext = Depends(tenant_context("account:read", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Retrieves immutable credit ledger records for the tenant.
    
    Analyst and Viewer roles see ledger transactions with user identities masked.
    Client Admin and Super Admin see full transaction details including authoring user.
    """
    is_analyst_or_viewer = (ctx.effective_role in ("analyst", "viewer"))

    where_clauses = ["cl.client_id = :cid"]
    params: Dict[str, Any] = {"cid": ctx.client_id}

    if from_date:
        where_clauses.append("cl.created_at >= :from_date")
        params["from_date"] = from_date
    if to_date:
        where_clauses.append("cl.created_at <= :to_date")
        params["to_date"] = to_date
    if account_id:
        where_clauses.append("cl.account_id = :account_id")
        params["account_id"] = account_id

    if type:
        t_clean = type.strip().lower()
        if t_clean == "reuse_fee":
            where_clauses.append("cl.delta < 0 AND (cl.reason ILIKE '%reuse%' OR cl.reason ILIKE 'catalog_reuse:hydrate%')")
        elif t_clean == "refresh_fee":
            where_clauses.append("cl.delta < 0 AND (cl.reason ILIKE '%refresh%')")
        elif t_clean == "refund":
            where_clauses.append("cl.delta > 0 AND (cl.reason ILIKE '%refund%' OR cl.reason ILIKE '%rollback%')")
        elif t_clean == "top_up":
            where_clauses.append("cl.delta > 0 AND (cl.reason ILIKE '%top_up%' OR cl.reason ILIKE '%grant%')")
        elif t_clean == "pipeline_run":
            where_clauses.append("(cl.reason ILIKE 'reserve:run%' OR cl.reason ILIKE 'settle:%' OR cl.reason ILIKE '%pipeline%')")
        else:
            where_clauses.append("cl.reason ILIKE :type_pat")
            params["type_pat"] = f"%{t_clean}%"

    where_sql = " AND ".join(where_clauses)

    # 1. Total matching count
    count_sql = text(f"SELECT COUNT(*) FROM public.credit_ledger cl WHERE {where_sql}")
    total_count = session.execute(count_sql, params).scalar() or 0

    # 2. Keyset vs offset pagination
    if cursor is not None:
        pagination_sql = f"{where_sql} AND cl.id < :cursor ORDER BY cl.created_at DESC, cl.id DESC LIMIT :lim"
        query_params = {**params, "cursor": cursor, "lim": page_size}
    else:
        offset = (page - 1) * page_size
        pagination_sql = f"{where_sql} ORDER BY cl.created_at DESC, cl.id DESC LIMIT :lim OFFSET :off"
        query_params = {**params, "lim": page_size, "off": offset}

    query_str = f"""
        SELECT cl.id, cl.created_at, cl.delta, cl.balance_after, cl.reason, cl.account_id,
               cl.ref_type, cl.ref_id,
               se.estimated_credits_saved,
               u.full_name AS user_name
        FROM public.credit_ledger cl
        LEFT JOIN platform_catalog.share_events se ON (cl.ref_type = 'share_event' AND cl.ref_id = se.id)
        LEFT JOIN public.users u ON (se.requested_by = u.id)
        WHERE {pagination_sql}
    """
    rows = session.execute(text(query_str), query_params).fetchall()

    # 3. Batch resolve account names from tenant schema
    account_ids = {r[5] for r in rows if r[5] is not None}
    account_names: Dict[int, str] = {}
    if account_ids and ctx.schema:
        try:
            acct_rows = session.execute(
                text(f"""
                    SELECT id, name
                    FROM "{ctx.schema}"."accounts"
                    WHERE id = ANY(:aids)
                """),
                {"aids": list(account_ids)},
            ).fetchall()
            for ar in acct_rows:
                account_names[ar[0]] = ar[1]
        except Exception as e:
            logger.warning(f"Failed to resolve tenant account names from {ctx.schema}: {e}")

    # 4. Build view entries
    items = []
    last_id = None
    for r in rows:
        eid = r[0]
        cat = r[1]
        delta = r[2]
        bal_after = r[3]
        reason = r[4] or ""
        aid = r[5]
        ref_type = r[6]
        ref_id = r[7]
        est_saved = r[8]
        u_name = r[9]

        entry_type = _classify_ledger_type(reason, delta)
        description = _clean_description(reason, entry_type)
        acct_name = account_names.get(aid) if aid else None

        entry_view = build_ledger_entry_view(
            entry_id=eid,
            created_at=cat,
            entry_type=entry_type,
            amount=delta,
            balance_after=bal_after if bal_after is not None else 0,
            description=description,
            account_id=aid,
            account_name=acct_name,
            user_name=u_name,
            ref_type=ref_type,
            ref_id=ref_id,
            estimated_credits_saved=est_saved,
            is_analyst_or_viewer=is_analyst_or_viewer,
        )
        items.append(entry_view)
        last_id = eid

    next_cursor = last_id if len(items) == page_size else None

    return {
        "items": items,
        "total": total_count,
        "page": page,
        "page_size": page_size,
        "next_cursor": next_cursor,
    }


@credit_router.get("/summary")
def get_credits_summary(
    period: str = Query("month", description="Aggregation period: month | quarter | all | custom"),
    from_date: Optional[str] = Query(None, description="ISO start date when period=custom"),
    to_date: Optional[str] = Query(None, description="ISO end date when period=custom"),
    ctx: TenantRequestContext = Depends(tenant_context("account:read", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Retrieves credit consumption summary, reuse savings, and daily series for the client."""
    now = datetime.now(timezone.utc)
    p_lower = (period or "month").lower()

    period_start = None
    period_end = None

    if p_lower == "month":
        period_start = now - timedelta(days=30)
    elif p_lower == "quarter":
        period_start = now - timedelta(days=90)
    elif p_lower == "custom":
        if from_date:
            try:
                period_start = datetime.fromisoformat(from_date)
            except Exception:
                pass
        if to_date:
            try:
                period_end = datetime.fromisoformat(to_date)
            except Exception:
                pass

    # 1. Client balance & allocations
    client = session.query(Client).filter_by(id=ctx.client_id).first()
    total_allocated = int(client.api_credits_total or 0) if client else 0
    total_used = int(client.api_credits_used or 0) if client else 0
    balance = max(0, total_allocated - total_used)

    # 2. Reuse metrics from platform_catalog.share_events (using stored values, never recomputed)
    reuse_where = ["target_client_id = :cid", "status IN ('done', 'approved')"]
    reuse_params: Dict[str, Any] = {"cid": ctx.client_id}
    if period_start:
        reuse_where.append("completed_at >= :pstart")
        reuse_params["pstart"] = period_start
    if period_end:
        reuse_where.append("completed_at <= :pend")
        reuse_params["pend"] = period_end

    reuse_sql = text(f"""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(fee_credits), 0) AS fees,
               COALESCE(SUM(estimated_credits_saved), 0) AS saved
        FROM platform_catalog.share_events
        WHERE {' AND '.join(reuse_where)}
    """)
    r_row = session.execute(reuse_sql, reuse_params).fetchone()
    reuse_count = int(r_row[0] or 0) if r_row else 0
    reuse_fees_paid = int(r_row[1] or 0) if r_row else 0
    reuse_estimated_saved = int(r_row[2] or 0) if r_row else 0
    reuse_net_savings = max(0, reuse_estimated_saved - reuse_fees_paid)

    # 3. Spend by type from public.credit_ledger
    ledger_where = ["client_id = :cid"]
    ledger_params: Dict[str, Any] = {"cid": ctx.client_id}
    if period_start:
        ledger_where.append("created_at >= :pstart")
        ledger_params["pstart"] = period_start
    if period_end:
        ledger_where.append("created_at <= :pend")
        ledger_params["pend"] = period_end

    ledger_sql = text(f"""
        SELECT delta, reason
        FROM public.credit_ledger
        WHERE {' AND '.join(ledger_where)}
    """)
    ledger_rows = session.execute(ledger_sql, ledger_params).fetchall()

    spent_by_type_map: Dict[str, Dict[str, int]] = {}
    refunds_total = 0

    for l_row in ledger_rows:
        delta = int(l_row[0] or 0)
        reason = l_row[1] or ""
        entry_type = _classify_ledger_type(reason, delta)

        if delta < 0:
            spent = abs(delta)
            if entry_type not in spent_by_type_map:
                spent_by_type_map[entry_type] = {"type": entry_type, "total_spent": 0, "count": 0}
            spent_by_type_map[entry_type]["total_spent"] += spent
            spent_by_type_map[entry_type]["count"] += 1
        elif delta > 0 and entry_type == "refund":
            refunds_total += delta

    spent_by_type = list(spent_by_type_map.values())
    spent_by_type.sort(key=lambda x: x["total_spent"], reverse=True)

    # 4. Top accounts by consumption
    top_accts_sql = text(f"""
        SELECT account_id, ABS(SUM(delta)) AS total_debit, COUNT(*) AS tx_count
        FROM public.credit_ledger
        WHERE {' AND '.join(ledger_where)} AND account_id IS NOT NULL AND delta < 0
        GROUP BY account_id
        ORDER BY total_debit DESC
        LIMIT 5
    """)
    top_acct_rows = session.execute(top_accts_sql, ledger_params).fetchall()
    top_account_ids = [r[0] for r in top_acct_rows]

    top_account_names: Dict[int, str] = {}
    if top_account_ids and ctx.schema:
        try:
            name_rows = session.execute(
                text(f"""
                    SELECT id, name
                    FROM "{ctx.schema}"."accounts"
                    WHERE id = ANY(:aids)
                """),
                {"aids": top_account_ids},
            ).fetchall()
            for nr in name_rows:
                top_account_names[nr[0]] = nr[1]
        except Exception:
            pass

    top_accounts = [
        {
            "account_id": r[0],
            "account_name": top_account_names.get(r[0], f"Account #{r[0]}"),
            "credits_spent": int(r[1]),
            "transactions": int(r[2]),
        }
        for r in top_acct_rows
    ]

    # 5. Daily series
    daily_sql = text(f"""
        SELECT TO_CHAR(DATE_TRUNC('day', created_at), 'YYYY-MM-DD') AS day_str,
               COALESCE(SUM(CASE WHEN delta < 0 THEN ABS(delta) ELSE 0 END), 0) AS debits,
               COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0) AS credits
        FROM public.credit_ledger
        WHERE {' AND '.join(ledger_where)}
        GROUP BY DATE_TRUNC('day', created_at)
        ORDER BY DATE_TRUNC('day', created_at) ASC
    """)
    daily_rows = session.execute(daily_sql, ledger_params).fetchall()
    daily_series = [
        {"date": r[0], "debits": int(r[1]), "credits": int(r[2]), "net": int(r[2]) - int(r[1])}
        for r in daily_rows
    ]

    return {
        "balance": balance,
        "total": total_allocated,
        "used": total_used,
        "spent_by_type": spent_by_type,
        "reuse": {
            "count": reuse_count,
            "fees_paid": reuse_fees_paid,
            "estimated_saved": reuse_estimated_saved,
            "net_savings": reuse_net_savings,
        },
        "refunds": refunds_total,
        "top_accounts": top_accounts,
        "daily_series": daily_series,
    }


@credit_router.get("/ledger/export.csv")
def export_credits_ledger_csv(
    from_date: Optional[str] = Query(None, alias="from", description="ISO start date filter"),
    to_date: Optional[str] = Query(None, alias="to", description="ISO end date filter"),
    type: Optional[str] = Query(None, description="Transaction type filter"),
    account_id: Optional[int] = Query(None, description="Optional account ID filter"),
    ctx: TenantRequestContext = Depends(tenant_context("account:read", explicit_slug_from="query")),
    session: Session = Depends(get_platform_db),
):
    """Streams a formula-injection-safe CSV export of the client's credit ledger.
    
    Restricted to Client Administrators and Super Administrators.
    Every exported cell is neutralized with sanitize_csv_cell.
    """
    if ctx.effective_role not in ("client_admin", "super_admin"):
        raise HTTPException(
            status_code=403,
            detail="Credit ledger export requires client administrator role",
        )

    where_clauses = ["cl.client_id = :cid"]
    params: Dict[str, Any] = {"cid": ctx.client_id}

    if from_date:
        where_clauses.append("cl.created_at >= :from_date")
        params["from_date"] = from_date
    if to_date:
        where_clauses.append("cl.created_at <= :to_date")
        params["to_date"] = to_date
    if account_id:
        where_clauses.append("cl.account_id = :account_id")
        params["account_id"] = account_id

    if type:
        t_clean = type.strip().lower()
        if t_clean == "reuse_fee":
            where_clauses.append("cl.delta < 0 AND (cl.reason ILIKE '%reuse%' OR cl.reason ILIKE 'catalog_reuse:hydrate%')")
        elif t_clean == "refresh_fee":
            where_clauses.append("cl.delta < 0 AND (cl.reason ILIKE '%refresh%')")
        elif t_clean == "refund":
            where_clauses.append("cl.delta > 0 AND (cl.reason ILIKE '%refund%' OR cl.reason ILIKE '%rollback%')")
        elif t_clean == "top_up":
            where_clauses.append("cl.delta > 0 AND (cl.reason ILIKE '%top_up%' OR cl.reason ILIKE '%grant%')")
        elif t_clean == "pipeline_run":
            where_clauses.append("(cl.reason ILIKE 'reserve:run%' OR cl.reason ILIKE 'settle:%' OR cl.reason ILIKE '%pipeline%')")
        else:
            where_clauses.append("cl.reason ILIKE :type_pat")
            params["type_pat"] = f"%{t_clean}%"

    where_sql = " AND ".join(where_clauses)

    query_str = f"""
        SELECT cl.id, cl.created_at, cl.delta, cl.balance_after, cl.reason, cl.account_id,
               cl.ref_type, cl.ref_id,
               se.estimated_credits_saved,
               u.full_name AS user_name
        FROM public.credit_ledger cl
        LEFT JOIN platform_catalog.share_events se ON (cl.ref_type = 'share_event' AND cl.ref_id = se.id)
        LEFT JOIN public.users u ON (se.requested_by = u.id)
        WHERE {where_sql}
        ORDER BY cl.created_at DESC, cl.id DESC
        LIMIT 10000
    """
    rows = session.execute(text(query_str), params).fetchall()

    # Batch resolve account names
    account_ids = {r[5] for r in rows if r[5] is not None}
    account_names: Dict[int, str] = {}
    if account_ids and ctx.schema:
        try:
            acct_rows = session.execute(
                text(f"""
                    SELECT id, name
                    FROM "{ctx.schema}"."accounts"
                    WHERE id = ANY(:aids)
                """),
                {"aids": list(account_ids)},
            ).fetchall()
            for ar in acct_rows:
                account_names[ar[0]] = ar[1]
        except Exception:
            pass

    # Build CSV output in memory
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    headers = [
        "Transaction ID",
        "Timestamp",
        "Type",
        "Description",
        "Account ID",
        "Account Name",
        "User",
        "Delta",
        "Balance After",
        "Reference",
        "Estimated Saved",
        "Net Saved vs Fresh",
    ]
    writer.writerow([sanitize_csv_cell(h) for h in headers])

    for r in rows:
        eid = r[0]
        cat = r[1].isoformat() if r[1] else ""
        delta = r[2]
        bal_after = r[3] if r[3] is not None else ""
        reason = r[4] or ""
        aid = r[5] or ""
        ref_type = r[6]
        ref_id = r[7]
        est_saved = r[8]
        u_name = r[9] or ""

        entry_type = _classify_ledger_type(reason, delta)
        desc = _clean_description(reason, entry_type)
        acct_name = account_names.get(r[5], "") if r[5] else ""
        ref_str = f"{ref_type}:{ref_id}" if (ref_type and ref_id) else ""
        saved_vs_fresh = max(0, est_saved - abs(delta)) if (est_saved is not None and delta < 0) else ""

        row_data = [
            eid,
            cat,
            entry_type,
            desc,
            aid,
            acct_name,
            u_name,
            delta,
            bal_after,
            ref_str,
            est_saved if est_saved is not None else "",
            saved_vs_fresh,
        ]
        writer.writerow([sanitize_csv_cell(cell) for cell in row_data])

    # Log audit event
    auth.log_audit(
        session,
        ctx.user_id,
        "credits_ledger_exported",
        details={"client_slug": ctx.client_slug, "rows_exported": len(rows)},
    )
    session.commit()

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"credits-ledger-{ctx.client_slug}-{date_str}.csv"
    csv_bytes = output.getvalue().encode("utf-8")

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@credit_router.get("/platform-summary")
def get_platform_credit_summary(
    client: Optional[str] = Query(None, description="Optional client slug filter"),
    pctx: PlatformRequestContext = Depends(platform_context("catalog:view_library")),
    session: Session = Depends(get_platform_db),
):
    """Retrieves platform-wide credit metrics and multi-tenant ledger utilization (super-admin only)."""
    if client:
        clean_slug = auth.normalize_client_slug(client)
        c_obj = session.query(Client).filter_by(slug=clean_slug).first()
        if not c_obj:
            raise HTTPException(status_code=404, detail=f"Client '{client}' not found")

        # Specific client platform metrics
        r_row = session.execute(text("""
            SELECT COUNT(*), COALESCE(SUM(fee_credits), 0), COALESCE(SUM(estimated_credits_saved), 0)
            FROM platform_catalog.share_events
            WHERE target_client_id = :cid AND status IN ('done', 'approved')
        """), {"cid": c_obj.id}).fetchone()

        return {
            "client_slug": clean_slug,
            "api_credits_total": c_obj.api_credits_total or 0,
            "api_credits_used": c_obj.api_credits_used or 0,
            "balance": max(0, (c_obj.api_credits_total or 0) - (c_obj.api_credits_used or 0)),
            "reuse_events": int(r_row[0] or 0) if r_row else 0,
            "fees_paid": int(r_row[1] or 0) if r_row else 0,
            "credits_saved": int(r_row[2] or 0) if r_row else 0,
        }

    # Global platform totals
    c_rows = session.execute(text("""
        SELECT id, slug, name, api_credits_total, api_credits_used
        FROM public.clients
        WHERE is_active = true
        ORDER BY api_credits_used DESC
    """)).fetchall()

    total_allocated = sum(int(r[3] or 0) for r in c_rows)
    total_used = sum(int(r[4] or 0) for r in c_rows)
    total_balance = max(0, total_allocated - total_used)

    # Reuse totals across platform
    r_global = session.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(fee_credits), 0), COALESCE(SUM(estimated_credits_saved), 0)
        FROM platform_catalog.share_events
        WHERE status IN ('done', 'approved')
    """)).fetchone()

    total_reuse_events = int(r_global[0] or 0) if r_global else 0
    total_fees_collected = int(r_global[1] or 0) if r_global else 0
    total_credits_saved = int(r_global[2] or 0) if r_global else 0

    clients_summary = [
        {
            "id": r[0],
            "slug": r[1],
            "name": r[2],
            "allocated": int(r[3] or 0),
            "used": int(r[4] or 0),
            "balance": max(0, int(r[3] or 0) - int(r[4] or 0)),
        }
        for r in c_rows
    ]

    return {
        "total_allocated": total_allocated,
        "total_used": total_used,
        "total_balance": total_balance,
        "total_reuse_events": total_reuse_events,
        "total_fees_collected": total_fees_collected,
        "total_credits_saved": total_credits_saved,
        "clients": clients_summary,
    }
