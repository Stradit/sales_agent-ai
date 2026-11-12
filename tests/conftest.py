"""Test-wide setup, loaded by pytest before any test module is imported.

Ensures a hermetic test environment:
1. Points DATABASE_URL to a dedicated test database (TEST_DATABASE_URL or sales_ai_test)
   so the live development database is NEVER polluted or altered by tests.
2. Generates a safe fallback JWT_SECRET_KEY for CI and isolated test execution.
3. Automatically tears down transient test schemas (_test_*) created during test runs.
"""
import os
import secrets
import pytest
from sqlalchemy import create_engine, text

# ── 1. Dedicated Hermetic Test Database ──────────────────────────────────────
# Prioritize explicit TEST_DATABASE_URL, or derive test database name from DATABASE_URL
raw_db_url = os.getenv("TEST_DATABASE_URL")
if not raw_db_url:
    current_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sales_ai_28-09")
    if "/" in current_url:
        base, sep, db_spec = current_url.rpartition("/")
        db_name = db_spec.split("?")[0]
        query = ("?" + db_spec.split("?", 1)[1]) if "?" in db_spec else ""
        raw_db_url = f"{base}/sales_ai_test{query}"
    else:
        raw_db_url = "postgresql://postgres:postgres@localhost:5432/sales_ai_test"

os.environ["DATABASE_URL"] = raw_db_url
os.environ["TEST_DATABASE_URL"] = raw_db_url

# ── 2. Test-Only JWT & Catalog Secrets ───────────────────────────────────────
os.environ.setdefault("JWT_SECRET_KEY", "test-only-" + secrets.token_urlsafe(32))
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AUTH_ENFORCED", "false")
os.environ.setdefault("CATALOG_TOKEN_SECRET", "test-catalog-secret-token-32b-long-secret")


# ── 3. Transient Test Schema Cleanup Hook ─────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def clean_test_schemas_session():
    """Ensure no transient test schemas (_test_*) linger in the test database."""
    yield
    try:
        engine = create_engine(os.environ["DATABASE_URL"])
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            schemas = [
                r[0] for r in conn.execute(
                    text("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE '_test_%'")
                ).fetchall()
            ]
            for sc in schemas:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{sc}" CASCADE'))
        engine.dispose()
    except Exception:
        pass
