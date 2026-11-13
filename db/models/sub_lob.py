"""SubLob ORM Model — Full Enterprise Entity Model. Nested child divisions / Level 3 grandchildren under a LOB."""

from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from db.models.base import Base


class SubLob(Base):
    __tablename__ = "sub_lobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lob_id = Column(Integer, ForeignKey("lobs.id", ondelete="CASCADE"), nullable=False)

    # ── Sub-LOB Identity & Entity Data ──
    name = Column(String(500))
    legal_name = Column(String(500))
    lei_code = Column(String(50))
    jurisdiction = Column(String(50))
    country = Column(String(100))
    city = Column(String(255))
    relationship_type = Column(String(255))
    status = Column(String(100), default="ACTIVE")
    entity_level = Column(String(100), default="Level 3 (Operating Sub-LOB)")
    parent_lob_lei = Column(String(50))
    parent_lob_name = Column(String(500))
    domain = Column(String(255))
    website_url = Column(Text)

    # ── Manual Verification Flag & Timestamp ──
    is_manually_verified = Column(Boolean, default=False)
    manually_verified_at = Column(DateTime(timezone=True))
    local_edited_at = Column(DateTime(timezone=True))

    metadata_ = Column("metadata", JSONB)

    # ── Relationships ──
    lob = relationship("Lob", back_populates="sub_lobs")

    def __repr__(self):
        return f"<SubLob(id={self.id}, name='{self.name}', lei='{self.lei_code}')>"
