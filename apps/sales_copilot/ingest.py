"""Ingestion: source rows -> versioned documents -> content-addressed chunks ->
Chroma vectors (README §5–§8).

`sync()` is a full, idempotent diff of the whole corpus:
  * documents are re-rendered and compared by content hash — unchanged text
    costs nothing, changed text becomes a new document version (SCD-2);
  * chunks are keyed by sha256 of their text, so identical text is stored and
    embedded once no matter how many rows/versions contain it;
  * the Chroma index is reconciled against the rag_index_entries ledger, so
    only missing (chunk, account) pairs are embedded and orphans are deleted.
Phase 3 (outbox triggers) will make it incremental; until then running this
every few minutes is cheap because unchanged documents never re-embed.
"""

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from psycopg2.extras import execute_values

from apps.sales_copilot import embed, settings, store
from db.connection import engine

# ── Data model ────────────────────────────────────────────────────────────────

Entity = Tuple[int, Optional[int], Optional[int], str, float]  # account, persona, lob, relation, confidence


@dataclass
class RenderedDoc:
    canonical_key: str
    doc_type: str
    title: str
    text: str
    url: Optional[str] = None
    published_at: Optional[datetime] = None
    entities: Set[Entity] = field(default_factory=set)
    sources: Set[Tuple[str, str]] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)


POST_CHANNEL_TYPES = {
    "news": "news", "cxo_news": "news", "rss": "news", "bloomberg": "news", "web_search": "news",
    "newsroom": "blog", "blog": "blog", "linkedin": "linkedin_post", "twitter": "social",
    "reddit": "reddit", "patents": "patent", "brokercheck": "news", "sec_proxy": "filing",
}
SKIP_CHANNELS = {"linkedin_jobs", "sec", "sec_mentions"}   # jobs: later phase; sec stubs: L0 junk
NAME_QUERY_CHANNELS = {"news", "reddit", "web_search", "bloomberg", "rss", "patents", "brokercheck"}
MAX_POST_WORDS = 2500

BOILERPLATE = [
    r"Skip Navigation.{0,400}?(Markets|Business|Investing)\b",
    r"(Accept|Reject) (all )?cookies?",
    r"Read more\b",
    r"Sign up for .{0,80}newsletter",
]

# ── Normalization / hashing ───────────────────────────────────────────────────


