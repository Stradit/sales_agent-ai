"""Platform Catalog ORM Models — Verified Account Intelligence Sharing Architecture.

Houses models for the platform-only platform_catalog schema:
- Company (canonical cross-tenant enterprise entity)
- CompanyIdentifier (global identifiers: cik, lei, domain)
- Snapshot (versioned, sanitized intelligence metadata)
- SnapshotBlob (immutable sanitized intelligence payload)
- ShareEvent (audit log, credit deduction, and exact rollback manifest)

And the tenant-level provenance model:
- AccountOrigin (records whether an account was freshly fetched or hydrated from catalog)
"""

from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from db.models.base import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = {"schema": "platform_catalog"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    canonical_key = Column(String(255), unique=True, nullable=False, index=True)
    legal_name = Column(String(500), nullable=True)
    display_name = Column(String(500), nullable=True)
    country = Column(String(100), nullable=True)
    merged_into_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    needs_review = Column(Boolean, default=False, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    identifiers = relationship("CompanyIdentifier", back_populates="company", cascade="all, delete-orphan")
    snapshots = relationship("Snapshot", back_populates="company", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Company(id={self.id}, canonical_key='{self.canonical_key}', name='{self.display_name}')>"


class CompanyIdentifier(Base):
    __tablename__ = "company_identifiers"
    __table_args__ = (
        UniqueConstraint("id_type", "id_value", name="uq_company_id_type_val"),
        {"schema": "platform_catalog"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    company_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    id_type = Column(String(32), nullable=False)  # cik | lei | domain
    id_value = Column(String(255), nullable=False, index=True)

    company = relationship("Company", back_populates="identifiers")

    def __repr__(self):
        return f"<CompanyIdentifier({self.id_type}='{self.id_value}', company_id={self.company_id})>"


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("company_id", "version", name="uq_snapshot_company_version"),
        {"schema": "platform_catalog"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    company_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    status = Column(String(32), default="ready", nullable=False)  # building | ready | failed | quarantined | superseded
    source_client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_account_id = Column(Integer, nullable=True)
    policy_version = Column(String(32), default="1.0.0", nullable=False)
    skeleton_version = Column(String(32), default="T0002", nullable=False)
    quality_score = Column(Numeric(5, 2), nullable=True)
    quality_grade = Column(String(16), nullable=True)
    source_extracted_at = Column(DateTime(timezone=True), nullable=True)
    taken_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    counts = Column(JSONB, default=dict, nullable=False)
    checksum = Column(String(128), nullable=False)
    checksum_version = Column(Integer, default=1, nullable=False)
    policy_hash = Column(String(64), nullable=True)

    company = relationship("Company", back_populates="snapshots")
    blob = relationship("SnapshotBlob", back_populates="snapshot", uselist=False, cascade="all, delete-orphan")
    share_events = relationship("ShareEvent", back_populates="snapshot", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Snapshot(id={self.id}, company_id={self.company_id}, v={self.version}, status='{self.status}')>"


class SnapshotBlob(Base):
    __tablename__ = "snapshot_blobs"
    __table_args__ = {"schema": "platform_catalog"}

    snapshot_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    payload = Column(JSONB, nullable=False)

    snapshot = relationship("Snapshot", back_populates="blob")

    def __repr__(self):
        return f"<SnapshotBlob(snapshot_id={self.snapshot_id})>"


class ShareEvent(Base):
    __tablename__ = "share_events"
    __table_args__ = {"schema": "platform_catalog"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mode = Column(String(32), nullable=False)  # push | request | refresh_pull
    requested_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason = Column(Text, nullable=True)
    status = Column(String(32), default="previewed", nullable=False)  # previewed | pending_approval | queued | running | done | failed | rolled_back
    idempotency_key = Column(String(128), unique=True, nullable=False, index=True)
    fee_credits = Column(Integer, default=0, nullable=False)
    created_ids = Column(JSONB, nullable=True)
    counts = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rollback_reason = Column(Text, nullable=True)

    snapshot = relationship("Snapshot", back_populates="share_events")

    def __repr__(self):
        return f"<ShareEvent(id={self.id}, target_client_id={self.target_client_id}, status='{self.status}')>"


class MergeCandidate(Base):
    __tablename__ = "merge_candidates"
    __table_args__ = {"schema": "platform_catalog"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    company_a_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_b_id = Column(
        BigInteger,
        ForeignKey("platform_catalog.companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason = Column(String(255), nullable=False)
    evidence = Column(JSONB, default=dict, nullable=False)
    status = Column(String(32), default="pending", nullable=False)  # pending | merged | dismissed
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<MergeCandidate(id={self.id}, a={self.company_a_id}, b={self.company_b_id}, status='{self.status}')>"


class AccountOrigin(Base):
    """Tenant-level provenance tracking table (present in both public template and tenant schemas)."""
    __tablename__ = "account_origin"

    account_id = Column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    origin_type = Column(String(32), default="fetched", nullable=False)  # fetched | catalog
    canonical_key = Column(String(255), nullable=True, index=True)
    snapshot_id = Column(BigInteger, nullable=True, index=True)
    snapshot_version = Column(Integer, nullable=True)
    hydrated_at = Column(DateTime(timezone=True), nullable=True)
    hydrated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    local_edits = Column(Boolean, default=False, nullable=False)
    source_extracted_at = Column(DateTime(timezone=True), nullable=True)
    snapshot_taken_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<AccountOrigin(account_id={self.account_id}, origin='{self.origin_type}', key='{self.canonical_key}')>"
