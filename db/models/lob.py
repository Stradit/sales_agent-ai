"""Lob ORM Model — 17 columns. Lines of Business / Sub-Organizations with scraping URLs."""

from sqlalchemy import Column, Integer, String, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from db.models.base import Base


class Lob(Base):
    __tablename__ = "lobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)

    # ── LOB Identity ──
    key = Column(String(255))
    lob_name = Column(String(500))
    domain = Column(String(255))
    website_url = Column(Text)
    crunchbase_url = Column(Text)
    relationship_type = Column(String(255))
    overview = Column(Text)
    audited_segment_revenue = Column(String(100))
    operating_head = Column(String(500))
    segment_headcount = Column(String(100))
    lei_code = Column(String(50))
    jurisdiction = Column(String(50))
    technologies = Column(JSONB)
    competitors = Column(JSONB)
    logo_url = Column(Text)
    financial_snippets = Column(JSONB)
    wikipedia_url = Column(Text)
    patents = Column(JSONB)
    raw_data = Column(JSONB)
    osint_feed_manifest = Column(JSONB)

    # ── Manual Verification Flag ──
    is_manually_verified = Column(Boolean, default=False)
    manually_verified_at = Column(DateTime(timezone=True))
    local_edited_at = Column(DateTime(timezone=True))

    # ── LOB Scraping URLs ──
    google_news_rss_url = Column(Text)
    reddit_rss_url = Column(Text)
    google_patents_url = Column(Text)
    google_trends_url = Column(Text)
    youtube_search_url = Column(Text)

    # ── Relationships ──
    account = relationship("Account", back_populates="lobs")
    sub_lobs = relationship("SubLob", back_populates="lob", cascade="all, delete-orphan")
    personas = relationship("Persona", back_populates="lob")

    def __repr__(self):
        return f"<Lob(id={self.id}, lob_name='{self.lob_name}')>"
