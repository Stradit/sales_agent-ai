"""Share Policy Enforcer — Strict Deny-by-Default Policy Enforcement & PII Sanitization (S-12).

Architecture:
1. Canonical policy loading & SHA-256 fingerprinting.
2. Manifest skeleton version discovery.
3. Recursive JSON PII scrubber (drops sensitive keys, redacts email/phone patterns).
4. Contact fields switchboard vs personal domain evaluation (B6.5).
5. Dynamic schema introspection to detect unapproved columns and log policy drift warnings.
"""

from datetime import datetime, date, timezone, timedelta
import hashlib
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("share_policy")

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "tenant_skeleton" / "share_policy.yaml"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "tenant_skeleton" / "manifest.yaml"

_CACHED_POLICY: Optional[Dict[str, Any]] = None
_CACHED_POLICY_HASH: Optional[str] = None
_CACHED_MANIFEST_VERSION: Optional[str] = None

# Blacklist key substrings in JSON objects that must be removed entirely (case-insensitive)
PII_KEY_SUBSTRINGS = (
    "email",
    "phone",
    "ssn",
    "password",
    "token",
    "secret",
    "bearer",
    "auth",
    "cookie",
    "salary",
    "compensation",
)

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
# Matches E.164 and international/US phone formats with separators
PHONE_REGEX = re.compile(r'(?:\+?\d{1,3}[-.\s]+)?(?:\(?\d{2,4}\)?[-.\s]+)?\d{3,4}[-.\s]+\d{3,4}\b')

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "protonmail.com",
    "zoho.com",
    "mail.com",
    "gmx.com",
    "yandex.com",
    "live.com",
}

SWITCHBOARD_LOCAL_PREFIXES = {
    "info",
    "contact",
    "support",
    "sales",
    "press",
    "media",
    "ir",
    "investors",
    "hello",
    "office",
    "general",
    "careers",
    "jobs",
    "inquiries",
    "help",
    "admin",
}


def compute_policy_hash() -> str:
    """Computes stable SHA-256 fingerprint of the canonical share_policy.yaml."""
    global _CACHED_POLICY_HASH
    if _CACHED_POLICY_HASH is None:
        if not POLICY_PATH.exists():
            raise FileNotFoundError(f"Share policy configuration missing at: {POLICY_PATH}")
        with open(POLICY_PATH, "rb") as f:
            _CACHED_POLICY_HASH = hashlib.sha256(f.read()).hexdigest()
    return _CACHED_POLICY_HASH


def get_manifest_skeleton_version() -> str:
    """Reads skeleton_version dynamically from manifest.yaml."""
    global _CACHED_MANIFEST_VERSION
    if _CACHED_MANIFEST_VERSION is None:
        if not MANIFEST_PATH.exists():
            raise FileNotFoundError(f"Tenant manifest missing at: {MANIFEST_PATH}")
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest_data = yaml.safe_load(f)
            _CACHED_MANIFEST_VERSION = str(manifest_data.get("skeleton_version", "T0002"))
    return _CACHED_MANIFEST_VERSION


def scrub_json_pii(val: Any) -> Any:
    """Recursively walks arbitrary JSON structures to strip PII.
    
    1. Removes any dictionary key containing sensitive substrings (email, phone, ssn, password, token, etc.).
    2. Replaces string values matching email or phone regex with [REDACTED_EMAIL] and [REDACTED_PHONE].
    """
    if isinstance(val, dict):
        clean_dict = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(sub in k_lower for sub in PII_KEY_SUBSTRINGS):
                # Drop sensitive key completely
                continue
            clean_dict[k] = scrub_json_pii(v)
        return clean_dict

    if isinstance(val, (list, tuple)):
        return [scrub_json_pii(item) for item in val]

    if isinstance(val, str):
        s = EMAIL_REGEX.sub("[REDACTED_EMAIL]", val)
        # Redact phone pattern if it contains digits
        s = PHONE_REGEX.sub("[REDACTED_PHONE]", s)
        return s

    return val


def scrub_record_json_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Applies recursive PII scrubbing to all JSON/JSONB (dict/list) columns in a record."""
    if not isinstance(record, dict):
        return record
    out = {}
    for k, v in record.items():
        if isinstance(v, (dict, list)):
            out[k] = scrub_json_pii(v)
        else:
            out[k] = v
    return out


def sanitize_contact_email(email: Optional[str], company_domain: Optional[str]) -> Optional[str]:
    """Sanitizes contact email per B6.5 rules.
    
    - Keeps company switchboard emails (e.g. info@acme.com, contact@acme.com).
    - Drops personal domain emails (gmail.com, yahoo.com, etc.).
    - Drops personal individual emails not matching switchboard prefixes.
    """
    if not email or not isinstance(email, str):
        return None
    email_clean = email.strip()
    if "@" not in email_clean:
        return None
    
    local_part, domain_part = email_clean.lower().split("@", 1)
    if domain_part in PERSONAL_EMAIL_DOMAINS:
        return None

    if local_part in SWITCHBOARD_LOCAL_PREFIXES:
        return email_clean

    if company_domain:
        norm_cdom = company_domain.strip().lower().replace("www.", "")
        if domain_part == norm_cdom or domain_part.endswith("." + norm_cdom):
            # Check if local part looks like an individual name (contains '.' or '_')
            if "." in local_part or "_" in local_part:
                return None
            return email_clean

    return None


def sanitize_phone_number(phone: Optional[str], is_switchboard: bool = True) -> Optional[str]:
    """Sanitizes company phone number per B6.5 rules."""
    if not phone or not isinstance(phone, str):
        return None
    phone_clean = phone.strip()
    if is_switchboard:
        return phone_clean
    return "[REDACTED_PHONE]"


def get_table_columns_in_db(session: Session, schema: str, table: str) -> List[str]:
    """Retrieves all column names for table in schema."""
    rows = session.execute(
        text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :s AND table_name = :t
            ORDER BY ordinal_position
        """),
        {"s": schema, "t": table},
    ).fetchall()
    return [r[0] for r in rows]
