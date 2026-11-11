"""Credit Service — Atomic Quotas, Credit Reservation, Settlement & Audit Ledger.

Implements the multi-tenant billing & quota architecture (§5.5):
1. Atomic credit reservations (RETURNING id) avoiding read-modify-write race conditions.
2. Two-phase credit settlement (reserve before run -> settle actual vs reserved on completion).
3. Immutable transaction ledger in public.credit_ledger.
4. Transaction-scoped PostgreSQL advisory locks (pg_advisory_xact_lock) for atomic account quotas.
"""

from typing import Any, Dict, Optional
from sqlalchemy import text
from sqlalchemy.engine import Engine
from db.connection import engine as default_engine
from db.tenant import schema_for, validate_slug


class InsufficientCreditsError(RuntimeError):
    """Raised when a client does not have enough API credits to perform an operation."""
    pass


class AccountQuotaExceededError(RuntimeError):
    """Raised when a client attempts to create an account beyond their plan quota."""
    pass


class CreditService:
    """Enterprise credit management and quota enforcement for multi-tenant organizations."""

    @classmethod
    def get_balance(cls, client_slug: str, engine: Optional[Engine] = None) -> Dict[str, Any]:
        """Fetches the current credit allocation and usage for a client."""
        eng = engine or default_engine
        clean_slug = validate_slug(client_slug)

        with eng.connect() as conn:
            row = conn.execute(text("""
                SELECT id, slug, api_credits_total, api_credits_used
                FROM public.clients
                WHERE slug = :s
            """), {"s": clean_slug}).fetchone()

            if not row:
                raise ValueError(f"Client '{clean_slug}' not found in registry.")

            cid, slug, total, used = row[0], row[1], row[2] or 0, row[3] or 0
            remaining = max(0, total - used)
            pct = round((used / total * 100), 1) if total > 0 else 0.0

            return {
                "client_id": cid,
                "client_slug": slug,
                "total": total,
                "used": used,
                "remaining": remaining,
                "percentage_used": pct,
            }

    @classmethod
    def reserve_credits(
        cls,
        client_slug: str,
        amount: int,
        run_id: Optional[str] = None,
        account_id: Optional[int] = None,
        reason: str = "reserve:run",
        engine: Optional[Engine] = None,
    ) -> bool:
        """Atomically reserves credits prior to starting a pipeline run.

        Uses UPDATE ... RETURNING to guarantee zero race conditions.
        Returns True if reservation was successful, False if balance is insufficient.
        """
        if amount <= 0:
            return True

        eng = engine or default_engine
        clean_slug = validate_slug(client_slug)

        with eng.begin() as conn:
            res = conn.execute(text("""
                UPDATE public.clients
                SET api_credits_used = api_credits_used + :n,
                    updated_at = NOW()
                WHERE slug = :s
                  AND api_credits_used + :n <= api_credits_total
                RETURNING id, api_credits_used, api_credits_total
            """), {"s": clean_slug, "n": amount}).fetchone()

            if not res:
                return False

            client_id = res[0]
            conn.execute(text("""
                INSERT INTO public.credit_ledger (client_id, account_id, run_id, delta, reason, created_at)
                VALUES (:cid, :aid, :rid, :delta, :reason, NOW())
            """), {
                "cid": client_id,
                "aid": account_id,
                "rid": run_id,
                "delta": -amount,
                "reason": reason,
            })

            return True

    @classmethod
    def settle_credits(
        cls,
        client_slug: str,
        reserved: int,
        actual: int,
        run_id: str,
        account_id: Optional[int] = None,
        engine: Optional[Engine] = None,
    ) -> Dict[str, Any]:
        """Settles actual vs reserved credits at the conclusion of a pipeline run.

        - If actual < reserved: refunds unused credits.
        - If actual > reserved: debits overage if balance allows.
        """
        eng = engine or default_engine
        clean_slug = validate_slug(client_slug)
        delta = reserved - actual  # Positive means refund; negative means overage debit

        with eng.begin() as conn:
            client_row = conn.execute(text("SELECT id FROM public.clients WHERE slug = :s"), {"s": clean_slug}).fetchone()
            if not client_row:
                raise ValueError(f"Client '{clean_slug}' not found.")
            client_id = client_row[0]

            if delta > 0:
                # Refund difference
                conn.execute(text("""
                    UPDATE public.clients
                    SET api_credits_used = GREATEST(0, api_credits_used - :refund),
                        updated_at = NOW()
                    WHERE slug = :s
                """), {"s": clean_slug, "refund": delta})

                conn.execute(text("""
                    INSERT INTO public.credit_ledger (client_id, account_id, run_id, delta, reason, created_at)
                    VALUES (:cid, :aid, :rid, :delta, :reason, NOW())
                """), {
                    "cid": client_id,
                    "aid": account_id,
                    "rid": run_id,
                    "delta": delta,
                    "reason": f"settle:refund:{run_id}",
                })

            elif delta < 0:
                # Overage debit
                overage = abs(delta)
                conn.execute(text("""
                    UPDATE public.clients
                    SET api_credits_used = api_credits_used + :overage,
                        updated_at = NOW()
                    WHERE slug = :s
                """), {"s": clean_slug, "overage": overage})

                conn.execute(text("""
                    INSERT INTO public.credit_ledger (client_id, account_id, run_id, delta, reason, created_at)
                    VALUES (:cid, :aid, :rid, :delta, :reason, NOW())
                """), {
                    "cid": client_id,
                    "aid": account_id,
                    "rid": run_id,
                    "delta": -overage,
                    "reason": f"settle:overage:{run_id}",
                })

        return cls.get_balance(clean_slug, eng)

    @classmethod
    def top_up_credits(
        cls,
        client_slug: str,
        amount: int,
        reason: str = "topup:manual",
        user_id: Optional[int] = None,
        engine: Optional[Engine] = None,
    ) -> Dict[str, Any]:
        """Allocates additional API credits to a client and logs audit entries."""
        if amount <= 0:
            raise ValueError("Top-up amount must be strictly positive.")

        eng = engine or default_engine
        clean_slug = validate_slug(client_slug)

        with eng.begin() as conn:
            row = conn.execute(text("""
                UPDATE public.clients
                SET api_credits_total = api_credits_total + :amt,
                    updated_at = NOW()
                WHERE slug = :s
                RETURNING id, api_credits_total, api_credits_used
            """), {"s": clean_slug, "amt": amount}).fetchone()

            if not row:
                raise ValueError(f"Client '{clean_slug}' not found.")

            client_id = row[0]

            # 1. Record in credit ledger
            conn.execute(text("""
                INSERT INTO public.credit_ledger (client_id, delta, reason, created_at)
                VALUES (:cid, :delta, :reason, NOW())
            """), {"cid": client_id, "delta": amount, "reason": reason})

            # 2. Record in audit log
            conn.execute(text("""
                INSERT INTO public.audit_log (client_id, actor_user_id, action, details, created_at)
                VALUES (:cid, :uid, 'credits:topup', :details, NOW())
            """), {
                "cid": client_id,
                "uid": user_id,
                "details": f'{{"amount": {amount}, "reason": "{reason}"}}',
            })

        return cls.get_balance(clean_slug, eng)

    @classmethod
    def acquire_account_quota_lock(cls, conn, client_slug: str) -> None:
        """Acquires a transaction-scoped advisory lock for account quota operations."""
        clean_slug = validate_slug(client_slug)
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"accounts:{clean_slug}"})

    @classmethod
    def check_and_enforce_account_quota(
        cls,
        conn,
        client_slug: str,
        schema_name: Optional[str] = None,
        max_accounts: int = 50,
    ) -> Dict[str, Any]:
        """Atomically checks account quota within the given transaction using advisory lock."""
        clean_slug = validate_slug(client_slug)
        target_schema = schema_name or schema_for(clean_slug)

        cls.acquire_account_quota_lock(conn, clean_slug)

        current_count = conn.execute(text(f'SELECT count(*) FROM "{target_schema}"."accounts"')).scalar() or 0
        if current_count >= max_accounts:
            raise AccountQuotaExceededError(
                f"Client '{clean_slug}' has reached the maximum allowed accounts limit ({max_accounts}). "
                f"Current accounts: {current_count}."
            )

        return {
            "allowed": True,
            "current_count": current_count,
            "max_accounts": max_accounts,
            "remaining": max(0, max_accounts - current_count),
        }
