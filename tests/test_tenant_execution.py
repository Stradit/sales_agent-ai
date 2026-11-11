"""Phase 4 Test Suite: Tenant Execution, Credit Ledger, Quotas, Durable Jobs & Audit.

Validates:
1. CreditService: Atomic reservations (RETURNING clause), refunds, overages, and top-ups.
2. CreditLedger: Full transaction history and immutable audit trail.
3. Advisory Locks: Transaction-scoped locks for atomic account quota enforcement.
4. JobStore & JobRegistry: Cross-restart persistence of background pipeline jobs in public.pipeline_jobs.
5. Multi-Tenant Scoping: Cross-tenant job isolation (fail-closed boundary on get_job & list_jobs).
6. TenantTask: Fail-closed task execution requiring client_slug and setting tenant_scope.
7. AuditService: Structured event logging linked to client organizations.
"""

import pytest
from sqlalchemy import text

from db.connection import engine
from db.tenant import get_current_slug, tenant_scope, TenantContextError
from db.provisioning import create_client_schema, drop_client_schema
from services.credit_service import (
    CreditService,
    InsufficientCreditsError,
    AccountQuotaExceededError,
)
from services.job_store import JobStore
import services.job_registry as job_registry
from services.audit_service import AuditService
from tasks.base import TenantTask


TEST_SLUG_EXEC = "test_p4_exec"
TEST_SCHEMA_EXEC = f"client_{TEST_SLUG_EXEC}"

