"""Dynamic Configuration Service — Manages encrypted credentials, DB persistence,
masking, hot runtime reloading, and third-party API connection testing.
"""

import base64
import hashlib
import json
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key
from sqlalchemy.orm import Session

import config
from db.connection import get_session
from db.models import AuditLog, SystemApiConfig

logger = logging.getLogger("config_service")

# Project root .env path
ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

# Ensure .env is loaded
load_dotenv(ENV_FILE_PATH)


# ── Master Encryption Key Setup ─────────────────────────────────────
def _get_fernet_cipher() -> Fernet:
    """Derives a deterministic 32-byte url-safe base64 key from JWT_SECRET_KEY or fallback."""
    raw_secret = os.getenv("CONFIG_ENCRYPTION_KEY") or os.getenv("JWT_SECRET_KEY") or "sales-ai-agent-default-secret-salt-2026"
    digest = hashlib.sha256(raw_secret.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(digest)
    return Fernet(fernet_key)


_cipher = _get_fernet_cipher()


def encrypt_value(plain_text: Optional[str]) -> Optional[str]:
    """Encrypts a plaintext string to a Fernet token."""
    if not plain_text:
        return None
    try:
        return _cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return plain_text


def decrypt_value(cipher_text: Optional[str]) -> Optional[str]:
    """Decrypts a Fernet token back to plaintext."""
    if not cipher_text:
        return None
    try:
        return _cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except Exception:
        # Fallback if stored unencrypted
        return cipher_text


def mask_secret(value: Optional[str]) -> str:
    """Masks secret keys (e.g. tvly-dev-••••••••3DCys) dynamically for safe client transmission."""
    if not value or not isinstance(value, str):
        return ""
    val = value.strip()
    if len(val) <= 8:
        return "••••••••"
    if len(val) >= 24:
        return f"{val[:8]}••••••••••••••••{val[-4:]}"
    return f"{val[:4]}••••••••{val[-4:]}"


# ── Default Configuration Definitions ──────────────────────────────
DEFAULT_CONFIG_SPECS: List[Dict[str, Any]] = [
    # 1. LLM & AI Intelligence Gateways
    {
        "config_key": "PRIMARY_LLM_PROVIDER",
        "category": "llm",
        "display_name": "Active LLM Gateway",
        "is_secret": False,
        "default_val": "experiential_labs",
        "extra_metadata": {
            "options": ["experiential_labs", "openai", "gemini", "custom_openai_compatible"],
            "description": "Select the primary intelligence engine used for persona generation, digests, and signals."
        }
    },
    {
        "config_key": "EXPLABS_API_KEY",
        "category": "llm",
        "display_name": "Experiential Labs API Key",
        "is_secret": True,
        "default_val": os.getenv("EXPLABS_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Experiential Labs API key..."}
    },
    {
        "config_key": "EXPLABS_BASE_URL",
        "category": "llm",
        "display_name": "Experiential Labs Base URL",
        "is_secret": False,
        "default_val": os.getenv("EXPLABS_BASE_URL", "https://api.experientiallabs.ai/v1"),
        "extra_metadata": {"placeholder": "https://api.experientiallabs.ai/v1"}
    },
    {
        "config_key": "EXPLABS_DEFAULT_MODEL",
        "category": "llm",
        "display_name": "Experiential Labs Model",
        "is_secret": False,
        "default_val": os.getenv("EXPLABS_DEFAULT_MODEL", "gpt-4o-mini"),
        "extra_metadata": {
            "options": ["gpt-4o-mini", "gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro", "gemini-1.5-flash"],
            "description": "Default model for analysis and extraction routines."
        }
    },
    {
        "config_key": "OPENAI_API_KEY",
        "category": "llm",
        "display_name": "OpenAI API Key",
        "is_secret": True,
        "default_val": os.getenv("OPENAI_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter OpenAI API key (sk-...)"}
    },
    {
        "config_key": "OPENAI_MODEL",
        "category": "llm",
        "display_name": "OpenAI Direct Model",
        "is_secret": False,
        "default_val": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "extra_metadata": {
            "options": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "o3-mini"],
            "description": "Direct OpenAI model fallback."
        }
    },
    {
        "config_key": "GEMINI_API_KEY",
        "category": "llm",
        "display_name": "Google Gemini API Key",
        "is_secret": True,
        "default_val": os.getenv("GEMINI_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Google Gemini API key...", "docs_url": "https://aistudio.google.com"}
    },
    {
        "config_key": "MONID_API_KEY",
        "category": "llm",
        "display_name": "Monid.ai Gateway API Key",
        "is_secret": True,
        "default_val": os.getenv("MONID_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Monid.ai API key...", "docs_url": "https://monid.ai"}
    },
    {
        "config_key": "MONID_BASE_URL",
        "category": "llm",
        "display_name": "Monid.ai Base URL",
        "is_secret": False,
        "default_val": os.getenv("MONID_BASE_URL", "https://api.monid.ai/v1"),
        "extra_metadata": {"placeholder": "https://api.monid.ai/v1"}
    },

    # 2. Search & Data Enrichment APIs
    {
        "config_key": "TAVILY_API_KEY",
        "category": "enrichment",
        "display_name": "Tavily Search API Key",
        "is_secret": True,
        "default_val": os.getenv("TAVILY_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Tavily API key...", "docs_url": "https://tavily.com"}
    },
    {
        "config_key": "APIFY_TOKEN",
        "category": "enrichment",
        "display_name": "Apify Scraping Token",
        "is_secret": True,
        "default_val": os.getenv("APIFY_TOKEN", ""),
        "extra_metadata": {"placeholder": "Enter Apify API token...", "docs_url": "https://apify.com"}
    },
    {
        "config_key": "SERPER_API_KEY",
        "category": "enrichment",
        "display_name": "Serper Google Search API Key",
        "is_secret": True,
        "default_val": os.getenv("SERPER_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Serper API key...", "docs_url": "https://serper.dev"}
    },
    {
        "config_key": "EXA_API_KEY",
        "category": "enrichment",
        "display_name": "Exa Neural Search API Key",
        "is_secret": True,
        "default_val": os.getenv("EXA_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Exa API key...", "docs_url": "https://exa.ai"}
    },
    {
        "config_key": "FIRECRAWL_API_KEY",
        "category": "enrichment",
        "display_name": "Firecrawl Web Extraction API Key",
        "is_secret": True,
        "default_val": os.getenv("FIRECRAWL_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Firecrawl API key...", "docs_url": "https://firecrawl.dev"}
    },
    {
        "config_key": "DIFFBOT_TOKEN",
        "category": "enrichment",
        "display_name": "Diffbot Knowledge Graph Token",
        "is_secret": True,
        "default_val": os.getenv("DIFFBOT_TOKEN", ""),
        "extra_metadata": {"placeholder": "Enter Diffbot API token...", "docs_url": "https://diffbot.com"}
    },
    {
        "config_key": "FULLENRICH_API_KEY",
        "category": "enrichment",
        "display_name": "FullEnrich Persona Enrichment API Key",
        "is_secret": True,
        "default_val": os.getenv("FULLENRICH_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter FullEnrich API key...", "docs_url": "https://fullenrich.com"}
    },
    {
        "config_key": "DATA_GOV_API_KEY",
        "category": "enrichment",
        "display_name": "Data.gov / SEC EDGAR API Key",
        "is_secret": True,
        "default_val": os.getenv("DATA_GOV_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Data.gov API key...", "docs_url": "https://data.gov"}
    },
    {
        "config_key": "FINNHUB_API_KEY",
        "category": "enrichment",
        "display_name": "Finnhub Financial Market API Key",
        "is_secret": True,
        "default_val": os.getenv("FINNHUB_API_KEY", ""),
        "extra_metadata": {"placeholder": "Enter Finnhub API key...", "docs_url": "https://finnhub.io"}
    },

    # 3. Email & Notifications (SMTP)
    {
        "config_key": "SMTP_HOST",
        "category": "email",
        "display_name": "SMTP Host",
        "is_secret": False,
        "default_val": os.getenv("SMTP_HOST", ""),
        "extra_metadata": {"placeholder": "smtp.sendgrid.net or email-smtp.us-east-1.amazonaws.com"}
    },
    {
        "config_key": "SMTP_PORT",
        "category": "email",
        "display_name": "SMTP Port",
        "is_secret": False,
        "default_val": os.getenv("SMTP_PORT", "587"),
        "extra_metadata": {"options": ["587", "465", "25", "2525"]}
    },
    {
        "config_key": "SMTP_USERNAME",
        "category": "email",
        "display_name": "SMTP Username",
        "is_secret": False,
        "default_val": os.getenv("SMTP_USERNAME", "") or os.getenv("SMTP_USER", ""),
        "extra_metadata": {"placeholder": "apikey or user@domain.com"}
    },
    {
        "config_key": "SMTP_PASSWORD",
        "category": "email",
        "display_name": "SMTP Password / API Key",
        "is_secret": True,
        "default_val": os.getenv("SMTP_PASSWORD", ""),
        "extra_metadata": {"placeholder": "Enter SMTP password..."}
    },
    {
        "config_key": "SMTP_FROM",
        "category": "email",
        "display_name": "Sender 'From' Address",
        "is_secret": False,
        "default_val": os.getenv("SMTP_FROM", "") or os.getenv("SMTP_USERNAME", ""),
        "extra_metadata": {"placeholder": "Doshin Alerts <alerts@company.com>"}
    },

    # 4. Security & System Limits
    {
        "config_key": "JWT_SECRET_KEY",
        "category": "system",
        "display_name": "JWT Master Secret Key",
        "is_secret": True,
        "default_val": os.getenv("JWT_SECRET_KEY", ""),
        "extra_metadata": {"placeholder": "Enter JWT secret key...", "description": "Master cryptographic salt for signing JWT tokens."}
    },
    {
        "config_key": "HTTP_TIMEOUT_SECONDS",
        "category": "system",
        "display_name": "Default HTTP Timeout (Seconds)",
        "is_secret": False,
        "default_val": "30",
        "extra_metadata": {"min": 5, "max": 180, "type": "number"}
    },
    {
        "config_key": "MAX_API_RETRIES",
        "category": "system",
        "display_name": "Max Network Retries",
        "is_secret": False,
        "default_val": "3",
        "extra_metadata": {"min": 1, "max": 10, "type": "number"}
    }
]


