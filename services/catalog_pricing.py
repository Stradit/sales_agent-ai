"""Catalog Pricing Engine — Enterprise Credit Accounting & Reuse Valuation.

Provides unified fee calculation and credit savings estimations for
cross-tenant account intelligence reuse and hydration.
"""

from typing import Any, Dict, Optional
import config
from db.models.client import Client
from db.models.platform_catalog import Snapshot
from services.credit_accounting_engine import CreditAccountingEngine


class CatalogPricing:
    """Enterprise rate card and fee calculation engine for the platform catalog."""

    @classmethod
    def fee_for(
        cls,
        snapshot: Optional[Snapshot] = None,
        client: Optional[Client] = None,
        mode: str = "instant",
        is_super_admin: bool = False,
    ) -> int:
        """Determines the exact credit fee for a catalog hydration/reuse operation.

        Rules:
        - Super-admin direct assignment (mode='assign' or is_super_admin=True) -> 0 credits.
        - Refresh of an already-paid snapshot version (mode='refresh') -> 0 credits.
        - Standard tenant hydrate -> config.CATALOG_HYDRATE_FEE (default 1 credit).
        """
        if is_super_admin or mode == "assign":
            return 0

        if mode == "refresh":
            return 0

        # Read configurable fee from config, defaulting to 1 credit
        return getattr(config, "CATALOG_HYDRATE_FEE", 1)

    @classmethod
    def estimated_credits_saved(cls, snapshot: Snapshot) -> int:
        """Calculates total estimated API credits saved by reusing verified intelligence.

        Uses vendor unit costs from CreditAccountingEngine.RATE_CARD:
        - Baseline firmographic enrichment (Diffbot + Serper + Perigon baseline = 25)
        - LOB & division mapping (2 credits per LOB)
        - Verified leadership personas (5 credits per persona)
        - Recent signals & jobs (1 credit per item)
        """
        counts = snapshot.counts or {}
        lobs_cnt = counts.get("lobs", 0)
        personas_cnt = counts.get("personas", 0)
        posts_cnt = counts.get("posts", 0)
        jobs_cnt = counts.get("jobs", 0)

        # Baseline crawl savings
        base_enrichment = 25

        # Itemized savings based on rate card vendor units
        lob_savings = lobs_cnt * 2
        persona_savings = personas_cnt * 5
        signals_savings = (posts_cnt + jobs_cnt) * 1

        return base_enrichment + lob_savings + persona_savings + signals_savings