TEST_SLUG_OTHER = "test_p4_other"
TEST_SCHEMA_OTHER = f"client_{TEST_SLUG_OTHER}"


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_phase4_environment():
    """Sets up test client records and schemas, and cleans them up after the module."""
    with engine.begin() as conn:
        for s in [TEST_SCHEMA_EXEC, TEST_SCHEMA_OTHER]:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
        for sl in [TEST_SLUG_EXEC, TEST_SLUG_OTHER]:
            conn.execute(text("DELETE FROM public.credit_ledger WHERE client_id IN (SELECT id FROM public.clients WHERE slug = :sl)"), {"sl": sl})
            conn.execute(text("DELETE FROM public.pipeline_jobs WHERE client_slug = :sl"), {"sl": sl})
            conn.execute(text("DELETE FROM public.audit_log WHERE client_id IN (SELECT id FROM public.clients WHERE slug = :sl)"), {"sl": sl})
            conn.execute(text("DELETE FROM public.clients WHERE slug = :sl"), {"sl": sl})

    # Provision schemas and seed client records
    create_client_schema(TEST_SLUG_EXEC)
    create_client_schema(TEST_SLUG_OTHER)

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.clients
            SET api_credits_total = 1000,
                api_credits_used = 200
            WHERE slug = :s
        """), {"s": TEST_SLUG_EXEC})

    yield

    with engine.begin() as conn:
        for s in [TEST_SCHEMA_EXEC, TEST_SCHEMA_OTHER]:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
        for sl in [TEST_SLUG_EXEC, TEST_SLUG_OTHER]:
            conn.execute(text("DELETE FROM public.credit_ledger WHERE client_id IN (SELECT id FROM public.clients WHERE slug = :sl)"), {"sl": sl})
            conn.execute(text("DELETE FROM public.pipeline_jobs WHERE client_slug = :sl"), {"sl": sl})
            conn.execute(text("DELETE FROM public.audit_log WHERE client_id IN (SELECT id FROM public.clients WHERE slug = :sl)"), {"sl": sl})
            conn.execute(text("DELETE FROM public.clients WHERE slug = :sl"), {"sl": sl})


class TestCreditLedgerAndReservation:
    """Verifies atomic credit reservation, settlement, and ledger trail."""

    def test_credit_balance_retrieval(self):
        balance = CreditService.get_balance(TEST_SLUG_EXEC, engine)
        assert balance["client_slug"] == TEST_SLUG_EXEC
        assert balance["total"] == 1000
        assert balance["used"] == 200
        assert balance["remaining"] == 800
        assert balance["percentage_used"] == 20.0

    def test_atomic_credit_reservation_success(self):
        # Reserve 300 credits
        ok = CreditService.reserve_credits(
            client_slug=TEST_SLUG_EXEC,
            amount=300,
            run_id="run_test_01",
            reason="reserve:run",
            engine=engine,
        )
        assert ok is True

        # Check balance
        bal = CreditService.get_balance(TEST_SLUG_EXEC, engine)
        assert bal["used"] == 500
        assert bal["remaining"] == 500

        # Verify ledger entry
        with engine.connect() as conn:
            entry = conn.execute(text("""
                SELECT delta, reason FROM public.credit_ledger
                WHERE run_id = 'run_test_01'
            """)).fetchone()
            assert entry is not None
            assert entry[0] == -300
            assert entry[1] == "reserve:run"

    def test_atomic_credit_reservation_insufficient_fails(self):
        # Remaining is 500, attempt to reserve 600
        ok = CreditService.reserve_credits(
            client_slug=TEST_SLUG_EXEC,
            amount=600,
            run_id="run_test_overflow",
            reason="reserve:overflow",
            engine=engine,
        )
        assert ok is False

        # Verify balance remains unchanged at 500 used
        bal = CreditService.get_balance(TEST_SLUG_EXEC, engine)
        assert bal["used"] == 500
        assert bal["remaining"] == 500

    def test_credit_settlement_refund(self):
        # Run finished using only 100 of the 300 reserved credits -> 200 refund
        new_bal = CreditService.settle_credits(
            client_slug=TEST_SLUG_EXEC,
            reserved=300,
            actual=100,
            run_id="run_test_01",
            engine=engine,
        )
        # Used was 500 -> refunded 200 -> now 300 used
        assert new_bal["used"] == 300
        assert new_bal["remaining"] == 700

        # Verify positive refund entry in ledger
        with engine.connect() as conn:
            refund = conn.execute(text("""
                SELECT delta, reason FROM public.credit_ledger
                WHERE run_id = 'run_test_01' AND delta > 0
            """)).fetchone()
            assert refund is not None
            assert refund[0] == 200
            assert "settle:refund" in refund[1]

    def test_credit_settlement_overage(self):
        # Run estimated 50, actually used 80 -> 30 overage debit
        new_bal = CreditService.settle_credits(
            client_slug=TEST_SLUG_EXEC,
            reserved=50,
            actual=80,
            run_id="run_test_overage",
            engine=engine,
        )
        # Used was 300 -> +30 overage -> 330 used
        assert new_bal["used"] == 330

        with engine.connect() as conn:
            overage = conn.execute(text("""
                SELECT delta, reason FROM public.credit_ledger
                WHERE run_id = 'run_test_overage'
            """)).fetchone()
            assert overage is not None
            assert overage[0] == -30
            assert "settle:overage" in overage[1]

    def test_credit_top_up_and_audit(self):
        # Top-up 500 credits
        bal = CreditService.top_up_credits(
            client_slug=TEST_SLUG_EXEC,
            amount=500,
            reason="topup:promo",
            engine=engine,
        )
        assert bal["total"] == 1500
        assert bal["remaining"] == 1500 - 330

        # Verify audit log entry
        with engine.connect() as conn:
            audit = conn.execute(text("""
                SELECT action, details FROM public.audit_log
                WHERE action = 'credits:topup'
            """)).fetchone()
            assert audit is not None
            assert "500" in str(audit[1])


class TestAccountQuotaAndAdvisoryLocks:
    """Verifies atomic account quota checking and advisory locking."""

    def test_account_quota_allowed_when_under_limit(self):
        with engine.begin() as conn:
            res = CreditService.check_and_enforce_account_quota(
                conn,
                client_slug=TEST_SLUG_EXEC,
                schema_name=TEST_SCHEMA_EXEC,
                max_accounts=10,
            )
            assert res["allowed"] is True
            assert res["current_count"] == 0
            assert res["max_accounts"] == 10

    def test_account_quota_blocks_when_limit_reached(self):
        # Insert 2 test accounts in tenant schema
        with engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO "{TEST_SCHEMA_EXEC}".accounts (key, display_name)
                VALUES ('acc_test_1', 'Test Acc 1'), ('acc_test_2', 'Test Acc 2')
            """))

        with engine.begin() as conn:
            with pytest.raises(AccountQuotaExceededError, match="maximum allowed accounts limit"):
                CreditService.check_and_enforce_account_quota(
                    conn,
                    client_slug=TEST_SLUG_EXEC,
                    schema_name=TEST_SCHEMA_EXEC,
                    max_accounts=2,
                )


