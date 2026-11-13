"""
Pipeline Tasks — Celery tasks for full end-to-end company onboarding runs.
Wraps the full main execution into an asynchronous background job.
100% Dynamic, Zero Hardcoding.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from celery_app import app
from main import run_pipeline


@app.task(bind=True, max_retries=2, default_retry_delay=120, name="tasks.full_company_pipeline")
def task_full_company_pipeline(self, company_name: str, target_url: str = None, client_slug: str = None):
    """
    Executes the complete Sales AI intelligence pipeline for a new company asynchronously:
    1. Account Firmographics & SEC EDGAR
    2. Sub-LOB Discovery & Segment Revenue Enrichment
    3. 4-Tier Org Hierarchy
    4. AI Persona Dossiers
    5. Dual JSON Artifacts & PostgreSQL DB Persistence
    """
    from db.tenant import tenant_scope, get_current_slug
    slug = client_slug or get_current_slug() or "stradit_india"
    print(f"[*] [CeleryWorker] Starting full pipeline for: '{company_name}' (URL: {target_url}) in tenant '{slug}'...")
    try:
        with tenant_scope(slug):
            run_pipeline(company_name=company_name, target_url=target_url)
        return {
            "status": "completed",
            "company_name": company_name,
            "target_url": target_url,
            "client_slug": slug,
        }
    except Exception as e:
        print(f"[!] [CeleryWorker ERROR] Full pipeline failed for '{company_name}' ({slug}): {e}")
        raise self.retry(exc=e)
