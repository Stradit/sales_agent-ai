import datetime
import os
import re
import json
import time
import urllib.parse
from urllib.parse import urlparse
from pathlib import Path
from typing import Dict, Any, Optional, List
from apify_client import ApifyClient
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from collectors.account_collector import (
    fetch_latest_10k_chunks,
    extract_full_patents,
    fetch_sec_exhibit_21_subsidiaries,
    fetch_gleif_ownership_tree,
)


class AccountServiceHTTPClient:
    """Enterprise HTTP Client with connection pooling, exponential retries, and safe timeouts."""

    _session: Optional[requests.Session] = None

    @classmethod
    def get_session(cls) -> requests.Session:
        if cls._session is None:
            session = requests.Session()
            retries = Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST"],
            )
            adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            cls._session = session
        return cls._session


class AccountInputResolver:
    """Pre-flight parameter normalization and identity resolution engine.

    Resolves:
      1. Root domain normalization (strips http://, www., path suffixes, query params)
      2. Clean company name (strips corporate suffixes: Inc, LLC, Corp, Ltd, Co, LP, PLC, etc.)
      3. Alias expansion (produces list of variations for multi-strategy search)
      4. SEC CIK and Ticker lookup from in-memory cached SEC index
      5. Generates optimized query strings per connector
    """
    _sec_title_map: Optional[Dict[str, Dict[str, str]]] = None

    @staticmethod
    def normalize_domain(url_or_domain: Optional[str]) -> Optional[str]:
        if not url_or_domain:
            return None
        raw = str(url_or_domain).strip().lower()
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        try:
            parsed = urlparse(raw)
            domain = parsed.netloc or parsed.path
            domain = re.sub(r"^www\.", "", domain)
            domain = domain.split(":")[0]
            domain = domain.split("/")[0]
            return domain if "." in domain else None
        except Exception:
            return None

    @staticmethod
    def clean_company_name(name: str) -> str:
        if not name:
            return ""
        cleaned = str(name).strip()
        cleaned = re.sub(r"\s*\([^)]*\)", "", cleaned)
        # Strip corporate suffixes (including & Co., & Company, etc.)
        pattern = (
            r"(?i)\s*(?:&|and)?\s*\b(inc\.?|incorporated|llc|l\.l\.c\.?|corp\.?|"
            r"corporation|co\.?|company|ltd\.?|limited|l\.p\.?|lp|plc|n\.a\.?|sa|ag|gmbh|nv|bv)\b.*$"
        )
        cleaned = re.sub(pattern, "", cleaned).strip()
        # Strip trailing punctuation or dangling ampersands
        cleaned = re.sub(r"[,.\-_\s&]+$", "", cleaned).strip()
        return cleaned or name.strip()

    @staticmethod
    def strip_leading_articles(name: str) -> str:
        """Strips leading 'The ' from company names for normalization."""
        return re.sub(r"(?i)^the\s+", "", name).strip()

    @classmethod
    def load_sec_tickers_index(cls) -> Dict[str, Dict[str, str]]:
        """Loads and indexes SEC company_tickers.json into memory (cached)."""
        if cls._sec_title_map is not None:
            return cls._sec_title_map

        sec_map: Dict[str, Dict[str, str]] = {}
        try:
            session = AccountServiceHTTPClient.get_session()
            headers = {"User-Agent": "SalesIntelBot admin@salesintel.io"}
            res = session.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                for _, item in data.items():
                    title = str(item.get("title", "")).strip().lower()
                    ticker = str(item.get("ticker", "")).strip().upper()
                    cik = str(item.get("cik_str", "")).zfill(10)
                    info = {"ticker": ticker, "cik": cik, "title": item.get("title", "")}
                    if title:
                        sec_map[title] = info
                        # Also index without leading 'The'
                        no_the = cls.strip_leading_articles(title).lower()
                        if no_the and no_the not in sec_map:
                            sec_map[no_the] = info
                        # Also index clean version
                        clean_t = cls.clean_company_name(title).lower()
                        if clean_t and clean_t not in sec_map:
                            sec_map[clean_t] = info
                        clean_no_the = cls.clean_company_name(no_the).lower()
                        if clean_no_the and clean_no_the not in sec_map:
                            sec_map[clean_no_the] = info
                    if ticker:
                        sec_map[ticker.lower()] = info
        except Exception as e:
            print(f"[!] [AccountInputResolver] SEC tickers index notice: {e}")

        cls._sec_title_map = sec_map
        return sec_map

    @classmethod
    def resolve(
        cls,
        company_name: str,
        domain: Optional[str] = None,
        ticker: Optional[str] = None,
        sec_cik: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Performs pre-flight parameter resolution across all input dimensions."""
        clean_name = cls.clean_company_name(company_name)
        no_the_name = cls.strip_leading_articles(company_name)
        clean_no_the = cls.strip_leading_articles(clean_name)
        root_domain = cls.normalize_domain(domain)

        aliases = [company_name.strip()]
        for candidate in [clean_name, no_the_name, clean_no_the]:
            if candidate and candidate.lower() not in [a.lower() for a in aliases]:
                aliases.append(candidate)
        if root_domain:
            domain_name = root_domain.split(".")[0].capitalize()
            if domain_name.lower() not in [a.lower() for a in aliases]:
                aliases.append(domain_name)

        resolved_ticker = ticker.upper() if ticker else None
        resolved_cik = str(sec_cik).zfill(10) if sec_cik else None

        if not resolved_ticker or not resolved_cik:
            sec_index = cls.load_sec_tickers_index()
            # Try multiple matching strategies in priority order
            search_keys = [
                company_name.lower().strip(),
                no_the_name.lower().strip(),
                clean_name.lower().strip(),
                clean_no_the.lower().strip(),
            ]
            match = None
            for sk in search_keys:
                if sk and sk in sec_index:
                    match = sec_index[sk]
                    break

            if not match and clean_no_the:
                for k, v in sec_index.items():
                    if k.startswith(clean_no_the.lower()) or clean_no_the.lower() in k:
                        match = v
                        break

            if match:
                if not resolved_ticker:
                    resolved_ticker = match.get("ticker")
                if not resolved_cik:
                    resolved_cik = match.get("cik")

        return {
            "original_name": company_name.strip(),
            "clean_name": clean_name or company_name.strip(),
            "root_domain": root_domain,
            "ticker": resolved_ticker,
            "sec_cik": resolved_cik,
            "aliases": aliases,
            "wikipedia_title": clean_no_the or clean_name or company_name.strip(),
            "glassdoor_query": clean_no_the or clean_name or company_name.strip(),
            "courtlistener_query": company_name.strip(),
            "gleif_query": company_name.strip(),
            "openfec_query": company_name.strip(),
        }


class RawDataLakeWriter:
    """Append-only immutable raw storage writer for data governance and future replayability."""

    @staticmethod
    def save_raw(
        raw_data: Any,
        source_name: str,
        entity_name: str,
        run_raw_dir: Optional[Path] = None,
        file_ext: str = "json",
    ) -> Optional[str]:
        """Saves raw data from any source and returns the relative stored filepath."""
        if not raw_data:
            return None

        try:
            if run_raw_dir:
                target_dir = Path(run_raw_dir) / source_name.lower()
            else:
                timestamp = time.strftime("%Y-%m-%d")
                target_dir = Path(config.OUTPUT_DIR) / timestamp / "raw" / source_name.lower()

            target_dir.mkdir(parents=True, exist_ok=True)

            safe_entity = re.sub(r"[^a-z0-9]+", "_", entity_name.lower()).strip("_")
            filename = f"{safe_entity}_{source_name.lower()}_raw.{file_ext}"
            file_path = target_dir / filename

            if file_ext == "json":
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(raw_data, f, indent=2, ensure_ascii=False)
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(str(raw_data))

            return str(file_path)
        except Exception as e:
            print(f"[!] [RawDataLake] Warning: Failed to save raw file for {source_name}: {e}")
            return None


class AccountCoalesceEngine:
    """Field-Level Priority Coalescing Engine for all 94 Account Columns."""

    @staticmethod
    def clean_text(val: Any) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        return s if s and s.lower() not in ["none", "null", "n/a", "undefined"] else None

    @staticmethod
    def clean_number(val: Any) -> Optional[float]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        try:
            s = str(val).strip().lower()
            multiplier = 1.0
            if s.endswith("b") or "billion" in s:
                multiplier = 1e9
            elif s.endswith("m") or "million" in s:
                multiplier = 1e6
            elif s.endswith("k") or "thousand" in s:
                multiplier = 1e3
            cleaned = re.sub(r"[^0-9.-]", "", s)
            if cleaned:
                return float(cleaned) * multiplier
            return None
        except Exception:
            return None

    @staticmethod
    def clean_phone(val: Any) -> Optional[str]:
        if not val:
            return None
        s = str(val).strip()
        # Generic international/domestic phone sanitization
        digits = re.sub(r"[^\d+]", "", s)
        return digits if len(re.sub(r"\D", "", digits)) >= 7 else None

    @classmethod
    def _extract_nested_location(cls, sources: List[Dict[str, Any]], field_keys: List[str]) -> Optional[str]:
        """Generic dynamic resolver across top-level and nested location/address dictionaries."""
        for src in sources:
            if not isinstance(src, dict):
                continue
            # Direct top-level check
            for k in field_keys:
                val = cls.clean_text(src.get(k))
                if val:
                    return val
            # Nested in location/address dicts or arrays
            for sub_key in ["location", "address", "addresses", "hq_address", "headquarters", "primary_address"]:
                sub = src.get(sub_key)
                if isinstance(sub, dict):
                    for k in field_keys:
                        val = cls.clean_text(sub.get(k))
                        if val:
                            return val
                elif isinstance(sub, list) and sub:
                    for item in sub:
                        if isinstance(item, dict):
                            for k in field_keys:
                                val = cls.clean_text(item.get(k))
                                if val:
                                    return val
        return None

    @classmethod
    def _extract_nested_metric(cls, sources: List[Dict[str, Any]], field_keys: List[str]) -> Optional[float]:
        """Generic dynamic resolver across top-level and nested rating/culture structures."""
        for src in sources:
            if not isinstance(src, dict):
                continue
            for k in field_keys:
                val = src.get(k)
                if val is not None:
                    num = cls.clean_number(val)
                    if num is not None:
                        return num
            for sub_key in ["rating", "ratings", "reviews", "score", "overview", "culture"]:
                sub = src.get(sub_key)
                if isinstance(sub, dict):
                    for k in field_keys:
                        val = sub.get(k)
                        if val is not None:
                            num = cls.clean_number(val)
                            if num is not None:
                                return num
        return None

    @classmethod
    def _extract_technologies(cls, sources: List[Dict[str, Any]]) -> List[str]:
        """Generic dynamic technology list resolver supporting strings and structured objects."""
        techs = []
        seen = set()
        for src in sources:
            if not isinstance(src, dict):
                continue
            for k in ["technologies", "tech_stack", "technologies_used"]:
                raw_list = src.get(k)
                if isinstance(raw_list, list):
                    for item in raw_list:
                        name = None
                        if isinstance(item, str) and item.strip():
                            name = item.strip()
                        elif isinstance(item, dict):
                            name = cls.clean_text(item.get("name") or item.get("title") or item.get("slug"))
                        if name and name.lower() not in seen:
                            seen.add(name.lower())
                            techs.append(name)
        return techs

    @classmethod
    def coalesce(
        cls,
        company_name: str,
        domain: Optional[str] = None,
        sec_data: Optional[Dict[str, Any]] = None,
        gleif_data: Optional[Dict[str, Any]] = None,
        opencorporates_data: Optional[Dict[str, Any]] = None,
        fmp_data: Optional[Dict[str, Any]] = None,
        courtlistener_data: Optional[Dict[str, Any]] = None,
        finnhub_data: Optional[Dict[str, Any]] = None,
        apify_crunchbase: Optional[Dict[str, Any]] = None,
        apify_glassdoor: Optional[Dict[str, Any]] = None,
        apify_twitter: Optional[Dict[str, Any]] = None,
        diffbot_data: Optional[Dict[str, Any]] = None,
        serper_data: Optional[Dict[str, Any]] = None,
        wiki_data: Optional[Dict[str, Any]] = None,
        fec_data: Optional[Dict[str, Any]] = None,
        firecrawl_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes field-by-field priority waterfall resolving all 94 columns.
        Zero data loss: all raw structures are preserved in raw_data JSONB.
        """
        sec = sec_data or {}
        gleif = gleif_data or {}
        opencorp = opencorporates_data or {}
        fmp = fmp_data or {}
        court = courtlistener_data or {}
        finnhub = finnhub_data or {}
        cb = apify_crunchbase or {}
        gd = apify_glassdoor or {}
        tw = apify_twitter or {}
        diff = diffbot_data or {}
        serp = serper_data or {}
        wiki = wiki_data or {}
        fec = fec_data or {}
        fc = firecrawl_data or {}

        # 1. Identity & Legal
        legal_name = cls.clean_text(
            sec.get("legal_name")
            or gleif.get("legal_name")
            or opencorp.get("name")
            or diff.get("name")
            or cb.get("name")
            or company_name
        )
        display_name = cls.clean_text(
            cb.get("name") or diff.get("name") or wiki.get("title") or company_name
        )
        slug_key = re.sub(r"[^a-z0-9]+", "-", (display_name or company_name).lower()).strip("-")

        target_domain = cls.clean_text(
            domain or diff.get("domain") or cb.get("domain") or serp.get("domain")
        )
        sec_cik = cls.clean_text(sec.get("sec_cik") or fmp.get("cik") or diff.get("sec_cik"))
        company_type = cls.clean_text(
            cb.get("company_type")
            or opencorp.get("company_type")
            or ("Public" if sec_cik else "Private")
        )
        operating_status = cls.clean_text(
            cb.get("operating_status")
            or opencorp.get("current_status")
            or gleif.get("status")
            or "Active"
        )
        founded_year = cls.clean_number(
            cb.get("founded_on") or diff.get("founded_year") or wiki.get("founded_year")
        )
        if not founded_year and diff.get("description"):
            m = re.search(r"founded(?:\s+on)?(?:\s+[A-Za-z]+\s+\d{1,2},)?\s+(\d{4})", str(diff.get("description")), re.IGNORECASE)
            if m:
                founded_year = int(m.group(1))

        # 2. Financials & Funding (SEC 10-K is authoritative, FMP/CB fallback)
        revenue = cls.clean_text(
            sec.get("revenue_formatted")
            or fmp.get("revenue_formatted")
            or cb.get("revenue_range")
            or diff.get("revenue")
        )
        total_funding = cls.clean_number(
            cb.get("total_funding_usd") or fmp.get("total_funding") or diff.get("total_funding")
        )
        last_funding_type = cls.clean_text(
            cb.get("last_funding_type") or ("IPO / Public" if sec_cik else None)
        )
        last_funding_date = cls.clean_text(cb.get("last_funding_at"))
        num_funding_rounds = cls.clean_number(cb.get("num_funding_rounds"))
        funding_status = cls.clean_text(
            cb.get("funding_status") or ("IPO" if sec_cik else "Private")
        )
        stock_symbol = cls.clean_text(
            sec.get("ticker")
            or fmp.get("symbol")
            or finnhub.get("symbol")
            or cb.get("stock_symbol")
        )
        stock_exchange = cls.clean_text(
            sec.get("exchange") or fmp.get("exchange") or cb.get("stock_exchange")
        )
        ipo_status = cls.clean_text(cb.get("ipo_status") or ("Public" if sec_cik else "Private"))

        # 3. Location & Contact (Dynamic unnesting across all sources)
        all_location_sources = [cb, diff, opencorp, sec, gleif]
        hq_city = cls._extract_nested_location(all_location_sources, ["city", "municipality", "town"])
        hq_state = cls._extract_nested_location(
            all_location_sources, ["state", "region", "province", "administrative_area"]
        )
        hq_country = cls._extract_nested_location(
            all_location_sources, ["country", "country_name", "jurisdiction"]
        )
        hq_street = cls._extract_nested_location(
            all_location_sources, ["street", "street_address", "address"]
        ) or cls.clean_text(diff.get("street") or diff.get("street_address"))
        postal_code = cls.clean_text(
            cb.get("postal_code")
            or diff.get("postal_code")
            or gleif.get("postal_code")
            or sec.get("zip")
            or cls._extract_nested_location(all_location_sources, ["postalCode", "postal_code", "zip", "zip_code"])
        )
        hq_location = cls.clean_text(diff.get("headquarters_location")) or (
            ", ".join(filter(None, [hq_city, hq_state, hq_country])) or None
        )
        phone_number = cls.clean_text(
            diff.get("phone_number")
            or diff.get("phone")
            or cb.get("phone")
            or opencorp.get("phone")
        )
        contact_email = cls.clean_text(
            diff.get("contact_email")
            or diff.get("email")
            or cb.get("email")
        )

        # 4. Social & External URLs
        website_url = cls.clean_text(
            f"https://{target_domain}"
            if target_domain
            else cb.get("website_url") or diff.get("website_url")
        )
        crunchbase_url = cls.clean_text(
            cb.get("crunchbase_url") or diff.get("crunchbase_url") or serp.get("crunchbase_url")
        )
        linkedin_url = cls.clean_text(
            cb.get("linkedin_url") or diff.get("linkedin_url") or serp.get("linkedin_url")
        )
        twitter_url = cls.clean_text(
            tw.get("twitter_url")
            or cb.get("twitter_url")
            or diff.get("twitter_url")
            or serp.get("twitter_url")
        )
        twitter_handle = cls.clean_text(
            tw.get("handle")
            or cb.get("twitter_handle")
            or diff.get("twitter_handle")
        )
        facebook_url = cls.clean_text(
            diff.get("facebook_url")
            or cb.get("facebook_url")
            or serp.get("facebook_url")
        )
        github_url = cls.clean_text(cb.get("github_url") or diff.get("github_url"))
        glassdoor_url = cls.clean_text(
            gd.get("glassdoor_url") or cb.get("glassdoor_url") or serp.get("glassdoor_url")
        )
        blog_url = cls.clean_text(
            diff.get("blog_url")
            or cb.get("blog_url")
            or (f"{website_url.rstrip('/')}/newsroom" if website_url else None)
        )

        # 5. SEC EDGAR URLs
        sec_edgar_url = cls.clean_text(
            f"https://www.sec.gov/edgar/browse/?CIK={sec_cik}" if sec_cik else None
        )
        sec_filings_rss = cls.clean_text(
            f"https://data.sec.gov/rss?cik={sec_cik}" if sec_cik else None
        )
        sec_submissions_url = cls.clean_text(
            f"https://data.sec.gov/submissions/CIK{sec_cik}.json" if sec_cik else None
        )

        # 6. Digital Footprint & Tech Stack
        all_techs = cls._extract_technologies([diff, cb, serp])
        global_traffic_rank = cls.clean_number(
            serp.get("global_traffic_rank") or cb.get("rank") or diff.get("traffic_rank")
        )
        monthly_visits = cls.clean_text(serp.get("monthly_visits") or cb.get("monthly_visits"))
        bounce_rate = cls.clean_text(serp.get("bounce_rate"))
        visit_duration = cls.clean_text(serp.get("visit_duration"))
        page_views_per_visit = cls.clean_number(serp.get("page_views_per_visit"))
        active_tech_count = cls.clean_number(
            diff.get("active_tech_count") or (len(all_techs) if all_techs else None)
        )
        it_spend = cls.clean_text(diff.get("it_spend") or cb.get("it_spend"))
        patents_granted = cls.clean_number(
            serp.get("patents_count")
            or (len(serp.get("patents", [])) if serp.get("patents") else None)
        )

        # 7. OSINT URLs (Dynamic Zero-Cost Launchpads)
        enc_name = urllib.parse.quote_plus(display_name or company_name)
        google_patents_url = f"https://patents.google.com/?assignee={enc_name}"
        google_trends_url = f"https://trends.google.com/trends/explore?q={enc_name}"
        news_query = f"{company_name} news"
        rss_url = f"https://news.google.com/rss/search?q={enc_name}&hl=en-US&gl=US&ceid=US:en"
        reddit_query = f"{company_name} site:reddit.com"
        reddit_rss_url = f"https://www.reddit.com/search.rss?q={enc_name}&sort=new"
        youtube_search_url = f"https://www.youtube.com/results?search_query={enc_name}+corporate"
        openalex_institution_url = f"https://openalex.org/institutions?search={enc_name}"
        wikidata_entity_url = cls.clean_text(
            wiki.get("wikidata_url") or f"https://www.wikidata.org/w/index.php?search={enc_name}"
        )

        # 8. Glassdoor Culture & Sentiment (Dynamic deep search)
        culture_score = cls._extract_nested_metric(
            [gd, cb], ["overall_rating", "overallRating", "rating", "score", "culture_rating"]
        )
        ceo_approval_rate = cls._extract_nested_metric(
            [gd, cb], ["ceo_approval_pct", "ceoApproval", "ceo_approval", "ceoApprovalRate", "ceo_rating"]
        )

        # 9. Master Raw Data JSONB (Extensible Data Lake Bucket)
        raw_payload = {
            "sec_edgar": sec,
            "gleif": gleif,
            "opencorporates": opencorp,
            "financial_modeling_prep": fmp,
            "courtlistener": court,
            "finnhub": finnhub,
            "apify_crunchbase": cb,
            "apify_glassdoor": gd,
            "apify_twitter": tw,
            "diffbot": diff,
            "serper": serp,
            "wikipedia": wiki,
            "openfec": fec,
        }

        # Authentic Revenue Resolution (SEC EDGAR, FMP, Crunchbase, or Diffbot Knowledge Graph)
        if not revenue:
            diff_rev = diff.get("yearly_revenue") or diff.get("revenue")
            if isinstance(diff_rev, dict):
                diff_rev = diff_rev.get("value")
            if diff_rev and isinstance(diff_rev, (int, float)) and diff_rev > 0:
                if diff_rev >= 1e9:
                    revenue = f"${diff_rev / 1e9:.2f}B"
                elif diff_rev >= 1e6:
                    revenue = f"${diff_rev / 1e6:.2f}M"
                else:
                    revenue = f"${diff_rev:,.0f}"

        # Industries & Keywords list synthesis (deduplicated)
        ind_cb = cb.get("industries") or []
        ind_diff = diff.get("industries") or []
        industries_merged = list(dict.fromkeys(ind_cb + ind_diff))

        kw_cb = cb.get("keywords") or []
        kw_diff = diff.get("keywords") or diff.get("descriptors") or []
        keywords_merged = list(dict.fromkeys(kw_cb + kw_diff))

        # Founders extraction
        founders_raw = (
            diff.get("founders")
            or (diff.get("_raw_diffbot", {}) if isinstance(diff.get("_raw_diffbot"), dict) else {}).get("founders")
            or cb.get("founders")
            or []
        )
        founders_merged = []
        for f in (founders_raw if isinstance(founders_raw, list) else []):
            if isinstance(f, str) and f.strip():
                founders_merged.append(f.strip())
            elif isinstance(f, dict) and f.get("name"):
                founders_merged.append(f["name"].strip())
        founders_merged = list(dict.fromkeys(founders_merged))

        # Dynamic suborganizations count from authentic child entities / subsidiaries
        num_suborgs = (
            diff.get("num_suborganizations")
            or (len(diff.get("subsidiaries")) if isinstance(diff.get("subsidiaries"), list) else None)
            or gleif.get("total_children_gleif")
            or gleif.get("total_child_entities_found")
            or cb.get("num_suborganizations")
            or None
        )

        # Authentic IT Spend (direct from collector, or None if not collected)
        it_spend = cls.clean_text(diff.get("it_spend") or cb.get("it_spend"))

        # Authentic Heat Score (direct from collector, or None if not collected)
        heat_score = diff.get("heat_score") or cb.get("heat_score")

        # OSINT feed manifest dictionary
        feed_manifest = {
            "key": slug_key,
            "display_name": display_name or company_name,
            "entity_type": "account",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "feeds": {
                "website_url": website_url,
                "twitter_handle": twitter_handle,
                "twitter_live_url": cls.clean_text(
                    f"https://x.com/{twitter_handle.lstrip('@')}"
                    if twitter_handle
                    else (
                        (f"https://{twitter_url}" if not twitter_url.startswith("http") else twitter_url)
                        if twitter_url
                        else f"https://x.com/search?q={urllib.parse.quote_plus(display_name or company_name)}&f=live"
                    )
                ),
                "reddit_query": reddit_query,
                "reddit_rss_url": reddit_rss_url,
                "news_query": news_query,
                "rss_url": rss_url,
                "google_patents_url": google_patents_url,
                "google_trends_url": google_trends_url,
                "youtube_search_url": youtube_search_url,
                "openalex_institution_url": openalex_institution_url,
                "wikidata_entity_url": wikidata_entity_url,
                "linkedin_url": linkedin_url,
                "github_url": github_url,
                "glassdoor_url": glassdoor_url,
                "blog_url": blog_url,
                "sec_edgar_url": sec_edgar_url,
                "sec_filings_rss": sec_filings_rss,
                "sec_submissions_url": sec_submissions_url,
            }
        }

        return {
            "key": slug_key,
            "display_name": display_name,
            "legal_name": legal_name,
            "primary_domain": target_domain,
            "domain": target_domain,
            "website_url": website_url,
            "crunchbase_url": crunchbase_url,
            "linkedin_url": linkedin_url,
            "twitter_url": twitter_url,
            "twitter_handle": twitter_handle,
            "facebook_url": facebook_url,
            "github_url": github_url,
            "glassdoor_url": glassdoor_url,
            "blog_url": blog_url,
            "operating_status": operating_status,
            "company_type": company_type,
            "founded_year": int(founded_year) if founded_year else None,
            "founders": founders_merged,
            "num_founders": len(founders_merged),
            "employee_count_range": cls.clean_text(
                cb.get("employee_count_range") or diff.get("employee_count_range")
            ),
            "headquarters_location": hq_location,
            "street_address": hq_street,
            "city": hq_city,
            "state": hq_state,
            "country": hq_country,
            "postal_code": postal_code,
            "phone_number": phone_number,
            "sanitized_phone": cls.clean_phone(phone_number),
            "contact_email": contact_email,
            "revenue": revenue,
            "estimated_revenue_range": revenue,
            "total_funding_amount_usd": total_funding,
            "total_funding_currency": "USD" if total_funding else None,
            "last_funding_type": last_funding_type,
            "last_funding_date": last_funding_date,
            "num_funding_rounds": int(num_funding_rounds) if num_funding_rounds else None,
            "funding_status": funding_status,
            "stock_symbol": stock_symbol,
            "stock_exchange": stock_exchange,
            "ipo_status": ipo_status,
            "sec_cik": sec_cik,
            "sec_edgar_url": sec_edgar_url,
            "sec_filings_rss": sec_filings_rss,
            "sec_submissions_url": sec_submissions_url,
            "global_traffic_rank": int(global_traffic_rank) if global_traffic_rank else None,
            "monthly_visits": monthly_visits,
            "bounce_rate": bounce_rate,
            "visit_duration": visit_duration,
            "page_views_per_visit": page_views_per_visit,
            "heat_score": int(heat_score) if heat_score else None,
            "trend_score_90d": (
                int(diff.get("trend_score_90d")) if diff.get("trend_score_90d") else None
            ),
            "active_tech_count": int(active_tech_count) if active_tech_count else None,
            "it_spend": it_spend,
            "num_suborganizations": int(num_suborgs) if num_suborgs else None,
            "patents_granted": int(patents_granted) if patents_granted else None,
            "trademarks_registered": (
                int(serp.get("trademarks_count")) if serp.get("trademarks_count") else None
            ),
            "twitter_live_url": cls.clean_text(
                f"https://x.com/{twitter_handle.lstrip('@')}"
                if twitter_handle
                else (
                    (f"https://{twitter_url}" if not twitter_url.startswith("http") else twitter_url)
                    if twitter_url
                    else f"https://x.com/search?q={urllib.parse.quote_plus(display_name or company_name)}&f=live"
                )
            ),
            "reddit_query": reddit_query,
            "reddit_rss_url": reddit_rss_url,
            "news_query": news_query,
            "rss_url": rss_url,
            "google_patents_url": google_patents_url,
            "google_trends_url": google_trends_url,
            "youtube_search_url": youtube_search_url,
            "openalex_institution_url": openalex_institution_url,
            "wikidata_entity_url": wikidata_entity_url,
            "industries": industries_merged,
            "keywords": keywords_merged,
            "overview_description": cls.clean_text(
                diff.get("description") or cb.get("short_description") or wiki.get("summary")
            ),
            "short_description": cls.clean_text(
                cb.get("short_description")
                or (wiki.get("summary", "").split(". ")[0] + "." if wiki.get("summary") else None)
                or (diff.get("description", "").split(". ")[0] + "." if diff.get("description") else None)
            ),
            "full_description": cls.clean_text(
                wiki.get("summary") or diff.get("description") or cb.get("short_description")
            ),
            "culture_score": culture_score,
            "ceo_approval_rate": ceo_approval_rate,
            "raw_data": raw_payload,
            "osint_feed_manifest": feed_manifest,
        }


class AccountValidator:
    """Pre-DB Quality and Completeness Validator Gate."""

    @staticmethod
    def validate_account(account_dossier: Dict[str, Any]) -> Dict[str, Any]:
        """Calculates completeness percentage and assigns quality grade."""
        critical_fields = ["display_name", "legal_name", "key", "domain", "revenue"]
        important_fields = [
            "sec_cik",
            "headquarters_location",
            "website_url",
            "global_traffic_rank",
            "active_tech_count",
        ]

        total_fields = len(account_dossier)
        populated = sum(
            1 for v in account_dossier.values() if v is not None and v != "" and v != []
        )
        score = int((populated / total_fields) * 100) if total_fields else 0

        missing_critical = [f for f in critical_fields if not account_dossier.get(f)]
        missing_important = [f for f in important_fields if not account_dossier.get(f)]

        grade = "A" if score >= 85 and not missing_critical else ("B" if score >= 70 else "C")
        ready_for_db = len(missing_critical) == 0 and score >= 65

        return {
            "score": score,
            "grade": grade,
            "ready_for_db": ready_for_db,
            "total_columns": total_fields,
            "populated_columns": populated,
            "missing_critical": missing_critical,
            "missing_important": missing_important,
        }


class AccountService:
    """Main Orchestration Service for Level 1 Account Intelligence Ingestion."""

    @classmethod
    def collect(
        cls,
        company_name: str,
        domain: Optional[str] = None,
        ticker: Optional[str] = None,
        sec_cik: Optional[str] = None,
        run_raw_dir: Optional[Path] = None,
        mock_connectors: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Coordinates all Level 1 Connectors, writes raw Data Lake files,
        coalesces 94 columns, and validates quality.
        """
        start_ts = time.time()
        # Pre-Flight Parameter & Identity Resolution
        resolved_inputs = AccountInputResolver.resolve(
            company_name=company_name,
            domain=domain,
            ticker=ticker,
            sec_cik=sec_cik,
        )
        effective_name = resolved_inputs.get("original_name")
        clean_name = resolved_inputs.get("clean_name")
        effective_domain = resolved_inputs.get("root_domain") or domain
        effective_ticker = resolved_inputs.get("ticker")
        effective_cik = resolved_inputs.get("sec_cik")

        print(
            f"[*] [AccountService] Starting Level 1 Ingestion for '{effective_name}'"
            f" (Clean: '{clean_name}', Domain: {effective_domain or 'Auto'}, "
            f"Ticker: {effective_ticker or 'N/A'}, CIK: {effective_cik or 'N/A'})..."
        )

        telemetry: Dict[str, Any] = {
            "company_name": effective_name,
            "resolved_inputs": resolved_inputs,
            "started_at": start_ts,
            "sources": {},
        }

        def _timed_fetch(name: str, fetch_fn):
            t0 = time.time()
            try:
                data = fetch_fn()
                elapsed = round((time.time() - t0) * 1000, 2)
                has_data = bool(data and (not isinstance(data, dict) or any(data.values())))
                telemetry["sources"][name] = {
                    "status": "success" if has_data else "empty",
                    "latency_ms": elapsed,
                    "records_or_keys": len(data) if isinstance(data, (dict, list)) else 1 if data else 0,
                    "reason_if_empty": None if has_data else "Source returned empty payload or no match",
                }
                return data
            except Exception as ex:
                elapsed = round((time.time() - t0) * 1000, 2)
                telemetry["sources"][name] = {
                    "status": "error",
                    "latency_ms": elapsed,
                    "error": str(ex),
                }
                print(f"[!] [AccountService] Connector '{name}' error: {ex}")
                return {}

        # 1. Multi-source connector execution (with mock injection support for offline testing)
        sec_data = (
            mock_connectors.get("sec")
            if mock_connectors
            else _timed_fetch(
                "sec_edgar", lambda: cls._fetch_sec_edgar(effective_name, effective_ticker, effective_cik)
            )
        )
        RawDataLakeWriter.save_raw(sec_data, "sec_edgar", effective_name, run_raw_dir)

        # Re-resolve ticker / CIK if SEC just discovered them
        if isinstance(sec_data, dict):
            if sec_data.get("ticker") and not effective_ticker:
                effective_ticker = sec_data.get("ticker")
            if sec_data.get("sec_cik") and not effective_cik:
                effective_cik = sec_data.get("sec_cik")

        gleif_data = (
            mock_connectors.get("gleif")
            if mock_connectors
            else _timed_fetch("gleif", lambda: cls._fetch_gleif(effective_name))
        )
        RawDataLakeWriter.save_raw(gleif_data, "gleif", effective_name, run_raw_dir)

        opencorp_data = (
            mock_connectors.get("opencorporates")
            if mock_connectors
            else _timed_fetch("opencorporates", lambda: cls._fetch_opencorporates(clean_name or effective_name))
        )
        RawDataLakeWriter.save_raw(opencorp_data, "opencorporates", effective_name, run_raw_dir)

        fmp_data = (
            mock_connectors.get("fmp")
            if mock_connectors
            else _timed_fetch("fmp", lambda: cls._fetch_fmp(effective_ticker))
        )
        RawDataLakeWriter.save_raw(fmp_data, "fmp", effective_name, run_raw_dir)

        court_data = (
            mock_connectors.get("courtlistener")
            if mock_connectors
            else _timed_fetch("courtlistener", lambda: cls._fetch_courtlistener(effective_name))
        )
        RawDataLakeWriter.save_raw(court_data, "courtlistener", effective_name, run_raw_dir)

        finnhub_data = (
            mock_connectors.get("finnhub")
            if mock_connectors
            else _timed_fetch("finnhub", lambda: cls._fetch_finnhub(effective_ticker))
        )
        RawDataLakeWriter.save_raw(finnhub_data, "finnhub", effective_name, run_raw_dir)

        cb_data = (
            mock_connectors.get("apify_crunchbase")
            if mock_connectors
            else _timed_fetch(
                "apify_crunchbase", lambda: cls._fetch_apify_crunchbase(effective_name, effective_domain)
            )
        )
        RawDataLakeWriter.save_raw(cb_data, "apify_crunchbase", effective_name, run_raw_dir)

        gd_data = (
            mock_connectors.get("apify_glassdoor")
            if mock_connectors
            else _timed_fetch("apify_glassdoor", lambda: cls._fetch_apify_glassdoor(clean_name or effective_name))
        )
        RawDataLakeWriter.save_raw(gd_data, "apify_glassdoor", effective_name, run_raw_dir)

        diffbot_data = (
            mock_connectors.get("diffbot")
            if mock_connectors
            else _timed_fetch("diffbot", lambda: cls._fetch_diffbot(effective_name, effective_domain))
        )
        RawDataLakeWriter.save_raw(diffbot_data, "diffbot", effective_name, run_raw_dir)

        serper_data = (
            mock_connectors.get("serper")
            if mock_connectors
            else _timed_fetch("serper", lambda: cls._fetch_serper(effective_name, effective_domain))
        )
        RawDataLakeWriter.save_raw(serper_data, "serper", effective_name, run_raw_dir)

        wiki_data = (
            mock_connectors.get("wikipedia")
            if mock_connectors
            else _timed_fetch("wikipedia", lambda: cls._fetch_wikipedia(clean_name or effective_name))
        )
        RawDataLakeWriter.save_raw(wiki_data, "wikipedia", effective_name, run_raw_dir)

        fec_data = (
            mock_connectors.get("openfec")
            if mock_connectors
            else _timed_fetch("openfec", lambda: cls._fetch_openfec(effective_name))
        )
        RawDataLakeWriter.save_raw(fec_data, "openfec", effective_name, run_raw_dir)

        # Tier-1 Corporate Web Scraper (Firecrawl v1)
        firecrawl_data = (
            mock_connectors.get("firecrawl")
            if mock_connectors
            else _timed_fetch(
                "firecrawl", lambda: cls._fetch_firecrawl_account(effective_domain, effective_name)
            )
        )
        RawDataLakeWriter.save_raw(firecrawl_data, "firecrawl", effective_name, run_raw_dir)

        # 2. Field-Level Coalesce Waterfall
        account_dossier = AccountCoalesceEngine.coalesce(
            company_name=effective_name,
            domain=effective_domain,
            sec_data=sec_data,
            gleif_data=gleif_data,
            opencorporates_data=opencorp_data,
            fmp_data=fmp_data,
            courtlistener_data=court_data,
            finnhub_data=finnhub_data,
            apify_crunchbase=cb_data,
            apify_glassdoor=gd_data,
            diffbot_data=diffbot_data,
            serper_data=serper_data,
            fec_data=fec_data,
            firecrawl_data=firecrawl_data,
        )

        # 3. Deep Intelligence Connectors (SEC 10-K, Patents, Exhibit 21, GLEIF Ownership Tree)
        # These are called AFTER coalescing so sec_cik is already resolved
        try:

            sec_cik_val = account_dossier.get("sec_cik")

            # 10-K Chunks (SEC EDGAR, free)
            tenk_data = {}
            if sec_cik_val:
                try:
                    tenk_data = fetch_latest_10k_chunks(sec_cik_val)
                    RawDataLakeWriter.save_raw(tenk_data, "sec_10k_chunks", company_name, run_raw_dir)
                    print(f"[+] [AccountService] 10-K chunks extracted for CIK {sec_cik_val}")
                except Exception as e:
                    print(f"[!] [AccountService] 10-K chunks notice: {e}")

            # USPTO Patent Intelligence (free)
            patents_data = {}
            try:
                patents_data = extract_full_patents(company_name)
                RawDataLakeWriter.save_raw(patents_data, "patents", company_name, run_raw_dir)
                if patents_data and isinstance(patents_data, dict) and patents_data.get("total_patents_found") is not None:
                    account_dossier["patents_granted"] = int(patents_data["total_patents_found"])
                print(f"[+] [AccountService] Patents extracted for '{company_name}'")
            except Exception as e:
                print(f"[!] [AccountService] Patents notice: {e}")

            # SEC Exhibit 21 — Official Subsidiary/LOB Names (free, highest value)
            ex21_data = {}
            if sec_cik_val:
                try:
                    ex21_data = fetch_sec_exhibit_21_subsidiaries(sec_cik_val)
                    RawDataLakeWriter.save_raw(ex21_data, "sec_exhibit21", company_name, run_raw_dir)
                    print(
                        f"[+] [AccountService] Exhibit 21: "
                        f"{ex21_data.get('total_subsidiaries_found', 0)} subsidiaries found"
                    )
                except Exception as e:
                    print(f"[!] [AccountService] Exhibit 21 notice: {e}")

            # GLEIF Ownership Tree — LEI child entities = LOB/subsidiary names
            gleif_tree_data = {}
            try:
                gleif_tree_data = fetch_gleif_ownership_tree(company_name, max_children=25)
                RawDataLakeWriter.save_raw(gleif_tree_data, "gleif_tree", company_name, run_raw_dir)
                print(
                    f"[+] [AccountService] GLEIF tree: "
                    f"{gleif_tree_data.get('total_child_entities_found', 0)} child entities found"
                )
            except Exception as e:
                print(f"[!] [AccountService] GLEIF tree notice: {e}")

            # Build discovered_lob_names from both sources (deduplicated)
            sec_sub_names = [
                s.get("legal_name") for s in ex21_data.get("subsidiaries", [])
                if s.get("legal_name")
            ]
            gleif_child_names = [
                c.get("legal_name") for c in gleif_tree_data.get("child_entities", [])
                if c.get("legal_name")
            ]
            all_lob_hints = list(dict.fromkeys(sec_sub_names + gleif_child_names))
            account_dossier["discovered_lob_names"] = all_lob_hints

            # Populate organisational_hierarchy_tree column
            account_dossier["organisational_hierarchy_tree"] = {
                "gleif_lei": gleif_tree_data.get("lei"),
                "gleif_children": gleif_tree_data.get("child_entities", []),
                "gleif_indirect_sublobs": gleif_tree_data.get("indirect_sub_lobs", []),
                "all_subsidiaries": gleif_tree_data.get("all_subsidiaries", []),
                "sec_exhibit21_subsidiaries": ex21_data.get("subsidiaries", []),
                "total_subsidiaries_sec": ex21_data.get("total_subsidiaries_found", 0),
                "total_children_gleif": gleif_tree_data.get("total_child_entities_found", 0),
                "total_indirect_sublobs": gleif_tree_data.get("total_indirect_sub_lobs_found", 0),
                "total_ultimate_children": gleif_tree_data.get("total_ultimate_children_found", 0),
            }

            # Populate multi_source_intelligence column with all deep data
            account_dossier["multi_source_intelligence"] = {
                "sec_10k_chunks": tenk_data,
                "patents": patents_data,
                "sec_exhibit21": ex21_data,
                "gleif_ownership_tree": gleif_tree_data,
                "fec_political": fec_data if isinstance(fec_data, dict) else {},
                "courtlistener": court_data if isinstance(court_data, dict) else {},
                "finnhub": finnhub_data if isinstance(finnhub_data, dict) else {},
                "opencorporates": opencorp_data if isinstance(opencorp_data, dict) else {},
            }

        except Exception as deep_err:
            print(f"[!] [AccountService] Deep connector block notice: {deep_err}")

        # 4. Pre-DB Completeness Validator & Telemetry Logging
        audit = AccountValidator.validate_account(account_dossier)
        account_dossier["_validation_audit"] = audit
        telemetry["total_latency_ms"] = round((time.time() - start_ts) * 1000, 2)
        telemetry["validation_score"] = audit.get("score")
        telemetry["validation_grade"] = audit.get("grade")

        try:
            from services.credit_accounting_engine import CreditAccountingEngine
            account_credit_tally = CreditAccountingEngine.tally_account_telemetry(telemetry.get("sources", {}))
            telemetry["credit_accounting"] = account_credit_tally
            account_dossier["_credit_accounting"] = account_credit_tally
        except Exception as tally_err:
            print(f"[!] [AccountService] Credit tally notice: {tally_err}")

        account_dossier["_telemetry"] = telemetry

        RawDataLakeWriter.save_raw(telemetry, "telemetry", effective_name, run_raw_dir)
        RawDataLakeWriter.save_raw(audit, "validation_report", effective_name, run_raw_dir)

        print(
            f"[+] [AccountService] Completed '{effective_name}': Completeness"
            f" {audit['score']}% (Grade: {audit['grade']}, Latency: {telemetry['total_latency_ms']}ms)"
        )

        return account_dossier

    # Ingestion Connector Implementations (Pure Dynamic HTTP)
    @staticmethod
    def _fetch_sec_edgar(
        company_name: str, ticker: Optional[str] = None, cik: Optional[str] = None
    ) -> Dict[str, Any]:
        """Free SEC EDGAR Submissions API with deep 4-tier CIK resolution waterfall."""
        headers = {"User-Agent": "SalesAIAgentResearch admin@salesai.com"}
        session = AccountServiceHTTPClient.get_session()
        sec_result: Dict[str, Any] = {}
        try:
            target_cik = cik
            target_ticker = ticker
            target_title = None

            def _is_valid_sec_candidate(candidate_name: str) -> bool:
                if not candidate_name:
                    return False
                suffix_pat = r"\b(the|corporation|corp|inc|incorporated|co|company|ltd|llc|reit|trust|holdings|group|plc|sa|ag|nv)\b"
                q_norm = re.sub(suffix_pat, "", company_name, flags=re.IGNORECASE).strip().lower()
                c_norm = re.sub(suffix_pat, "", candidate_name, flags=re.IGNORECASE).strip().lower()
                q_words = set(re.findall(r"\b[a-z0-9]{3,}\b", q_norm))
                c_words = set(re.findall(r"\b[a-z0-9]{3,}\b", c_norm))
                if not q_words or not c_words:
                    return False
                if q_norm == c_norm or q_words == c_words:
                    return True
                if len(q_words) == 1 and len(c_words) > 1:
                    return False
                if len(c_words) == 1 and len(q_words) > 1:
                    return False
                overlap = len(q_words.intersection(c_words))
                return overlap >= max(2, int(len(q_words) * 0.75))

            # ── Tier 1: SEC Official Registrants & Tickers Index (Dynamic Matching) ──
            if not target_cik:
                try:
                    res = session.get(
                        "https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=10
                    )
                    if res.ok:
                        data = res.json()
                        for entry in data.values():
                            entry_ticker = str(entry.get("ticker", "")).upper()
                            entry_title = str(entry.get("title", ""))
                            if target_ticker and entry_ticker == target_ticker.upper():
                                target_cik = str(entry.get("cik_str")).zfill(10)
                                target_title = entry.get("title")
                                target_ticker = entry.get("ticker")
                                break
                            if _is_valid_sec_candidate(entry_title):
                                target_cik = str(entry.get("cik_str")).zfill(10)
                                target_title = entry.get("title")
                                target_ticker = entry.get("ticker")
                                break
                except Exception as e:
                    print(f"[!] SEC EDGAR Tier 1 notice: {e}")

            # ── Tier 2: SEC EDGAR Official Search Index (EFTS API - Strict Registrant Matching) ──
            if not target_cik:
                try:
                    search_q = urllib.parse.quote_plus(f'"{company_name}"')
                    efts_url = f"https://efts.sec.gov/LATEST/search-index?q={search_q}&forms=10-K,10-Q,8-K,X-17A-5,CA-1,DEF 14A,C"
                    efts_res = session.get(efts_url, headers=headers, timeout=8)
                    if efts_res.ok:
                        hits = efts_res.json().get("hits", {}).get("hits", [])
                        for hit in hits[:10]:
                            display_names = hit.get("_source", {}).get("display_names", [])
                            cik_candidates = hit.get("_source", {}).get("ciks", [])
                            for dname in display_names:
                                if _is_valid_sec_candidate(dname) and cik_candidates:
                                    target_cik = str(cik_candidates[0]).zfill(10)
                                    target_title = dname
                                    break
                            if target_cik:
                                break
                except Exception as e:
                    print(f"[!] SEC EDGAR Tier 2 (EFTS) notice: {e}")

            # ── Tier 3: Dynamic Serper / Google OSINT Search for SEC CIK ──
            if not target_cik and config.SERPER_API_KEY:
                try:
                    serp_res = session.post(
                        "https://google.serper.dev/search",
                        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
                        json={"q": f'"{company_name}" site:sec.gov/edgar/browse OR "Central Index Key" CIK', "num": 3},
                        timeout=8,
                    )
                    if serp_res.ok:
                        serp_json = serp_res.json()
                        for org in serp_json.get("organic", []):
                            title = org.get("title", "")
                            link = org.get("link", "")
                            snip = org.get("snippet", "")
                            if not _is_valid_sec_candidate(title) and not _is_valid_sec_candidate(snip):
                                continue
                            cik_in_link = re.search(r"CIK[=/\s]*0*(\d{5,10})", link, re.IGNORECASE)
                            if cik_in_link:
                                target_cik = str(cik_in_link.group(1)).zfill(10)
                                print(f"[+] [AccountService] Dynamically resolved CIK via EDGAR link for '{company_name}': {target_cik}")
                                break
                            cik_in_snip = re.search(r"\bCIK[:\s#]*0*(\d{7,10})\b", snip, re.IGNORECASE)
                            if cik_in_snip:
                                target_cik = str(cik_in_snip.group(1)).zfill(10)
                                print(f"[+] [AccountService] Dynamically resolved CIK via snippet for '{company_name}': {target_cik}")
                                break
                except Exception as e:
                    print(f"[!] SEC EDGAR Tier 3 (OSINT CIK) notice: {e}")

            if target_cik:
                sec_result["sec_cik"] = str(target_cik).zfill(10)
                sec_result["legal_name"] = target_title or company_name
                sec_result["ticker"] = target_ticker

                # Fetch full SEC Submissions profile
                sub_url = f"https://data.sec.gov/submissions/CIK{sec_result['sec_cik']}.json"
                sub_res = session.get(sub_url, headers=headers, timeout=10)
                if sub_res.ok:
                    sub_data = sub_res.json()
                    sec_result["sic_code"] = sub_data.get("sic")
                    sec_result["sic_description"] = sub_data.get("sicDescription")
                    sec_result["fiscal_year_end"] = sub_data.get("fiscalYearEnd")
                    sec_result["phone"] = sub_data.get("phone")

                    addr = sub_data.get("addresses", {}).get("business", {})
                    sec_result["city"] = addr.get("city")
                    sec_result["state"] = addr.get("stateOrCountry")
                    sec_result["postal_code"] = addr.get("zipCode")
                    sec_result["street"] = addr.get("street1")
                    sec_result["sec_name"] = sub_data.get("name")
                    sec_result["former_names"] = [
                        f.get("name") for f in sub_data.get("formerNames", []) if f.get("name")
                    ]
                    sec_result["_raw_submission"] = sub_data
                    print(f"[+] [AccountService] SEC profile fetched for CIK {sec_result['sec_cik']} ({sub_data.get('name')})")

        except Exception as e:
            print(f"[!] SEC EDGAR connector warning: {e}")
        return sec_result

    @staticmethod
    def _fetch_gleif(company_name: str) -> Dict[str, Any]:
        """Free G20 GLEIF LEI Registry."""
        session = AccountServiceHTTPClient.get_session()
        try:
            base_url = "https://api.gleif.org/api/v1/lei-records"
            enc_comp = urllib.parse.quote_plus(company_name)
            url = f"{base_url}?filter[entity.legalName]={enc_comp}&page[size]=1"
            res = session.get(url, timeout=10)
            if res.ok:
                data = res.json()
                items = data.get("data", [])
                if items:
                    attr = items[0].get("attributes", {}).get("entity", {})
                    return {
                        "lei": items[0].get("attributes", {}).get("lei"),
                        "legal_name": attr.get("legalName", {}).get("name"),
                        "jurisdiction": attr.get("jurisdiction"),
                        "country": attr.get("legalAddress", {}).get("country"),
                        "status": attr.get("status"),
                    }
        except Exception as e:
            print(f"[!] GLEIF connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_opencorporates(company_name: str) -> Dict[str, Any]:
        """OpenCorporates Global Registry."""
        session = AccountServiceHTTPClient.get_session()
        try:
            base_url = "https://api.opencorporates.com/v0.4/companies/search"
            enc_comp = urllib.parse.quote_plus(company_name)
            url = f"{base_url}?q={enc_comp}&per_page=1"
            res = session.get(url, timeout=10)
            if res.ok:
                data = res.json()
                companies = data.get("results", {}).get("companies", [])
                if companies:
                    c = companies[0].get("company", {})
                    return {
                        "name": c.get("name"),
                        "company_number": c.get("company_number"),
                        "jurisdiction": c.get("jurisdiction_code"),
                        "current_status": c.get("current_status"),
                        "company_type": c.get("company_type"),
                    }
        except Exception as e:
            print(f"[!] OpenCorporates connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_courtlistener(company_name: str) -> Dict[str, Any]:
        """CourtListener / RECAP Federal Legal Dockets API (100% Free)."""
        session = AccountServiceHTTPClient.get_session()
        try:
            base_url = "https://www.courtlistener.com/api/rest/v3/search/"
            enc_comp = urllib.parse.quote_plus(company_name)
            url = f"{base_url}?q={enc_comp}&type=d"
            res = session.get(
                url, headers={"User-Agent": "SalesAIAgentResearch admin@salesai.com"}, timeout=10
            )
            if res.ok:
                data = res.json()
                results = data.get("results", [])
                return {"total_dockets": data.get("count", 0), "recent_dockets": results[:3]}
        except Exception as e:
            print(f"[!] CourtListener connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_finnhub(ticker: Optional[str]) -> Dict[str, Any]:
        """Finnhub Real-Time Market Data, Valuation & Company Profile API."""
        if not ticker:
            return {}
        api_key = os.getenv("FINNHUB_API_KEY")
        if not api_key:
            return {}
        session = AccountServiceHTTPClient.get_session()
        result: Dict[str, Any] = {"symbol": ticker.upper()}
        try:
            # 1. Company Profile 2 (Market Cap, Shares, Exchange, IPO, Logo, Industry) - 100% Free
            p_res = session.get(
                f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={api_key}",
                timeout=10,
            )
            if p_res.ok and p_res.json():
                p_data = p_res.json()
                result["profile"] = p_data
                result["exchange"] = p_data.get("exchange")
                result["market_capitalization"] = p_data.get("marketCapitalization")
                result["share_outstanding"] = p_data.get("shareOutstanding")
                result["ipo"] = p_data.get("ipo")
                result["logo"] = p_data.get("logo")
                result["finnhub_industry"] = p_data.get("finnhubIndustry")

            # 2. Real-Time Stock Quote (Current Price, Day Change %, High, Low) - 100% Free
            q_res = session.get(
                f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}",
                timeout=10,
            )
            if q_res.ok and q_res.json():
                q_data = q_res.json()
                result["quote"] = q_data
                result["current_price"] = q_data.get("c")
                result["day_change"] = q_data.get("d")
                result["day_change_percent"] = q_data.get("dp")

            # 3. Industry Peers / Competitors - 100% Free
            peers_res = session.get(
                f"https://finnhub.io/api/v1/stock/peers?symbol={ticker}&token={api_key}",
                timeout=10,
            )
            if peers_res.ok and peers_res.json():
                result["peers"] = peers_res.json()

            # 4. News Sentiment (optional premium endpoint - gracefully fallback if 403)
            try:
                s_res = session.get(
                    f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={api_key}",
                    timeout=5,
                )
                if s_res.ok and s_res.json():
                    result["sentiment"] = s_res.json()
            except Exception:
                pass

            return result if len(result) > 1 else {}
        except Exception as e:
            print(f"[!] Finnhub connector warning: {e}")
            return result if len(result) > 1 else {}

    @staticmethod
    def _fetch_fmp(ticker: Optional[str]) -> Dict[str, Any]:
        """Financial Modeling Prep API."""
        if not ticker:
            return {}
        api_key = os.getenv("FMP_API_KEY")
        if not api_key:
            return {}
        session = AccountServiceHTTPClient.get_session()
        try:
            res = session.get(
                f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={api_key}",
                timeout=10,
            )
            if res.ok and res.json():
                item = res.json()[0]
                return {
                    "symbol": item.get("symbol"),
                    "revenue_formatted": f"${item.get('mktCap', 0)/1e9:.1f}B Market Cap",
                    "exchange": item.get("exchangeShortName"),
                    "cik": item.get("cik"),
                }
        except Exception as e:
            print(f"[!] FMP connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_apify_crunchbase(company_name: str, domain: Optional[str]) -> Dict[str, Any]:
        """Apify Crunchbase company lookup by domain or name (johnvc/crunchbase-company-api).

        Returns the raw row plus the snake_case keys coalesce() reads.
        """
        if not config.APIFY_TOKEN:
            return {}
        try:

            client = ApifyClient(config.APIFY_TOKEN)
            run = client.actor("johnvc/crunchbase-company-api").call(
                run_input={"companyNames": [domain or company_name]}
            )
            items = client.dataset(run["defaultDatasetId"]).list_items().items
            row = next((i for i in items if i.get("result_type") == "company"), None)
            if not row:
                return {}

            def _social(*hosts: str) -> Optional[str]:
                for url in row.get("socialMediaLinks") or []:
                    if re.sub(r"^www\.", "", urlparse(url).netloc.lower()) in hosts:
                        return url
                return None

            website = row.get("website")
            rounds = row.get("fundingRounds") or []  # newest first
            sub_orgs = row.get("subOrganizations")
            return {
                **row,
                "domain": re.sub(r"^www\.", "", urlparse(website).netloc) if website else None,
                "company_type": row.get("companyType"),
                "operating_status": row.get("operatingStatus"),
                "total_funding_usd": row.get("totalFunding"),
                "last_funding_type": rounds[0].get("round") if rounds else None,
                "num_funding_rounds": row.get("numFundingRounds"),
                "stock_symbol": row.get("stockSymbol"),
                "ipo_status": row.get("ipoStatus"),
                "phone": row.get("contactPhone"),
                "email": row.get("contactEmail"),
                "website_url": website,
                "crunchbase_url": row.get("companyUrl"),
                "linkedin_url": _social("linkedin.com"),
                "twitter_url": _social("twitter.com", "x.com"),
                "facebook_url": _social("facebook.com"),
                "monthly_visits": row.get("monthlyVisits"),
                "it_spend": row.get("itSpendEstimateUsd"),
                "heat_score": row.get("heatScore"),
                "num_suborganizations": len(sub_orgs) if sub_orgs else None,
                "employee_count_range": row.get("employeeCount"),
                "short_description": row.get("description"),
                "tech_stack": row.get("techStack"),
            }
        except Exception as e:
            print(f"[!] Apify Crunchbase warning: {e}")
        return {}

    @staticmethod
    def _fetch_apify_glassdoor(company_name: str) -> Dict[str, Any]:
        """Apify Glassdoor lookup in two steps.

        A DuckDuckGo search (johnvc/DuckDuckGo-Scraper-for-serp-rankings) finds the
        company's glassdoor.com page, then johnvc/glassdoor-reviews-api reads its
        latest reviews. overall_rating is the average of those reviews, not
        Glassdoor's headline rating.
        """
        if not config.APIFY_TOKEN:
            return {}
        try:

            client = ApifyClient(config.APIFY_TOKEN)
            run = client.actor("johnvc/DuckDuckGo-Scraper-for-serp-rankings").call(
                run_input={"query": f"{company_name} glassdoor reviews", "max_pages": 1}
            )
            pages = client.dataset(run["defaultDatasetId"]).list_items().items
            words = company_name.lower().split()
            first_word = re.sub(r"[^a-z0-9]", "", words[0]) if words else ""
            glassdoor_url = next(
                (
                    r["link"]
                    for page in pages
                    for r in page.get("organic_results") or []
                    if re.match(r"https://www\.glassdoor\.com/(Reviews|Overview)/[^?#]+\.htm$", r.get("link") or "")
                    and first_word in r["link"].lower()
                ),
                None,
            )
            if not glassdoor_url:
                return {}

            run = client.actor("johnvc/glassdoor-reviews-api").call(
                run_input={"companyUrls": [glassdoor_url], "maxReviewsPerCompany": 5}
            )
            reviews = [
                i
                for i in client.dataset(run["defaultDatasetId"]).list_items().items
                if i.get("result_type") == "review"
            ]
            ratings = [r["overallRating"] for r in reviews if isinstance(r.get("overallRating"), (int, float))]
            return {
                "glassdoor_url": glassdoor_url,
                "company_name": reviews[0].get("companyName") if reviews else None,
                "overall_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "reviews": reviews,
            }
        except Exception as e:
            print(f"[!] Apify Glassdoor warning: {e}")
        return {}

    @staticmethod
    def _fetch_diffbot(company_name: str, domain: Optional[str]) -> Dict[str, Any]:
        """Diffbot Knowledge Graph deep entity extraction."""
        token = getattr(config, "DIFFBOT_TOKEN", None) or os.getenv("DIFFBOT_TOKEN")
        if not token:
            return {}
        session = AccountServiceHTTPClient.get_session()
        try:
            base_url = "https://kg.diffbot.com/kg/v3/enhance"
            enc_comp = urllib.parse.quote_plus(company_name)
            url = f"{base_url}?token={token}&type=Organization&name={enc_comp}"
            res = session.get(url, timeout=12)
            if res.ok:
                data = res.json().get("data", [])
                if data:
                    entity = data[0].get("entity", {})
                    loc = entity.get("location", {}) if isinstance(entity.get("location"), dict) else {}
                    social = {
                        p.get("type"): p.get("url")
                        for p in entity.get("socialProfiles", [])
                        if isinstance(p, dict)
                    }

                    # Phone
                    phone_val = None
                    phone_numbers = entity.get("phoneNumbers", [])
                    if phone_numbers and isinstance(phone_numbers, list):
                        fp = phone_numbers[0]
                        phone_val = fp.get("string") or fp.get("digits") if isinstance(fp, dict) else str(fp)
                    if not phone_val:
                        phone_val = entity.get("phone")

                    # Email
                    email_val = None
                    email_addresses = entity.get("emailAddresses", [])
                    if email_addresses and isinstance(email_addresses, list):
                        fe = email_addresses[0]
                        email_val = fe.get("contactString") if isinstance(fe, dict) else str(fe)
                    if not email_val:
                        email_val = entity.get("contact_email") or entity.get("email")

                    # URLs
                    def _mk_url(u, pfx="https://"):
                        if not u:
                            return None
                        us = str(u).strip()
                        return us if us.startswith("http") else f"{pfx}{us}"

                    linkedin_val = _mk_url(social.get("linkedin") or entity.get("linkedInUri") or entity.get("linkedinUri"))
                    tw_uri = entity.get("twitterUri")
                    tw_url = _mk_url(social.get("twitter") or tw_uri)
                    tw_handle = None
                    if tw_uri:
                        handle_part = str(tw_uri).split("/")[-1].split("?")[0].strip()
                        tw_handle = f"@{handle_part}" if not handle_part.startswith("@") else handle_part
                    cb_url = _mk_url(social.get("crunchbase") or entity.get("crunchbaseUri"))
                    hp_url = entity.get("homepageUri")
                    blog_val = f"{hp_url.rstrip('/')}/newsroom" if hp_url else None

                    # Revenue
                    rev_obj = entity.get("revenue") or entity.get("yearlyRevenue")
                    rev_val = None
                    if isinstance(rev_obj, dict):
                        rev_val = rev_obj.get("value")
                    elif isinstance(rev_obj, (int, float)):
                        rev_val = float(rev_obj)
                    elif entity.get("yearlyRevenues"):
                        yrev = entity.get("yearlyRevenues", [{}])[0]
                        rev_val = yrev.get("revenue") or yrev.get("value")

                    # Industries & Keywords
                    ind_list = entity.get("industries", []) or []
                    cat_list = [c.get("name") for c in entity.get("categories", []) if isinstance(c, dict) and c.get("name")]
                    all_ind = list(dict.fromkeys(ind_list + cat_list))
                    desc_list = entity.get("descriptors", []) or []

                    subs = [s.get("name") for s in entity.get("subsidiaries", []) if isinstance(s, dict) and s.get("name")]

                    return {
                        "name": entity.get("name"),
                        "legal_name": entity.get("legalName") or entity.get("fullName"),
                        "domain": hp_url,
                        "website_url": hp_url,
                        "description": entity.get("description"),
                        "phone": phone_val,
                        "phone_number": phone_val,
                        "contact_email": email_val,
                        "street": loc.get("street") or loc.get("address"),
                        "street_address": loc.get("street") or loc.get("address"),
                        "city": (
                            loc.get("city", {}).get("name")
                            if isinstance(loc.get("city"), dict)
                            else loc.get("city")
                        ),
                        "state": (
                            loc.get("region", {}).get("name")
                            if isinstance(loc.get("region"), dict)
                            else loc.get("region")
                        ),
                        "country": (
                            loc.get("country", {}).get("name")
                            if isinstance(loc.get("country"), dict)
                            else loc.get("country")
                        ),
                        "postal_code": loc.get("postalCode"),
                        "nb_employees": entity.get("nbEmployees"),
                        "employee_count_range": (
                            f"{entity.get('nbEmployeesMin', '')}-{entity.get('nbEmployeesMax', '')}"
                            if entity.get('nbEmployeesMin')
                            else str(entity.get('nbEmployees', ''))
                        ),
                        "revenue": rev_val,
                        "yearly_revenue": rev_val,
                        "technologies": [
                            t.get("name") for t in entity.get("technologies", []) if isinstance(t, dict) and t.get("name")
                        ],
                        "competitors": [
                            c.get("name") for c in entity.get("competitors", []) if isinstance(c, dict) and c.get("name")
                        ],
                        "subsidiaries": subs,
                        "num_suborganizations": len(subs),
                        "patents_count": len(entity.get("patents", [])),
                        "linkedin_url": linkedin_val,
                        "twitter_url": tw_url,
                        "twitter_handle": tw_handle,
                        "crunchbase_url": cb_url,
                        "facebook_url": social.get("facebook") or entity.get("facebookUri"),
                        "github_url": social.get("github") or entity.get("githubUri"),
                        "glassdoor_url": social.get("glassdoor") or entity.get("glassdoorUri"),
                        "blog_url": blog_val,
                        "industries": all_ind,
                        "keywords": desc_list,
                        "_raw_diffbot": entity,
                    }
        except Exception as e:
            print(f"[!] Diffbot connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_serper(company_name: str, domain: Optional[str]) -> Dict[str, Any]:
        """Google Serper Live Web & Patents Indexing."""
        if not config.SERPER_API_KEY:
            return {}
        headers = {"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"}
        session = AccountServiceHTTPClient.get_session()
        try:
            res = session.post(
                "https://google.serper.dev/search",
                json={"q": f"{company_name} headquarters official website patents", "num": 5},
                headers=headers,
                timeout=10,
            )
            if res.ok:
                organic = res.json().get("organic", [])
                return {"organic_results": organic}
        except Exception as e:
            print(f"[!] Serper connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_wikipedia(company_name: str) -> Dict[str, Any]:
        """Free Wikipedia REST API."""
        session = AccountServiceHTTPClient.get_session()
        try:
            clean_q = re.sub(r"[\s,]+(Inc|LLC|Ltd|Corp|Corporation|Co)\.?$", "", company_name, flags=re.IGNORECASE).strip()
            wiki_slug = urllib.parse.quote(clean_q.replace(" ", "_"))
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_slug}"
            res = session.get(
                url, headers={"User-Agent": "SalesAIAgentResearch admin@salesai.com"}, timeout=8
            )
            if not res.ok and clean_q != company_name:
                orig_slug = urllib.parse.quote(company_name.replace(" ", "_"))
                url_orig = f"https://en.wikipedia.org/api/rest_v1/page/summary/{orig_slug}"
                res = session.get(url_orig, headers={"User-Agent": "SalesAIAgentResearch admin@salesai.com"}, timeout=8)
            if res.ok:
                data = res.json()
                return {
                    "title": data.get("title"),
                    "summary": data.get("extract"),
                    "wikipedia_url": data.get("content_urls", {}).get("desktop", {}).get("page"),
                }
        except Exception as e:
            print(f"[!] Wikipedia connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_openfec(company_name: str) -> Dict[str, Any]:
        """OpenFEC Political & PAC Committees API (100% Free)."""
        api_key = getattr(config, "DATA_GOV_API_KEY", None) or "DEMO_KEY"
        session = AccountServiceHTTPClient.get_session()
        try:
            base_url = "https://api.open.fec.gov/v1/committees/"
            enc_comp = urllib.parse.quote_plus(company_name)
            url = f"{base_url}?api_key={api_key}&q={enc_comp}&per_page=5"
            res = session.get(url, timeout=8)
            if res.ok:
                data = res.json()
                results = data.get("results", [])
                return {
                    "total_committees": data.get("pagination", {}).get("count", 0),
                    "committees": [
                        {
                            "name": c.get("name"),
                            "committee_id": c.get("committee_id"),
                            "designation": c.get("designation_full"),
                            "committee_type": c.get("committee_type_full"),
                            "party": c.get("party_full"),
                            "treasurer_name": c.get("treasurer_name"),
                        }
                        for c in results
                    ],
                    "_raw_fec": data,
                }
        except Exception as e:
            print(f"[!] OpenFEC connector warning: {e}")
        return {}

    @staticmethod
    def _fetch_firecrawl_account(domain: Optional[str], company_name: str) -> Dict[str, Any]:
        """Tier-1 Web Scraper (Firecrawl v1) for corporate overview, metadata, and leadership links."""
        if not getattr(config, "FIRECRAWL_API_KEY", None):
            return {}
        try:
            from services.firecrawl_service import FirecrawlService
            return FirecrawlService.scrape_account_overview(domain=domain, company_name=company_name)
        except Exception as e:
            print(f"[!] Firecrawl account connector warning: {e}")
            return {}

