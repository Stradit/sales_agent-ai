"""Copilot pipeline console (the /copilot-pipeline page, /api/copilotpipeline/*).

* Runs: sync (incremental), reembed (every chunk re-embedded into a staging Chroma
  collection, then swapped in), or eval-only — each optionally followed by the
  guardrail/safety suite and the golden-set eval. Every step is recorded with
  timings and metrics so the UI can show the pipeline step by step. A run holds
  the same advisory lock as sync.process, so it never overlaps the background sync.
* Traces: one document through source → document → chunks → vectors → Chroma
  (with nearest neighbours), and one question through the retrieval steps.

No LLM requests anywhere here.
"""

import json
import math
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from apps.sales_copilot import embed, guardrails, ingest, retrieve, settings, store, sync
from db.connection import engine

STAGES = [
    ("schema", "Schema check", "Apply schema.sql when its fingerprint changed"),
    ("render", "Render documents", "Source rows → documents (junk filter, URL/body dedup, attribution)"),
    ("version", "Version & chunk", "Content-hash diff, SCD-2 versions, content-addressed chunks"),
    ("embed", "Embed chunks", "Local embedding model, each distinct chunk text once"),
    ("index", "Write vectors", "Chroma upsert/delete + Postgres ledger"),
    ("verify", "Verify index", "Chroma matches the ledger; sampled vectors exist"),
    ("guardrails", "Guardrails", "Guardrail, injection and moderation suites + injection scan of the index"),
    ("eval", "Evaluation", "Golden set: intents, entities, recall@10, ACL and PII leaks, gates"),
]
SYNC_STAGES = {"schema", "render", "version", "embed", "index"}
MODES = ("sync", "reembed", "eval")
MAX_LOG_LINES = 300

_lock = threading.Lock()
_current: Optional[Dict[str, Any]] = None


