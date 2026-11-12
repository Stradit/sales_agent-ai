"""Client ORM Model — Multi-tenant client registry for the platform layer (public schema).

Each client has a unique slug that determines its dedicated PostgreSQL schema:
schema_name = f"client_{slug}" (e.g. "client_stradit_india").
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from db.models.base import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(500), nullable=False)                         # e.g. "Stradit India PVT LTD"
    slug = Column(String(100), unique=True, nullable=False, index=True) # e.g. "stradit_india"
    legal_name = Column(String(500))                                   # e.g. "Stradit India Private Limited"
    schema_name = Column(String(120), unique=True, nullable=False)      # e.g. "client_stradit_india"
    plan_tier = Column(String(50), default="standard", nullable=False)  # "standard" | "enterprise"
    is_active = Column(Boolean, default=True, nullable=False)
    max_accounts = Column(Integer, default=20, nullable=False)
    
    # Client lifecycle state machine (§4.3)
    # Allowed states: "pending", "provisioning", "active", "suspended", "archived", "failed", "deleted"
    status = Column(String(50), default="active", nullable=False)
    skeleton_version = Column(String(32), default="T0001", nullable=False)
    provisioned_at = Column(DateTime(timezone=True), nullable=True)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    refresh_cron = Column(String(64), default="0 0 * * *", nullable=False)
    max_concurrent_jobs = Column(Integer, default=3, nullable=False)
    last_error = Column(Text, nullable=True)

    # API credits tracking per client
    api_credits_total = Column(Integer, default=0, nullable=False)
    api_credits_used = Column(Integer, default=0, nullable=False)

    # Verified Intelligence Catalog Settings (§6, Phase 1)
    catalog_publish = Column(Boolean, default=False, nullable=False)
    catalog_consume = Column(Boolean, default=False, nullable=False)
    reuse_requires_approval = Column(Boolean, default=True, nullable=False)

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                         onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    created_by = relationship("User", foreign_keys=[created_by_id])
    user_access = relationship("UserClientAccess", back_populates="client", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Client(id={self.id}, name='{self.name}', slug='{self.slug}', schema='{self.schema_name}')>"
