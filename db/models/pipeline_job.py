"""PipelineJob Model — Persistent, cross-restart background job state."""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from db.models.base import Base


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    job_id = Column(String(64), primary_key=True)
    client_slug = Column(String(64), nullable=False, index=True)
    account_id = Column(Integer, nullable=True)
    job_type = Column(String(64), nullable=False)
    status = Column(String(32), default="queued", nullable=False)
    progress_pct = Column(Integer, default=0, nullable=False)
    message = Column(Text, nullable=True)
    credits_reserved = Column(Integer, default=0, nullable=False)
    credits_used = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)
    meta = Column(JSONB, default=dict, nullable=False)
    result = Column(JSONB, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_pipeline_jobs_client_status", "client_slug", "status"),
        Index("ix_pipeline_jobs_created_at", "created_at"),
    )

    def __repr__(self):
        return f"<PipelineJob(job_id='{self.job_id}', client_slug='{self.client_slug}', status='{self.status}')>"
