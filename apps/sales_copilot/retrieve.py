"""Retrieval (README §9.2): Chroma vector search + Postgres full-text search,
fused with Reciprocal Rank Fusion, then verified against Postgres so only
chunks of CURRENT documents on accounts the user may see are ever returned."""

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

import auth
from apps.sales_copilot import embed, settings, store

RRF_K = 60
NEWSY = {"news", "linkedin_post", "reddit", "blog", "social", "cxo_move", "signal", "filing", "patent"}
STOP = set("""a an the and or of to in on for with at by from is are was were be been it its this that these those
what which who whom whose how why when where do does did can could should would will shall may might me my
our your their his her them they he she we you i about tell give show list any some all into than then there
please""".split())


def all_account_ids(session) -> List[int]:
    return [r[0] for r in session.execute(text("SELECT id FROM accounts")).fetchall()]


def acl_account_ids(session, user) -> List[int]:
    """Accounts this user may see — same rule as auth.require_account_access (README §11.1)."""
    return auth.account_scope(session, user)


def _or_tsquery(q: str, drop: set) -> Optional[str]:
    words = [w for w in re.findall(r"[a-zA-Z0-9]{2,}", q.lower()) if w not in STOP and w not in drop]
    return " | ".join(dict.fromkeys(words)) if words else None


def _account_terms(session, account_ids: List[int]) -> set:
    """Words of account names/aliases. The account filter already scopes results, and
    "BNY" matches nearly every BNY chunk, so as a keyword it only dilutes ranking."""
    rows = session.execute(text("SELECT display_name, legal_name, aliases FROM accounts WHERE id = ANY(:a)"),
                           {"a": account_ids}).fetchall()
    terms = set()
    for dn, ln, aliases in rows:
        for n in [dn, ln, *(aliases or [])]:
            terms.update(re.findall(r"[a-z0-9]{2,}", (n or "").lower()))
    return terms


def _keyword(session, q: str, acl: List[int], limit: int, drop: set = frozenset()) -> List[bytes]:
    tsq = _or_tsquery(q, drop)
    if not tsq:
        return []
    terms = tsq.split(" | ")
    # Coverage first: a chunk matching more of the distinct query words beats a long chunk that
    # repeats one common word (ts_rank alone favours the latter); ts_rank breaks ties.
    rows = session.execute(text("""
        SELECT c.chunk_hash,
               (SELECT count(*) FROM unnest(CAST(:terms AS text[])) t WHERE c.tsv @@ to_tsquery('english', t)) AS cov,
               max(ts_rank_cd(c.tsv, to_tsquery('english', :q))) AS r
        FROM rag_chunks c
        JOIN rag_document_chunks dc ON dc.chunk_hash = c.chunk_hash
        JOIN rag_documents d ON d.id = dc.document_id AND d.is_current AND d.deleted_at IS NULL
        JOIN rag_document_entities e ON e.canonical_key = d.canonical_key AND e.account_id = ANY(:acl)
        WHERE c.tsv @@ to_tsquery('english', :q)
        GROUP BY c.chunk_hash ORDER BY cov DESC, r DESC LIMIT :lim"""), {"q": tsq, "terms": terms, "acl": acl, "lim": limit}).fetchall()
    return [bytes(r[0]) for r in rows]


def _vector(q: str, acl: List[int], limit: int, since_ts: Optional[int]) -> List[bytes]:
    where: Dict[str, Any] = {"account_id": {"$in": [int(a) for a in acl]}}
    if since_ts:
        where = {"$and": [where, {"published_ts": {"$gte": int(since_ts)}}]}
    hits = store.query(settings.collection_name(), embed.embed_query(q), limit, where)
    seen, out = set(), []
    for h in hits:
        if h["chunk_hash"] not in seen:
            seen.add(h["chunk_hash"])
            out.append(h["chunk_hash"])
    return out


