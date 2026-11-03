"""User ORM Model — Login accounts, roles, and lockout state.

Two roles for now: 'super_admin' (manages user accounts) and 'user' (normal
dashboard access). Kept as a plain string column, not a Postgres enum, so a
third tier can be added later without a schema migration — same convention
already used for personas.tier / personas.decision_authority.
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from db.models.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    role = Column(String(50), nullable=False, default="user")  # 'super_admin' | 'user'
    is_active = Column(Boolean, nullable=False, default=True)
    # Granular dashboard & feature access flags managed by super_admin
    has_dashboard_access = Column(Boolean, nullable=False, default=True)
    has_command_center_access = Column(Boolean, nullable=False, default=True)
    has_tasks_access = Column(Boolean, nullable=False, default=True)
    has_pipeline_access = Column(Boolean, nullable=False, default=True)

    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True))

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = relationship("User", remote_side=[id])
    client_access = relationship("UserClientAccess", primaryjoin="User.id == UserClientAccess.user_id", back_populates="user", cascade="all, delete-orphan")

    last_login_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<User(id={self.id}, email='{self.email}', role='{self.role}')>"