_MOJIBAKE = {"â€™": "’", "â€˜": "‘", "â€œ": "“", "â€\x9d": "”", "â€“": "–", "â€”": "—", "â€¦": "…",
             "â€¢": "•", "Â ": " ", "Â·": "·", "Ã©": "é", "Ã¨": "è", "Ã¼": "ü", "Ã¶": "ö"}


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 text that was decoded as Windows-1252 somewhere upstream ("Wealthâ€™s")."""
    if not any(ch in s for ch in "âÃÂ"):
        return s
    try:
        return s.encode("cp1252").decode("utf-8")        # whole-string repair when it round-trips cleanly
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    for bad, good in _MOJIBAKE.items():
        s = s.replace(bad, good)

    # Latin-1 variant: "â\x80\x99" — repair each UTF-8 byte run on its own
    def _run(m):
        try:
            return m.group(0).encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"[\xc2-\xf4][\x80-\xbf]{1,3}", _run, s)


def normalize(text: Any) -> str:
    s = unicodedata.normalize("NFKC", fix_mojibake(str(text or "")))
    s = re.sub(r"[​-‏﻿]", "", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)          # markdown images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)      # markdown links -> text
    s = re.sub(r"[#*_>`|]+", " ", s)
    for pat in BOILERPLATE:
        s = re.sub(pat, " ", s, flags=re.I | re.S)
    return re.sub(r"\s+", " ", s).strip()


def sha(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def canonical_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"activity[-:](\d{10,})", url)
    if m and "linkedin.com" in url:
        return f"linkedin:activity:{m.group(1)}"
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if not u.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(u.query)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "trk", "ref", "src"}]
    path = u.path.rstrip("/") or "/"
    return urlunparse((u.scheme.lower() or "https", u.netloc.lower(), path, "", urlencode(query), ""))


def parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    for parse in (parsedate_to_datetime, lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))):
        try:
            dt = parse(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _name_tokens(name: str) -> List[str]:
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in (name or "").split()]
    parts = [p for p in parts if len(p) >= 3]
    return [parts[0], parts[-1]] if len(parts) > 1 else []


def _mentions_person(text_lower: str, name: str) -> bool:
    toks = _name_tokens(name)
    return bool(toks) and all(re.search(rf"\b{re.escape(t)}\b", text_lower) for t in toks)


def _join(items: Iterable[Any], sep: str = ", ") -> str:
    return sep.join(str(x).strip() for x in (items or []) if x and str(x).strip())


# ── Reference data ────────────────────────────────────────────────────────────


class Ref:
    """Accounts, personas and LOBs loaded once per sync."""

    def __init__(self, conn):
        cur = conn.cursor()
        cur.execute("SELECT * FROM accounts")
        cols = [d[0] for d in cur.description]
        self.accounts = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
        self.account_by_key = {a["key"]: a for a in self.accounts.values()}
        self.account_aliases: Dict[int, List[str]] = {}
        for a in self.accounts.values():
            names = {a.get("display_name"), a.get("legal_name"), *(a.get("aliases") or [])}
            self.account_aliases[a["id"]] = sorted(
                {n.lower().strip() for n in names if n and len(n.strip()) >= 3}, key=len, reverse=True)

        cur.execute("SELECT * FROM personas")
        cols = [d[0] for d in cur.description]
        self.personas = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
        self.personas_by_key: Dict[str, List[dict]] = {}
        for p in self.personas.values():
            if p.get("key"):
                self.personas_by_key.setdefault(p["key"], []).append(p)

        cur.execute("SELECT id, account_id, lob_name FROM lobs")
        self.lob_names = {r[0]: r[2] for r in cur.fetchall()}

    def account_name(self, account_id: int) -> str:
        a = self.accounts.get(account_id) or {}
        return a.get("display_name") or a.get("legal_name") or a.get("key") or "Account"

    def mentions_account(self, text_lower: str, account_id: int) -> bool:
        return any(re.search(rf"\b{re.escape(n)}\b", text_lower) for n in self.account_aliases.get(account_id, []))


def person_name(p: dict) -> str:
    return p.get("full_name") or p.get("display_name") or "Unnamed"


# ── Renderers (README §5.1) ───────────────────────────────────────────────────


def render_personas(ref: Ref) -> Iterable[RenderedDoc]:
    for p in ref.personas.values():
        acct = ref.account_name(p["account_id"])
        lob = ref.lob_names.get(p.get("lob_id"))
        ents = {(p["account_id"], p["id"], p.get("lob_id"), "about", 1.0)}
        name = person_name(p)

        lines = [f"{name} — {p.get('title') or 'Executive'} at {acct}."]
        if lob:
            lines.append(f"Line of business: {lob}.")
        tier_bits = [x for x in (p.get("tier"), p.get("decision_authority"), p.get("budget_authority")) if x]
        if tier_bits:
            lines.append("Seniority / authority: " + "; ".join(str(x) for x in tier_bits) + ".")
        if p.get("headline") and p.get("headline") != p.get("title"):
            lines.append(f"Headline: {p['headline']}.")
        if p.get("is_new_in_role"):
            lines.append("New in role.")
        if p.get("current_role_tenure_months"):
            lines.append(f"Tenure in current role: {p['current_role_tenure_months']} months.")
        roles = [r for r in (p.get("employment_history") or []) if isinstance(r, dict)][:3]
        if roles:
            lines.append("Career: " + "; ".join(
                f"{r.get('title', '')} at {r.get('company', '')} ({r.get('start_date') or '?'}–{r.get('end_date') or '?'})"
                for r in roles) + ".")
        edu = [e for e in (p.get("education_history") or []) if isinstance(e, dict)][:2]
        if edu:
            lines.append("Education: " + "; ".join(
                _join([e.get("degree"), e.get("field_of_study"), e.get("school") or e.get("institution")], " ")
                for e in edu) + ".")
        elif p.get("degree") or p.get("institution"):
            lines.append(f"Education: {_join([p.get('degree'), p.get('institution')], ' ')}.")
        if p.get("skills"):
            lines.append(f"Skills: {_join(p['skills'][:8])}.")
        loc = _join([p.get("city"), p.get("country")])
        if loc:
            lines.append(f"Location: {loc}.")
        yield RenderedDoc(f"persona:{p['id']}", "persona_card", f"{name} — {p.get('title') or ''}".strip(" —"),
                          " ".join(lines), entities=ents, sources={("personas", str(p["id"]))},
                          metadata={"persona_id": p["id"]})

        cp = (p.get("extended_profile") or {}).get("callprep") or {}
        objections = cp.get("objections") or [{"objection": o, "counter": ""} for o in (p.get("key_objections") or [])]
        if p.get("value_proposition") or objections:
            cl = [f"Sales call-prep for {name} ({p.get('title') or 'Executive'}, {acct})."]
            if p.get("personalized_icebreaker"):
                cl.append(f"Icebreaker: {p['personalized_icebreaker']}")
            if p.get("value_proposition"):
                cl.append(f"Value proposition: {p['value_proposition']}")
            if p.get("communication_style"):
                cl.append(f"Communication style: {p['communication_style']}.")
            if p.get("target_kpis"):
                cl.append(f"Target KPIs: {_join(p['target_kpis'])}.")
            if p.get("operational_pain_points"):
                cl.append(f"Pain points: {_join(p['operational_pain_points'])}.")
            for o in objections:
                if isinstance(o, dict) and o.get("objection"):
                    cl.append(f"Likely objection: {o['objection']}" + (f" — Suggested response: {o['counter']}" if o.get("counter") else ""))
            if cp.get("confidence"):
                cl.append(f"(Call-prep confidence: {cp['confidence']}.)")
            yield RenderedDoc(f"callprep:{p['id']}", "callprep", f"Call-prep: {name}", " ".join(cl),
                              published_at=parse_date(cp.get("generated_at")), entities=ents,
                              sources={("personas.callprep", str(p["id"]))}, metadata={"persona_id": p["id"]})


def render_accounts(ref: Ref) -> Iterable[RenderedDoc]:
    for a in ref.accounts.values():
        name = ref.account_name(a["id"])
        lines = [f"{name} ({a.get('legal_name') or name})."]
        for label, col in [("About", "short_description"), ("Description", "full_description"),
                           ("Headquarters", "headquarters_location"), ("Employees", "employee_count_range"),
                           ("Revenue", "estimated_revenue_range"), ("Founded", "founded_year")]:
            if a.get(col) and not (col == "full_description" and a.get(col) == a.get("short_description")):
                lines.append(f"{label}: {a[col]}.")
        if a.get("industries"):
            lines.append(f"Industries: {_join(a['industries'] if isinstance(a['industries'], list) else [a['industries']])}.")
        if a.get("stock_symbol"):
            lines.append(f"Stock: {a['stock_symbol']} {a.get('stock_exchange') or ''}.")
        counts = [f"{a[c]} {lbl}" for c, lbl in [("c_suite_count", "C-suite"), ("vp_count", "VPs"),
                  ("director_count", "directors"), ("manager_count", "managers")] if a.get(c)]
        if counts:
            lines.append("Contacts mapped: " + ", ".join(counts) + ".")
        yield RenderedDoc(f"account:{a['id']}", "account_card", name, " ".join(lines),
                          entities={(a["id"], None, None, "about", 1.0)}, sources={("accounts", str(a["id"]))})


def render_lobs(conn, ref: Ref) -> Iterable[RenderedDoc]:
    cur = conn.cursor()
    cur.execute("""SELECT id, account_id, lob_name, overview, relationship_type, audited_segment_revenue,
                          segment_headcount, operating_head, technologies, competitors FROM lobs""")
    for lid, aid, name, overview, rel, rev, hc, head, tech, comp in cur.fetchall():
        lines = [f"{name} — line of business of {ref.account_name(aid)}."]
        for label, val in [("Overview", overview), ("Relationship", rel), ("Segment revenue", rev),
                           ("Headcount", hc), ("Operating head", head)]:
            if val:
                lines.append(f"{label}: {val}.")
        if tech:
            lines.append(f"Technologies: {_join(tech if isinstance(tech, list) else [tech])}.")
        if comp:
            lines.append(f"Competitors: {_join(comp if isinstance(comp, list) else [comp])}.")
        if len(lines) == 1:
            continue   # name only — nothing worth retrieving
        yield RenderedDoc(f"lob:{lid}", "lob_card", name, " ".join(lines),
                          entities={(aid, None, lid, "about", 1.0)}, sources={("lobs", str(lid))})


def render_signals(conn, ref: Ref) -> Iterable[RenderedDoc]:
    cur = conn.cursor()
    cur.execute("SELECT id, account_id, category, title, details, last_seen FROM opportunity_signals")
    for sid, aid, cat, title, details, last_seen in cur.fetchall():
        summary = details.get("summary") if isinstance(details, dict) else details
        text = f"Opportunity signal at {ref.account_name(aid)} ({(cat or '').replace('_', ' ')}): {title}. {summary or ''}"
        yield RenderedDoc(f"signal:{sid}", "signal", title or "Signal", text, published_at=parse_date(last_seen),
                          entities={(aid, None, None, "about", 1.0)}, sources={("opportunity_signals", str(sid))})


def _digest_entities(ref: Ref, target_key: str, kind: str) -> Set[Entity]:
    if kind == "company":
        a = ref.account_by_key.get(target_key)
        return {(a["id"], None, None, "about", 1.0)} if a else set()
    return {(p["account_id"], p["id"], p.get("lob_id"), "about", 1.0) for p in ref.personas_by_key.get(target_key, [])}


def _section_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("summary") or value.get("text") or "")
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if isinstance(v, str))
    return ""


def render_digests(conn, ref: Ref) -> Iterable[RenderedDoc]:
    cur = conn.cursor()
    cur.execute("SELECT target_key, kind, generated_at, digest FROM digests")
    for tkey, kind, generated_at, d in cur.fetchall():
        if not isinstance(d, dict) or "dry" in str(d.get("llm", "")).lower():
            continue
        ents = _digest_entities(ref, tkey, kind)
        if not ents:
            continue
        subject = d.get("company") or tkey
        for ch in d.get("channels") or []:
            if not isinstance(ch, dict) or not ch.get("summary"):
                continue
            hook = (ch.get("storyline") or {}).get("hook") if isinstance(ch.get("storyline"), dict) else None
            parts = [f"{ch.get('channel_label') or ch.get('channel')} digest for {subject}: {ch['summary']}",
                     ch.get("interpretation") or "", f"Sales angle: {ch['sales_angle']}" if ch.get("sales_angle") else "",
                     f"Storyline: {hook}" if hook else ""]
            yield RenderedDoc(f"digest:{tkey}:{ch.get('channel')}", "digest_channel",
                              f"{ch.get('channel_label') or ch.get('channel')} digest — {subject}",
                              " ".join(p for p in parts if p), published_at=parse_date(generated_at), entities=ents,
                              sources={("digests.channel", f"{tkey}:{ch.get('channel')}")})
        for pkind in ("personality_profile", "psychological_profile"):
            prof = d.get(pkind)
            if not isinstance(prof, dict):
                continue
            parts = []
            for k, v in prof.items():
                if k in ("caveats",):
                    continue
                if isinstance(v, dict) and not v.get("summary"):
                    parts.extend(f"{kk.replace('_', ' ')}: {_section_text(vv)}" for kk, vv in v.items() if _section_text(vv))
                elif _section_text(v):
                    parts.append(f"{k.replace('_', ' ')}: {_section_text(v)}")
            if parts:
                label = "Personality profile" if pkind == "personality_profile" else "Psychological profile"
                yield RenderedDoc(f"profile:{tkey}:{pkind}", "personality_profile", f"{label} — {subject}",
                                  f"{label} of {subject}. " + " ".join(parts), published_at=parse_date(generated_at),
                                  entities=ents, sources={("digests.profile", f"{tkey}:{pkind}")})


def render_cxo(conn, ref: Ref) -> Iterable[RenderedDoc]:
    cur = conn.cursor()
    cur.execute("""SELECT id, target_key, person_name, designation, event_type, effective_date, previous_role,
                          context, article_title, article_url, published_at FROM cxo_movements""")
    for cid, tkey, pname, desig, ev, eff, prev, ctx, atitle, aurl, pub in cur.fetchall():
        a = ref.account_by_key.get(tkey)
        if not a:
            continue
        ents = {(a["id"], None, None, "about", 1.0)}
        for p in ref.personas.values():
            if p["account_id"] == a["id"] and pname and person_name(p).lower() == pname.lower():
                ents.add((a["id"], p["id"], p.get("lob_id"), "about", 1.0))
        text = (f"Leadership change at {ref.account_name(a['id'])}: {pname} — {ev or ''} {desig or ''}"
                f"{f' (effective {eff})' if eff else ''}. {f'Previously: {prev}. ' if prev else ''}{normalize(ctx)[:1500]}")
        cu = canonical_url(aurl)
        yield RenderedDoc(f"url:{cu}" if cu else f"cxo:{cid}", "cxo_move", atitle or f"{pname} — {ev}", text,
                          url=aurl, published_at=parse_date(pub) or parse_date(eff), entities=ents,
                          sources={("cxo_movements", str(cid))})


def render_posts(conn, ref: Ref) -> Iterable[RenderedDoc]:
    cur = conn.cursor(name="copilot_posts")   # server-side cursor: 24k rows
    cur.itersize = 2000
    cur.execute("SELECT id, target_key, kind, channel, post_url, body, author, extra, published_at FROM posts")
    for pid, tkey, kind, channel, url, body, author, extra, pub in cur:
        if channel in SKIP_CHANNELS:
            continue
        text = normalize(body)
        if len(text) < settings.MIN_POST_CHARS:
            continue                                   # L0 junk filter
        words = text.split()
        if len(words) > MAX_POST_WORDS:
            text = " ".join(words[:MAX_POST_WORDS])
        title = normalize((extra or {}).get("title") if isinstance(extra, dict) else "")[:200]
        low = f"{title} {text}".lower()

        ents: Set[Entity] = set()
        if kind == "company":
            a = ref.account_by_key.get(tkey)
            if a:
                ents.add((a["id"], None, None, "about", 1.0))
        else:
            for p in ref.personas_by_key.get(tkey, []):
                aid, name = p["account_id"], person_name(p)
                if channel == "sec_proxy":
                    ents.add((aid, None, None, "account_context", 0.6))
                elif channel == "linkedin":
                    if author and _name_tokens(author) and _name_tokens(author) == _name_tokens(name):
                        ents.add((aid, p["id"], p.get("lob_id"), "authored", 1.0))
                    elif _mentions_person(low, name):
                        ents.add((aid, p["id"], p.get("lob_id"), "mentions", 0.9))
                    else:
                        ents.add((aid, None, None, "account_context", 0.6))
                elif channel in NAME_QUERY_CHANNELS:
                    # README §6.4: name-query results only count for the person when the
                    # full name is actually in the text ("David D." queries return noise).
                    if _mentions_person(low, name):
                        ents.add((aid, p["id"], p.get("lob_id"), "mentions", 0.9))
                    elif ref.mentions_account(low, aid):
                        ents.add((aid, None, None, "account_context", 0.6))
                else:
                    ents.add((aid, None, None, "account_context", 0.6))
        if not ents:
            continue

        cu = canonical_url(url)
        key = f"url:{cu}" if cu else f"hash:{sha(text).hex()[:32]}"   # L2 canonical identity
        yield RenderedDoc(key, POST_CHANNEL_TYPES.get(channel, "news"), title or text[:90], text, url=url,
                          published_at=parse_date(pub), entities=ents, sources={("posts", str(pid))},
                          metadata={"channel": channel})
    cur.close()


JOB_DESC_CHARS = 1200


def render_jobs(conn, ref: Ref) -> Iterable[RenderedDoc]:
    """One doc per posting (from linkedin_jobs — the same postings also sit in
    posts[channel='linkedin_jobs'], which render_posts skips: L2 dedup by table)
    plus a weekly hiring roll-up per account (counts only, no LLM)."""
    cur = conn.cursor()
    cur.execute("""SELECT id, target_key, title, company_name, location, employment_type, workplace_type,
                          posted_date, job_url, description, first_seen FROM linkedin_jobs""")
    weekly: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for jid, tkey, title, company, loc, etype, wtype, posted, url, desc, first_seen in cur.fetchall():
        a = ref.account_by_key.get(tkey)
        if not a or not title:
            continue
        when = parse_date(posted) or parse_date(first_seen)
        body = normalize(desc)[:JOB_DESC_CHARS]
        text_ = (f"Job posting at {ref.account_name(a['id'])}: {title}. "
                 + (f"Location: {loc}. " if loc else "")
                 + (f"{_join([etype, wtype], ', ')}. " if (etype or wtype) else "")
                 + body)
        cu = canonical_url(url)
        yield RenderedDoc(f"url:{cu}" if cu else f"job:{jid}", "job", f"Job: {title}", text_, url=url,
                          published_at=when, entities={(a["id"], None, None, "about", 1.0)},
                          sources={("linkedin_jobs", str(jid))})
        if when:
            iso = when.isocalendar()
            wk = weekly.setdefault((a["id"], f"{iso[0]}-W{iso[1]:02d}"),
                                   {"n": 0, "titles": {}, "locs": {}, "start": when})
            wk["n"] += 1
            t = re.sub(r"\s+", " ", title.strip())
            wk["titles"][t] = wk["titles"].get(t, 0) + 1
            if loc:
                wk["locs"][loc] = wk["locs"].get(loc, 0) + 1
            wk["start"] = min(wk["start"], when)
    for (aid, week), wk in weekly.items():
        top = sorted(wk["titles"].items(), key=lambda kv: -kv[1])[:8]
        locs = sorted(wk["locs"].items(), key=lambda kv: -kv[1])[:5]
        text_ = (f"Hiring at {ref.account_name(aid)} in week {week}: {wk['n']} job postings. "
                 f"Top roles: {'; '.join(f'{t} ({n})' for t, n in top)}. "
                 + (f"Main locations: {'; '.join(f'{l} ({n})' for l, n in locs)}." if locs else ""))
        yield RenderedDoc(f"jobtheme:{aid}:{week}", "job_theme", f"Hiring summary {week} — {ref.account_name(aid)}",
                          text_, published_at=wk["start"], entities={(aid, None, None, "about", 1.0)},
                          sources={("linkedin_jobs.weekly", f"{aid}:{week}")})


# ── Near-duplicate fingerprint (README §6, L5) ────────────────────────────────

SIMHASH_TYPES = {"news", "blog", "linkedin_post", "reddit", "social", "cxo_move", "filing"}


def simhash64(text_: str) -> int:
    """64-bit SimHash over word 3-shingles; returned as a signed int for Postgres bigint."""
    words = re.findall(r"[a-z0-9]+", text_.lower())
    shingles = [" ".join(words[i:i + 3]) for i in range(max(1, len(words) - 2))]
    acc = [0] * 64
    for sh in shingles:
        h = int.from_bytes(hashlib.md5(sh.encode()).digest()[:8], "big")
        for b in range(64):
            acc[b] += 1 if (h >> b) & 1 else -1
    v = sum(1 << b for b in range(64) if acc[b] > 0)
    return v - (1 << 64) if v >= (1 << 63) else v


ACTIVITY_LABEL = {"email": "Email", "meeting": "Meeting", "call": "Call", "note": "Note", "transcript": "Meeting transcript",
                  "linkedin": "LinkedIn message", "task_done": "Completed task"}


def render_activities(conn, ref: Ref) -> Iterable[RenderedDoc]:
    """CRM activities (apps/sales_crm/activities.py). Private ones are never indexed."""
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('activities') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT a.id, a.type, a.direction, a.subject, a.summary, a.body, a.occurred_at, a.metadata,
                          coalesce(u.full_name, u.email),
                          coalesce(array_agg(l.object_type || ':' || l.object_id) FILTER (WHERE l.object_id IS NOT NULL), '{}')
                   FROM activities a LEFT JOIN users u ON u.id = a.owner_user_id
                   LEFT JOIN activity_links l ON l.activity_id = a.id
                   WHERE a.visibility = 'team' GROUP BY a.id, u.id""")
    for aid, typ, direction, subject, summary, body, when, meta, owner, links in cur.fetchall():
        entities: Set[Entity] = set()
        names = []
        for link in links:
            otype, oid = link.split(":", 1)
            oid = int(oid)
            if otype == "account" and oid in ref.accounts:
                entities.add((oid, None, None, "about", 1.0))
            elif otype == "persona" and oid in ref.personas:
                p = ref.personas[oid]
                entities.add((p["account_id"], oid, p.get("lob_id"), "about", 1.0))
                names.append(person_name(p))
        if not entities:
            continue
        acct = ", ".join(sorted({ref.account_name(e[0]) for e in entities}))
        label = ACTIVITY_LABEL.get(typ, typ)
        with_whom = f" with {', '.join(names)}" if names else ""
        parts = [f"{label}{with_whom} at {acct} on {when:%Y-%m-%d}, logged by {owner or 'a colleague'}."]
        if subject:
            parts.append(f"Subject: {subject}.")
        if summary:
            parts.append(("Key points: " if typ == "transcript" else "Notes: ") + summary.replace("\n", " "))
        actions = (meta or {}).get("action_items") or []
        if actions:
            parts.append("Action items: " + "; ".join(f"{a.get('speaker')}: {a.get('text')}" for a in actions[:8]))
        if typ == "transcript" and body:
            parts.append("Transcript: " + " ".join(body.split()[:1500]))
        yield RenderedDoc(f"activity:{aid}", "activity", subject or f"{label}{with_whom}", "\n".join(parts),
                          published_at=when, entities=entities, sources={("activities", str(aid))})