# ── In-Memory Cached State & Invalidation ──────────────────────────
_CONFIG_CACHE: Dict[str, str] = {}
_LAST_CACHE_REFRESH: float = 0.0
_CACHE_TTL_SECONDS = 60.0


def initialize_default_configs(db: Session) -> None:
    """Seeds default configurations in the database if not already existing."""
    for spec in DEFAULT_CONFIG_SPECS:
        existing = db.query(SystemApiConfig).filter(SystemApiConfig.config_key == spec["config_key"]).first()
        if not existing:
            raw_val = spec["default_val"]
            enc_val = encrypt_value(raw_val) if (spec["is_secret"] and raw_val) else raw_val
            new_item = SystemApiConfig(
                config_key=spec["config_key"],
                category=spec["category"],
                display_name=spec["display_name"],
                encrypted_value=enc_val,
                is_secret=spec["is_secret"],
                extra_metadata=spec.get("extra_metadata", {})
            )
            db.add(new_item)
        else:
            # Sync metadata & display name updates
            existing.extra_metadata = spec.get("extra_metadata", {})
            existing.display_name = spec["display_name"]
    db.commit()


def get_all_configs_dto(db: Session) -> List[Dict[str, Any]]:
    """Fetches all config items formatted for the Super Admin frontend UI (empty secret values, clean boolean flag)."""
    initialize_default_configs(db)
    items = db.query(SystemApiConfig).order_by(SystemApiConfig.id.asc()).all()

    result = []
    for item in items:
        raw_val = decrypt_value(item.encrypted_value) if item.is_secret else item.encrypted_value
        is_set = bool(raw_val and raw_val.strip())
        val_to_send = "" if item.is_secret else (raw_val or "")
        masked = mask_secret(raw_val) if (item.is_secret and is_set) else ""

        result.append({
            "id": item.id,
            "config_key": item.config_key,
            "category": item.category,
            "display_name": item.display_name,
            "value": val_to_send,
            "masked_value": masked,
            "is_secret": item.is_secret,
            "is_configured": is_set,
            "extra_metadata": item.extra_metadata or {},
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "updated_by": item.updated_by
        })
    return result


