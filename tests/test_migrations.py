"""Migration files are well-formed (no database needed). See docs/DB_DEPLOYMENT_PLAN.md."""
import re
from pathlib import Path

from alembic.script import ScriptDirectory

from db.migrate import _config

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "migrations" / "sql"


def _scripts():
    return ScriptDirectory.from_config(_config())


def test_single_linear_history():
    sd = _scripts()
    assert len(sd.get_heads()) == 1, "two migrations share a parent - rebase the newer one onto the head"
    revs = list(sd.walk_revisions())          # head -> base
    assert revs[-1].revision == "0001" and revs[-1].down_revision is None
    for r in revs:
        assert re.fullmatch(r"\d{4}", r.revision), f"revision ids are 4-digit sequence numbers: {r.revision}"


def test_revision_numbers_are_sequential():
    nums = sorted(int(r.revision) for r in _scripts().walk_revisions())
    assert nums == list(range(1, len(nums) + 1))


def test_every_migration_after_baseline_has_a_downgrade():
    for r in _scripts().walk_revisions():
        if r.revision == "0001":
            continue
        src = Path(r.path).read_text(encoding="utf-8")
        assert not re.search(r"def downgrade\(\) -> None:\s*pass\s*$", src), \
            f"{r.revision}: write a downgrade (or raise with a reason if it truly can't be undone)"


def test_baseline_sql_is_clean():
    sql = (SQL_DIR / "0001_baseline.sql").read_text(encoding="utf-8")
    assert "\\restrict" not in sql and "\r" not in sql
    assert not re.search(r"^SET |set_config\(", sql, re.M), "session settings must not be in the baseline"
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in sql
    # Tables owned by apps/content_pipeline are not part of this app's schema
    for other in ("targets", "run_history"):
        assert not re.search(rf"^CREATE TABLE public\.{other} ", sql, re.M)


def test_manifest_matches_baseline_tables():
    sql = (SQL_DIR / "0001_baseline.sql").read_text(encoding="utf-8")
    tables = set(re.findall(r"^CREATE TABLE public\.(\w+) ", sql, re.M))
    manifest = (SQL_DIR / "0001_baseline.columns.txt").read_text(encoding="utf-8").splitlines()
    assert {line.split(".", 1)[0] for line in manifest if line} == tables
