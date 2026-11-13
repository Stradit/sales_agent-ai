"""Persona ORM Model — 58 columns. Executives with scraping URLs, AI dossier, skills, KPIs."""

from sqlalchemy import Column, Integer, String, Text, ForeignKey, ARRAY, Boolean, Float, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from db.models.base import Base


class Persona(Base):
    __tablename__ = "personas"

    # ── Primary Key & Foreign Keys ──
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    lob_id = Column(Integer, ForeignKey("lobs.id", ondelete="SET NULL"), nullable=True)

    # ── Person Identity ──
    external_id = Column(String(255))
    key = Column(String(255))
    display_name = Column(String(500))
    full_name = Column(String(500))
    first_name = Column(String(255))
    last_name = Column(String(255))
    title = Column(String(500))
    tier = Column(String(100))
    seniority_raw = Column(String(100))
    departments = Column(ARRAY(Text))
    email = Column(String(255))
    email_status = Column(String(100))
    phone = Column(String(100))
    linkedin_url = Column(Text)
    crunchbase_permalink = Column(String(255))
    city = Column(String(255))
    state = Column(String(255))
    country = Column(String(255))
    source = Column(String(255))
    hierarchy_level = Column(Integer)
    decision_authority = Column(String(100))
    budget_authority = Column(String(100))
    raw_data = Column(JSONB)
    osint_feed_manifest = Column(JSONB)

    # ── Verified Executive Intelligence & OSINT URLs ──
    corporate_bio_url = Column(Text)
    crunchbase_url = Column(Text)
    sec_cik = Column(String(20))
    sec_insider_trades_url = Column(Text)
    fec_contributions_url = Column(Text)
    quiver_insider_url = Column(Text)
    bloomberg_url = Column(Text)
    wsj_article_url = Column(Text)
    media_interview_url = Column(Text)
    annual_report_url = Column(Text)
    zoominfo_url = Column(Text)
    rss_url = Column(Text)
    youtube_url = Column(Text)
    podcast_url = Column(Text)
    openinsider_url = Column(Text)
    secform4_url = Column(Text)
    wayback_url = Column(Text)
    theorg_url = Column(Text)
    seeking_alpha_url = Column(Text)
    external_board_url = Column(Text)
    twitter_handle = Column(String(100))
    twitter_live_url = Column(Text)
    google_patents_url = Column(Text)
    google_scholar_url = Column(Text)
    openalex_author_url = Column(Text)
    orcid_search_url = Column(Text)
    wikidata_person_url = Column(Text)
    reddit_rss_url = Column(Text)
    google_trends_url = Column(Text)
    youtube_interviews_url = Column(Text)
    podcast_search_url = Column(Text)
    reddit_query = Column(Text)
    news_query = Column(Text)
    patents_query = Column(Text)
    youtube_channel_id = Column(String(255))

    # ── Persona Dossier ──
    degree = Column(String(500))
    institution = Column(String(500))
    prior_company = Column(String(500))
    communication_style = Column(Text)
    engagement_rate = Column(String(50))
    value_proposition = Column(Text)
    personalized_icebreaker = Column(Text)
    social_platform = Column(String(50))
    social_profile_url = Column(Text)
    social_presence_level = Column(String(100))

    # ── Array Fields ──
    skills = Column(ARRAY(Text))
    target_kpis = Column(ARRAY(Text))
    operational_pain_points = Column(ARRAY(Text))
    key_objections = Column(ARRAY(Text))

    # ── Career Employment History & Personal Intelligence (FullEnrich) ──
    headline = Column(String(500))
    employment_history = Column(JSONB)
    past_companies = Column(ARRAY(Text))
    previous_titles = Column(ARRAY(Text))
    current_role_tenure_months = Column(Integer)
    is_new_in_role = Column(Boolean, default=False)
    career_trajectory_score = Column(Float)
    education_history = Column(JSONB)
    personal_email = Column(String(255))
    direct_mobile_phone = Column(String(100))

    # ── Manual Verification Flag ──
    is_manually_verified = Column(Boolean, default=False)
    manually_verified_at = Column(DateTime(timezone=True))
    local_edited_at = Column(DateTime(timezone=True))

    # ── Extended Profile — enrichment data with no dedicated column ──
    # Stores: legal_name, middle_name, age, political_affiliation, home_address,
    # lob_mapped_name, data_corrections, additional_urls, enrichment_meta
    extended_profile = Column(JSONB)


    # ── Relationships ──
    account = relationship("Account", back_populates="personas")
    lob = relationship("Lob", back_populates="personas")

    def __repr__(self):
        return f"<Persona(id={self.id}, full_name='{self.full_name}', tier='{self.tier}')>"
