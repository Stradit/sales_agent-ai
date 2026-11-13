"""
Refresh Tasks — 15-Day cycle tasks for Celery + Redis.
Only refreshes fast-moving executive movements, 4-tier hierarchy, and all social/scraping URLs.
Skips heavy static firmographics & LOB discovery.
100% Dynamic, Zero Hardcoding.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from celery_app import app
import config
from db.connection import get_session
from db.models import Account, Lob
from db.repositories import PersonaRepository
from collectors.hierarchy_collector import scrape_hierarchy
from collectors.persona_enricher import build_persona_dossier
from serializer import PipelineSerializer


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="tasks.refresh_account")
def task_refresh_account_hierarchy_and_social(self, account_key: str, client_slug: Optional[str] = None):
    """
    15-Day Refresh Task for a single company:
    1. Loads existing Account & LOBs from PostgreSQL.
    2. Re-queries Apollo/Monid & Crunchbase for executive changes.
    3. Regenerates all 18 scraping/search URLs per person & account.
    4. Synthesizes AI dossiers for top C-Suite.
    5. UPSERTs refreshed Personas into PostgreSQL and updates timestamps.
    6. Updates output/<company>_social_and_content.json.
    """
    from db.tenant import tenant_scope, get_current_slug
    slug = client_slug or get_current_slug() or "stradit_india"
    print(f"[*] [CeleryWorker] Starting 15-Day Refresh for Account: '{account_key}' in tenant '{slug}'...")

    with tenant_scope(slug):
        session = get_session()

    try:
        # 1. Fetch existing Account from DB
        account = session.query(Account).filter_by(key=account_key).first()
        if not account:
            msg = f"[!] Account with key '{account_key}' not found in database."
            print(msg)
            return {"status": "error", "message": msg}

        company_name = account.legal_name or account.display_name or account_key
        account_domain = account.domain or account.primary_domain or account.website_url
        sec_cik = account.sec_cik

        # 2. Fetch existing LOBs from DB
        db_lobs = session.query(Lob).filter_by(account_id=account.id).all()
        lobs_data = [
            {
                "lob_name": lob.lob_name,
                "domain": lob.domain,
                "website_url": lob.website_url,
                "overview": lob.overview,
                "audited_segment_revenue": lob.audited_segment_revenue,
                "operating_head": lob.operating_head,
                "segment_headcount": lob.segment_headcount,
                "required_account": {
                    "key": lob.key,
                    "display_name": lob.lob_name,
                    "sec_cik": sec_cik,
                }
            }
            for lob in db_lobs
        ]

        # 3. Scrape Fresh Hierarchy & Generate Fresh Scraping URLs
        print(f"[*] [CeleryWorker] Re-querying Apollo & Crunchbase for: '{company_name}'...")
        account_hierarchy = scrape_hierarchy(account_domain, company_name, sec_cik=sec_cik)

        # 4. Synthesize AI Persona Dossiers for top leadership
        c_suite = account_hierarchy.get("c_suite", [])
        if c_suite:
            ceo = c_suite[0]
            ceo["persona_dossier"] = build_persona_dossier(
                ceo.get("name"), ceo.get("title"), company_name, ceo.get("linkedin_url")
            )
            if ceo["persona_dossier"].get("level_3_personal_touch", {}).get("social_media", {}).get("profile_url"):
                verified_url = ceo["persona_dossier"]["level_3_personal_touch"]["social_media"]["profile_url"]
                ceo["linkedin_url"] = verified_url
                ceo["required_person_data"]["linkedin_url"] = verified_url

            if len(c_suite) > 1:
                exec2 = c_suite[1]
                exec2["persona_dossier"] = build_persona_dossier(
                    exec2.get("name"), exec2.get("title"), company_name, exec2.get("linkedin_url")
                )
                if exec2["persona_dossier"].get("level_3_personal_touch", {}).get("social_media", {}).get("profile_url"):
                    verified_url = exec2["persona_dossier"]["level_3_personal_touch"]["social_media"]["profile_url"]
                    exec2["linkedin_url"] = verified_url
                    exec2["required_person_data"]["linkedin_url"] = verified_url

        # 5. Build Fresh Social & Content Launchpad
        # Construct account_data dictionary from existing DB record for serializer
        account_data = {
            "key": account.key,
            "display_name": account.display_name,
            "legal_name": account.legal_name,
            "domain": account.domain,
            "ticker": account.stock_symbol,
            "sec_cik": account.sec_cik,
            "sec_edgar_url": account.sec_edgar_url,
            "sec_filings_rss": account.sec_filings_rss,
            "sec_submissions_url": account.sec_submissions_url,
            "linkedin_url": account.linkedin_url,
            "twitter_handle": account.twitter_handle,
            "twitter_live_url": account.twitter_live_url,
            "reddit_query": account.reddit_query,
            "reddit_rss_url": account.reddit_rss_url,
            "news_query": account.news_query,
            "rss_url": account.rss_url,
            "google_patents_url": account.google_patents_url,
            "google_trends_url": account.google_trends_url,
            "youtube_search_url": account.youtube_search_url,
            "openalex_institution_url": account.openalex_institution_url,
            "wikidata_entity_url": account.wikidata_entity_url,
            "blog_url": account.blog_url,
            "youtube_channel_id": account.youtube_channel_id,
        }

        social_and_content_doc = PipelineSerializer.build_social_and_content_payload(
            account_data=account_data,
            account_hierarchy=account_hierarchy,
            lobs_data=lobs_data
        )

        # 6. Save Updated Social Launchpad JSON
        safe_name = account.key.lower().replace(" ", "_").replace(".", "").replace(",", "")
        out_file_content = config.OUTPUT_DIR / f"{safe_name}_social_and_content.json"
        PipelineSerializer.save_json(social_and_content_doc, out_file_content)

        # 7. Update PostgreSQL DB (UPSERT personas and update account timestamp)
        persona_repo = PersonaRepository(session)
        tree_root = account_hierarchy.get("organisational_hierarchy_tree")
        updated_count = persona_repo.upsert_all(account, account_hierarchy, tree_root)

        account.extracted_at = datetime.now(timezone.utc)
        account.updated_at = datetime.now(timezone.utc)
        account.total_contacts_captured = updated_count
        account.c_suite_count = len(account_hierarchy.get("c_suite", []))
        account.vp_count = len(account_hierarchy.get("vp_level", []))
        account.director_count = len(account_hierarchy.get("director_level", []))
        account.manager_count = len(account_hierarchy.get("manager_level", []))

        session.commit()

        result = {
            "status": "success",
            "account_key": account_key,
            "personas_refreshed": updated_count,
            "social_json": str(out_file_content),
            "refreshed_at": datetime.now(timezone.utc).isoformat()
        }
        print(f"[+] [CeleryWorker] Completed 15-Day Refresh for '{account_key}'. Total Personas: {updated_count}")
        return result

    except Exception as e:
        session.rollback()
        print(f"[!] [CeleryWorker ERROR] Refresh failed for '{account_key}': {e}")
        raise self.retry(exc=e)
    finally:
        session.close()


@app.task(name="tasks.scheduled_15day_refresh")
def task_scheduled_15day_refresh(account_keys: Optional[List[str]] = None, client_slug: Optional[str] = None):
    """
    Multi-tenant orchestrator task for the scheduled refresh cycle:
    - If client_slug is provided: dispatches refresh jobs for that specific tenant.
    - If client_slug is None: iterates active clients in public.clients, enforcing concurrency caps.
    """
    from sqlalchemy import text
    from db.connection import engine
    from db.tenant import tenant_scope
    from services.job_store import JobStore

    if client_slug:
        active_clients = [(client_slug, 3)]
    else:
        with engine.connect() as conn:
            active_clients = conn.execute(text(
                "SELECT slug, max_concurrent_jobs FROM public.clients WHERE status = 'active'"
            )).fetchall()

    dispatched = []
    for slug, max_jobs in active_clients:
        active_jobs = JobStore.count_active_jobs(slug)
        if active_jobs >= max_jobs:
            print(f"[*] Skipping {slug}: at concurrency limit ({active_jobs}/{max_jobs})")
            continue

        with tenant_scope(slug):
            session = get_session()
            try:
                if account_keys:
                    target_keys = account_keys
                    print(f"[*] [CeleryBeat] User-specified refresh for {len(target_keys)} account(s) in {slug}: {target_keys}")
                else:
                    accounts = session.query(Account.key).all()
                    target_keys = [a.key for a in accounts if a.key]
                    print(f"[*] [CeleryBeat] Automated refresh for {len(target_keys)} account(s) in {slug}: {target_keys}")

                for key in target_keys:
                    task = task_refresh_account_hierarchy_and_social.delay(account_key=key, client_slug=slug)
                    dispatched.append({"client_slug": slug, "account_key": key, "task_id": task.id})
            finally:
                session.close()

    return {
        "status": "dispatched",
        "total_accounts": len(dispatched),
        "jobs": dispatched,
        "triggered_at": datetime.now(timezone.utc).isoformat()
    }