class Busy(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_table() -> None:
    from db.migrate import schema_managed
    if schema_managed():   # table comes from migrations (db/migrate.py)
        return
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE IF NOT EXISTS rag_pipeline_runs (
            id           text PRIMARY KEY,
            mode         text NOT NULL,
            options      jsonb NOT NULL DEFAULT '{}',
            status       text NOT NULL,
            triggered_by int,
            started_at   timestamptz NOT NULL DEFAULT now(),
            finished_at  timestamptz,
            stages       jsonb NOT NULL DEFAULT '[]',
            logs         jsonb NOT NULL DEFAULT '[]',
            summary      jsonb NOT NULL DEFAULT '{}')"""))


def _save(run: Dict[str, Any]) -> None:
    try:
        with engine.begin() as c:
            c.execute(text("""
                INSERT INTO rag_pipeline_runs (id, mode, options, status, triggered_by, started_at, finished_at, stages, logs, summary)
                VALUES (:id, :mode, :opt, :st, :by, :sa, :fa, :stages, :logs, :sum)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, finished_at = EXCLUDED.finished_at,
                  stages = EXCLUDED.stages, logs = EXCLUDED.logs, summary = EXCLUDED.summary"""),
                {"id": run["id"], "mode": run["mode"], "opt": json.dumps(run["options"]), "st": run["status"],
                 "by": run["triggered_by"], "sa": run["started_at"], "fa": run["finished_at"],
                 "stages": json.dumps(run["stages"], default=str), "logs": json.dumps(run["logs"], default=str),
                 "sum": json.dumps(run["summary"], default=str)})
    except Exception as e:
        print(f"[copilot-pipeline] could not save run {run['id']}: {e}")


# ── Runs ──────────────────────────────────────────────────────────────────────


def current() -> Optional[Dict[str, Any]]:
    with _lock:
        return json.loads(json.dumps(_current, default=str)) if _current else None


def history(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_table()
    with engine.connect() as c:
        rows = c.execute(text("""SELECT id, mode, options, status, triggered_by, started_at, finished_at, summary
                                 FROM rag_pipeline_runs ORDER BY started_at DESC LIMIT :n"""), {"n": limit}).fetchall()
    return [{"id": r[0], "mode": r[1], "options": r[2], "status": _live_status(r[0], r[3]), "triggered_by": r[4],
             "started_at": r[5], "finished_at": r[6], "summary": r[7]} for r in rows]


def _live_status(run_id: str, status: str) -> str:
    """A stored 'running' row that isn't this process's live run was cut off by a restart."""
    if status == "running" and not (_current and _current["id"] == run_id):
        return "interrupted"
    return status


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    cur = current()
    if cur and cur["id"] == run_id:
        return cur
    ensure_table()
    with engine.connect() as c:
        r = c.execute(text("""SELECT id, mode, options, status, triggered_by, started_at, finished_at, stages, logs, summary
                              FROM rag_pipeline_runs WHERE id = :i"""), {"i": run_id}).fetchone()
    if not r:
        return None
    return {"id": r[0], "mode": r[1], "options": r[2], "status": _live_status(r[0], r[3]), "triggered_by": r[4], "started_at": r[5],
            "finished_at": r[6], "stages": r[7], "logs": r[8], "summary": r[9]}


def start(mode: str, run_guardrails: bool, run_eval: bool, user_id: Optional[int]) -> Dict[str, Any]:
    """Start a run in a background thread. Raises Busy if a run or a background sync is active."""
    global _current
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    ensure_table()
    with _lock:
        if _current and _current["status"] == "running":
            raise Busy("A pipeline run is already in progress.")
        conn = None
        if mode != "eval":
            conn = engine.raw_connection()
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (sync.LOCK_KEY,))
            if not cur.fetchone()[0]:
                conn.close()
                raise Busy("The background index sync is running — try again in a minute.")
        stages = []
        for key, label, desc in STAGES:
            skip = (key in SYNC_STAGES and mode == "eval") or \
                   (key == "guardrails" and not run_guardrails) or (key == "eval" and not run_eval)
            stages.append({"key": key, "label": label, "desc": desc, "status": "skipped" if skip else "pending",
                           "started_at": None, "finished_at": None, "seconds": None, "metrics": {}, "detail": {},
                           "error": None})
        _current = {"id": uuid.uuid4().hex[:12], "mode": mode,
                    "options": {"guardrails": run_guardrails, "eval": run_eval}, "status": "running",
                    "triggered_by": user_id, "started_at": _now(), "finished_at": None,
                    "stages": stages, "logs": [], "summary": {}}
        run = _current
    _save(run)
    threading.Thread(target=_execute, args=(run, conn), name=f"copilot-pipeline-{run['id']}", daemon=True).start()
    return current()


def _stage(run: Dict[str, Any], key: str) -> Dict[str, Any]:
    return next(s for s in run["stages"] if s["key"] == key)


def _log(run: Dict[str, Any], line: str) -> None:
    with _lock:
        run["logs"].append({"t": _now(), "line": str(line)})
        del run["logs"][:-MAX_LOG_LINES]


def _mark(run: Dict[str, Any], key: str, status: str, metrics: Optional[Dict[str, Any]] = None,
          detail: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
    with _lock:
        st = _stage(run, key)
        if status == "running" and not st["started_at"]:
            st["started_at"], st["_t0"] = _now(), time.time()
        if metrics:
            st["metrics"].update(metrics)
        if detail:
            st["detail"].update(detail)
        if error:
            st["error"] = error
        st["status"] = status
        if status in ("done", "warn", "failed"):
            st["finished_at"] = _now()
            st["seconds"] = round(time.time() - st.pop("_t0", time.time()), 2)


def _execute(run: Dict[str, Any], conn) -> None:
    try:
        if run["mode"] != "eval":
            _run_sync(run, conn)
        _verify(run)
        if _stage(run, "guardrails")["status"] != "skipped":
            _guardrails(run)
        if _stage(run, "eval")["status"] != "skipped":
            _eval(run)
        failed = [s for s in run["stages"] if s["status"] == "failed"]
        run["status"] = "failed" if failed else "succeeded"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
        _log(run, "ERROR " + err)
        _log(run, traceback.format_exc()[-1500:])
        with _lock:
            for s in run["stages"]:
                if s["status"] == "running":
                    s["status"], s["error"], s["finished_at"] = "failed", err, _now()
                    s.pop("_t0", None)
                elif s["status"] == "pending":
                    s["status"] = "skipped"
        run["status"] = "failed"
    finally:
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute("SELECT pg_advisory_unlock(%s)", (sync.LOCK_KEY,))
                conn.commit()
            finally:
                conn.close()
            sync._state["running"] = False
        with _lock:
            for s in run["stages"]:
                s.pop("_t0", None)
            run["finished_at"] = _now()
            run["summary"] = _summarize(run)
        _save(run)


def _run_sync(run: Dict[str, Any], conn) -> None:
    sync._state.update(running=True, last_error=None)
    cur = conn.cursor()
    cur.execute("SELECT max(id), count(*) FROM rag_outbox WHERE processed_at IS NULL")
    max_id, pending = cur.fetchone()
    conn.commit()
    _log(run, f"{pending or 0} outbox change(s) pending")

    def progress(stage: str, status: str, **metrics: Any) -> None:
        detail = {}
        if "by_type" in metrics:
            detail["by_type"] = metrics.pop("by_type")
        if "sample_head" in metrics:
            detail["sample_head"] = metrics.pop("sample_head")
        _mark(run, stage, status, metrics, detail)

    result = ingest.sync(log=lambda line: _log(run, line), progress=progress, rebuild=run["mode"] == "reembed")
    if max_id:
        cur.execute("UPDATE rag_outbox SET processed_at = now() WHERE id <= %s AND processed_at IS NULL", (max_id,))
        conn.commit()
    result["outbox_processed"] = pending or 0
    sync._state.update(last_run=_now(), last_result=result)
    _log(run, f"sync finished in {result.get('seconds')}s")


def _verify(run: Dict[str, Any]) -> None:
    _mark(run, "verify", "running")
    name = settings.collection_name()
    with engine.connect() as c:
        ledger, distinct = c.execute(text("""
            SELECT count(*), count(DISTINCT chunk_hash) FROM rag_index_entries ie
            JOIN rag_index_versions v ON v.id = ie.index_version_id WHERE v.collection_name = :n"""), {"n": name}).fetchone()
        sample = c.execute(text("""
            SELECT ie.chunk_hash, ie.account_id FROM rag_index_entries ie
            JOIN rag_index_versions v ON v.id = ie.index_version_id WHERE v.collection_name = :n
            ORDER BY random() LIMIT 25"""), {"n": name}).fetchall()
    vectors = store.count(name)
    ids = [store.record_id(bytes(h), a) for h, a in sample]
    found = store.get_vectors(name, ids)
    bad_dims = sum(1 for v in found.values() if len(v["embedding"]) != settings.EMBED_DIMS)
    ok = vectors == ledger and len(found) == len(ids) and not bad_dims
    staging_left = False
    try:
        staging_left = any(getattr(col, "name", col) == name + "__rebuild" for col in store.client().list_collections())
    except Exception:
        pass
    _mark(run, "verify", "done" if ok else "warn",
          {"chroma_vectors": vectors, "ledger_entries": ledger, "distinct_chunks": distinct,
           "matches_ledger": vectors == ledger, "sampled": len(ids), "sample_found": len(found),
           "wrong_dims": bad_dims, "staging_left": staging_left, "collection": name})


def _guardrails(run: Dict[str, Any]) -> None:
    from apps.sales_copilot.eval import run_eval
    _mark(run, "guardrails", "running")
    safety = run_eval.run_safety()
    rx = r"(ignore|disregard) (all |the )?(previous|prior|above) (instructions|prompts|rules)|you are now (a|an|dan)|system prompt|<\|im_start\|>"
    with engine.connect() as c:
        n_inj = c.execute(text("SELECT count(*) FROM rag_chunks WHERE text ~* :rx"), {"rx": rx}).scalar()
        rows = c.execute(text("SELECT text FROM rag_chunks WHERE text ~* :rx LIMIT 5"), {"rx": rx}).fetchall()
    samples = []
    for (t,) in rows:
        _, n = guardrails.sanitize_evidence(t)
        line = next((l for l in t.split("\n") if guardrails.INJECTION_RE.search(l)), t)[:300]
        samples.append({"before": line, "after": guardrails.sanitize_evidence(line)[0], "removed": n})
    summ = safety["summary"]
    failures = run_eval._gate_failures(summ)
    _mark(run, "guardrails", "failed" if failures else "done",
          {**summ, "index_injection_chunks": n_inj, "gate_failures": len(failures)},
          {"rows": safety["rows"], "gate_failures": failures, "injection_samples": samples,
           "checks": _guardrail_catalog()})
    _log(run, f"guardrails: {len(safety['rows'])} cases, gate failures: {failures or 'none'}")


def _guardrail_catalog() -> List[Dict[str, str]]:
    """What the guardrails do, for the UI legend."""
    return [
        {"name": "Evidence sanitiser", "where": "retrieval → prompt", "what": "Replaces instruction-like sentences in scraped text"},
        {"name": "Answer checks", "where": "after generation", "what": "Leakage, citations, contact details, links, figures, language"},
        {"name": "Draft checks", "where": "email drafts", "what": "Structure, unsupported figures, over-claims, spam wording, placeholders, length"},
        {"name": "Moderation", "where": "before routing", "what": "Abusive messages never reach search or the AI"},
        {"name": "ACL", "where": "inside every query", "what": "Only accounts the user can open"},
        {"name": "PII", "where": "ingest + contact cards", "what": "Personal email/mobile never indexed or shown"},
    ]


def _eval(run: Dict[str, Any]) -> None:
    from apps.sales_copilot.eval import run_eval
    _mark(run, "eval", "running")
    out = run_eval.run()
    summ = dict(out["summary"])
    g = _stage(run, "guardrails")
    combined = {**summ, **{k: v for k, v in g["metrics"].items() if k in run_eval.GATES}}
    failures = run_eval._gate_failures(combined)
    changes = run_eval._compare_with_last({**combined, "passed": not failures})
    gates = []
    for k, limit in run_eval.GATES.items():
        v = combined.get(k)
        if v is None:
            continue
        mx = k in run_eval.MAX_GATES
        gates.append({"metric": k, "value": v, "gate": f"{'≤' if mx else '≥'} {limit}",
                      "ok": (v <= limit) if mx else (v >= limit)})
    _mark(run, "eval", "failed" if failures else "done", {**summ, "gate_failures": len(failures)},
          {"results": out["results"], "gates": gates, "gate_failures": failures, "changes": changes})
    _log(run, f"eval: {summ.get('cases')} cases, recall@10={summ.get('recall_at_10')}, gates failed: {failures or 'none'}")


def _summarize(run: Dict[str, Any]) -> Dict[str, Any]:
    m = {s["key"]: s["metrics"] for s in run["stages"]}
    out = {"seconds": round(sum(s["seconds"] or 0 for s in run["stages"]), 1)}
    for stage, keys in (("render", ["documents"]), ("version", ["new_versions", "tombstoned"]),
                        ("embed", ["total"]), ("index", ["added", "removed", "chroma_total"]),
                        ("verify", ["matches_ledger"]),
                        ("guardrails", ["guardrail_accuracy", "injection_accuracy"]),
                        ("eval", ["intent_accuracy", "recall_at_10", "acl_leaks", "pii_leaks"])):
        for k in keys:
            if k in m.get(stage, {}):
                out[("embedded" if (stage, k) == ("embed", "total") else k)] = m[stage][k]
    return out


# ── Status ────────────────────────────────────────────────────────────────────


def overview() -> Dict[str, Any]:
    ensure_table()
    return {"index": retrieve.index_stats(), "sync": sync.state(), "current": current(), "runs": history(15),
            "config": {"embed_model": settings.EMBED_MODEL, "dims": settings.EMBED_DIMS,
                       "chunker": settings.CHUNKER_VERSION, "attribution": settings.ATTRIBUTION_VERSION,
                       "render": settings.RENDER_VERSION, "chunk_max_words": settings.CHUNK_MAX_WORDS,
                       "chunk_overlap_words": settings.CHUNK_OVERLAP_WORDS, "collection": settings.collection_name(),
                       "chroma": settings.CHROMA_URL or "embedded"},
            "stages": [{"key": k, "label": l, "desc": d} for k, l, d in STAGES]}


# ── Traces ────────────────────────────────────────────────────────────────────


def _vec_info(v: List[float], head: int = 48) -> Dict[str, Any]:
    return {"dims": len(v), "norm": round(math.sqrt(sum(x * x for x in v)), 4), "head": [round(x, 4) for x in v[:head]]}


def _cos(a: List[float], b: List[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return round(sum(x * y for x, y in zip(a, b)) / (na * nb), 4) if na and nb else 0.0


def _titles_for(s, hashes: List[bytes]) -> Dict[bytes, Dict[str, Any]]:
    if not hashes:
        return {}
    rows = s.execute(text("""
        SELECT DISTINCT ON (dc.chunk_hash) dc.chunk_hash, d.id, d.doc_type, d.title
        FROM rag_document_chunks dc JOIN rag_documents d ON d.id = dc.document_id AND d.is_current AND d.deleted_at IS NULL
        WHERE dc.chunk_hash = ANY(:h) ORDER BY dc.chunk_hash, d.id DESC"""), {"h": hashes}).fetchall()
    return {bytes(r[0]): {"document_id": r[1], "doc_type": r[2], "title": r[3] or ""} for r in rows}


def search_documents(s, q: str, doc_type: str, limit: int = 30) -> Dict[str, Any]:
    like = f"%{q.strip()}%"
    rows = s.execute(text("""
        SELECT d.id, d.canonical_key, d.doc_type, d.title, d.version, d.published_at,
               (SELECT count(*) FROM rag_document_chunks dc WHERE dc.document_id = d.id)
        FROM rag_documents d
        WHERE d.is_current AND d.deleted_at IS NULL
          AND (:q = '' OR d.title ILIKE :like OR d.canonical_key ILIKE :like)
          AND (:t = '' OR d.doc_type = :t)
        ORDER BY (:q <> '' AND d.title ILIKE :starts) DESC, d.published_at DESC NULLS LAST, d.id DESC
        LIMIT :n"""), {"q": q.strip(), "like": like, "starts": f"{q.strip()}%", "t": doc_type or "", "n": limit}).fetchall()
    types = s.execute(text("""SELECT doc_type, count(*) FROM rag_documents WHERE is_current AND deleted_at IS NULL
                              GROUP BY 1 ORDER BY 2 DESC""")).fetchall()
    return {"documents": [{"id": r[0], "canonical_key": r[1], "doc_type": r[2], "title": r[3], "version": r[4],
                           "published_at": r[5], "chunks": r[6]} for r in rows],
            "doc_types": [{"doc_type": r[0], "count": r[1]} for r in types]}


def trace_document(s, doc_id: int, focus_ordinal: int = 0) -> Optional[Dict[str, Any]]:
    """Every step one document went through, ending in its vectors and nearest neighbours."""
    t0 = time.time()
    d = s.execute(text("""SELECT id, canonical_key, version, is_current, doc_type, title, url, published_at, content_hash,
                                 simhash, render_version, metadata, valid_from, valid_to, deleted_at
                          FROM rag_documents WHERE id = :i"""), {"i": doc_id}).fetchone()
    if not d:
        return None
    key = d[1]
    sources = s.execute(text("SELECT source_table, source_pk, first_seen FROM rag_document_sources WHERE canonical_key = :k ORDER BY 1, 2"),
                        {"k": key}).fetchall()
    versions = s.execute(text("""SELECT id, version, is_current, content_hash, valid_from, valid_to, deleted_at,
                                        (SELECT count(*) FROM rag_document_chunks dc WHERE dc.document_id = rd.id)
                                 FROM rag_documents rd WHERE canonical_key = :k ORDER BY version DESC LIMIT 20"""), {"k": key}).fetchall()
    entities = s.execute(text("""
        SELECT e.account_id, a.display_name, e.persona_id, coalesce(p.full_name, p.display_name), e.lob_id, e.relation, e.confidence
        FROM rag_document_entities e JOIN accounts a ON a.id = e.account_id LEFT JOIN personas p ON p.id = e.persona_id
        WHERE e.canonical_key = :k ORDER BY e.confidence DESC, 2, 4"""), {"k": key}).fetchall()
    chunks = s.execute(text("""
        SELECT dc.ordinal, dc.chunk_hash, c.text, c.token_count, c.created_at,
               (SELECT count(DISTINCT d2.canonical_key) FROM rag_document_chunks dc2
                  JOIN rag_documents d2 ON d2.id = dc2.document_id AND d2.is_current AND d2.deleted_at IS NULL
                WHERE dc2.chunk_hash = dc.chunk_hash)
        FROM rag_document_chunks dc JOIN rag_chunks c ON c.chunk_hash = dc.chunk_hash
        WHERE dc.document_id = :i ORDER BY dc.ordinal"""), {"i": doc_id}).fetchall()
    name = settings.collection_name()
    ledger = s.execute(text("""
        SELECT ie.chunk_hash, ie.account_id, ie.indexed_at FROM rag_index_entries ie
        JOIN rag_index_versions v ON v.id = ie.index_version_id AND v.collection_name = :n
        WHERE ie.chunk_hash = ANY(:h)"""), {"n": name, "h": [bytes(c[1]) for c in chunks]}).fetchall() if chunks else []
    ledger_by_chunk: Dict[bytes, List[Any]] = {}
    for h, a, created in ledger:
        ledger_by_chunk.setdefault(bytes(h), []).append((a, created))
    ids = [store.record_id(h, a) for h, lst in ledger_by_chunk.items() for a, _ in lst]
    try:
        vecs = store.get_vectors(name, ids)
        chroma_error = None
    except Exception as e:
        vecs, chroma_error = {}, str(e)[:300]

    chunk_out, prev_vec, focus_vec = [], None, None
    for ordinal, h, body, ntok, created, shared in chunks:
        h = bytes(h)
        header, _, content = body.partition("\n")
        _, removed = guardrails.sanitize_evidence(body)
        entries = []
        vec = None
        for a, created_at in ledger_by_chunk.get(h, []):
            rid = store.record_id(h, a)
            got = vecs.get(rid)
            vec = vec or (got or {}).get("embedding")
            entries.append({"id": rid, "account_id": a, "in_chroma": got is not None, "ledger_at": created_at,
                            "metadata": (got or {}).get("metadata")})
        if ordinal == focus_ordinal:
            focus_vec = vec
        chunk_out.append({
            "ordinal": ordinal, "hash": h.hex(), "header": header, "text": content, "words": len(body.split()),
            "tokens": ntok, "created_at": created, "shared_by_documents": shared,
            "guardrails": {"injection_sentences": removed, "emails": len(guardrails.EMAIL_RE.findall(content)),
                           "phones": len(guardrails.PHONE_RE.findall(content))},
            "vector": _vec_info(vec) if vec else None,
            "similarity_to_previous": _cos(vec, prev_vec) if vec and prev_vec else None,
            "index_entries": entries,
        })
        prev_vec = vec or prev_vec

    neighbours = []
    if focus_vec:
        try:
            hits = store.query(name, focus_vec, 12, None)
            info = _titles_for(s, [hh["chunk_hash"] for hh in hits])
            seen = set()
            for hh in hits:
                if hh["chunk_hash"] in seen:
                    continue
                seen.add(hh["chunk_hash"])
                meta = info.get(hh["chunk_hash"], {})
                neighbours.append({"hash": hh["chunk_hash"].hex(), "distance": round(hh["distance"], 4),
                                   "similarity": round(1 - hh["distance"], 4), "account_id": hh["meta"].get("account_id"),
                                   "self": meta.get("document_id") == doc_id, **meta})
        except Exception as e:
            chroma_error = chroma_error or str(e)[:300]

    return {
        "document": {"id": d[0], "canonical_key": key, "version": d[2], "is_current": d[3], "doc_type": d[4],
                     "title": d[5], "url": d[6], "published_at": d[7], "content_hash": bytes(d[8]).hex(),
                     "simhash": format(d[9] & (2 ** 64 - 1), "016x") if d[9] is not None else None,
                     "render_version": d[10], "metadata": d[11], "valid_from": d[12], "valid_to": d[13],
                     "deleted_at": d[14]},
        "sources": [{"table": r[0], "pk": r[1], "first_seen": r[2]} for r in sources],
        "versions": [{"id": r[0], "version": r[1], "is_current": r[2], "content_hash": bytes(r[3]).hex()[:16],
                      "valid_from": r[4], "valid_to": r[5], "deleted_at": r[6], "chunks": r[7]} for r in versions],
        "entities": [{"account_id": r[0], "account": r[1], "persona_id": r[2], "persona": r[3], "lob_id": r[4],
                      "relation": r[5], "confidence": round(r[6], 2)} for r in entities],
        "chunks": chunk_out,
        "focus_ordinal": focus_ordinal,
        "neighbours": neighbours,
        "collection": name,
        "chroma_error": chroma_error,
        "config": {"embed_model": settings.EMBED_MODEL, "dims": settings.EMBED_DIMS, "chunker": settings.CHUNKER_VERSION,
                   "chunk_max_words": settings.CHUNK_MAX_WORDS, "chunk_overlap_words": settings.CHUNK_OVERLAP_WORDS},
        "ms": int((time.time() - t0) * 1000),
    }


def trace_query(s, q: str, account_ids: Optional[List[int]], persona_id: Optional[int] = None) -> Dict[str, Any]:
    """The retrieval steps for one question, with each leg's raw results (no LLM)."""
    steps: List[Dict[str, Any]] = []

    def step(key: str, label: str, t: float, **data: Any) -> None:
        steps.append({"key": key, "label": label, "ms": int((time.time() - t) * 1000), **data})

    acl = retrieve.all_account_ids(s)
    scope = [a for a in (account_ids or acl) if a in set(acl)]
    name = settings.collection_name()

    t = time.time()
    drop = retrieve._account_terms(s, scope)
    tsq = retrieve._or_tsquery(q, drop)
    _, n_inj = guardrails.sanitize_evidence(q)
    step("prepare", "Prepare query", t, scope=scope, tsquery=tsq, dropped_terms=sorted(drop)[:30],
         question_injection_sentences=n_inj)

    t = time.time()
    qvec = embed.embed_query(q)
    step("embed", "Embed question", t, model=settings.EMBED_MODEL, vector=_vec_info(qvec))

    t = time.time()
    where = {"account_id": {"$in": [int(a) for a in scope]}}
    try:
        vhits = store.query(name, qvec, 20, where)
        verr = None
    except Exception as e:
        vhits, verr = [], str(e)[:300]
    vec_rank: List[bytes] = []
    for h in vhits:
        if h["chunk_hash"] not in vec_rank:
            vec_rank.append(h["chunk_hash"])
    info = _titles_for(s, vec_rank)
    step("vector", "Vector search (Chroma)", t, error=verr,
         hits=[{"rank": i + 1, "hash": h["chunk_hash"].hex(), "similarity": round(1 - h["distance"], 4),
                "account_id": h["meta"].get("account_id"), **info.get(h["chunk_hash"], {})}
               for i, h in enumerate(vhits)])

    t = time.time()
    kw_rank = retrieve._keyword(s, q, scope, 20, drop=drop)
    kinfo = _titles_for(s, kw_rank)
    step("keyword", "Keyword search (Postgres FTS)", t,
         hits=[{"rank": i + 1, "hash": h.hex(), **kinfo.get(h, {})} for i, h in enumerate(kw_rank)])

    t = time.time()
    fused: Dict[bytes, Dict[str, Any]] = {}
    for leg, ranked in (("vector", vec_rank), ("keyword", kw_rank)):
        for rank, h in enumerate(ranked):
            f = fused.setdefault(h, {"score": 0.0, "vector_rank": None, "keyword_rank": None})
            f["score"] += 1.0 / (retrieve.RRF_K + rank + 1)
            f[f"{leg}_rank"] = rank + 1
    allinfo = {**info, **kinfo}
    rrf = sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:20]
    step("fuse", "Reciprocal rank fusion", t, k=retrieve.RRF_K,
         hits=[{"hash": h.hex(), "score": round(f["score"], 5), "vector_rank": f["vector_rank"],
                "keyword_rank": f["keyword_rank"], "both": bool(f["vector_rank"] and f["keyword_rank"]),
                **allinfo.get(h, {})} for h, f in rrf])

    t = time.time()
    final = retrieve.search(s, q, acl, persona_id=persona_id, account_ids=scope)
    evidence = []
    for i, h in enumerate(final):
        _, removed = guardrails.sanitize_evidence(h["text"])
        evidence.append({"n": i + 1, "hash": h["chunk_hash"].hex(), "document_id": h["document_id"],
                         "doc_type": h["doc_type"], "title": h["title"], "score": round(h["score"], 5),
                         "published_at": h["published_at"], "tokens": int(len(h["text"].split()) * 1.3),
                         "snippet": h["snippet"], "injection_sentences": removed, "url": h["url"]})
    step("rerank", "Verify, boost, dedup, budget", t, token_budget=settings.EVIDENCE_TOKENS,
         max_items=settings.MAX_EVIDENCE_ITEMS, evidence=evidence,
         tokens_used=sum(e["tokens"] for e in evidence))
    return {"question": q, "steps": steps, "collection": name}
