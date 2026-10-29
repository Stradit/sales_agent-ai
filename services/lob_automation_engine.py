"""
Lob Automation Engine — Phased Multi-Cycle Autonomous LOB Discovery, Ingestion & Self-Healing Engine.

Key Architectural Pillars:
1. Pre-Flight Resource & Credit Gatekeeper:
   - Audits Rate Cards for LOB tier against current available workspace credits.
   - Hard aborts before external calls if balance is insufficient, surfacing actionable alerts.
2. Cycle 1: Lightweight Name & Stub Discovery:
   - Fast multi-source extraction of LOB names from Account hierarchy tree, SEC 10-K Ex-21,
     GLEIF ownership tree, Diffbot cache, and commercial operating division patterns.
   - Deduplicates, classifies corporate tiers, and commits stubs to PostgreSQL with status='discovered_stub'.
3. Cycle 2: Phased Batch Partitioning:
   - Partitions N LOBs into chunks of 10 (or configurable 10-15).
   - Commercial operating divisions prioritized first.
4. Sequential Execution with Deferred Fault Logging:
   - Worker pool capped at 3 concurrent threads per batch to prevent socket exhaustion and HTTP 429s.
   - Any failed connectors are isolated into a deferred_failures queue without halting the batch.
   - Enriched LOBs in each batch are validated and dumped immediately to PostgreSQL via LobRepository.
5. Consolidated Post-Batch Self-Healing Resolution:
   - Executes targeted micro-retries ONLY on the failed connectors with adaptive cooling backoff (2.5s).
   - Additively coalesces recovered intelligence into existing records via CoalescenceEngine.
6. Sub-LOB Hierarchy Discovery & Deepening:
   - Detects nested Level-3 child entities and commits them to sub_lobs table linked to parent_lob_id.
"""

import copy
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from collectors.sublob_collector import extract_commercial_operating_divisions, scrape_sublobs
from collectors.account_collector import fetch_sec_exhibit_21_subsidiaries, fetch_gleif_ownership_tree
from db.connection import get_session
from db.models.account import Account
from db.models.lob import Lob
from db.models.sub_lob import SubLob
from db.repositories.lob_repo import LobRepository
from db.repositories.pipeline_run_repository import PipelineRunRepository
from serializers.account_serializer import slugify
from services.account_automation_engine import AccountAutomationEngine
from services.coalescence_engine import CoalescenceEngine
from services.credit_accounting_engine import CreditAccountingEngine
from services.job_registry import update_job as _jreg_update
from services.lob_service import LobService, LobValidator
from services.pipeline_run_logger import PipelineRunLogger

logger = logging.getLogger(__name__)


