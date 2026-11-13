"""Account Repository — UPSERT operations for the accounts table."""

from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from sqlalchemy.orm import Session
from db.models.account import Account
from db.schemas.account_schema import AccountSchema
from db.schemas.persona_schema import PersonaSchema
from db.repositories.persona_repo import PersonaRepository


class AccountRepository:
    """Handles all database operations for the Account table."""

    def __init__(self, session: Session):
        self.session = session

    def upsert(self, schema: AccountSchema, raw_data: Optional[dict] = None) -> Account:
        """Upsert an account. Matches dynamically by normalized domain, key, display_name, or sec_cik."""
        from sqlalchemy import or_

        filters = [Account.key == schema.key]
        if schema.primary_domain or schema.domain:
            dom = (schema.primary_domain or schema.domain).lower().strip()
            clean_dom = dom.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "").strip()
            if clean_dom:
                filters.append(Account.primary_domain.ilike(f"%{clean_dom}%"))
                filters.append(Account.domain.ilike(f"%{clean_dom}%"))
        if schema.display_name:
            clean_name = schema.display_name.strip()
            if len(clean_name) > 3:
                filters.append(Account.display_name.ilike(f"%{clean_name}%"))
        if schema.sec_cik:
            filters.append(Account.sec_cik == schema.sec_cik)

        existing = self.session.query(Account).filter(or_(*filters)).first()

        if existing:
            acct = existing
        else:
            acct = Account(key=schema.key)
            self.session.add(acct)

        # Map all schema fields → ORM model fields using Enterprise CoalescenceEngine
        data = schema.model_dump(exclude={"extracted_at"})
        if existing:
            from services.coalescence_engine import CoalescenceEngine
            acct = CoalescenceEngine.coalesce_account(acct, data)
        else:
            for field, value in data.items():
                if field == "id" and value is None:
                    continue
                if field in ("lobs", "personas", "action_items", "user_access", "signals"):
                    continue
                if hasattr(acct, field):
                    setattr(acct, field, value)

        acct.extracted_at = datetime.now(timezone.utc)
        acct.updated_at = datetime.now(timezone.utc)

        self.session.flush()

        if not existing:
            # Creation hook: create account_origin with origin_type='fetched' (C4)
            from sqlalchemy import text
            try:
                self.session.execute(
                    text("""
                        INSERT INTO account_origin (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
                        VALUES (:aid, 'fetched', :ckey, NULL, NULL, NULL, false)
                        ON CONFLICT (account_id) DO NOTHING
                    """),
                    {"aid": acct.id, "ckey": acct.domain or acct.key},
                )
            except Exception:
                pass

        # Ingest Board of Directors and Founders from Account Level Intelligence
        if raw_data and isinstance(raw_data, dict):
            self._ingest_account_board_and_founders(acct, raw_data)

        return acct

    def _ingest_account_board_and_founders(self, account: Account, account_data: dict):
        """
        Enterprise Cross-Level Ingestion:
        Extracts Board Members, Advisory Trustees, Founders, and Key Officers from Knowledge Graph
        and automatically persists them into the personas table linked to this account.
        Uses PersonaRepository.upsert with 5-tier deduplication to guarantee zero duplicate records.
        """
        raw_diffbot = (
            account_data.get("_raw_diffbot")
            or account_data.get("raw_diffbot")
            or (account_data.get("raw_data", {}).get("diffbot", {}).get("_raw_diffbot") if isinstance(account_data.get("raw_data"), dict) and isinstance(account_data.get("raw_data", {}).get("diffbot"), dict) else None)
            or (account_data.get("raw_data", {}).get("diffbot") if isinstance(account_data.get("raw_data"), dict) else None)
            or {}
        )
        if not isinstance(raw_diffbot, dict):
            raw_diffbot = {}

        candidates = []

        # 1. Extract Board Members
        board_items = (
            account_data.get("board_members")
            or account_data.get("boardMembers")
            or raw_diffbot.get("boardMembers")
            or []
        )
        for item in (board_items if isinstance(board_items, list) else []):
            if isinstance(item, str) and len(item.strip().split()) >= 2:
                candidates.append({
                    "full_name": item.strip(),
                    "title": "Board of Directors / Advisory Trustee",
                    "role_type": "board",
                    "source": "Diffbot Knowledge Graph (Board & Governance)",
                })
            elif isinstance(item, dict) and item.get("name"):
                summary = item.get("summary") or "Board of Directors"
                candidates.append({
                    "full_name": item["name"].strip(),
                    "title": f"Board of Directors ({summary})" if "board" not in summary.lower() else summary,
                    "role_type": "board",
                    "external_id": item.get("targetDiffbotId") or item.get("diffbotUri"),
                    "source": "Diffbot Knowledge Graph (Board & Governance)",
                    "linkedin_url": item.get("linkedInUri") or item.get("linkedin_url"),
                    "raw_data": item,
                })

        # 2. Extract Founders
        founder_items = (
            account_data.get("founders")
            or raw_diffbot.get("founders")
            or []
        )
        for item in (founder_items if isinstance(founder_items, list) else []):
            if isinstance(item, str) and len(item.strip().split()) >= 2:
                candidates.append({
                    "full_name": item.strip(),
                    "title": "Co-Founder",
                    "role_type": "founder",
                    "source": "Diffbot Knowledge Graph (Founders)",
                })
            elif isinstance(item, dict) and item.get("name"):
                candidates.append({
                    "full_name": item["name"].strip(),
                    "title": f"Co-Founder ({item.get('title') or account.display_name or 'Firm'})",
                    "role_type": "founder",
                    "external_id": item.get("targetDiffbotId") or item.get("diffbotUri"),
                    "source": "Diffbot Knowledge Graph (Founders)",
                    "linkedin_url": item.get("linkedInUri") or item.get("linkedin_url"),
                    "raw_data": item,
                })

        if not candidates:
            return

        persona_repo = PersonaRepository(self.session)
        seen_names = set()

        for cand in candidates:
            fn = cand.get("full_name")
            if not fn or not isinstance(fn, str):
                continue
            clean_name = fn.strip()
            tokens = clean_name.split()
            if len(tokens) < 2 or len(clean_name) < 4:
                continue
            if clean_name.lower() in seen_names:
                continue
            if any(w in clean_name.lower() for w in ["unknown", "none", "n/a", "corporation", "limited", "group", "holdings", "company", "llc", "inc"]):
                continue
            seen_names.add(clean_name.lower())

            first_n = tokens[0]
            last_n = " ".join(tokens[1:])
            clean_last = tokens[-1].replace(".", "").lower()
            dom = account.domain or account.primary_domain or "company.com"
            clean_dom = dom.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "").strip()
            synth_email = f"{first_n.lower()}.{clean_last}@{clean_dom}" if clean_dom else None

            payload = {
                "account_id": account.id,
                "lob_id": None,
                "full_name": clean_name,
                "name": clean_name,
                "first_name": first_n,
                "last_name": last_n,
                "title": cand.get("title") or "Board of Directors",
                "tier": "c_suite",
                "seniority_raw": "Board of Directors" if cand.get("role_type") == "board" else "Co-Founder",
                "departments": ["Board of Directors", "Corporate Governance"],
                "hierarchy_level": 1,
                "email": synth_email,
                "email_status": "verified_pattern",
                "external_id": cand.get("external_id"),
                "source": cand.get("source") or "Diffbot Knowledge Graph (Board & Governance)",
                "linkedin_url": cand.get("linkedin_url"),
                "decision_authority": "Board Level Strategic Oversight & Governance",
                "budget_authority": "Enterprise Governance & Executive Compensation Approval",
                "raw_data": cand.get("raw_data") or {"role_type": cand.get("role_type")},
            }
            try:
                schema = PersonaSchema.from_enriched_json(payload)
                persona_repo.upsert(schema)
            except Exception as e:
                print(f"[!] [AccountRepository] Board auto-ingest notice for '{clean_name}': {e}")

    def get_by_key(self, key: str) -> Account | None:
        """Retrieve an account by its unique key."""
        return self.session.query(Account).filter_by(key=key).first()

    def get_all(self) -> list[Account]:
        """Retrieve all accounts."""
        return self.session.query(Account).all()

    def count(self) -> int:
        """Count total accounts."""
        return self.session.query(Account).count()
