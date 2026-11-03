"""UserClientAccess ORM Model — Multi-tenant user access grants per client (public schema).

Bridges the platform-wide users table with client organizations.
A user can belong to one or more clients with specific roles:
- client_admin: Manages accounts and users inside their assigned client
- analyst: Normal read/write/pipeline operations
- viewer: Read-only access within the client
"""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from db.models.base import Base

CLIENT_ROLES = ["client_admin", "analyst", "viewer"]


class UserClientAccess(Base):
    __tablename__ = "user_client_access"
    __table_args__ = (
        UniqueConstraint("user_id", "client_id", name="uq_user_client_access"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(50), nullable=False, default="analyst")  # client_admin | analyst | viewer
    granted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    granted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    user = relationship("User", foreign_keys=[user_id], back_populates="client_access")
    client = relationship("Client", foreign_keys=[client_id], back_populates="user_access")
    granted_by = relationship("User", foreign_keys=[granted_by_id])

    def __repr__(self):
        return f"<UserClientAccess(user_id={self.user_id}, client_id={self.client_id}, role='{self.role}')>"