def get_config_value(key: str, default: Optional[str] = None, db: Optional[Session] = None) -> Optional[str]:
    """Retrieves a decrypted config value from DB (with caching) or falls back to os.getenv."""
    global _CONFIG_CACHE, _LAST_CACHE_REFRESH
    now = time.time()

    if now - _LAST_CACHE_REFRESH > _CACHE_TTL_SECONDS or key not in _CONFIG_CACHE:
        try:
            should_close = False
            if db is None:
                db = get_session()
                should_close = True

            record = db.query(SystemApiConfig).filter(SystemApiConfig.config_key == key).first()
            if record and record.encrypted_value:
                val = decrypt_value(record.encrypted_value) if record.is_secret else record.encrypted_value
                _CONFIG_CACHE[key] = val
            else:
                _CONFIG_CACHE[key] = os.getenv(key, default)

            if should_close:
                db.close()
        except Exception as e:
            logger.warning(f"Could not read config {key} from DB: {e}. Using env fallback.")
            return os.getenv(key, default)

    return _CONFIG_CACHE.get(key, default) or default


def update_configs(updates: Dict[str, Any], user_id: int, db: Session) -> Tuple[bool, str]:
    """Updates multiple config keys, encrypts secrets, updates runtime in-memory config, and records audit log."""
    global _CONFIG_CACHE, _LAST_CACHE_REFRESH
    updated_keys = []

    for key, new_val in updates.items():
        if new_val is None:
            continue
        val_str = str(new_val).strip()

        # If it's a masked placeholder string (e.g. ••••••••), do not overwrite with masked text
        if "••••" in val_str:
            continue

        record = db.query(SystemApiConfig).filter(SystemApiConfig.config_key == key).first()
        if record:
            enc_val = encrypt_value(val_str) if record.is_secret else val_str
            record.encrypted_value = enc_val
            record.updated_by = user_id
            record.updated_at = datetime.now(timezone.utc)
            updated_keys.append(key)
            _CONFIG_CACHE[key] = val_str

    if updated_keys:
        # Audit Log
        audit = AuditLog(
            actor_user_id=user_id,
            action="API_CONFIG_UPDATED",
            details={"updated_keys": updated_keys}
        )
        db.add(audit)
        db.commit()
        _LAST_CACHE_REFRESH = time.time()

        # Write directly to physical .env file so it stays synchronized
        try:
            if not os.path.exists(ENV_FILE_PATH):
                with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
                    pass
            for key in updated_keys:
                val_to_write = _CONFIG_CACHE.get(key, "")
                # Set in .env file
                set_key(
                    ENV_FILE_PATH,
                    key,
                    val_to_write,
                    quote_mode="always" if (" " in val_to_write or "#" in val_to_write) else "auto"
                )
                # Sync process environment
                os.environ[key] = val_to_write
            logger.info(f"Updated {len(updated_keys)} keys in {ENV_FILE_PATH}")
        except Exception as env_err:
            logger.error(f"Failed to synchronize .env file: {env_err}")

        # Hot-reload runtime config attributes
        apply_runtime_hot_reload()

    return True, f"Successfully updated {len(updated_keys)} configuration settings."