def render_all(conn) -> Tuple[Dict[str, RenderedDoc], Ref]:
    ref = Ref(conn)
    docs: Dict[str, RenderedDoc] = {}
    gens = [render_personas(ref), render_accounts(ref), render_lobs(conn, ref), render_signals(conn, ref),
            render_digests(conn, ref), render_cxo(conn, ref), render_posts(conn, ref), render_jobs(conn, ref),
            render_activities(conn, ref)]
    for gen in gens:
        for d in gen:
            if not d.text.strip():
                continue
            prev = docs.get(d.canonical_key)
            if prev is None:
                docs[d.canonical_key] = d
                continue
            # L2 merge: same item found under several targets -> one document, union of links
            prev.entities |= d.entities
            prev.sources |= d.sources
            if len(d.text) > len(prev.text):
                prev.text, prev.title = d.text, d.title or prev.title
            prev.published_at = prev.published_at or d.published_at
    return docs, ref


# ── Chunking (README §5.3) ────────────────────────────────────────────────────


def chunk(doc: RenderedDoc, ref: Ref) -> List[Tuple[bytes, str, int]]:
    accounts = sorted({e[0] for e in doc.entities})
    acct = ", ".join(ref.account_name(a) for a in accounts[:2])
    date = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else ""
    header = "[" + " · ".join(x for x in (acct, doc.doc_type.replace("_", " "), date, (doc.title or "")[:120]) if x) + "]"

    words = doc.text.split()
    if len(words) <= settings.CHUNK_MAX_WORDS:
        pieces = [doc.text]
    else:
        sentences = re.split(r"(?<=[.!?])\s+", doc.text)
        pieces, cur = [], []
        for s in sentences:
            sw = s.split()
            if cur and len(cur) + len(sw) > settings.CHUNK_MAX_WORDS:
                pieces.append(" ".join(cur))
                cur = cur[-settings.CHUNK_OVERLAP_WORDS:]
            cur.extend(sw[: settings.CHUNK_MAX_WORDS])
        if cur:
            pieces.append(" ".join(cur))
    out = []
    for p in pieces:
        full = f"{header}\n{p}"
        out.append((sha(f"{settings.CHUNKER_VERSION}\n{full}"), full, int(len(full.split()) * 1.3)))
    return out