def search(session, q: str, acl: List[int], persona_id: Optional[int] = None,
           account_ids: Optional[List[int]] = None, since: Optional[datetime] = None,
           limit: int = settings.MAX_EVIDENCE_ITEMS, doc_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    scope = [a for a in (account_ids or acl) if a in set(acl)]
    if not scope:
        return []
    since_ts = int(since.timestamp()) if since else None
    try:
        vec = _vector(q, scope, 60, since_ts)
    except Exception as e:   # Chroma unavailable -> keyword-only (README §19.4 "limited search")
        print(f"[copilot] vector search unavailable, keyword only: {e}")
        vec = []
    kw = _keyword(session, q, scope, 60, drop=_account_terms(session, scope))
    person_tokens: List[str] = []
    if persona_id:
        name = session.execute(text("SELECT coalesce(full_name, display_name) FROM personas WHERE id = :p"),
                               {"p": persona_id}).scalar() or ""
        person_tokens = [t for t in re.findall(r"[a-z]{3,}", name.lower())][-1:]   # last name

    scores: Dict[bytes, float] = {}
    for ranked in (vec, kw):
        for rank, h in enumerate(ranked):
            scores[h] = scores.get(h, 0.0) + 1.0 / (RRF_K + rank + 1)
    if persona_id:
        # Always consider the persona's own documents, even if neither leg ranked them.
        for (h,) in session.execute(text("""
                SELECT DISTINCT dc.chunk_hash FROM rag_document_entities e
                JOIN rag_documents d ON d.canonical_key = e.canonical_key AND d.is_current AND d.deleted_at IS NULL
                JOIN rag_document_chunks dc ON dc.document_id = d.id
                WHERE e.persona_id = :p AND e.confidence >= 0.5"""), {"p": persona_id}).fetchall():
            scores.setdefault(bytes(h), 1.0 / (RRF_K + 60))
    if not scores:
        return []

    # Verify in Postgres: current doc, ACL, persona link; fetch display fields.
    rows = session.execute(text("""
        SELECT dc.chunk_hash, c.text, d.id, d.version, d.canonical_key, d.doc_type, d.title, d.url, d.published_at, d.simhash,
               array_agg(DISTINCT e.account_id) AS accounts,
               array_agg(DISTINCT e.persona_id) FILTER (WHERE e.persona_id IS NOT NULL AND e.confidence >= 0.5) AS personas
        FROM rag_document_chunks dc
        JOIN rag_chunks c ON c.chunk_hash = dc.chunk_hash
        JOIN rag_documents d ON d.id = dc.document_id AND d.is_current AND d.deleted_at IS NULL
        JOIN rag_document_entities e ON e.canonical_key = d.canonical_key AND e.account_id = ANY(:acl)
        WHERE dc.chunk_hash = ANY(:hashes)
        GROUP BY dc.chunk_hash, c.text, d.id"""), {"acl": scope, "hashes": list(scores)}).fetchall()

    now = datetime.now(timezone.utc)
    best_per_doc: Dict[int, Dict[str, Any]] = {}
    for h, body, doc_id, ver, ckey, dtype, title, url, pub, simhash, accts, personas in rows:
        if doc_types and dtype not in doc_types:
            continue
        s = scores[bytes(h)]
        personas = personas or []
        curated = dtype in ("persona_card", "callprep", "personality_profile", "digest_channel")
        if persona_id and persona_id in personas:
            s *= 3.0 if curated else 2.0      # the person's own curated docs first
            if not curated and person_tokens and not any(t in body.lower() for t in person_tokens):
                s *= 0.3                      # long article linked to them, but THIS chunk doesn't name them
        elif persona_id and dtype in ("persona_card", "callprep", "personality_profile"):
            s *= 0.3            # another person's card: rarely what a person-scoped question wants
        if pub and dtype in NEWSY:
            age = max((now - pub).days, 0)
            s *= 1 + 0.3 * math.exp(-age / 30)
        hit = {"chunk_hash": bytes(h), "text": body, "document_id": doc_id, "version": ver, "canonical_key": ckey,
               "doc_type": dtype, "title": title or "", "url": url, "published_at": pub, "accounts": accts,
               "personas": personas, "score": s, "simhash": simhash, "snippet": body.split("\n", 1)[-1][:300]}
        if doc_id not in best_per_doc or best_per_doc[doc_id]["score"] < s:
            best_per_doc[doc_id] = hit          # one chunk per document keeps results diverse
    ranked = []
    for hit in sorted(best_per_doc.values(), key=lambda x: -x["score"]):
        # L5: syndicated copies of the same story (SimHash within 3 bits) collapse to the best one
        sh = hit["simhash"]
        if sh is not None and any(k["simhash"] is not None and bin((sh ^ k["simhash"]) & (2 ** 64 - 1)).count("1") <= 3
                                  for k in ranked):
            continue
        ranked.append(hit)

    # Token budget
    out, used = [], 0
    for hit in ranked:
        cost = int(len(hit["text"].split()) * 1.3)
        if out and used + cost > settings.EVIDENCE_TOKENS:
            continue
        out.append(hit)
        used += cost
        if len(out) >= limit:
            break
    return out


def index_stats() -> Dict[str, Any]:
    from db.connection import get_session
    s = get_session()
    try:
        q = lambda sql: s.execute(text(sql)).fetchall()
        by_type = q("""SELECT doc_type, count(*) FROM rag_documents WHERE is_current AND deleted_at IS NULL
                       GROUP BY 1 ORDER BY 2 DESC""")
        chunks = q("SELECT count(*) FROM rag_chunks")[0][0]
        ledger = q("""SELECT count(*), count(DISTINCT chunk_hash) FROM rag_index_entries ie
                      JOIN rag_index_versions v ON v.id = ie.index_version_id AND v.status = 'active'""")[0]
        vectors = store.count(settings.collection_name())
        state = q("SELECT last_reconcile_at, last_reconcile_stats FROM rag_sync_state WHERE source_table='*'")
        return {
            "collection": settings.collection_name(),
            "documents_by_type": {r[0]: r[1] for r in by_type},
            "documents_total": sum(r[1] for r in by_type),
            "chunks_distinct": chunks,
            "index_entries": ledger[0], "index_distinct_chunks": ledger[1],
            "chroma_vectors": vectors,
            "chroma_matches_ledger": vectors == ledger[0],
            "last_sync_at": state[0][0] if state else None,
            "last_sync": state[0][1] if state else None,
        }
    finally:
        s.close()