def apply_runtime_hot_reload() -> None:
    """Refreshes global variables in config.py dynamically."""
    try:
        provider = get_config_value("PRIMARY_LLM_PROVIDER", "experiential_labs")
        explabs_key = get_config_value("EXPLABS_API_KEY", "")
        explabs_url = get_config_value("EXPLABS_BASE_URL", "https://api.experientiallabs.ai/v1")
        explabs_model = get_config_value("EXPLABS_DEFAULT_MODEL", "gpt-4o-mini")
        openai_key = get_config_value("OPENAI_API_KEY", "")
        openai_model = get_config_value("OPENAI_MODEL", "gpt-4o-mini")
        gemini_key = get_config_value("GEMINI_API_KEY", "")
        monid_key = get_config_value("MONID_API_KEY", "")
        monid_url = get_config_value("MONID_BASE_URL", "https://api.monid.ai/v1")

        config.EXPLABS_API_KEY = explabs_key
        config.EXPLABS_BASE_URL = explabs_url
        config.EXPLABS_DEFAULT_MODEL = explabs_model
        config.OPENAI_API_KEY = openai_key
        config.OPENAI_MODEL = openai_model
        config.GEMINI_API_KEY = gemini_key
        config.MONID_API_KEY = monid_key
        config.MONID_BASE_URL = monid_url

        if provider == "openai" or (not explabs_key and openai_key):
            config.LLM_API_KEY = openai_key
            config.LLM_BASE_URL = "https://api.openai.com/v1"
            config.LLM_MODEL = openai_model
        elif provider == "gemini":
            config.LLM_API_KEY = gemini_key
            config.LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
            config.LLM_MODEL = "gemini-1.5-pro"
        else:
            config.LLM_API_KEY = explabs_key or openai_key
            config.LLM_BASE_URL = explabs_url
            config.LLM_MODEL = explabs_model

        config.TAVILY_API_KEY = get_config_value("TAVILY_API_KEY", "")
        config.APIFY_TOKEN = get_config_value("APIFY_TOKEN", "")
        config.SERPER_API_KEY = get_config_value("SERPER_API_KEY", "")
        config.EXA_API_KEY = get_config_value("EXA_API_KEY", "")
        config.FIRECRAWL_API_KEY = get_config_value("FIRECRAWL_API_KEY", "")
        config.DIFFBOT_TOKEN = get_config_value("DIFFBOT_TOKEN", "")
        config.FULLENRICH_API_KEY = get_config_value("FULLENRICH_API_KEY", "")
        config.DATA_GOV_API_KEY = get_config_value("DATA_GOV_API_KEY", "")

        logger.info("Runtime configurations hot-reloaded successfully.")
    except Exception as e:
        logger.error(f"Error applying runtime config hot reload: {e}")