# ── Sync ──────────────────────────────────────────────────────────────────────


_schema_checked = False


def ensure_schema(force: bool = False) -> None:
    """Apply schema.sql only when it changed (fingerprint stored in rag_schema_meta).
    Re-running the trigger DDL on every API (re)start would take AccessExclusive
    locks on busy tables — with uvicorn --reload that deadlocked a running sync."""
    global _schema_checked
    if _schema_checked and not force:
        return
    from pathlib import Path
    sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS rag_schema_meta (id int PRIMARY KEY, fingerprint text, applied_at timestamptz)")
        cur.execute("SELECT fingerprint FROM rag_schema_meta WHERE id = 1")
        row = cur.fetchone()
        conn.commit()
        if row and row[0] == fingerprint and not force:
            _schema_checked = True
            return
        # Raw cursor with no params: the trigger DDL uses format('%I', ...), which a
        # parameterized execute would treat as placeholders.
        cur.execute("SET lock_timeout = '5s'")
        cur.execute(sql)
        cur.execute("""INSERT INTO rag_schema_meta (id, fingerprint, applied_at) VALUES (1, %s, now())
                       ON CONFLICT (id) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, applied_at = now()""",
                    (fingerprint,))
        conn.commit()
        _schema_checked = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def active_index_version(conn) -> Tuple[int, str]:
    name = settings.collection_name()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM rag_index_versions WHERE collection_name = %s", (name,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT 1 FROM rag_index_versions WHERE status = 'active'")
        status = "building" if cur.fetchone() else "active"
        cur.execute("""INSERT INTO rag_index_versions (collection_name, embed_model, dims, chunker_version,
                       attribution_version, status, activated_at)
                       VALUES (%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'active' THEN now() END) RETURNING id""",
                    (name, settings.EMBED_MODEL, settings.EMBED_DIMS, settings.CHUNKER_VERSION,
                     settings.ATTRIBUTION_VERSION, status, status))
        row = (cur.fetchone()[0], status)
        conn.commit()
    return row[0], name


