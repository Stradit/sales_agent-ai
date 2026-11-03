"""PipelineRun Repository — Handles persistence, upserts, and querying of execution runs."""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from db.models.pipeline_run import PipelineRun
from db.schemas.pipeline_run_schema import PipelineRunSchema


class PipelineRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, schema: PipelineRunSchema) -> PipelineRun:
        """Inserts or updates an execution run record by run_id."""
        existing = self.session.query(PipelineRun).filter_by(run_id=schema.run_id).first()

        data_dict = schema.model_dump(exclude_unset=True)

        if existing:
            for k, v in data_dict.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.now(timezone.utc)
            self.session.flush()
            return existing
        else:
            new_run = PipelineRun(**data_dict)
            self.session.add(new_run)
            self.session.flush()
            return new_run

    def get_by_run_id(self, run_id: str) -> Optional[PipelineRun]:
        """Retrieves a specific run by its run_id string."""
        return self.session.query(PipelineRun).filter_by(run_id=run_id).first()

    def list_recent_runs(self, limit: int = 50) -> List[PipelineRun]:
        """Returns the most recent runs sorted by started_at descending."""
        return (
            self.session.query(PipelineRun)
            .order_by(PipelineRun.started_at.desc())
            .limit(limit)
            .all()
        )

    def get_credits_summary(self) -> Dict[str, Any]:
        """Aggregates total credits used and runs count using optimized SQL queries to prevent table locks."""
        from sqlalchemy import func
        total_runs, total_credits = self.session.query(
            func.count(PipelineRun.id),
            func.coalesce(func.sum(PipelineRun.total_credits_used), 0)
        ).first()

        # Query recent runs for active vendor breakdown instead of full table scan
        recent_runs = (
            self.session.query(PipelineRun.credits_breakdown)
            .filter(PipelineRun.credits_breakdown.isnot(None))
            .order_by(PipelineRun.started_at.desc())
            .limit(100)
            .all()
        )
        vendor_totals: Dict[str, int] = {}
        for (breakdown,) in recent_runs:
            if not isinstance(breakdown, dict):
                continue
            vendors = breakdown.get("vendors", {})
            for v_name, v_info in vendors.items():
                if isinstance(v_info, dict):
                    vendor_totals[v_name] = vendor_totals.get(v_name, 0) + (v_info.get("credits_consumed", 0) or 0)

        return {
            "total_runs_executed": total_runs or 0,
            "total_lifetime_credits_consumed": int(total_credits or 0),
            "vendor_breakdown": vendor_totals
        }
