"""Account ORM Model — 89 columns. Core company identity, firmographics, financials, scraping URLs."""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, BigInteger, String, Text, Float, Date, DateTime, ARRAY, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from db.models.base import Base


class Account(Base):
    __tablename__ = "accounts"

    # ── Primary Key ──
    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ──
    key = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(500))
    legal_name = Column(String(500))
    domain = Column(String(255))
    primary_domain = Column(String(255))
    website_url = Column(Text)
    crunchbase_url = Column(Text)
    operating_status = Column(String(50))
    company_type = Column(String(50))

    # ── Firmographics ──
    founded_date = Column(Date)
    founded_year = Column(Integer)
    employee_count_range = Column(String(50))
    short_description = Column(Text)
    full_description = Column(Text)

    # ── Location ──
    headquarters_location = Column(String(500))
    city = Column(String(255))
    state = Column(String(255))
    country = Column(String(255))
    postal_code = Column(String(50))

    # ── Contact & Social ──
    phone_number = Column(String(50))
    sanitized_phone = Column(String(50))
    contact_email = Column(String(255))
    linkedin_url = Column(Text)
    twitter_url = Column(Text)
    twitter_handle = Column(String(100))
    facebook_url = Column(Text)

    # ── Financials & Funding ──
    estimated_revenue_range = Column(String(100))
    total_funding_amount = Column(BigInteger)
    total_funding_amount_usd = Column(BigInteger)
    total_funding_currency = Column(String(10))
    last_funding_type = Column(String(100))
    last_funding_date = Column(Date)
    num_funding_rounds = Column(Integer)
    funding_status = Column(String(100))

    # ── Market & IPO ──
    stock_symbol = Column(String(20))
    stock_exchange = Column(String(20))
    sec_cik = Column(String(20))
    sec_name = Column(String(500))
    ipo_status = Column(String(50))
    ipo_date = Column(Date)

    # ── Acquisitions & Sub-orgs ──
    num_suborganizations = Column(Integer, default=0)
    num_acquisitions = Column(Integer)

    # ── Web Traffic & Growth ──
    global_traffic_rank = Column(Integer)
    monthly_visits = Column(BigInteger)
    bounce_rate = Column(Float)
    visit_duration = Column(Float)
    page_views_per_visit = Column(Float)
    heat_score = Column(Integer)
    trend_score_90d = Column(Float)

    # ── Tech & Patents ──
    active_tech_count = Column(Integer)
    it_spend = Column(String(100))
    patents_granted = Column(Integer)
    trademarks_registered = Column(Integer)
    total_apps = Column(Integer)
    total_downloads = Column(BigInteger)

    # ── Key People ──
    num_founders = Column(Integer, default=0)
    num_contacts = Column(Integer)

    # ── Timestamps ──
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # ── Manual Verification Flag ──
    is_manually_verified = Column(Boolean, default=False)
    manually_verified_at = Column(DateTime(timezone=True))

    # ── Pipeline Run Metadata ──
    extracted_at = Column(DateTime(timezone=True))
    schema_version = Column(String(20))
    lobs_count = Column(Integer, default=0)
    total_contacts_captured = Column(Integer, default=0)
    c_suite_count = Column(Integer, default=0)
    vp_count = Column(Integer, default=0)
    director_count = Column(Integer, default=0)
    manager_count = Column(Integer, default=0)

    # ── Account Scraping URLs ──
    sec_edgar_url = Column(Text)
    sec_filings_rss = Column(Text)
    sec_submissions_url = Column(Text)
    twitter_live_url = Column(Text)
    reddit_query = Column(Text)
    reddit_rss_url = Column(Text)
    news_query = Column(Text)
    rss_url = Column(Text)
    google_patents_url = Column(Text)
    google_trends_url = Column(Text)
    youtube_search_url = Column(Text)
    openalex_institution_url = Column(Text)
    wikidata_entity_url = Column(Text)
    github_url = Column(Text)
    glassdoor_url = Column(Text)
    blog_url = Column(Text)
    youtube_channel_id = Column(String(100))

    # ── Array & JSONB Intelligence Fields ──
    industries = Column(ARRAY(Text))
    industry_groups = Column(ARRAY(Text))
    aliases = Column(ARRAY(Text))
    founders = Column(ARRAY(Text))
    headquarters_regions = Column(ARRAY(Text))
    keywords = Column(ARRAY(Text))
    multi_source_intelligence = Column(JSONB)
    organisational_hierarchy_tree = Column(JSONB)
    raw_data = Column(JSONB)
    osint_feed_manifest = Column(JSONB)

    # ── Credits & Usage ──
    api_credits_total = Column(Integer, default=0, nullable=False)
    api_credits_used = Column(Integer, default=0, nullable=False)

    # ── Relationships ──
    lobs = relationship("Lob", back_populates="account", cascade="all, delete-orphan")
    personas = relationship("Persona", back_populates="account", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Account(id={self.id}, key='{self.key}', legal_name='{self.legal_name}')>"