def _noop_progress(stage: str, status: str, **metrics: Any) -> None:
    pass


def sync(log=print, progress=None, rebuild: bool = False) -> Dict[str, Any]:
    """progress(stage, status, **metrics) receives structured step events (pipeline UI).
    rebuild=True re-embeds every chunk into a staging collection and swaps it in."""
    p = progress or _noop_progress
    t0 = time.time()
    p("schema", "running")
    ensure_schema()
    p("schema", "done")
    conn = engine.raw_connection()
    stats: Dict[str, Any] = {}
    try:
        index_id, collection = active_index_version(conn)
        p("render", "running")
        docs, ref = render_all(conn)
        conn.commit()          # release the read locks taken while rendering before writing anything
        stats["rendered_docs"] = len(docs)
        by_type: Dict[str, int] = {}
        for d in docs.values():
            by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
        p("render", "done", documents=len(docs), by_type=by_type)
        log(f"[copilot] rendered {len(docs)} documents in {time.time() - t0:.1f}s")
        p("version", "running")

        cur = conn.cursor()
        cur.execute("SELECT id, canonical_key, version, content_hash, deleted_at, simhash FROM rag_documents WHERE is_current")
        current = {r[1]: r for r in cur.fetchall()}
        # Backfill near-dup fingerprints for unchanged docs indexed before L5 existed
        missing_sim = [(simhash64(d.text), current[k][0]) for k, d in docs.items()
                       if d.doc_type in SIMHASH_TYPES and k in current and current[k][5] is None
                       and bytes(current[k][3]) == sha(d.text)]
        if missing_sim:
            execute_values(cur, "UPDATE rag_documents d SET simhash = v.s FROM (VALUES %s) AS v(s, id) WHERE d.id = v.id",
                           missing_sim, page_size=2000)

        new_versions = unchanged = undeleted = 0
        chunk_rows: Dict[bytes, Tuple[str, int]] = {}
        doc_chunk_rows: List[Tuple[int, int, bytes]] = []
        for key, d in docs.items():
            h = sha(d.text)
            cur_row = current.get(key)
            if cur_row and bytes(cur_row[3]) == h:
                if cur_row[4] is not None:
                    cur.execute("UPDATE rag_documents SET deleted_at = NULL WHERE id = %s", (cur_row[0],))
                    undeleted += 1
                unchanged += 1
                continue
            version = 1
            if cur_row:
                cur.execute("UPDATE rag_documents SET is_current = false, valid_to = now() WHERE id = %s", (cur_row[0],))
                version = cur_row[2] + 1
            cur.execute("""INSERT INTO rag_documents (canonical_key, version, is_current, doc_type, title, url,
                           published_at, content_hash, simhash, render_version, metadata)
                           VALUES (%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (key, version, d.doc_type, d.title[:500], d.url, d.published_at, h,
                         simhash64(d.text) if d.doc_type in SIMHASH_TYPES else None,
                         settings.RENDER_VERSION, json.dumps(d.metadata)))
            doc_id = cur.fetchone()[0]
            new_versions += 1
            for i, (ch_hash, ch_text, ntok) in enumerate(chunk(d, ref)):   # L3: identical chunk text -> one row
                chunk_rows[ch_hash] = (ch_text, ntok)
                doc_chunk_rows.append((doc_id, i, ch_hash))
        if chunk_rows:
            execute_values(cur, "INSERT INTO rag_chunks (chunk_hash, text, token_count) VALUES %s ON CONFLICT DO NOTHING",
                           [(k, v[0], v[1]) for k, v in chunk_rows.items()], page_size=1000)
        if doc_chunk_rows:
            execute_values(cur, "INSERT INTO rag_document_chunks (document_id, ordinal, chunk_hash) VALUES %s",
                           doc_chunk_rows, page_size=2000)

        # Tombstone documents whose sources disappeared
        gone = [r[0] for k, r in current.items() if k not in docs and r[4] is None]
        if gone:
            cur.execute("UPDATE rag_documents SET deleted_at = now() WHERE id = ANY(%s)", (gone,))

        # Entities + sources: full replace (cheap, keeps attribution rules authoritative)
        cur.execute("DELETE FROM rag_document_entities")
        ent_rows = {(k, e[0], e[1], e[2], e[3]): e[4] for k, d in docs.items() for e in d.entities}
        execute_values(cur, """INSERT INTO rag_document_entities (canonical_key, account_id, persona_id, lob_id,
                               relation, confidence) VALUES %s ON CONFLICT DO NOTHING""",
                       [(*k, v) for k, v in ent_rows.items()], page_size=2000)
        cur.execute("DELETE FROM rag_document_sources")
        execute_values(cur, "INSERT INTO rag_document_sources (canonical_key, source_table, source_pk) VALUES %s ON CONFLICT DO NOTHING",
                       [(k, t, pk) for k, d in docs.items() for t, pk in d.sources], page_size=2000)
        conn.commit()
        stats.update(new_versions=new_versions, unchanged=unchanged, undeleted=undeleted, tombstoned=len(gone),
                     new_chunks_seen=len(chunk_rows))
        log(f"[copilot] documents: {new_versions} new versions, {unchanged} unchanged, {len(gone)} tombstoned")
        p("version", "done", new_versions=new_versions, unchanged=unchanged, undeleted=undeleted,
          tombstoned=len(gone), new_chunks_seen=len(chunk_rows), entity_links=len(ent_rows))

        stats.update(index_documents(conn, index_id, collection, log, progress=p, rebuild=rebuild))
        stats["seconds"] = round(time.time() - t0, 1)
        cur.execute("""INSERT INTO rag_sync_state (source_table, last_reconcile_at, last_reconcile_stats)
                       VALUES ('*', now(), %s) ON CONFLICT (source_table) DO UPDATE
                       SET last_reconcile_at = now(), last_reconcile_stats = EXCLUDED.last_reconcile_stats""",
                    (json.dumps(stats),))
        conn.commit()
        return stats
    finally:
        conn.close()


def index_documents(conn, index_id: int, collection: str, log=print, progress=None,
                    rebuild: bool = False) -> Dict[str, Any]:
    """Reconcile Chroma with the set of (chunk, account) pairs current documents need (README §5.4).
    rebuild=True embeds every pair into "<collection>__rebuild", then swaps it in and rewrites the
    ledger, so chat keeps searching the old vectors until the new ones are complete."""
    p = progress or _noop_progress
    cur = conn.cursor()
    cur.execute("""
        SELECT dc.chunk_hash, e.account_id, min(d.doc_type), max(d.published_at),
               bool_or(e.persona_id IS NOT NULL)
        FROM rag_documents d
        JOIN rag_document_chunks dc ON dc.document_id = d.id
        JOIN rag_document_entities e ON e.canonical_key = d.canonical_key
        WHERE d.is_current AND d.deleted_at IS NULL
        GROUP BY dc.chunk_hash, e.account_id""")
    desired = {(bytes(r[0]), r[1]): r for r in cur.fetchall()}
    cur.execute("SELECT chunk_hash, account_id FROM rag_index_entries WHERE index_version_id = %s", (index_id,))
    existing = {(bytes(r[0]), r[1]) for r in cur.fetchall()}
    target = collection
    if rebuild:
        target = collection + "__rebuild"
        store.drop(target)                 # leftover from an interrupted rebuild
        to_add, to_remove = list(desired), []
    else:
        to_add = [k for k in desired if k not in existing]
        to_remove = [k for k in existing if k not in desired]
    p("embed", "running", done=0, total=len({h for h, _ in to_add}), model=settings.EMBED_MODEL,
      dims=settings.EMBED_DIMS)

    if to_add:
        hashes = list({h for h, _ in to_add})
        cur.execute("SELECT chunk_hash, text FROM rag_chunks WHERE chunk_hash = ANY(%s)", (hashes,))
        texts = {bytes(r[0]): r[1] for r in cur.fetchall()}
        log(f"[copilot] embedding {len(hashes)} distinct chunks for {len(to_add)} index entries…")
        t = time.time()
        vectors: Dict[bytes, List[float]] = {}
        step = 512
        for i in range(0, len(hashes), step):
            batch = hashes[i:i + step]
            for h, v in zip(batch, embed.embed_documents([texts[h] for h in batch])):
                vectors[h] = v   # each distinct text embedded once, reused across accounts
            done = min(i + step, len(hashes))
            log(f"[copilot]   {done}/{len(hashes)} embedded ({time.time() - t:.0f}s)")
            p("embed", "running", done=done, total=len(hashes), seconds=round(time.time() - t, 1))
        sample = vectors[hashes[0]]
        p("embed", "done", done=len(hashes), total=len(hashes), seconds=round(time.time() - t, 1),
          chunks_per_second=round(len(hashes) / max(time.time() - t, 0.001), 1),
          dims=len(sample), sample_norm=round(sum(x * x for x in sample) ** 0.5, 4),
          sample_head=[round(x, 4) for x in sample[:24]])
        p("index", "running", to_add=len(to_add), to_remove=len(to_remove), collection=target)
        ids, embs, docs_, metas = [], [], [], []
        for h, aid in to_add:
            _, _, doc_type, pub, person_scoped = desired[(h, aid)]
            ids.append(store.record_id(h, aid))
            embs.append(vectors[h])
            docs_.append(texts[h])
            metas.append({"account_id": int(aid), "doc_type": doc_type,
                          "published_ts": int(pub.timestamp()) if pub else 0,
                          "is_person_scoped": bool(person_scoped)})
        store.upsert(target, ids, embs, docs_, metas)    # Chroma first, then ledger (README §5.4)
        if rebuild:
            store.swap(target, collection)
            cur.execute("DELETE FROM rag_index_entries WHERE index_version_id = %s", (index_id,))
        execute_values(cur, "INSERT INTO rag_index_entries (index_version_id, chunk_hash, account_id) VALUES %s ON CONFLICT DO NOTHING",
                       [(index_id, h, a) for h, a in to_add], page_size=2000)
        conn.commit()
    else:
        p("embed", "done", done=0, total=0)
        p("index", "running", to_add=0, to_remove=len(to_remove), collection=target)
    if to_remove:
        store.delete(collection, [store.record_id(h, a) for h, a in to_remove])
        for h, a in to_remove:
            cur.execute("DELETE FROM rag_index_entries WHERE index_version_id=%s AND chunk_hash=%s AND account_id=%s",
                        (index_id, h, a))
        conn.commit()
    total = store.count(collection)
    log(f"[copilot] index: +{len(to_add)} / -{len(to_remove)} entries; Chroma now holds {total}")
    p("index", "done", added=len(to_add), removed=len(to_remove), chroma_total=total, ledger_total=len(desired),
      rebuilt=rebuild)
    return {"index_added": len(to_add), "index_removed": len(to_remove), "index_total": total,
            "ledger_total": len(desired)}