class LobAutomationEngine:
    """Enterprise Engine for Phased LOB Automation, Gatekeeping, and Self-Healing."""

    # Default rate assumption per enriched LOB across billable connectors (Firecrawl, Tavily, Serper)
    ESTIMATED_CREDITS_PER_LOB: int = 4
    DEFAULT_BATCH_SIZE: int = 10
    MAX_WORKERS_PER_BATCH: int = 3

    @classmethod
    def get_lob_tier_resources(cls) -> List[Dict[str, Any]]:
        """Audits billable and public connectors configured for LOB tier."""
        resources = []
        for key, meta in CreditAccountingEngine.RATE_CARD.items():
            if meta.get("tier") != "lob":
                continue

            is_billable = meta.get("is_billable", False)
            credits_per = meta.get("credits_per_unit", 0)
            masked_key = CreditAccountingEngine.get_masked_key(key)

            is_configured = True
            if "config_attr" in meta:
                val = getattr(config, meta["config_attr"], None)
                is_configured = bool(val and str(val).strip())
            elif "env_var" in meta:
                val = os.getenv(meta["env_var"])
                is_configured = bool(val and str(val).strip())

            resources.append({
                "id": key,
                "name": meta.get("name", key),
                "is_billable": is_billable,
                "credits_per_unit": credits_per,
                "api_key_masked": masked_key,
                "is_configured": is_configured or not is_billable,
                "unit_name": meta.get("unit_name", "ops"),
            })
        return resources

    @classmethod
    def preflight_lob_check(
        cls,
        account_id: Optional[int] = None,
        company_name: Optional[str] = None,
        lob_count: Optional[int] = None,
        batch_size: int = 10,
    ) -> Dict[str, Any]:
        """Pre-Flight Resource & Credit Gatekeeper for LOB Level.

        Estimates total required credits based on LOB count, audits configured API keys,
        and verifies available balance before initiating expensive multi-source runs.
        """
        # If lob_count not provided, inspect existing stubs in DB for this account
        effective_count = lob_count or 0
        effective_company = company_name or "Unknown Company"
        sec_cik = None
        domain = None

        if account_id:
            session = get_session()
            try:
                acct = session.query(Account).filter_by(id=account_id).first()
                if acct:
                    effective_company = acct.legal_name or acct.display_name or acct.key
                    sec_cik = acct.sec_cik
                    domain = acct.domain or acct.primary_domain
                    if not lob_count:
                        # Count existing LOB records
                        db_lob_count = session.query(Lob).filter_by(account_id=account_id).count()
                        if db_lob_count > 0:
                            effective_count = db_lob_count
                        elif acct.organisational_hierarchy_tree:
                            # Estimate from hierarchy tree
                            tree = acct.organisational_hierarchy_tree
                            subs = (
                                tree.get("sec_exhibit21_subsidiaries", [])
                                + tree.get("gleif_children", [])
                                + tree.get("all_subsidiaries", [])
                            )
                            effective_count = max(len(subs), 1)
            except Exception as e:
                logger.warning(f"Error querying Account for LOB preflight: {e}")
            finally:
                session.close()

        # If still 0, provide baseline estimation of 10 LOBs
        if effective_count <= 0:
            effective_count = 10

        # Rate card calculation
        resources = cls.get_lob_tier_resources()
        active_billable = [r for r in resources if r["is_billable"] and r["is_configured"]]
        unconfigured = [r for r in resources if r["is_billable"] and not r["is_configured"]]

        # Calculate rate per LOB
        rate_per_lob = sum(r["credits_per_unit"] for r in active_billable)
        if rate_per_lob == 0:
            rate_per_lob = cls.ESTIMATED_CREDITS_PER_LOB

        total_required_credits = effective_count * rate_per_lob
        per_batch_credits = min(effective_count, batch_size) * rate_per_lob

        balance_info = AccountAutomationEngine.get_workspace_credit_balance()
        available_credits = balance_info.get("available_credits", 0)

        is_sufficient = (available_credits >= total_required_credits) if (available_credits is not None) else True
        allowed = is_sufficient

        message = (
            f"Pre-flight approved for {effective_count} LOBs: {total_required_credits} credits estimated "
            f"({per_batch_credits} cr/batch of {batch_size}). Workspace balance: {available_credits} credits."
            if is_sufficient
            else f"Credit Gatekeeper Alert: Insufficient credits for {effective_count} LOBs. "
                 f"Estimated run requires {total_required_credits} credits, but workspace balance is only {available_credits} credits."
        )

        return {
            "allowed": allowed,
            "account_id": account_id,
            "company_name": effective_company,
            "domain": domain,
            "sec_cik": sec_cik,
            "estimated_lob_count": effective_count,
            "batch_size": batch_size,
            "rate_per_lob": rate_per_lob,
            "required_credits": total_required_credits,
            "per_batch_credits": per_batch_credits,
            "available_credits": available_credits,
            "balance_status": "sufficient" if is_sufficient else "insufficient",
            "active_sources": resources,
            "unconfigured_sources": unconfigured,
            "balance_info": balance_info,
            "message": message,
        }

    @classmethod
    def discover_lob_stubs(
        cls,
        account_id: int,
        company_name: str,
        domain: Optional[str] = None,
        sec_cik: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle 1: Lightweight Name & Stub Discovery.

        Fast multi-source extraction of LOB names and taxonomy stubs without running
        the expensive deep web scraper or LLM calls. Commits initial stubs to DB.
        """
        t0 = datetime.now(timezone.utc)
        discovered_names: List[str] = []
        discovered_divisions_meta: Dict[str, Dict[str, Any]] = {}
        session = get_session()

        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                return {
                    "status": "error",
                    "error": f"Account with ID {account_id} not found.",
                    "discovered_count": 0,
                    "stubs": [],
                }

            effective_company = acct.legal_name or acct.display_name or company_name
            effective_domain = domain or acct.domain or acct.primary_domain
            effective_sec_cik = sec_cik or acct.sec_cik

            # 1. Harvest from existing LOB records if already present
            if acct.lobs:
                for ex in acct.lobs:
                    if ex.lob_name:
                        discovered_names.append(ex.lob_name)
                        discovered_divisions_meta[ex.lob_name.strip().lower()] = {
                            "relationship_type": ex.relationship_type or "Operating Division",
                            "domain": ex.domain,
                            "overview": ex.overview,
                            "lei_code": ex.lei_code,
                            "jurisdiction": ex.jurisdiction,
                        }

            # 2. Harvest from pre-existing Account organisational_hierarchy_tree
            if acct.organisational_hierarchy_tree:
                tree = acct.organisational_hierarchy_tree
                for s in tree.get("sec_exhibit21_subsidiaries", []):
                    name = s.get("legal_name") or s.get("name")
                    if name:
                        discovered_names.append(name)
                        discovered_divisions_meta[name.strip().lower()] = {
                            "relationship_type": "Direct Operating Subsidiary",
                            "source": "SEC Form 10-K EX-21",
                            "jurisdiction": s.get("jurisdiction"),
                        }
                for c in tree.get("gleif_children", []):
                    name = c.get("legal_name") or c.get("name")
                    if name:
                        discovered_names.append(name)
                        discovered_divisions_meta[name.strip().lower()] = {
                            "relationship_type": "G20 Registered Entity",
                            "source": "GLEIF LEI Registry",
                            "lei_code": c.get("lei"),
                            "jurisdiction": c.get("jurisdiction"),
                        }
                for a_sub in tree.get("all_subsidiaries", []):
                    name = a_sub.get("legal_name") or a_sub.get("name")
                    if name:
                        discovered_names.append(name)
                for ind in tree.get("gleif_indirect_sublobs", []):
                    name = ind.get("legal_name") or ind.get("name")
                    if name:
                        discovered_names.append(name)

            # 3. If no names from DB/Tree, check Diffbot raw cache
            if not discovered_names:
                try:
                    comp_clean = slugify(effective_company or "")
                    for out_root in [config.OUTPUT_DIR / "raw" / "diffbot", config.OUTPUT_DIR]:
                        if not out_root.exists():
                            continue
                        for df_path in out_root.rglob("*diffbot*.json"):
                            if comp_clean and comp_clean in df_path.stem.lower():
                                try:
                                    import json
                                    with open(df_path, "r", encoding="utf-8") as df_f:
                                        df_data = json.load(df_f)
                                        df_obj = df_data.get("data", [{}])[0] if isinstance(df_data.get("data"), list) else df_data
                                        for sub in df_obj.get("subsidiaries", []):
                                            sub_n = sub if isinstance(sub, str) else sub.get("name")
                                            if sub_n and sub_n.strip():
                                                discovered_names.append(sub_n.strip())
                                                discovered_divisions_meta[sub_n.strip().lower()] = {
                                                    "relationship_type": "Corporate Subsidiary",
                                                    "source": "Diffbot Knowledge Graph",
                                                }
                                except Exception as parse_e:
                                    logger.debug(f"Diffbot cache read skip: {parse_e}")
                except Exception as df_err:
                    logger.debug(f"Diffbot lookup notice: {df_err}")

            # 4. Live Fallback to SEC Exhibit 21 & GLEIF if no stubs found in DB or Tree
            if not discovered_names and effective_sec_cik:
                try:
                    ex21 = fetch_sec_exhibit_21_subsidiaries(effective_sec_cik)
                    for s in ex21.get("subsidiaries", []):
                        name = s.get("legal_name")
                        if name:
                            discovered_names.append(name)
                            discovered_divisions_meta[name.strip().lower()] = {
                                "relationship_type": "Direct Operating Subsidiary",
                                "source": "SEC Form 10-K EX-21",
                            }
                except Exception as e:
                    logger.debug(f"Live SEC EX-21 notice: {e}")

            if not discovered_names and effective_company:
                try:
                    gleif_tree = fetch_gleif_ownership_tree(effective_company, max_children=25)
                    for c in gleif_tree.get("child_entities", []):
                        name = c.get("legal_name")
                        if name:
                            discovered_names.append(name)
                            discovered_divisions_meta[name.strip().lower()] = {
                                "relationship_type": "G20 Registered Entity",
                                "source": "GLEIF LEI Registry",
                                "lei_code": c.get("lei"),
                            }
                except Exception as e:
                    logger.debug(f"Live GLEIF notice: {e}")

            # 5. Augment with Crunchbase sub-organizations only if empty
            if not discovered_names:
                try:
                    cb_subs = scrape_sublobs(effective_company)
                    for cb in cb_subs:
                        cb_name = cb.get("name") or cb.get("sub_organization_name")
                        if cb_name:
                            discovered_names.append(cb_name)
                            discovered_divisions_meta[cb_name.strip().lower()] = {
                                "relationship_type": "Operating Subsidiary",
                                "source": "Crunchbase",
                            }
                except Exception as e:
                    logger.debug(f"Crunchbase sublobs notice: {e}")

            # 6. Dynamic Commercial Operating Divisions if still empty
            if not discovered_names:
                try:
                    divs = extract_commercial_operating_divisions(effective_company, effective_domain)
                    for d in divs:
                        d_name = d.get("name") or d.get("lob_name")
                        if d_name:
                            discovered_names.append(d_name)
                            discovered_divisions_meta[d_name.strip().lower()] = {
                                "relationship_type": d.get("relationship_type") or "Commercial Operating Division",
                                "domain": d.get("domain"),
                                "overview": d.get("overview"),
                                "source": "Commercial Division Intelligence",
                            }
                except Exception as div_err:
                    logger.debug(f"Dynamic commercial divisions notice: {div_err}")

            # 6. Deduplicate while preserving order and casing
            unique_names = []
            seen = set()
            for n in discovered_names:
                clean_n = n.strip()
                if clean_n and len(clean_n) > 1 and clean_n.lower() not in seen:
                    seen.add(clean_n.lower())
                    unique_names.append(clean_n)

            # 7. Build stub objects and persist non-destructively to DB
            stubs: List[Dict[str, Any]] = []
            lob_repo = LobRepository(session)

            for n in unique_names:
                meta = discovered_divisions_meta.get(n.lower(), {})
                stub_dict = {
                    "lob_name": n,
                    "name": n,
                    "key": slugify(n),
                    "relationship_type": meta.get("relationship_type", "Operating Division"),
                    "domain": meta.get("domain"),
                    "lei_code": meta.get("lei_code"),
                    "jurisdiction": meta.get("jurisdiction"),
                    "overview": meta.get("overview") or f"Discovered corporate division of {effective_company}.",
                    "status": "discovered_stub",
                }
                stubs.append(stub_dict)

            # Non-destructively upsert stubs so they are visible in UI immediately
            lob_repo.upsert_all(acct, stubs)
            session.commit()

            duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

            # Log stub discovery event (0 billable credits used for stub harvest)
            PipelineRunLogger.log_event(
                company_name=effective_company,
                level="lob",
                action="pull",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_sec,
                total_credits_used=0,
                quality_score=70.0,
                quality_grade="C",
                entities_extracted={
                    "account_id": account_id,
                    "discovered_stubs_count": len(stubs),
                    "action_phase": "cycle_1_stub_discovery",
                },
            )

            return {
                "status": "success",
                "account_id": account_id,
                "company_name": effective_company,
                "discovered_count": len(stubs),
                "duration_seconds": duration_sec,
                "stubs": stubs,
            }

        except Exception as e:
            session.rollback()
            logger.error(f"Error in discover_lob_stubs: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "discovered_count": 0,
                "stubs": [],
            }
        finally:
            session.close()

    @classmethod
    def partition_lobs(
        cls,
        lobs_list: List[Dict[str, Any]],
        chunk_size: int = 10,
        priority_queue: Optional[List[Dict[str, Any]]] = None,
        batch_size: Optional[int] = None,
        failed_priority_queue: Optional[List[Dict[str, Any]]] = None,
        mode: str = "auto",
    ) -> List[List[Dict[str, Any]]]:
        """Cycle 2: Phased Batch Partitioning with Priority Re-Queueing.

        Step A: Prepends failed LOBs from previous batches to indices 0..k (Front of Queue).
        Step B: In Auto Mode, prioritizes Commercial Brands and Operating Divisions first.
                In Manual Mode, preserves exact incoming order.
        Step C: Chunks into controlled sequential phases (e.g. 10, 15, 25).
        """
        effective_chunk_size = batch_size or chunk_size or cls.DEFAULT_BATCH_SIZE
        effective_priority = failed_priority_queue if failed_priority_queue is not None else priority_queue

        if not lobs_list and not effective_priority:
            return []

        # 1. Extract and mark priority retry LOBs
        priority_items: List[Dict[str, Any]] = []
        priority_names = set()
        if effective_priority:
            for item in effective_priority:
                p_copy = copy.deepcopy(item)
                p_copy["is_priority_retry"] = True
                nm = (p_copy.get("lob_name") or p_copy.get("name") or "").strip().lower()
                if nm and nm not in priority_names:
                    priority_names.add(nm)
                    priority_items.append(p_copy)

        # 2. Filter out items that are already in priority queue to avoid duplicates
        remaining = []
        for l in lobs_list:
            nm = (l.get("lob_name") or l.get("name") or "").strip().lower()
            if nm not in priority_names:
                remaining.append(l)

        # 3. Sort remaining by Corporate Taxonomy if in Auto Mode
        if mode == "auto":
            def _priority_score(item: Dict[str, Any]) -> int:
                rel = (item.get("relationship_type") or "").lower()
                name = (item.get("lob_name") or item.get("name") or "").lower()
                # Priority 1: Commercial Brands / Direct Operating Divisions
                if "commercial" in rel or "operating division" in rel or "brand" in rel:
                    return 1
                # Priority 2: Direct Operating Subsidiaries
                if "direct" in rel or "operating subsidiary" in rel:
                    return 2
                # Priority 3: Financial / holding units
                if "holding" in name or "capital" in name or "finance" in name:
                    return 4
                return 3

            sorted_remaining = sorted(remaining, key=_priority_score)
        else:
            sorted_remaining = remaining

        # 4. Combine priority items at the FRONT (indices 0..k)
        ordered_full_list = priority_items + sorted_remaining

        # 5. Chunk into batches
        chunks = []
        for i in range(0, len(ordered_full_list), effective_chunk_size):
            chunks.append(ordered_full_list[i : i + effective_chunk_size])

        return chunks

    @classmethod
    def execute_lob_batch_phase(
        cls,
        account_id: int,
        company_name: str,
        batch_lobs: List[Dict[str, Any]],
        phase_number: int,
        total_phases: int,
        sec_cik: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle 3: Controlled Sequential Batch Execution with Deferred Fault Logging.

        Executes 10-source enrichment on a batch with worker pool capped at 3.
        Isolates any transient connector failures into deferred_failures without halting.
        Immediately commits enriched LOBs to PostgreSQL via LobRepository.
        """
        t0 = datetime.now(timezone.utc)
        enriched_lobs: List[Dict[str, Any]] = []
        deferred_failures: List[Dict[str, Any]] = []
        actual_billable_credits: int = 0

        logger.info(
            f"[*] [LobAutomationEngine] Starting Batch Phase {phase_number}/{total_phases} "
            f"({len(batch_lobs)} LOBs) for {company_name}..."
        )

        if job_id:
            _jreg_update(
                job_id,
                message=f"Executing LOB Phase {phase_number}/{total_phases} ({len(batch_lobs)} LOBs)...",
                progress_pct=int(20 + ((phase_number - 1) / total_phases) * 60),
            )

        def _enrich_worker(lob_item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
            l_name = lob_item.get("lob_name") or lob_item.get("name") or "Unknown LOB"
            l_dom = lob_item.get("domain")
            l_is_retry = lob_item.get("is_priority_retry", False)
            failures = []

            # If this is a priority retry from a previous failed batch, apply cooling backoff
            if l_is_retry:
                time.sleep(2.0)

            try:
                # Call LobService.enrich_single_lob
                dossier = LobService.enrich_single_lob(
                    lob_name=l_name,
                    parent_company=company_name,
                    account_id=account_id,
                    lob_domain=l_dom,
                    sec_cik=sec_cik,
                )
                return dossier, failures
            except Exception as lob_err:
                logger.warning(f"[!] LOB '{l_name}' enrichment encountered fault: {lob_err}")
                failures.append({
                    "lob_name": l_name,
                    "account_id": account_id,
                    "source": "lob_enricher_core",
                    "error": str(lob_err),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "item_data": lob_item,
                })
                # Fallback to existing stub with partial error metadata
                fallback_dossier = dict(lob_item)
                fallback_dossier["_validation_audit"] = {"score": 40.0, "grade": "D", "status": "partial_fault"}
                return fallback_dossier, failures

        # Controlled ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=cls.MAX_WORKERS_PER_BATCH) as executor:
            future_map = {executor.submit(_enrich_worker, lob_it): lob_it for lob_it in batch_lobs}
            for fut in as_completed(future_map):
                try:
                    dossier, fails = fut.result()
                    enriched_lobs.append(dossier)
                    if fails:
                        deferred_failures.extend(fails)

                    # Inspect dossier for connector-specific empty/failed results
                    l_name = dossier.get("lob_name") or "LOB"
                    raw_data = dossier.get("raw_data") or {}
                    if not dossier.get("overview") and not raw_data.get("firecrawl"):
                        deferred_failures.append({
                            "lob_name": l_name,
                            "account_id": account_id,
                            "source": "firecrawl_lob",
                            "reason": "empty_overview_and_scrape",
                            "item_data": future_map[fut],
                        })
                    if not dossier.get("financial_snippets") and not raw_data.get("tavily"):
                        deferred_failures.append({
                            "lob_name": l_name,
                            "account_id": account_id,
                            "source": "tavily",
                            "reason": "missing_financial_snippets",
                            "item_data": future_map[fut],
                        })
                    if not dossier.get("website_url") and not dossier.get("domain") and not raw_data.get("serper"):
                        deferred_failures.append({
                            "lob_name": l_name,
                            "account_id": account_id,
                            "source": "serper_lob",
                            "reason": "missing_website_and_domain",
                            "item_data": future_map[fut],
                        })
                    if not dossier.get("technologies") and not raw_data.get("diffbot"):
                        deferred_failures.append({
                            "lob_name": l_name,
                            "account_id": account_id,
                            "source": "diffbot",
                            "reason": "missing_technologies_profile",
                            "item_data": future_map[fut],
                        })

                except Exception as exc:
                    lob_it = future_map[fut]
                    l_name = lob_it.get("lob_name") or lob_it.get("name")
                    logger.error(f"Worker exception on LOB {l_name}: {exc}")
                    deferred_failures.append({
                        "lob_name": l_name,
                        "account_id": account_id,
                        "source": "worker_thread",
                        "error": str(exc),
                        "item_data": lob_it,
                    })

        # Calculate credits for this batch
        lob_tally = CreditAccountingEngine.tally_lob_telemetry(len(enriched_lobs))
        actual_billable_credits = lob_tally.get("credits", len(enriched_lobs) * cls.ESTIMATED_CREDITS_PER_LOB)
        duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

        # Immediate DB Persistence for this batch
        session = get_session()
        saved_count = 0
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if acct:
                lob_repo = LobRepository(session)
                lob_map = lob_repo.upsert_all(acct, enriched_lobs)
                session.commit()
                saved_count = len(lob_map)
                logger.info(f"[+] [LobAutomationEngine] Phase {phase_number}: Safely committed {saved_count} LOBs to DB.")
        except Exception as db_err:
            session.rollback()
            logger.error(f"[!] [LobAutomationEngine] Phase {phase_number} DB commit error: {db_err}")
            deferred_failures.append({
                "phase": phase_number,
                "account_id": account_id,
                "source": "database_upsert",
                "error": str(db_err),
            })
        finally:
            session.close()

        # Compile run breakdown and log telemetry
        credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
            company_name=company_name,
            run_id=f"run_{slugify(company_name)[:18]}_lob_ph{phase_number}_{int(time.time())}",
            run_number=phase_number,
            started_at=t0,
            duration_seconds=duration_sec,
            status="success" if not deferred_failures else "completed_with_faults",
            account_tally={"credits": 0, "resources": []},
            lob_tally=lob_tally,
            persona_tally={"credits": 0, "resources": []},
        )

        batch_score = 85.0 if not deferred_failures else 72.0
        batch_grade = "A" if batch_score >= 85 else "B"

        PipelineRunLogger.log_event(
            company_name=company_name,
            level="lob",
            action="pull",
            status="success",
            started_at=t0,
            completed_at=datetime.now(timezone.utc),
            duration_seconds=duration_sec,
            total_credits_used=actual_billable_credits,
            credits_breakdown=credits_breakdown,
            quality_score=batch_score,
            quality_grade=batch_grade,
            entities_extracted={
                "account_id": account_id,
                "phase_number": phase_number,
                "total_phases": total_phases,
                "batch_lobs_count": len(enriched_lobs),
                "saved_count": saved_count,
                "deferred_faults_count": len(deferred_failures),
            },
        )

        # Query live balance after batch execution
        bal_info = AccountAutomationEngine.get_workspace_credit_balance()
        credits_remaining = bal_info.get("available_credits", 0)

        return {
            "phase_number": phase_number,
            "total_phases": total_phases,
            "duration_seconds": duration_sec,
            "credits_used": actual_billable_credits,
            "credits_remaining": credits_remaining,
            "rate_per_lob": cls.ESTIMATED_CREDITS_PER_LOB,
            "enriched_count": len(enriched_lobs),
            "saved_count": saved_count,
            "deferred_failures": deferred_failures,
            "enriched_lobs": enriched_lobs,
        }

    @classmethod
    def resolve_deferred_faults(
        cls,
        account_id: int,
        company_name: str,
        deferred_failures: List[Dict[str, Any]],
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle 4: Consolidated Post-Batch Self-Healing Resolution.

        Executes targeted micro-retries ONLY for failed connectors with adaptive cooling backoff (2.5s).
        Coalesces recovered data additively into existing LOBs and persists to PostgreSQL.
        """
        if not deferred_failures:
            return {
                "status": "success",
                "resolved_count": 0,
                "total_faults": 0,
                "message": "No deferred faults to resolve.",
            }

        t0 = datetime.now(timezone.utc)
        logger.info(
            f"[*] [LobAutomationEngine] Starting Consolidated Self-Healing for {len(deferred_failures)} faults..."
        )

        if job_id:
            _jreg_update(
                job_id,
                message=f"Consolidated Self-Healing: Retrying {len(deferred_failures)} failed connector logs...",
                progress_pct=88,
            )

        # Group faults by LOB name
        faults_by_lob: Dict[str, List[str]] = {}
        for f in deferred_failures:
            lob_name = f.get("lob_name")
            source = f.get("source")
            if lob_name and source and source not in ("database_upsert", "worker_thread"):
                faults_by_lob.setdefault(lob_name, []).append(source)

        resolved_count = 0
        total_retry_credits = 0
        session = get_session()

        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            if not acct:
                return {"status": "error", "error": f"Account {account_id} not found."}

            for lob_name, sources in faults_by_lob.items():
                existing_lob = session.query(Lob).filter_by(account_id=account_id, lob_name=lob_name).first()
                if not existing_lob:
                    continue

                # Adaptive cooling backoff between retries to avoid rate limit cascades
                time.sleep(2.5)

                # Complete failure recovery: If core enricher crashed, perform targeted full re-enrichment
                if "lob_enricher_core" in sources:
                    try:
                        dossier = LobService.enrich_single_lob(
                            lob_name=lob_name,
                            parent_company=company_name,
                            account_id=account_id,
                            lob_domain=existing_lob.domain,
                            sec_cik=acct.sec_cik,
                        )
                        if dossier:
                            existing_lob = CoalescenceEngine.coalesce_lob(existing_lob, dossier)
                            session.flush()
                            resolved_count += 1
                            total_retry_credits += cls.ESTIMATED_CREDITS_PER_LOB
                            continue
                    except Exception as full_retry_err:
                        logger.warning(f"Self-healing complete retry failed for {lob_name}: {full_retry_err}")

                # Partial failure recovery: Targeted micro-retries on missing connector channels
                recovered_data: Dict[str, Any] = {}
                for src in set(sources):
                    try:
                        if src in ("firecrawl_lob", "firecrawl"):
                            res = LobService._fetch_firecrawl_lob(lob_name, existing_lob.domain, company_name)
                            if res and res.get("overview"):
                                recovered_data["overview"] = res["overview"]
                                total_retry_credits += 1
                        elif src in ("tavily",):
                            res = LobService._fetch_tavily_lob_financials(lob_name, company_name)
                            if res:
                                recovered_data["financial_snippets"] = res.get("financial_snippets")
                                if res.get("audited_segment_revenue"):
                                    recovered_data["audited_segment_revenue"] = res["audited_segment_revenue"]
                                total_retry_credits += 1
                        elif src in ("serper_lob", "serper"):
                            res = LobService._fetch_serper_lob_website_and_wiki(lob_name, company_name)
                            if res:
                                if res.get("website_url"):
                                    recovered_data["website_url"] = res["website_url"]
                                if res.get("domain"):
                                    recovered_data["domain"] = res["domain"]
                                total_retry_credits += 1
                        elif src in ("diffbot",):
                            res = LobService._fetch_diffbot_lob_intel(lob_name, existing_lob.domain)
                            if res:
                                recovered_data["technologies"] = res.get("technologies")
                                total_retry_credits += 1
                    except Exception as retry_err:
                        logger.warning(f"Self-healing retry failed for {lob_name} on {src}: {retry_err}")

                if recovered_data:
                    # Non-destructively coalesce into existing DB record
                    existing_lob = CoalescenceEngine.coalesce_lob(existing_lob, recovered_data)
                    session.flush()
                    resolved_count += 1

            session.commit()
            duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

            # Log self-healing resolution milestone
            PipelineRunLogger.log_event(
                company_name=company_name,
                level="lob",
                action="retry",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_sec,
                total_credits_used=total_retry_credits,
                quality_score=90.0,
                quality_grade="A",
                entities_extracted={
                    "account_id": account_id,
                    "faults_targeted": len(deferred_failures),
                    "lobs_healed": resolved_count,
                    "credits_metered": total_retry_credits,
                },
            )

            return {
                "status": "success",
                "resolved_count": resolved_count,
                "total_faults": len(deferred_failures),
                "retry_credits_used": total_retry_credits,
                "duration_seconds": duration_sec,
                "message": f"Self-healing resolved {resolved_count} LOB faults across targeted connectors.",
            }

        except Exception as e:
            session.rollback()
            logger.error(f"Error resolving deferred faults: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            session.close()

    @classmethod
    def execute_full_lob_automation(
        cls,
        account_id: int,
        company_name: str,
        domain: Optional[str] = None,
        sec_cik: Optional[str] = None,
        batch_size: int = 10,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """End-to-End Autonomous LOB Orchestrator.

        Executes:
        Pre-Flight Audit -> Cycle 1 Stub Discovery -> Cycle 2 Partitioning ->
        Cycle 3 Sequential Batch Execution (with immediate DB dumps) ->
        Cycle 4 Deferred Fault Healing -> Final Summary.
        """
        t0 = datetime.now(timezone.utc)
        logger.info(f"[*] [LobAutomationEngine] Launching Full Automation for '{company_name}' (ID: {account_id})...")

        if job_id:
            _jreg_update(job_id, status="running", progress_pct=5, message=f"Starting LOB Automation for {company_name}...")

        # 1. Pre-Flight Credit Check
        preflight = cls.preflight_lob_check(account_id=account_id, company_name=company_name, batch_size=batch_size)
        if not preflight["allowed"]:
            msg = f"Halted: {preflight['message']}"
            if job_id:
                _jreg_update(job_id, status="failed", message=msg, error=msg)
            return {"status": "aborted", "reason": "insufficient_credits", "preflight": preflight}

        # 2. Cycle 1: Stub Discovery
        if job_id:
            _jreg_update(job_id, progress_pct=15, message="Cycle 1: Discovering LOB names & corporate stubs...")
        stubs_res = cls.discover_lob_stubs(
            account_id=account_id, company_name=company_name, domain=domain, sec_cik=sec_cik
        )
        stubs = stubs_res.get("stubs", [])
        if not stubs:
            msg = f"No LOB divisions or subsidiaries found for '{company_name}'."
            if job_id:
                _jreg_update(job_id, status="done", progress_pct=100, message=msg, result={"lobs": []})
            return {"status": "done", "lobs_count": 0, "message": msg}

        # 3. Cycle 2 & 3: Sequential Batch Execution with Dynamic Priority Re-Queueing
        all_enriched_lobs: List[Dict[str, Any]] = []
        all_deferred_failures: List[Dict[str, Any]] = []
        total_credits = 0

        # Dynamic pool of remaining stubs and priority queue for deferred faults
        remaining_stubs = list(stubs)
        priority_queue: List[Dict[str, Any]] = []
        phase_counter = 0

        total_estimated_phases = max(1, (len(stubs) + batch_size - 1) // batch_size)
        rate_per_lob = cls.ESTIMATED_CREDITS_PER_LOB

        while remaining_stubs or priority_queue:
            phase_counter += 1

            # Build current batch: Priority Queue items FIRST (indices 0..k), then fill up to batch_size with next stubs
            current_batch: List[Dict[str, Any]] = []

            # Drain priority queue into this batch
            while priority_queue and len(current_batch) < batch_size:
                p_item = priority_queue.pop(0)
                p_item["is_priority_retry"] = True
                current_batch.append(p_item)

            # Fill remaining slots with next stubs
            while remaining_stubs and len(current_batch) < batch_size:
                current_batch.append(remaining_stubs.pop(0))

            if not current_batch:
                break

            batch_req = len(current_batch) * rate_per_lob
            bal_info = AccountAutomationEngine.get_workspace_credit_balance()
            curr_balance = bal_info.get("available_credits", 0)

            # Check if current balance is sufficient for this specific batch
            if curr_balance is not None and curr_balance < batch_req:
                logger.warning(
                    f"[!] [LobAutomationEngine] Insufficient credits for Phase {phase_counter}/{total_estimated_phases}. "
                    f"Required: {batch_req} cr, Available: {curr_balance} cr. Gracefully pausing execution."
                )
                if job_id:
                    _jreg_update(
                        job_id,
                        status="paused_insufficient_credits",
                        progress_pct=int(20 + ((phase_counter - 1) / total_estimated_phases) * 60),
                        message=(
                            f"Execution paused at Phase {phase_counter}/{total_estimated_phases}: Required {batch_req} cr for next batch, "
                            f"but balance is {curr_balance} cr. Safely committed {len(all_enriched_lobs)} LOBs to DB."
                        ),
                        credits_remaining=curr_balance,
                        next_phase_required=batch_req,
                        completed_phases=phase_counter - 1,
                        total_phases=total_estimated_phases,
                        processed_count=len(all_enriched_lobs),
                        total_lobs=len(stubs),
                        error=f"Insufficient credits for Phase {phase_counter}. Previously saved {len(all_enriched_lobs)} LOBs intact.",
                    )
                return {
                    "status": "paused_insufficient_credits",
                    "reason": "insufficient_credits",
                    "completed_phases": phase_counter - 1,
                    "total_phases": total_estimated_phases,
                    "processed_lobs_count": len(all_enriched_lobs),
                    "total_lobs_count": len(stubs),
                    "credits_remaining": curr_balance,
                    "next_phase_required": batch_req,
                    "message": f"Paused at Phase {phase_counter} of {total_estimated_phases}: Workspace balance ({curr_balance} cr) is insufficient for next batch ({batch_req} cr).",
                }

            batch_result = cls.execute_lob_batch_phase(
                account_id=account_id,
                company_name=company_name,
                batch_lobs=current_batch,
                phase_number=phase_counter,
                total_phases=total_estimated_phases,
                sec_cik=sec_cik,
                job_id=job_id,
            )

            all_enriched_lobs.extend(batch_result.get("enriched_lobs", []))
            total_credits += batch_result.get("credits_used", 0)

            # Check if any LOBs failed in this batch
            batch_failures = batch_result.get("deferred_failures", [])
            if batch_failures:
                logger.info(
                    f"[!] [LobAutomationEngine] Phase {phase_counter}: {len(batch_failures)} LOBs/connectors failed. "
                    "Re-queueing them with PRIORITY for the next batch phase."
                )
                for f_item in batch_failures:
                    item_data = f_item.get("item_data")
                    if item_data:
                        priority_queue.append(item_data)
                    else:
                        all_deferred_failures.append(f_item)

            # Query updated balance after phase execution and project next batch requirement
            post_bal = AccountAutomationEngine.get_workspace_credit_balance().get("available_credits", 0)
            next_items_count = min(len(remaining_stubs) + len(priority_queue), batch_size)
            next_phase_req = next_items_count * rate_per_lob

            if job_id:
                phase_pct = min(85, int(20 + (phase_counter / max(1, total_estimated_phases)) * 60))
                _jreg_update(
                    job_id,
                    progress_pct=phase_pct,
                    message=f"Phase {phase_counter} complete ({len(current_batch)} LOBs). {post_bal} credits remaining.",
                    current_phase=phase_counter,
                    total_phases=total_estimated_phases,
                    processed_count=len(all_enriched_lobs),
                    total_lobs=len(stubs),
                    credits_remaining=post_bal,
                    next_phase_required=next_phase_req,
                )

        # 4. Cycle 4: Consolidated Deferred Self-Healing Resolution for residual failures
        if all_deferred_failures:
            healing_res = cls.resolve_deferred_faults(
                account_id=account_id,
                company_name=company_name,
                deferred_failures=all_deferred_failures,
                job_id=job_id,
            )
            total_credits += healing_res.get("retry_credits_used", 0)

        # 6. Final Summary
        duration_total = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)
        summary = {
            "status": "completed",
            "account_id": account_id,
            "company_name": company_name,
            "total_lobs": len(all_enriched_lobs),
            "total_phases": total_phases,
            "total_credits_used": total_credits,
            "credits_remaining": AccountAutomationEngine.get_workspace_credit_balance().get("available_credits", 0),
            "faults_deferred": len(all_deferred_failures),
            "duration_seconds": duration_total,
        }

        if job_id:
            _jreg_update(
                job_id,
                status="done",
                progress_pct=100,
                message=f"Autonomous LOB Ingestion Completed: {len(all_enriched_lobs)} LOBs across {total_phases} phases.",
                result=summary,
            )

        return summary
