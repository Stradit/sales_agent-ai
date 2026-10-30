"""Copilot settings (COPILOT_* env vars). Deliberately NOT named config.py — the
main app and apps/content_pipeline each already have a top-level `config`/`db`,
and colliding module names have caused real crashes here (README §2.4)."""

import os

import config as app_config  # main app config (BASE_DIR, OUTPUT_DIR; importing it loads the repo-root .env)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


# ── Embeddings (free, local) ──
EMBED_MODEL = _env("COPILOT_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIMS = int(_env("COPILOT_EMBED_DIMS", "384"))
CHUNKER_VERSION = "c1"
ATTRIBUTION_VERSION = "r1"
RENDER_VERSION = "v2"   # v2: mojibake repair in normalize()
CHUNK_MAX_WORDS = 280          # ~370 tokens incl. header — under bge-small's 512-token input limit
CHUNK_OVERLAP_WORDS = 40
MIN_POST_CHARS = 80            # L0 junk filter (README §6)

# ── Chroma ──
# Set COPILOT_CHROMA_URL (e.g. http://localhost:8010) to use a Chroma server;
# otherwise an embedded persistent store under output/chroma is used, owned by
# ONE process (the API server syncs in-process; see api.py /admin/sync).
CHROMA_URL = _env("COPILOT_CHROMA_URL", "")
CHROMA_PATH = str(app_config.OUTPUT_DIR / "chroma")


def collection_name() -> str:
    slug = EMBED_MODEL.split("/")[-1].lower()
    return f"sales_copilot__e{slug}-{EMBED_DIMS}__{CHUNKER_VERSION}__{ATTRIBUTION_VERSION}"


# ── LLM: any OpenAI-compatible chat-completions provider ──
# LLM_API_BASE / LLM_API_KEY pick the provider (e.g. https://heyroute.ai/v1). Unset, it is
# OpenRouter with OPENROUTER_API_KEY, as before. The OpenRouter key is only ever sent to
# OpenRouter. Copilot, call-prep and the content pipeline all read these same settings.
LLM_API_BASE = (_env("LLM_API_BASE") or "https://openrouter.ai/api/v1").rstrip("/")
IS_OPENROUTER = "openrouter.ai" in LLM_API_BASE
LLM_API_KEY = _env("LLM_API_KEY") or (_env("OPENROUTER_API_KEY") if IS_OPENROUTER else "")
LLM_CHAT_URL = f"{LLM_API_BASE}/chat/completions"
LLM_MODELS = [m.strip() for m in _env(
    "COPILOT_LLM_MODELS",
    "nvidia/nemotron-3-super-120b-a12b:free,qwen/qwen3.8-27b:free,google/gemma-4-31b-it:free",
).split(",") if m.strip()]
LLM_MAX_TOKENS = 700

# ── Quota (README §10.4) ──
DAILY_REQUEST_LIMIT = int(_env("OPENROUTER_DAILY_LIMIT", "50"))
USER_DAILY_TOKENS = int(_env("LLM_USER_DAILY_TOKENS", "90000"))
USER_DAILY_REQUEST_SHARE = float(_env("LLM_USER_DAILY_REQUEST_SHARE", "0.30"))   # 15 of 50
QUOTA_SPLIT = {"copilot": 0.40, "callprep": 0.10, "profiles": 0.20, "pool": 0.30}

# ── Background sync (README §7.2–7.4) ──
SYNC_INTERVAL_SECONDS = int(_env("COPILOT_SYNC_INTERVAL_SECONDS", "300"))   # drain the outbox every 5 min
FULL_RECONCILE_HOURS = int(_env("COPILOT_FULL_RECONCILE_HOURS", "24"))       # safety-net full diff + GC
AUTOSYNC = _env("COPILOT_AUTOSYNC", "1") not in ("0", "false", "False")

# ── Retention (README §8.1.1) ──
RETAIN_ENTITY_FULL_MONTHS = 13
RETAIN_ENTITY_QUARTERLY_YEARS = 3
RETAIN_EVENT_PREV_DAYS = 30
RETAIN_TOMBSTONE_DAYS = 30
RETAIN_CHAT_MONTHS = 12
RETAIN_DELETED_NOTES_DAYS = 30
ORPHAN_CHUNK_DAYS = 7
ENTITY_DOC_TYPES = ("persona_card", "callprep", "account_card", "lob_card", "personality_profile",
                    "digest_channel", "signal", "cxo_move", "job_theme")
MAX_NOTES_PER_USER = 500
NOTE_SIMILARITY_MIN = 0.55

# ── Retrieval / prompt budget ──
EVIDENCE_TOKENS = 2800
MAX_EVIDENCE_ITEMS = 8              # an upper limit, not a target: only sources that pass the gates below count
# Vector relevance gate (retrieve._vector): cosine distance cap, and how far behind the best match a hit may be
VECTOR_MAX_DISTANCE = float(_env("COPILOT_VECTOR_MAX_DISTANCE", "0.33"))
VECTOR_DISTANCE_BAND = float(_env("COPILOT_VECTOR_DISTANCE_BAND", "0.10"))
RECENT_TURNS = 6
MAX_NOTES_IN_PROMPT = 5
