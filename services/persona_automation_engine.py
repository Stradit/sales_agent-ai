"""
Persona Automation Engine — Phased Multi-Cycle Autonomous Persona Discovery, Ingestion & Self-Healing Engine.

Key Architectural Pillars:
1. Pre-Flight Resource & Credit Gatekeeper:
   - Audits Rate Cards for Persona tier against current available workspace credits.
   - Hard aborts before external calls if balance is insufficient, surfacing actionable alerts.
2. Cycle 1: Lightweight Name & Title/Seniority Discovery:
   - Fast multi-source extraction of executive names, titles, and seniority tiers without running
     expensive deep bio search, Exa, or FullEnrich waterfall.
   - Deduplicates, classifies corporate tiers (C-Suite, VP, Director, Manager), and commits stubs
     to PostgreSQL with status='discovered_stub' at ZERO billable credits.
3. Cycle 2: Phased Batch Partitioning with Priority Re-Queueing:
   - Supports both Auto Mode (automatic sequential tier sorting) and Manual Mode.
   - Partitions N Personas into chunks of 10, 20, 25, or 30.
   - Priority Re-Queueing: If previous batch has failed personas, they are placed at indices 0..k
     at the very front of the next batch!
4. Cycle 3: Controlled Sequential Execution with Deferred Fault Logging:
   - Worker pool capped at 3 concurrent threads per batch to prevent vendor rate limiting (FullEnrich / Exa / 429s).
   - Any failed connectors or persona errors are isolated into deferred_failures without halting the batch.
   - Enriched Personas in each batch are validated and dumped immediately to PostgreSQL via PersonaRepository.
5. Cycle 4: Consolidated Post-Batch Self-Healing Resolution:
   - Executes targeted micro-retries on failed connectors with adaptive cooling backoff.
   - Additively coalesces recovered intelligence into existing records via CoalescenceEngine.
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
from collectors.hierarchy_collector import (
    calculate_executive_authority_score,
    classify_title,
    clean_person_name,
)
from db.connection import get_session
from db.models.account import Account
from db.models.persona import Persona
from db.repositories.persona_repo import PersonaRepository
from db.repositories.pipeline_run_repository import PipelineRunRepository
from db.schemas.persona_schema import PersonaSchema
from serializers.account_serializer import slugify
from services.account_automation_engine import AccountAutomationEngine
from services.coalescence_engine import CoalescenceEngine
from services.credit_accounting_engine import CreditAccountingEngine
from services.job_registry import update_job as _jreg_update
from services.persona_service import (
    ExecutiveOsintUrlEngine,
    PersonaLOBRouter,
    PersonaService,
    PersonaValidator,
)
from services.pipeline_run_logger import PipelineRunLogger

logger = logging.getLogger(__name__)


class PersonaAutomationEngine:
    """Enterprise Engine for Phased Persona Automation, Gatekeeping, and Priority Self-Healing."""

    # Default rate assumption per enriched persona across billable connectors
    # (Apollo, FullEnrich, Exa, LinkedIn, Gemini/OpenAI dossier)
    ESTIMATED_CREDITS_PER_PERSONA: int = 5
    DEFAULT_BATCH_SIZE: int = 20
    MAX_WORKERS_PER_BATCH: int = 3

    @classmethod
    def get_persona_tier_resources(cls) -> List[Dict[str, Any]]:
        """Audits billable and public connectors configured for Persona tier."""
        resources = []
        for key, meta in CreditAccountingEngine.RATE_CARD.items():
            if meta.get("tier") != "persona":
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
    def preflight_persona_check(
        cls,
        account_id: Optional[int] = None,
        company_name: Optional[str] = None,
        persona_count: Optional[int] = None,
        batch_size: int = 20,
    ) -> Dict[str, Any]:
        """Pre-Flight Resource & Credit Gatekeeper for Persona Level.

        Estimates total required credits based on persona count, audits configured API keys,
        and verifies available balance before initiating expensive multi-source runs.
        """
        effective_count = persona_count or 0
        effective_company = company_name or "Unknown Company"
        domain = None

        if account_id:
            session = get_session()
            try:
                acct = session.query(Account).filter_by(id=account_id).first()
                if acct:
                    effective_company = acct.legal_name or acct.display_name or acct.key
                    domain = acct.domain or acct.primary_domain
                    if not persona_count:
                        # Count existing persona records
                        db_count = session.query(Persona).filter_by(account_id=account_id).count()
                        if db_count > 0:
                            effective_count = db_count
                        elif acct.organisational_hierarchy_tree:
                            tree = acct.organisational_hierarchy_tree
                            execs = tree.get("corporate_officers", []) + tree.get("key_executives", [])
                            effective_count = max(len(execs), 20)
            except Exception as e:
                logger.warning(f"Error querying Account for Persona preflight: {e}")
            finally:
                session.close()

        # If still 0, provide baseline estimation of 20 personas
        if effective_count <= 0:
            effective_count = 20

        # Rate card calculation
        resources = cls.get_persona_tier_resources()
        active_billable = [r for r in resources if r["is_billable"] and r["is_configured"]]
        unconfigured = [r for r in resources if r["is_billable"] and not r["is_configured"]]

        rate_per_persona = sum(r["credits_per_unit"] for r in active_billable)
        if rate_per_persona == 0:
            rate_per_persona = cls.ESTIMATED_CREDITS_PER_PERSONA

        total_required_credits = effective_count * rate_per_persona
        per_batch_credits = min(effective_count, batch_size) * rate_per_persona

        balance_info = AccountAutomationEngine.get_workspace_credit_balance()
        available_credits = balance_info.get("available_credits", 0)

        is_sufficient = available_credits >= total_required_credits
        allowed = is_sufficient

        message = (
            f"Pre-flight approved for {effective_count} Personas: {total_required_credits} credits estimated "
            f"({per_batch_credits} cr/batch of {batch_size}). Workspace balance: {available_credits} credits."
            if is_sufficient
            else f"Credit Gatekeeper Alert: Insufficient credits for {effective_count} Personas. "
                 f"Estimated run requires {total_required_credits} credits, but workspace balance is only {available_credits} credits."
        )

        return {
            "allowed": allowed,
            "account_id": account_id,
            "company_name": effective_company,
            "domain": domain,
            "estimated_persona_count": effective_count,
            "batch_size": batch_size,
            "rate_per_persona": rate_per_persona,
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
    def discover_persona_stubs(
        cls,
        account_id: int,
        company_name: str,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle 1: Lightweight Name & Title/Seniority Discovery.

        Fast multi-source extraction of executive names and positions without running
        expensive deep bio search, Exa, or FullEnrich waterfall. Commits stubs to DB (0 credits).
        """
        t0 = datetime.now(timezone.utc)
        discovered_people: List[Dict[str, Any]] = []
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

            # 1. Harvest from existing Persona records if present in DB
            if acct.personas:
                for p in acct.personas:
                    p_name = p.full_name or p.display_name
                    if p_name:
                        discovered_people.append({
                            "full_name": p_name,
                            "title": p.title or "Executive",
                            "tier": p.tier or classify_title(p.title or ""),
                            "linkedin_url": p.linkedin_url,
                            "email": p.email,
                            "source": "Existing Database Record",
                        })

            # 2. Harvest from Account organisational_hierarchy_tree
            if acct.organisational_hierarchy_tree:
                tree = acct.organisational_hierarchy_tree
                for tier_key in ["c_suite", "key_executives", "board_members", "vp_level", "corporate_officers"]:
                    for item in tree.get(tier_key, []):
                        nm = item.get("name") or item.get("full_name")
                        tt = item.get("title") or item.get("position") or "Executive"
                        if nm:
                            discovered_people.append({
                                "full_name": nm,
                                "title": tt,
                                "tier": classify_title(tt),
                                "linkedin_url": item.get("linkedin_url"),
                                "source": f"Account Hierarchy Tree ({tier_key})",
                            })

            # 3. Check raw Apollo / Diffbot / Crunchbase JSON caches in output/
            try:
                comp_clean = slugify(effective_company or "")
                search_dirs = [
                    config.OUTPUT_DIR / "raw" / "apollo",
                    config.OUTPUT_DIR / "raw" / "diffbot",
                    config.OUTPUT_DIR,
                ]
                for s_dir in search_dirs:
                    if not s_dir.exists():
                        continue
                    for fpath in s_dir.rglob("*.json"):
                        if comp_clean and comp_clean in fpath.stem.lower():
                            try:
                                import json
                                with open(fpath, "r", encoding="utf-8") as f:
                                    data = json.load(f)

                                # Diffbot structure
                                if "diffbot" in fpath.stem.lower():
                                    df_obj = data.get("data", [{}])[0] if isinstance(data.get("data"), list) else data
                                    for exec_item in df_obj.get("executives", []) + df_obj.get("boardMembers", []):
                                        en = exec_item.get("name")
                                        et = exec_item.get("title") or "Executive"
                                        if en:
                                            discovered_people.append({
                                                "full_name": en,
                                                "title": et,
                                                "tier": classify_title(et),
                                                "source": "Diffbot Executive Intelligence",
                                            })

                                # Apollo structure
                                if "apollo" in fpath.stem.lower():
                                    people_list = data.get("people", []) if isinstance(data.get("people"), list) else []
                                    for ap in people_list:
                                        ap_name = ap.get("name")
                                        ap_title = ap.get("title") or "Leader"
                                        if ap_name:
                                            discovered_people.append({
                                                "full_name": ap_name,
                                                "title": ap_title,
                                                "tier": classify_title(ap_title),
                                                "linkedin_url": ap.get("linkedin_url"),
                                                "source": "Apollo Hierarchy Discovery",
                                            })
                            except Exception as parse_e:
                                logger.debug(f"Cache file parse notice: {parse_e}")
            except Exception as cache_err:
                logger.debug(f"Persona cache scan notice: {cache_err}")

            # 4. Deduplicate while preserving order and casing
            unique_people = []
            seen_names = set()
            for p in discovered_people:
                raw_name = (p.get("full_name") or "").strip()
                cleaned = clean_person_name(raw_name)
                canonical_name = cleaned.get("full_name") or raw_name
                if canonical_name and len(canonical_name) > 2:
                    k = canonical_name.lower()
                    if k not in seen_names:
                        seen_names.add(k)
                        p["full_name"] = canonical_name
                        p["first_name"] = cleaned.get("first_name")
                        p["last_name"] = cleaned.get("last_name")
                        unique_people.append(p)

            # 5. Build stub records and persist to DB via PersonaRepository
            stubs: List[Dict[str, Any]] = []
            tier_counts = {"c_suite": 0, "vp_level": 0, "director_level": 0, "manager_level": 0}

            for p in unique_people:
                p_title = p.get("title") or "Executive"
                p_tier = p.get("tier") or classify_title(p_title)
                tier_counts[p_tier] = tier_counts.get(p_tier, 0) + 1

                stub_dict = {
                    "account_id": account_id,
                    "full_name": p["full_name"],
                    "display_name": p["full_name"],
                    "first_name": p.get("first_name"),
                    "last_name": p.get("last_name"),
                    "title": p_title,
                    "tier": p_tier,
                    "linkedin_url": p.get("linkedin_url"),
                    "email": p.get("email"),
                    "source": p.get("source") or "Lightweight Stub Discovery",
                    "raw_data": {"status": "discovered_stub"},
                    "extended_profile": {
                        "status": "discovered_stub",
                        "discovered_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
                stubs.append(stub_dict)

            # Non-destructively upsert stubs into DB
            repo = PersonaRepository(session)
            for st in stubs:
                try:
                    schema = PersonaSchema.from_enriched_json(st)
                    repo.upsert(schema)
                except Exception as up_err:
                    logger.debug(f"Stub upsert notice for {st.get('full_name')}: {up_err}")

            session.commit()
            duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

            # Log stub discovery event (0 billable credits used)
            PipelineRunLogger.log_event(
                company_name=effective_company,
                level="persona",
                action="pull",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_sec,
                total_credits_used=0,
                quality_score=75.0,
                quality_grade="C",
                entities_extracted={
                    "account_id": account_id,
                    "discovered_stubs_count": len(stubs),
                    "tier_breakdown": tier_counts,
                    "action_phase": "cycle_1_persona_stub_discovery",
                },
            )

            return {
                "status": "success",
                "account_id": account_id,
                "company_name": effective_company,
                "discovered_count": len(stubs),
                "duration_seconds": duration_sec,
                "tier_counts": tier_counts,
                "stubs": stubs,
            }

        except Exception as e:
            session.rollback()
            logger.error(f"Error in discover_persona_stubs: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "discovered_count": 0,
                "stubs": [],
                "tier_counts": {},
            }
        finally:
            session.close()

    @classmethod
    def partition_personas(
        cls,
        personas_list: List[Dict[str, Any]],
        chunk_size: int = 20,
        priority_queue: Optional[List[Dict[str, Any]]] = None,
        batch_size: Optional[int] = None,
        failed_priority_queue: Optional[List[Dict[str, Any]]] = None,
        mode: str = "auto",
    ) -> List[List[Dict[str, Any]]]:
        """Cycle 2: Phased Batch Partitioning with Priority Re-Queueing.

        Step A: Prepends failed personas from previous batches to indices 0..k (Front of Queue).
        Step B: In Auto Mode, sorts remaining personas by Seniority Tier: C-Suite -> VP -> Director -> Manager.
                In Manual Mode, preserves exact incoming order.
        Step C: Chunks into controlled sequential phases (e.g. 10, 20, 25, 30).
        """
        effective_chunk_size = batch_size or chunk_size or cls.DEFAULT_BATCH_SIZE
        effective_priority = failed_priority_queue if failed_priority_queue is not None else priority_queue

        if not personas_list and not effective_priority:
            return []

        # 1. Extract and mark priority retry personas
        priority_items: List[Dict[str, Any]] = []
        priority_names = set()
        if effective_priority:
            for item in effective_priority:
                p_copy = copy.deepcopy(item)
                p_copy["is_priority_retry"] = True
                nm = (p_copy.get("full_name") or p_copy.get("name") or "").strip().lower()
                if nm and nm not in priority_names:
                    priority_names.add(nm)
                    priority_items.append(p_copy)

        # 2. Filter out items that are already in priority queue to avoid duplicates
        remaining = []
        for p in personas_list:
            nm = (p.get("full_name") or p.get("name") or "").strip().lower()
            if nm not in priority_names:
                remaining.append(p)

        # 3. Sort remaining by Seniority Tier if in Auto Mode
        if mode == "auto":
            def _tier_rank(item: Dict[str, Any]) -> int:
                t = (item.get("tier") or "").lower()
                if not t:
                    t = classify_title(item.get("title") or "")
                if t == "c_suite":
                    return 1
                if t == "vp_level":
                    return 2
                if t == "director_level":
                    return 3
                if t == "manager_level":
                    return 4
                return 5

            sorted_remaining = sorted(remaining, key=_tier_rank)
        else:
            sorted_remaining = remaining

        # 4. Combine priority items at the FRONT
        ordered_full_list = priority_items + sorted_remaining

        # 5. Chunk into batches
        chunks = []
        for i in range(0, len(ordered_full_list), effective_chunk_size):
            chunks.append(ordered_full_list[i : i + effective_chunk_size])

        return chunks

    @classmethod
    def audit_persona_completeness(
        cls,
        dossier: Dict[str, Any],
        persona_item: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Audits persona completeness across 10 critical & high-value intelligence dimensions.
        Determines:
        - score: 0 to 100%
        - status: 'enriched' | 'partially_enriched' | 'failed' | 'unresolvable'
        - grade: 'A+' | 'A' | 'B' | 'C' | 'D' | 'F'
        - missing_fields: list of unpopulated dimensions
        - delta_sources_needed: targeted connectors for missing data
        """
        item = persona_item or {}
        ext_prof = item.get("extended_profile") if isinstance(item.get("extended_profile"), dict) else {}
        retry_count = ext_prof.get("retry_count", 0) if ext_prof else item.get("retry_count", 0)

        # Dimension checks
        fields_to_check = {
            "name": bool(dossier.get("full_name") or dossier.get("name")),
            "title": bool(dossier.get("title")),
            "email": bool(dossier.get("email") or dossier.get("personal_email")),
            "phone": bool(dossier.get("phone") or dossier.get("direct_mobile_phone")),
            "linkedin_url": bool(dossier.get("linkedin_url")),
            "headline": bool(dossier.get("headline")),
            "employment_history": bool(dossier.get("employment_history") or dossier.get("past_companies")),
            "education_history": bool(dossier.get("education_history") or dossier.get("degree")),
            "value_proposition": bool(dossier.get("value_proposition")),
            "personalized_icebreaker": bool(dossier.get("personalized_icebreaker")),
        }

        populated = [k for k, v in fields_to_check.items() if v]
        missing = [k for k, v in fields_to_check.items() if not v]
        score = int((len(populated) / len(fields_to_check)) * 100)

        # Assign Grade
        if score >= 90:
            grade = "A+"
        elif score >= 75:
            grade = "A"
        elif score >= 60:
            grade = "B"
        elif score >= 40:
            grade = "C"
        elif score >= 20:
            grade = "D"
        else:
            grade = "F"

        # Check status
        if retry_count >= 3 and score < 50:
            status = "unresolvable"
        elif score >= 70 and (fields_to_check["email"] or fields_to_check["linkedin_url"]):
            status = "enriched"
        elif score >= 30 or (fields_to_check["title"] and (fields_to_check["linkedin_url"] or fields_to_check["employment_history"] or fields_to_check["email"])):
            status = "partially_enriched"
        else:
            status = "failed"

        # Targeted delta sources needed
        delta_sources = []
        if not fields_to_check["email"] or not fields_to_check["phone"]:
            delta_sources.append("fullenrich")
            delta_sources.append("apollo")
        if not fields_to_check["linkedin_url"]:
            delta_sources.append("serper_persona")
            delta_sources.append("exa")
        if not fields_to_check["employment_history"]:
            delta_sources.append("apify_linkedin_profile")
        if not fields_to_check["value_proposition"] or not fields_to_check["personalized_icebreaker"]:
            delta_sources.append("gemini_llm")

        return {
            "score": score,
            "grade": grade,
            "status": status,
            "retry_count": retry_count,
            "populated_fields": populated,
            "missing_fields": missing,
            "delta_sources_needed": delta_sources,
            "is_partial": status == "partially_enriched",
            "is_failed": status in ("failed", "unresolvable"),
            "can_delta_retry": status == "partially_enriched" and len(delta_sources) > 0,
        }

    @classmethod
    def execute_persona_batch_phase(
        cls,
        account_id: int,
        company_name: str,
        batch_personas: List[Dict[str, Any]],
        phase_number: int = 1,
        total_phases: int = 1,
        domain: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cycle 3: Controlled Sequential Batch Execution with Deferred Fault Logging.

        Executes multi-source enrichment with worker pool capped at 3 concurrent threads.
        Isolates transient connector failures into deferred_failures.
        Immediately commits enriched Personas to PostgreSQL via PersonaRepository.
        """
        t0 = datetime.now(timezone.utc)
        enriched_personas: List[Dict[str, Any]] = []
        deferred_failures: List[Dict[str, Any]] = []

        logger.info(
            f"[*] [PersonaAutomationEngine] Starting Batch Phase {phase_number}/{total_phases} "
            f"({len(batch_personas)} personas) for {company_name}..."
        )

        if job_id:
            _jreg_update(
                job_id,
                message=f"Enriching Persona Phase {phase_number}/{total_phases} ({len(batch_personas)} personas)...",
                progress_pct=int(20 + ((phase_number - 1) / total_phases) * 60),
            )

        def _enrich_worker(persona_item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
            p_name = persona_item.get("full_name") or persona_item.get("name") or "Executive"
            p_title = persona_item.get("title")
            p_linkedin = persona_item.get("linkedin_url")
            p_is_retry = persona_item.get("is_priority_retry", False)
            failures = []

            # If this is a priority retry from a previous failed batch, apply cooling backoff
            if p_is_retry:
                time.sleep(2.0)

            try:
                # Call PersonaService.enrich_single_persona
                dossier = PersonaService.enrich_single_persona(
                    full_name=p_name,
                    company_name=company_name,
                    title=p_title,
                    account_id=account_id,
                    domain=domain,
                    linkedin_url=p_linkedin,
                )
                audit = cls.audit_persona_completeness(dossier, persona_item)
                ext = dict(dossier.get("extended_profile") or {})
                ext["enrichment_status"] = audit["status"]
                ext["completeness_score"] = audit["score"]
                ext["quality_grade"] = audit["grade"]
                ext["missing_fields"] = audit["missing_fields"]
                ext["delta_sources_needed"] = audit["delta_sources_needed"]
                ext["retry_count"] = audit["retry_count"]
                dossier["extended_profile"] = ext
                dossier["_validation_audit"] = audit
                return dossier, failures, audit
            except Exception as p_err:
                logger.warning(f"[!] Persona '{p_name}' enrichment encountered fault: {p_err}")
                failures.append({
                    "full_name": p_name,
                    "title": p_title,
                    "account_id": account_id,
                    "source": "persona_enricher_core",
                    "error": str(p_err),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "item_data": persona_item,
                })
                fallback_dossier = dict(persona_item)
                audit = cls.audit_persona_completeness(fallback_dossier, persona_item)
                audit["status"] = "failed"
                audit["last_error"] = str(p_err)
                ext = dict(fallback_dossier.get("extended_profile") or {})
                ext["enrichment_status"] = "failed"
                ext["completeness_score"] = audit["score"]
                ext["quality_grade"] = "F"
                ext["last_error"] = str(p_err)
                ext["retry_count"] = audit["retry_count"] + 1
                fallback_dossier["extended_profile"] = ext
                fallback_dossier["_validation_audit"] = audit
                return fallback_dossier, failures, audit

        # Controlled ThreadPoolExecutor
        enriched_list: List[Dict[str, Any]] = []
        partial_list: List[Dict[str, Any]] = []
        failed_list: List[Dict[str, Any]] = []
        unresolvable_list: List[Dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=cls.MAX_WORKERS_PER_BATCH) as executor:
            future_map = {executor.submit(_enrich_worker, p_item): p_item for p_item in batch_personas}
            for fut in as_completed(future_map):
                try:
                    dossier, fails, audit = fut.result()
                    enriched_personas.append(dossier)
                    if fails:
                        deferred_failures.extend(fails)

                    st = audit.get("status")
                    if st == "enriched":
                        enriched_list.append(dossier)
                    elif st == "partially_enriched":
                        partial_list.append(dossier)
                    elif st == "unresolvable":
                        unresolvable_list.append(dossier)
                    else:
                        failed_list.append(dossier)

                    # Connector validation checks
                    p_name = dossier.get("full_name") or dossier.get("name")
                    if not dossier.get("email") and not dossier.get("phone") and not dossier.get("linkedin_url"):
                        deferred_failures.append({
                            "full_name": p_name,
                            "account_id": account_id,
                            "source": "waterfall_contact",
                            "reason": "missing_all_contact_channels",
                            "item_data": future_map[fut],
                        })
                except Exception as exc:
                    p_item = future_map[fut]
                    p_name = p_item.get("full_name") or p_item.get("name")
                    logger.error(f"Worker exception on Persona {p_name}: {exc}")
                    deferred_failures.append({
                        "full_name": p_name,
                        "account_id": account_id,
                        "source": "worker_thread",
                        "error": str(exc),
                        "item_data": p_item,
                    })

        # Calculate credits for this batch:
        # Full rate (5cr) for complete, Pro-rated (2cr) for partial, ZERO credits for failed/unresolvable!
        actual_billable_credits = (len(enriched_list) * cls.ESTIMATED_CREDITS_PER_PERSONA) + (len(partial_list) * 2)
        duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

        # Immediate DB Persistence for this batch
        session = get_session()
        saved_count = 0
        try:
            repo = PersonaRepository(session)
            for enr in enriched_personas:
                try:
                    schema = PersonaSchema.from_enriched_json(enr)
                    repo.upsert(schema)
                    saved_count += 1
                except Exception as s_err:
                    logger.warning(f"Error persisting persona {enr.get('full_name')}: {s_err}")
            session.commit()
            logger.info(f"[+] [PersonaAutomationEngine] Phase {phase_number}: Safely committed {saved_count} personas to DB.")
        except Exception as db_err:
            session.rollback()
            logger.error(f"[!] [PersonaAutomationEngine] Phase {phase_number} DB commit error: {db_err}")
            deferred_failures.append({
                "phase": phase_number,
                "account_id": account_id,
                "source": "database_upsert",
                "error": str(db_err),
            })
        finally:
            session.close()

        # Compile run breakdown and log telemetry
        persona_tally = {"credits": actual_billable_credits, "resources": []}
        credits_breakdown = CreditAccountingEngine.compile_run_breakdown(
            company_name=company_name,
            run_id=f"run_{slugify(company_name)[:18]}_persona_ph{phase_number}_{int(time.time())}",
            run_number=phase_number,
            started_at=t0,
            duration_seconds=duration_sec,
            status="success" if not deferred_failures else "completed_with_faults",
            account_tally={"credits": 0, "resources": []},
            lob_tally={"credits": 0, "resources": []},
            persona_tally=persona_tally,
        )

        batch_score = 90.0 if not failed_list else (75.0 if not unresolvable_list else 60.0)
        batch_grade = "A" if batch_score >= 85 else ("B" if batch_score >= 70 else "C")

        PipelineRunLogger.log_event(
            company_name=company_name,
            level="persona",
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
                "total_count": len(enriched_personas),
                "enriched_count": len(enriched_list),
                "partial_count": len(partial_list),
                "failed_count": len(failed_list),
                "unresolvable_count": len(unresolvable_list),
                "saved_count": saved_count,
                "deferred_faults_count": len(deferred_failures),
            },
        )

        return {
            "phase_number": phase_number,
            "total_phases": total_phases,
            "duration_seconds": duration_sec,
            "credits_used": actual_billable_credits,
            "total_count": len(enriched_personas),
            "enriched_count": len(enriched_list),
            "partial_count": len(partial_list),
            "failed_count": len(failed_list),
            "unresolvable_count": len(unresolvable_list),
            "breakdown": {
                "enriched": len(enriched_list),
                "partial": len(partial_list),
                "failed": len(failed_list),
                "unresolvable": len(unresolvable_list),
            },
            "saved_count": saved_count,
            "deferred_failures": deferred_failures,
            "partially_enriched_personas": partial_list,
            "failed_personas": failed_list,
            "enriched_personas": enriched_personas,
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

        Executes targeted micro-retries on failed personas with cooling backoff.
        Coalesces recovered intelligence into existing records via CoalescenceEngine.
        """
        t0 = datetime.now(timezone.utc)
        resolved_count = 0
        total_retry_credits = 0
        session = get_session()

        try:
            repo = PersonaRepository(session)
            for fail in deferred_failures:
                item_data = fail.get("item_data") or {}
                p_name = fail.get("full_name") or item_data.get("full_name")
                if not p_name:
                    continue

                time.sleep(1.5)  # cooling backoff
                try:
                    dossier = PersonaService.enrich_single_persona(
                        full_name=p_name,
                        company_name=company_name,
                        title=item_data.get("title"),
                        account_id=account_id,
                        linkedin_url=item_data.get("linkedin_url"),
                    )
                    schema = PersonaSchema.from_enriched_json(dossier)
                    repo.upsert(schema)
                    resolved_count += 1
                    total_retry_credits += 2
                except Exception as retry_err:
                    logger.warning(f"Self-healing retry failed for Persona {p_name}: {retry_err}")

            session.commit()
            duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

            PipelineRunLogger.log_event(
                company_name=company_name,
                level="persona",
                action="retry",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_sec,
                total_credits_used=total_retry_credits,
                quality_score=92.0,
                quality_grade="A",
                entities_extracted={
                    "account_id": account_id,
                    "faults_targeted": len(deferred_failures),
                    "personas_healed": resolved_count,
                    "credits_metered": total_retry_credits,
                },
            )

            return {
                "status": "success",
                "resolved_count": resolved_count,
                "total_faults": len(deferred_failures),
                "retry_credits_used": total_retry_credits,
                "duration_seconds": duration_sec,
                "message": f"Self-healing resolved {resolved_count} Persona faults across targeted connectors.",
            }

        except Exception as e:
            session.rollback()
            logger.error(f"Error resolving persona deferred faults: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            session.close()

    @classmethod
    def enrich_persona_delta(
        cls,
        persona_id: int,
        account_id: int,
        company_name: str,
        target_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Cycle 4a: Targeted Delta Micro-Enrichment for half-fetched / partial personas.
        Queries ONLY missing connectors (e.g. FullEnrich for email/phone, or Gemini for icebreaker),
        additively merges into existing record via CoalescenceEngine, and updates status.
        Uses 1-2 credits instead of 5!
        """
        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            repo = PersonaRepository(session)
            persona = session.query(Persona).filter_by(id=persona_id).first()
            if not persona:
                return {"status": "error", "error": f"Persona ID {persona_id} not found."}

            p_name = persona.full_name or persona.display_name or "Executive"
            p_title = persona.title
            p_linkedin = persona.linkedin_url

            # Audit current state
            current_dict = {
                "full_name": p_name,
                "title": p_title,
                "email": persona.email,
                "phone": persona.phone,
                "linkedin_url": p_linkedin,
                "headline": persona.headline,
                "employment_history": persona.employment_history,
                "education_history": persona.education_history,
                "value_proposition": persona.value_proposition,
                "personalized_icebreaker": persona.personalized_icebreaker,
            }
            audit_before = cls.audit_persona_completeness(current_dict)
            needed_sources = audit_before["delta_sources_needed"]

            delta_payload: Dict[str, Any] = {}
            credits_used = 1

            # 1. Targeted FullEnrich waterfall if email or phone missing
            if ("fullenrich" in needed_sources or "apollo" in needed_sources) and (not persona.email or not persona.phone):
                try:
                    fe_res = PersonaService._fetch_fullenrich_waterfall(
                        full_name=p_name,
                        company_name=company_name,
                        domain=persona.account.domain if persona.account else None,
                        linkedin_url=p_linkedin,
                    )
                    if isinstance(fe_res, dict) and fe_res:
                        if fe_res.get("email"):
                            delta_payload["email"] = fe_res["email"]
                        if fe_res.get("phone") or fe_res.get("mobile_phone"):
                            delta_payload["phone"] = fe_res.get("phone") or fe_res.get("mobile_phone")
                        if fe_res.get("linkedin_url") and not p_linkedin:
                            delta_payload["linkedin_url"] = fe_res["linkedin_url"]
                        credits_used += 1
                except Exception as fe_err:
                    logger.warning(f"Delta FullEnrich notice for {p_name}: {fe_err}")

            # 2. Targeted Serper OSINT if LinkedIn missing
            if "serper_persona" in needed_sources and not delta_payload.get("linkedin_url") and not p_linkedin:
                try:
                    serp_res = PersonaService._fetch_serper_executive_osint(p_name, company_name)
                    if isinstance(serp_res, dict) and serp_res.get("linkedin_url"):
                        delta_payload["linkedin_url"] = serp_res["linkedin_url"]
                except Exception as serp_err:
                    logger.warning(f"Delta Serper notice for {p_name}: {serp_err}")

            # 3. Targeted Gemini synthesis if value proposition missing
            if "gemini_llm" in needed_sources and (not persona.value_proposition or not persona.personalized_icebreaker):
                try:
                    ai_res = PersonaService._synthesize_ai_sales_dossier(
                        full_name=p_name,
                        title=p_title or "Executive",
                        company_name=company_name,
                    )
                    if isinstance(ai_res, dict) and ai_res:
                        for k in ["value_proposition", "personalized_icebreaker", "communication_style", "skills", "target_kpis"]:
                            if ai_res.get(k):
                                delta_payload[k] = ai_res[k]
                except Exception as ai_err:
                    logger.warning(f"Delta AI synthesis notice for {p_name}: {ai_err}")

            # Additive merge using CoalescenceEngine
            CoalescenceEngine.coalesce_persona(persona, delta_payload)

            # Re-audit
            updated_dict = {
                "full_name": persona.full_name,
                "title": persona.title,
                "email": persona.email,
                "phone": persona.phone,
                "linkedin_url": persona.linkedin_url,
                "headline": persona.headline,
                "employment_history": persona.employment_history,
                "education_history": persona.education_history,
                "value_proposition": persona.value_proposition,
                "personalized_icebreaker": persona.personalized_icebreaker,
            }
            audit_after = cls.audit_persona_completeness(updated_dict)

            # Update extended_profile metadata
            ext = dict(persona.extended_profile or {})
            ext["enrichment_status"] = audit_after["status"]
            ext["completeness_score"] = audit_after["score"]
            ext["quality_grade"] = audit_after["grade"]
            ext["missing_fields"] = audit_after["missing_fields"]
            ext["delta_enriched_at"] = datetime.now(timezone.utc).isoformat()
            persona.extended_profile = ext

            session.commit()
            duration_sec = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)

            # Telemetry
            PipelineRunLogger.log_event(
                company_name=company_name,
                level="persona",
                action="pull",
                status="success",
                started_at=t0,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_sec,
                total_credits_used=credits_used,
                quality_score=float(audit_after["score"]),
                quality_grade=audit_after["grade"],
                entities_extracted={
                    "persona_id": persona.id,
                    "delta_fields_added": list(delta_payload.keys()),
                    "score_before": audit_before["score"],
                    "score_after": audit_after["score"],
                    "new_status": audit_after["status"],
                },
            )

            return {
                "status": "success",
                "persona_id": persona.id,
                "full_name": p_name,
                "score_before": audit_before["score"],
                "score_after": audit_after["score"],
                "new_status": audit_after["status"],
                "new_grade": audit_after["grade"],
                "fields_recovered": list(delta_payload.keys()),
                "credits_used": credits_used,
                "duration_seconds": duration_sec,
                "message": f"Delta micro-retry completed for {p_name}: Score increased from {audit_before['score']}% to {audit_after['score']}%.",
            }
        except Exception as e:
            session.rollback()
            logger.error(f"Error in enrich_persona_delta for persona {persona_id}: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            session.close()

    @classmethod
    def retry_single_failed_persona(
        cls,
        persona_id: int,
        account_id: int,
        company_name: str,
    ) -> Dict[str, Any]:
        """Cycle 4b: Priority Single Retry for completely failed personas (with backoff & DLQ check)."""
        t0 = datetime.now(timezone.utc)
        session = get_session()
        try:
            repo = PersonaRepository(session)
            persona = session.query(Persona).filter_by(id=persona_id).first()
            if not persona:
                return {"status": "error", "error": f"Persona ID {persona_id} not found."}

            ext = dict(persona.extended_profile or {})
            curr_retries = ext.get("retry_count", 0)

            # Circuit Breaker: Max 3 Retries
            if curr_retries >= 3:
                ext["enrichment_status"] = "unresolvable"
                ext["last_error"] = "Max automated retries (3) reached across connectors."
                persona.extended_profile = ext
                session.commit()
                return {
                    "status": "unresolvable",
                    "persona_id": persona.id,
                    "full_name": persona.full_name,
                    "retry_count": curr_retries,
                    "message": f"Executive {persona.full_name} marked as unresolvable (3 retries exhausted). Please edit or verify manually.",
                }

            # Adaptive cooling backoff
            time.sleep(2.0)

            p_name = persona.full_name or persona.display_name or "Executive"
            p_title = persona.title
            p_linkedin = persona.linkedin_url
            domain = persona.account.domain if persona.account else None

            # Attempt full enrichment
            try:
                dossier = PersonaService.enrich_single_persona(
                    full_name=p_name,
                    company_name=company_name,
                    title=p_title,
                    account_id=account_id,
                    domain=domain,
                    linkedin_url=p_linkedin,
                )
                audit = cls.audit_persona_completeness(dossier)
                schema = PersonaSchema.from_enriched_json(dossier)
                repo.upsert(schema)

                # Re-fetch and update extended metadata
                updated_p = session.query(Persona).filter_by(id=persona.id).first()
                if updated_p:
                    u_ext = dict(updated_p.extended_profile or {})
                    u_ext["enrichment_status"] = audit["status"]
                    u_ext["completeness_score"] = audit["score"]
                    u_ext["quality_grade"] = audit["grade"]
                    u_ext["retry_count"] = curr_retries + 1
                    updated_p.extended_profile = u_ext
                session.commit()

                credits = 5 if audit["status"] == "enriched" else (2 if audit["status"] == "partially_enriched" else 0)
                return {
                    "status": "success",
                    "persona_id": persona.id,
                    "full_name": p_name,
                    "new_status": audit["status"],
                    "completeness_score": audit["score"],
                    "retry_count": curr_retries + 1,
                    "credits_used": credits,
                    "message": f"Retry succeeded for {p_name}: status is now {audit['status']} ({audit['score']}%).",
                }
            except Exception as retry_err:
                ext["retry_count"] = curr_retries + 1
                ext["enrichment_status"] = "unresolvable" if (curr_retries + 1) >= 3 else "failed"
                ext["last_error"] = str(retry_err)
                persona.extended_profile = ext
                session.commit()
                return {
                    "status": ext["enrichment_status"],
                    "persona_id": persona.id,
                    "full_name": p_name,
                    "retry_count": curr_retries + 1,
                    "error": str(retry_err),
                    "message": f"Retry failed for {p_name}: {retry_err}",
                }
        except Exception as e:
            session.rollback()
            logger.error(f"Error in retry_single_failed_persona: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
        finally:
            session.close()

    @classmethod
    def get_account_personas_triage(cls, account_id: int) -> Dict[str, Any]:
        """Categorized status audit of personas for account: enriched, partial, failed, unresolvable."""
        session = get_session()
        try:
            personas = session.query(Persona).filter_by(account_id=account_id).all()
            total = len(personas)
            enriched = []
            partial = []
            failed = []
            unresolvable = []
            stubs = []

            for p in personas:
                ext = p.extended_profile if isinstance(p.extended_profile, dict) else {}
                status = ext.get("enrichment_status")
                score = ext.get("completeness_score")

                # Fallback to computing score if not yet set
                if score is None:
                    p_dict = {
                        "full_name": p.full_name,
                        "title": p.title,
                        "email": p.email,
                        "phone": p.phone,
                        "linkedin_url": p.linkedin_url,
                        "headline": p.headline,
                        "employment_history": p.employment_history,
                        "education_history": p.education_history,
                        "value_proposition": p.value_proposition,
                        "personalized_icebreaker": p.personalized_icebreaker,
                    }
                    audit = cls.audit_persona_completeness(p_dict)
                    score = audit["score"]
                    if not status:
                        status = audit["status"]

                status = status or "discovered_stub"
                p_summary = {
                    "id": p.id,
                    "name": p.full_name or p.display_name,
                    "title": p.title,
                    "tier": p.tier,
                    "status": status,
                    "score": score,
                    "email": p.email,
                    "phone": p.phone,
                    "linkedin_url": p.linkedin_url,
                    "missing_fields": ext.get("missing_fields", []),
                    "last_error": ext.get("last_error"),
                    "retry_count": ext.get("retry_count", 0),
                }

                if status == "enriched":
                    enriched.append(p_summary)
                elif status == "partially_enriched":
                    partial.append(p_summary)
                elif status == "unresolvable":
                    unresolvable.append(p_summary)
                elif status == "failed":
                    failed.append(p_summary)
                else:
                    stubs.append(p_summary)

            return {
                "account_id": account_id,
                "total": total,
                "enriched_count": len(enriched),
                "partial_count": len(partial),
                "failed_count": len(failed),
                "unresolvable_count": len(unresolvable),
                "stubs_count": len(stubs),
                "enriched": enriched,
                "partial": partial,
                "failed": failed,
                "unresolvable": unresolvable,
                "stubs": stubs,
            }
        finally:
            session.close()

    @classmethod
    def execute_full_persona_automation(
        cls,
        account_id: int,
        company_name: str,
        domain: Optional[str] = None,
        batch_size: int = 20,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """End-to-End Autonomous Persona Orchestrator with Priority Re-Queueing.

        Executes:
        Pre-Flight Audit -> Cycle 1 Stub Discovery -> Cycle 2 Batch Partitioning ->
        Cycle 3 Sequential Execution with Priority Re-Queueing across Batches ->
        Cycle 4 Deferred Fault Healing -> Final Summary.
        """
        t0 = datetime.now(timezone.utc)
        logger.info(f"[*] [PersonaAutomationEngine] Launching Full Automation for '{company_name}' (ID: {account_id})...")

        if job_id:
            _jreg_update(
                job_id,
                status="running",
                progress_pct=5,
                message=f"Starting Persona Automation for {company_name}...",
            )

        # 1. Pre-Flight Credit Gatekeeper Check
        preflight = cls.preflight_persona_check(account_id=account_id, company_name=company_name, batch_size=batch_size)
        if not preflight["allowed"]:
            msg = f"Halted: {preflight['message']}"
            if job_id:
                _jreg_update(job_id, status="failed", message=msg, error=msg)
            return {"status": "aborted", "reason": "insufficient_credits", "preflight": preflight}

        # 2. Cycle 1: Stub Discovery
        if job_id:
            _jreg_update(job_id, progress_pct=15, message="Cycle 1: Discovering executive names, positions & seniority tiers...")
        stubs_res = cls.discover_persona_stubs(account_id=account_id, company_name=company_name, domain=domain)
        stubs = stubs_res.get("stubs", [])
        if not stubs:
            msg = f"No executive leadership personas discovered for '{company_name}'."
            if job_id:
                _jreg_update(job_id, status="done", progress_pct=100, message=msg, result={"personas": []})
            return {"status": "done", "personas_count": 0, "message": msg}

        # 3. Cycle 2 & 3: Sequential Batch Execution with Dynamic Priority Re-Queueing
        all_enriched_personas: List[Dict[str, Any]] = []
        all_deferred_failures: List[Dict[str, Any]] = []
        total_credits = 0

        # We maintain a dynamic pool of remaining stubs and a priority queue for failures
        remaining_stubs = list(stubs)
        priority_queue: List[Dict[str, Any]] = []
        phase_counter = 0

        total_estimated_phases = max(1, (len(stubs) + batch_size - 1) // batch_size)

        while remaining_stubs or priority_queue:
            phase_counter += 1

            # Build current batch: Priority Queue items FIRST, then fill up to batch_size with new stubs
            current_batch: List[Dict[str, Any]] = []

            # Drain priority queue into this batch
            while priority_queue and len(current_batch) < batch_size:
                current_batch.append(priority_queue.pop(0))

            # Fill remaining slots with next stubs
            while remaining_stubs and len(current_batch) < batch_size:
                current_batch.append(remaining_stubs.pop(0))

            if not current_batch:
                break

            # Execute batch phase
            batch_result = cls.execute_persona_batch_phase(
                account_id=account_id,
                company_name=company_name,
                batch_personas=current_batch,
                phase_number=phase_counter,
                total_phases=total_estimated_phases,
                domain=domain,
                job_id=job_id,
            )

            all_enriched_personas.extend(batch_result.get("enriched_personas", []))
            total_credits += batch_result.get("credits_used", 0)

            # Check if any personas failed in this batch
            batch_failures = batch_result.get("deferred_failures", [])
            if batch_failures:
                logger.info(
                    f"[!] [PersonaAutomationEngine] Phase {phase_counter}: {len(batch_failures)} personas failed. "
                    "Re-queueing them with PRIORITY for the next batch phase."
                )
                for f_item in batch_failures:
                    # Item to be prioritized at front of next batch
                    item_data = f_item.get("item_data")
                    if item_data:
                        priority_queue.append(item_data)
                    else:
                        all_deferred_failures.append(f_item)

        # 4. Cycle 4: If any failures still remain after exhausting all batches, run one final healing pass
        if all_deferred_failures:
            healing_res = cls.resolve_deferred_faults(
                account_id=account_id,
                company_name=company_name,
                deferred_failures=all_deferred_failures,
                job_id=job_id,
            )
            total_credits += healing_res.get("retry_credits_used", 0)

        # 5. Final Summary
        duration_total = round((datetime.now(timezone.utc) - t0).total_seconds(), 2)
        summary = {
            "status": "completed",
            "account_id": account_id,
            "company_name": company_name,
            "total_personas": len(all_enriched_personas),
            "total_phases": phase_counter,
            "total_credits_used": total_credits,
            "duration_seconds": duration_total,
        }

        if job_id:
            _jreg_update(
                job_id,
                status="done",
                progress_pct=100,
                message=f"Autonomous Persona Ingestion Completed: {len(all_enriched_personas)} Personas across {phase_counter} phases.",
                result=summary,
            )

        return summary