class TestPersistentPipelineJobs:
    """Verifies public.pipeline_jobs durability and cross-restart recovery."""

    def test_job_store_lifecycle(self):
        jid = JobStore.create_job(
            job_type="account_enrich",
            meta={"company": "Test Durability Inc", "account_id": 42},
            client_slug=TEST_SLUG_EXEC,
            engine=engine,
        )
        assert jid.startswith("pjob_")

        # Verify created
        j = JobStore.get_job(jid, engine=engine)
        assert j is not None
        assert j["status"] == "queued"
        assert j["client_slug"] == TEST_SLUG_EXEC

        # Update to running
        JobStore.update_job(jid, status="running", progress_pct=50, message="Collecting LOBs...", engine=engine)
        j_run = JobStore.get_job(jid, engine=engine)
        assert j_run["status"] == "running"
        assert j_run["progress_pct"] == 50
        assert j_run["message"] == "Collecting LOBs..."
        assert j_run["started_at"] is not None

        # Update to done
        JobStore.update_job(jid, status="done", progress_pct=100, result={"lobs_count": 3}, engine=engine)
        j_done = JobStore.get_job(jid, engine=engine)
        assert j_done["status"] == "done"
        assert j_done["progress_pct"] == 100
        assert j_done["completed_at"] is not None
        assert j_done["result"]["lobs_count"] == 3

    def test_job_survives_memory_wipe(self):
        # Create job through job_registry
        jid = job_registry.create_job(
            job_type="persona_enrich",
            meta={"name": "Jane Doe"},
            client_slug=TEST_SLUG_EXEC,
        )

        # Confirm job exists
        assert job_registry.get_job(jid) is not None

        # Wipe in-memory dictionary completely (simulating uvicorn worker restart)
        with job_registry._lock:
            job_registry._jobs.clear()
        assert len(job_registry._jobs) == 0

        # Call get_job -> must seamlessly restore from database!
        recovered = job_registry.get_job(jid)
        assert recovered is not None
        assert recovered["job_id"] == jid
        assert recovered["client_slug"] == TEST_SLUG_EXEC
        assert recovered["job_type"] == "persona_enrich"

    def test_cross_tenant_job_isolation(self):
        jid = JobStore.create_job(
            job_type="account_enrich",
            meta={"target": "private_data"},
            client_slug=TEST_SLUG_EXEC,
            engine=engine,
        )

        # Reading with correct tenant succeeds
        assert JobStore.get_job(jid, client_slug=TEST_SLUG_EXEC, engine=engine) is not None

        # Reading with another tenant returns None (fail-closed boundary!)
        assert JobStore.get_job(jid, client_slug=TEST_SLUG_OTHER, engine=engine) is None

        # Listing jobs for other tenant does not leak this job
        other_jobs = JobStore.list_jobs(client_slug=TEST_SLUG_OTHER, engine=engine)
        job_ids = [j["job_id"] for j in other_jobs]
        assert jid not in job_ids


class TestTenantTaskScoping:
    """Verifies Celery TenantTask fail-closed enforcement."""

    def test_tenant_task_fails_closed_without_slug(self):
        class DummyTask(TenantTask):
            def run(self, *args, **kwargs):
                return "executed"

        task_instance = DummyTask()
        with pytest.raises(TenantContextError, match="client_slug is required"):
            task_instance(account_id=10)

    def test_tenant_task_binds_scope_during_execution(self):
        observed_slug = None

        class DummyTask(TenantTask):
            def run(self, *args, **kwargs):
                nonlocal observed_slug
                observed_slug = get_current_slug()
                return "ok"

        task_instance = DummyTask()
        res = task_instance(client_slug=TEST_SLUG_EXEC)
        assert res == "ok"
        assert observed_slug == TEST_SLUG_EXEC


class TestAuditService:
    """Verifies audit trail persistence and client filtering."""

    def test_audit_event_logging_and_filtering(self):
        event_id = AuditService.log_event(
            action="schema:repair",
            client_slug=TEST_SLUG_EXEC,
            details={"repaired_tables": 3},
            engine=engine,
        )
        assert event_id > 0

        # Retrieve events for test client
        events = AuditService.list_events(client_slug=TEST_SLUG_EXEC, engine=engine)
        assert len(events) > 0
        latest = events[0]
        assert latest["action"] == "schema:repair"
        assert latest["client_slug"] == TEST_SLUG_EXEC
        assert latest["details"]["repaired_tables"] == 3