# ── Live Connection Testers ─────────────────────────────────────────
def test_provider_connection(provider: str, custom_params: Optional[Dict[str, Any]] = None, db: Optional[Session] = None) -> Dict[str, Any]:
    """Pings a third-party service with uncommitted or currently configured credentials."""
    params = custom_params or {}
    start_time = time.time()

    try:
        if provider in ["openai", "llm"]:
            api_key = params.get("OPENAI_API_KEY") or get_config_value("OPENAI_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "OpenAI API Key is not configured."}
            
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200:
                models = [m.get("id") for m in resp.json().get("data", []) if "gpt" in m.get("id", "")]
                return {"success": True, "latency_ms": latency, "message": f"Connected to OpenAI. {len(models)} models available."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"OpenAI error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "experiential_labs":
            api_key = params.get("EXPLABS_API_KEY") or get_config_value("EXPLABS_API_KEY", "", db)
            base_url = params.get("EXPLABS_BASE_URL") or get_config_value("EXPLABS_BASE_URL", "https://api.experientiallabs.ai/v1", db)
            if not api_key:
                return {"success": False, "error": "Experiential Labs API Key is not configured."}

            headers = {"Authorization": f"Bearer {api_key}"}
            models_url = f"{base_url.rstrip('/')}/models"
            resp = requests.get(models_url, headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 201]:
                return {"success": True, "latency_ms": latency, "message": "Successfully authenticated with Experiential Labs Gateway."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Gateway error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "gemini":
            api_key = params.get("GEMINI_API_KEY") or get_config_value("GEMINI_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Gemini API Key is not configured."}

            resp = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}", timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200:
                models = [m.get("name") for m in resp.json().get("models", [])]
                return {"success": True, "latency_ms": latency, "message": f"Connected to Google Gemini. {len(models)} models available."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Gemini error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "monid":
            api_key = params.get("MONID_API_KEY") or get_config_value("MONID_API_KEY", "", db)
            base_url = params.get("MONID_BASE_URL") or get_config_value("MONID_BASE_URL", "https://api.monid.ai/v1", db)
            if not api_key:
                return {"success": False, "error": "Monid API Key is not configured."}

            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(f"{base_url.rstrip('/')}/health", headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 204]:
                return {"success": True, "latency_ms": latency, "message": "Connected to Monid.ai gateway."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Monid error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "tavily":
            api_key = params.get("TAVILY_API_KEY") or get_config_value("TAVILY_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Tavily API Key is not configured."}

            payload = {"api_key": api_key, "query": "ping", "search_depth": "basic", "max_results": 1}
            resp = requests.post("https://api.tavily.com/search", json=payload, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200:
                return {"success": True, "latency_ms": latency, "message": "Tavily search API verified successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Tavily error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "apify":
            token = params.get("APIFY_TOKEN") or get_config_value("APIFY_TOKEN", "", db)
            if not token:
                return {"success": False, "error": "Apify Token is not configured."}

            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get("https://api.apify.com/v2/users/me", headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200:
                user_info = resp.json().get("data", {}).get("username", "verified")
                return {"success": True, "latency_ms": latency, "message": f"Apify verified for user '{user_info}'."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Apify error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "serper":
            api_key = params.get("SERPER_API_KEY") or get_config_value("SERPER_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Serper API Key is not configured."}

            headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
            resp = requests.post("https://google.serper.dev/search", json={"q": "ping"}, headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200:
                return {"success": True, "latency_ms": latency, "message": "Serper search API authenticated successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Serper error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "fullenrich":
            api_key = params.get("FULLENRICH_API_KEY") or get_config_value("FULLENRICH_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "FullEnrich API Key is not configured."}

            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get("https://app.fullenrich.com/api/v1/user", headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 204]:
                return {"success": True, "latency_ms": latency, "message": "FullEnrich API authenticated successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"FullEnrich error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "diffbot":
            token = params.get("DIFFBOT_TOKEN") or get_config_value("DIFFBOT_TOKEN", "", db)
            if not token:
                return {"success": False, "error": "Diffbot Token is not configured."}

            resp = requests.get(f"https://api.diffbot.com/v3/organization?token={token}&url=diffbot.com", timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 201]:
                return {"success": True, "latency_ms": latency, "message": "Diffbot Knowledge Graph API verified successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Diffbot error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "data_gov":
            api_key = params.get("DATA_GOV_API_KEY") or get_config_value("DATA_GOV_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Data.gov API Key is not configured."}

            resp = requests.get(f"https://api.gsa.gov/analytics/dap/v1.1/reports/summary/data?api_key={api_key}&limit=1", timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 201, 204]:
                return {"success": True, "latency_ms": latency, "message": "Data.gov API key authenticated successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Data.gov error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "firecrawl":
            api_key = params.get("FIRECRAWL_API_KEY") or get_config_value("FIRECRAWL_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Firecrawl API Key is not configured."}

            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get("https://api.firecrawl.dev/v1/team/credit-usage", headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 201]:
                return {"success": True, "latency_ms": latency, "message": "Firecrawl API verified successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Firecrawl error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "exa":
            api_key = params.get("EXA_API_KEY") or get_config_value("EXA_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Exa API Key is not configured."}

            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            resp = requests.post("https://api.exa.ai/search", json={"query": "test", "numResults": 1}, headers=headers, timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code in [200, 201]:
                return {"success": True, "latency_ms": latency, "message": "Exa Neural Search API authenticated successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Exa error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "finnhub":
            api_key = params.get("FINNHUB_API_KEY") or get_config_value("FINNHUB_API_KEY", "", db)
            if not api_key:
                return {"success": False, "error": "Finnhub API Key is not configured."}

            resp = requests.get(f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={api_key}", timeout=10)
            latency = int((time.time() - start_time) * 1000)
            if resp.status_code == 200 and resp.json().get("c", 0) > 0:
                return {"success": True, "latency_ms": latency, "message": "Finnhub Market Data API authenticated successfully."}
            else:
                return {"success": False, "latency_ms": latency, "error": f"Finnhub error (HTTP {resp.status_code}): {resp.text[:120]}"}

        elif provider == "smtp":
            host = params.get("SMTP_HOST") or get_config_value("SMTP_HOST", "", db)
            port = int(params.get("SMTP_PORT") or get_config_value("SMTP_PORT", "587", db))
            user = params.get("SMTP_USERNAME") or get_config_value("SMTP_USERNAME", "", db)
            password = params.get("SMTP_PASSWORD") or get_config_value("SMTP_PASSWORD", "", db)

            if not host:
                return {"success": False, "error": "SMTP Host is not configured."}

            server = smtplib.SMTP(host, port, timeout=10)
            server.ehlo()
            if port == 587:
                server.starttls()
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.quit()

            latency = int((time.time() - start_time) * 1000)
            return {"success": True, "latency_ms": latency, "message": f"SMTP handshake with {host}:{port} completed successfully."}

        else:
            return {"success": False, "error": f"No tester implemented for provider '{provider}'."}

    except Exception as e:
        latency = int((time.time() - start_time) * 1000)
        return {"success": False, "latency_ms": latency, "error": str(e)}
