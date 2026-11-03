"""PipelineRun ORM Model — Audit table for execution telemetry, credits, and run logs."""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Numeric, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON
from db.models.base import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id = Column(String(100), unique=True, nullable=False, index=True)
    company_name = Column(String(255), nullable=False, index=True)
    target_url = Column(String(500))
    status = Column(String(50), nullable=False, default="staged", index=True)

    quality_score = Column(Numeric(5, 2), default=0.0)
    quality_grade = Column(String(10), default="N/A")

    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Numeric(8, 2), default=0.0)

    total_credits_used = Column(Integer, nullable=False, default=0)
    credits_breakdown = Column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict)
    entities_extracted = Column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict)

    raw_storage_dir = Column(String(500))
    enriched_storage_dir = Column(String(500))
    execution_logs = Column(JSONB().with_variant(JSON, "sqlite"), nullable=False, default=list)
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<PipelineRun(run_id='{self.run_id}', company='{self.company_name}', status='{self.status}', credits={self.total_credits_used})>"
