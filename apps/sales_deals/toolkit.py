"""Stage toolkits for the deal room (apps/sales_copilot/README.md §21.1, phase D3).

Everything here is computed from data already in the DB — NO LLM requests:
  intro      why-now triggers (signals, leadership moves, hiring, news) + who to approach
  discovery  question bank per stakeholder (from their KPIs / pains / objections) + MEDDICC tracker
  proposal   value map (offering ↔ evidence), competitive battlecard, objection → response pack
  pilot      success criteria (committee KPIs), 6-week plan, risk alerts
  contract   approval chain, procurement/security prep, expansion map (other LOBs / offerings)
Anything that needs writing (an email, a proposal outline) links to the copilot with a
prefilled question, so AI requests stay explicit and quota-governed.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import text

OFFERINGS = {"ai": "Applied AI", "data": "Data Analytics", "cyber": "Cybersecurity", "cloud": "Cloud & Infrastructure",
             "testing": "Automated AI Testing", "digital_assets": "Digital Assets & Blockchain"}
# Mirrors STRADIT_OFFERINGS keyword lists in frontend/js/modules/constants.js
OFFERING_KEYWORDS = {
    "ai": r"artificial intelligence|\bai\b|\bllm|generative ai|genai|machine learning|copilot|\bagents?\b|agentic",
    "data": r"data quality|data integration|predictive|forecast|dashboard|analytics|business intelligence|reporting",
    "cyber": r"cyber|security|breach|ransomware|threat|vulnerabilit|incident response|data protection|identity",
    "cloud": r"cloud|\baws\b|azure|\bgcp\b|data cent|moderni[sz]ation|multi-cloud|legacy|infrastructure",
    "testing": r"test automation|quality engineering|\bqa\b|testing|regression",
    "digital_assets": r"digital asset|tokeni[sz]|blockchain|stablecoin|crypto|custody|smart contract|on-chain",
}
MEDDICC = [("metrics", "Metrics", "What measurable outcome will they judge success by?"),
           ("economic_buyer", "Economic buyer", "Who signs off the budget?"),
           ("decision_criteria", "Decision criteria", "What will they compare vendors on?"),
           ("decision_process", "Decision process", "Steps, people and timeline to a signed decision"),
           ("identify_pain", "Identified pain", "The business pain this deal fixes"),
           ("champion", "Champion", "Who sells this internally when you're not in the room?"),
           ("competition", "Competition", "Alternatives they're considering, including doing nothing")]


def _q(s, sql: str, **kw) -> List[Dict[str, Any]]:
    return [dict(r) for r in s.execute(text(sql), kw).mappings()]


def _account(s, account_id: int) -> Dict[str, Any]:
    return _q(s, "SELECT id, key, display_name, legal_name, aliases FROM accounts WHERE id = :a", a=account_id)[0]


def _account_like(acct: Dict[str, Any]) -> List[str]:
    names = {acct["display_name"], acct["legal_name"], *(acct.get("aliases") or [])}
    return [f"%{n}%" for n in names if n and len(n) >= 3]


def _committee(s, deal_id: int) -> List[Dict[str, Any]]:
    return _q(s, """
        SELECT ds.persona_id, ds.role, ds.sentiment, coalesce(p.full_name, p.display_name) AS name, p.title,
               p.target_kpis, p.operational_pain_points, p.key_objections, p.extended_profile -> 'callprep' AS callprep,
               p.budget_authority, p.decision_authority, p.hierarchy_level, p.account_id, p.lob_id
        FROM deal_stakeholders ds JOIN personas p ON p.id = ds.persona_id WHERE ds.deal_id = :d""", d=deal_id)


def _lc(phrase: str) -> str:
    """Lower-case the first letter for mid-sentence use, keeping acronyms (AI, MLOps, KYC) intact."""
    p = (phrase or "").strip().rstrip(".")
    return p[0].lower() + p[1:] if len(p) > 1 and p[1].islower() else p


# ── Intro ─────────────────────────────────────────────────────────────────────


def intro(s, deal: Dict[str, Any]) -> Dict[str, Any]:
    acct = _account(s, deal["account_id"])
    likes = _account_like(acct)
    signals = _q(s, """SELECT title, category, details ->> 'summary' AS summary, last_seen FROM opportunity_signals
                       WHERE account_id = :a ORDER BY last_seen DESC NULLS LAST LIMIT 40""", a=deal["account_id"])
    seen, why_now = set(), []
    for x in signals:
        key = (x["summary"] or x["title"] or "")[:80]
        if key in seen:
            continue
        seen.add(key)
        why_now.append({"kind": "signal", "title": x["title"], "detail": (x["summary"] or "")[:220],
                        "date": x["last_seen"], "label": (x["category"] or "signal").replace("_", " ")})
        if len(why_now) >= 5:
            break
    moves = _q(s, """SELECT person_name, event_type, designation, effective_date, article_title, article_url, first_seen
                     FROM cxo_movements WHERE company_name ILIKE ANY(:likes) OR target_key = :k
                     ORDER BY first_seen DESC LIMIT 5""", likes=likes, k=acct["key"])
    for m in moves:
        why_now.append({"kind": "leadership", "title": f"{m['person_name']} — {m['event_type'] or 'change'} {m['designation'] or ''}".strip(),
                        "detail": m["article_title"] or "", "url": m["article_url"], "date": m["effective_date"] or m["first_seen"],
                        "label": "leadership change"})
    hiring = _q(s, """SELECT d.title, d.published_at FROM rag_documents d
                      JOIN rag_document_entities e ON e.canonical_key = d.canonical_key AND e.account_id = :a
                      WHERE d.doc_type = 'job_theme' AND d.is_current ORDER BY d.published_at DESC LIMIT 1""", a=deal["account_id"])
    if hiring:
        body = _q(s, """SELECT c.text FROM rag_documents d JOIN rag_document_chunks dc ON dc.document_id = d.id
                        JOIN rag_chunks c ON c.chunk_hash = dc.chunk_hash
                        WHERE d.title = :t AND d.is_current LIMIT 1""", t=hiring[0]["title"])
        why_now.append({"kind": "hiring", "title": hiring[0]["title"], "label": "hiring",
                        "detail": (body[0]["text"].split("\n", 1)[-1] if body else "")[:240], "date": hiring[0]["published_at"]})

    on_deal = {x["persona_id"] for x in _committee(s, deal["id"])}
    people = _q(s, """
        SELECT p.id, coalesce(p.full_name, p.display_name) AS name, p.title, p.decision_authority, p.budget_authority,
               p.tier, (p.value_proposition IS NOT NULL) AS has_callprep, p.personalized_icebreaker, p.lob_id,
               EXISTS (SELECT 1 FROM posts po WHERE po.target_key = p.key AND po.kind = 'person'
                       AND po.channel IN ('linkedin','news') AND po.first_seen > now() - interval '60 days') AS recently_active
        FROM personas p WHERE p.account_id = :a
        ORDER BY (p.decision_authority ILIKE '%final%') DESC, (p.value_proposition IS NOT NULL) DESC,
                 p.hierarchy_level NULLS LAST LIMIT 40""", a=deal["account_id"])
    offering_re = "|".join(OFFERING_KEYWORDS[o] for o in deal["offerings"] if o in OFFERING_KEYWORDS)
    ranked = []
    for p in people:
        if p["id"] in on_deal:
            continue
        score, why = 0, []
        if "final" in (p["decision_authority"] or "").lower():
            score += 3; why.append("final decision authority")
        if p["has_callprep"]:
            score += 2; why.append("call-prep ready")
        if p["recently_active"]:
            score += 2; why.append("active publicly in the last 60 days")
        if offering_re and re.search(offering_re, p["title"] or "", re.I):
            score += 2; why.append("title matches the deal's offerings")
        if deal.get("lob_id") and p["lob_id"] == deal["lob_id"]:
            score += 2; why.append("in the deal's line of business")
        ranked.append({**p, "score": score, "why": why})
    ranked.sort(key=lambda x: -x["score"])
    return {"why_now": why_now[:8], "who_to_approach": ranked[:6],
            "prompts": [f"Draft a short intro email to {p['name']}" for p in ranked[:2]]}


# ── Discovery ─────────────────────────────────────────────────────────────────


def discovery(s, deal: Dict[str, Any]) -> Dict[str, Any]:
    committee = _committee(s, deal["id"])
    banks = []
    for p in committee:
        qs = []
        for pain in (p["operational_pain_points"] or [])[:3]:
            qs.append(f"How is {_lc(pain)} affecting your team today, and what have you already tried?")
        for kpi in (p["target_kpis"] or [])[:2]:
            qs.append(f"How do you measure {_lc(kpi)} today, and where does it need to be in 12 months?")
        objections = ((p["callprep"] or {}).get("objections") or [{"objection": o} for o in (p["key_objections"] or [])])[:2]
        for o in objections:
            if isinstance(o, dict) and o.get("objection"):
                qs.append(f"Pre-empt: \"{o['objection']}\" — what would you need to see to be comfortable on that?")
        if p["role"] == "economic_buyer":
            qs.append("What has to be true for this to get budget this year, and who else weighs in?")
        if p["role"] == "technical_evaluator":
            qs.append("What would a technical evaluation need to prove, and on what timeline?")
        if not qs:
            qs = ["What are your top priorities for the next two quarters?",
                  "Where do current processes slow your team down the most?",
                  "How are decisions like this usually made and approved?"]
        banks.append({"persona_id": p["persona_id"], "name": p["name"], "role": p["role"], "questions": qs[:6]})
    q = deal.get("qualification") or {}
    roles = {x["role"]: x["name"] for x in committee}
    pains = sorted({pp for x in committee for pp in (x["operational_pain_points"] or [])})
    competitors = _competitors(s, deal)
    meddicc = []
    for key, label, hint in MEDDICC:
        suggested = {"economic_buyer": roles.get("economic_buyer"), "champion": roles.get("champion"),
                     "identify_pain": "; ".join(pains[:3]) or None,
                     "competition": ", ".join(c["name"] for c in competitors[:3]) or None}.get(key)
        meddicc.append({"key": key, "label": label, "hint": hint, "value": q.get(key) or "", "suggested": suggested or ""})
    filled = sum(1 for m in meddicc if m["value"] or m["suggested"])
    return {"question_bank": banks, "meddicc": meddicc, "meddicc_score": f"{filled}/{len(meddicc)}",
            "generic_questions": ["What happens if you do nothing this year?", "Who else is affected by this problem?",
                                  "What does the approval process look like for a project this size?"]}


# ── Proposal ──────────────────────────────────────────────────────────────────


def _competitors(s, deal: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = _q(s, "SELECT competitors FROM lobs WHERE account_id = :a AND competitors IS NOT NULL", a=deal["account_id"])
    counts: Dict[str, int] = {}
    for r in rows:
        for c in (r["competitors"] or []):
            counts[str(c)] = counts.get(str(c), 0) + 1
    return [{"name": k, "lobs": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:6]]


def proposal(s, deal: Dict[str, Any]) -> Dict[str, Any]:
    value_map = []
    for o in deal["offerings"] or []:
        if o not in OFFERING_KEYWORDS:
            continue
        ev = _q(s, """
            SELECT DISTINCT ON (d.canonical_key) d.title, d.doc_type, d.url, d.published_at
            FROM rag_documents d JOIN rag_document_entities e ON e.canonical_key = d.canonical_key AND e.account_id = :a
            JOIN rag_document_chunks dc ON dc.document_id = d.id JOIN rag_chunks c ON c.chunk_hash = dc.chunk_hash
            WHERE d.is_current AND d.deleted_at IS NULL AND d.doc_type IN ('signal','news','blog','linkedin_post','job_theme','digest_channel')
              AND c.text ~* :re
            ORDER BY d.canonical_key, d.published_at DESC NULLS LAST LIMIT 40""", a=deal["account_id"], re=OFFERING_KEYWORDS[o])
        ev.sort(key=lambda x: x["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        value_map.append({"offering": o, "label": OFFERINGS[o], "evidence": ev[:4], "evidence_count": len(ev)})
    committee = _committee(s, deal["id"])
    pack = []
    for p in committee:
        cp = p["callprep"] or {}
        for ob in (cp.get("objections") or [{"objection": o, "counter": ""} for o in (p["key_objections"] or [])])[:3]:
            if isinstance(ob, dict) and ob.get("objection"):
                pack.append({"who": p["name"], "role": p["role"], "objection": ob["objection"], "response": ob.get("counter") or ""})
    acct = _account(s, deal["account_id"])
    comp = _competitors(s, deal)
    for c in comp:
        c["mentions"] = s.execute(text("""
            SELECT count(DISTINCT d.id) FROM rag_documents d
            JOIN rag_document_entities e ON e.canonical_key = d.canonical_key AND e.account_id = :a
            JOIN rag_document_chunks dc ON dc.document_id = d.id JOIN rag_chunks ch ON ch.chunk_hash = dc.chunk_hash
            WHERE d.is_current AND ch.text ILIKE :n"""), {"a": deal["account_id"], "n": f"%{c['name'].split(' ')[0]}%"}).scalar()
    kpis = sorted({k for p in committee for k in (p["target_kpis"] or [])})
    pains = sorted({k for p in committee for k in (p["operational_pain_points"] or [])})
    return {"value_map": value_map, "battlecard": {"competitors": comp,
            "positioning": [f"Anchor on {acct['display_name']}'s own priorities (see value map) rather than feature comparisons.",
                            "Offer a scoped pilot with agreed success criteria to lower switching risk.",
                            "Bring a reference architecture that fits their existing stack (see LOB technologies)."]},
            "objection_pack": pack, "business_case": {"pains": pains[:5], "kpis": kpis[:5]},
            "prompts": [f"Draft a one-page proposal outline for {acct['display_name']} covering "
                        + ", ".join(OFFERINGS[o] for o in deal["offerings"] if o in OFFERINGS)]}


# ── Pilot ─────────────────────────────────────────────────────────────────────


def pilot(s, deal: Dict[str, Any]) -> Dict[str, Any]:
    committee = _committee(s, deal["id"])
    kpis = sorted({k for p in committee for k in (p["target_kpis"] or [])})
    criteria = [f"Agreed baseline and target for: {k}" for k in kpis[:4]] or \
        ["Agree 2-3 measurable outcomes with the champion before kickoff"]
    start = date.today() + timedelta(days=7)
    plan = [{"week": "Week 0", "date": start.isoformat(), "milestone": "Kickoff: success criteria, data access, owners signed off"},
            {"week": "Week 1-2", "date": (start + timedelta(days=14)).isoformat(), "milestone": "Build + first results on a narrow use case"},
            {"week": "Week 3-4", "date": (start + timedelta(days=28)).isoformat(), "milestone": "Expand to target users; weekly check-ins"},
            {"week": "Week 5", "date": (start + timedelta(days=35)).isoformat(), "milestone": "Measure against criteria; security review"},
            {"week": "Week 6", "date": (start + timedelta(days=42)).isoformat(), "milestone": "Results readout with economic buyer; decision"}]
    risks = []
    last = s.execute(text("SELECT max(created_at) FROM deal_activity WHERE deal_id = :d"), {"d": deal["id"]}).scalar()
    if last and (datetime.now(timezone.utc) - last).days > 14:
        risks.append(f"No activity for {(datetime.now(timezone.utc) - last).days} days")
    for p in committee:
        if p["sentiment"] == "negative" or p["role"] == "blocker":
            risks.append(f"{p['name']} is a blocker / negative — plan how to address their concerns")
        moved = s.execute(text("""SELECT 1 FROM cxo_movements WHERE lower(person_name) = lower(:n)
                                  AND first_seen > now() - interval '90 days' LIMIT 1"""), {"n": p["name"]}).scalar()
        if moved:
            risks.append(f"{p['name']} ({p['role'].replace('_', ' ')}) had a recent leadership change — re-confirm sponsorship")
    if "champion" not in {p["role"] for p in committee}:
        risks.append("No champion — pilots without an internal champion rarely convert")
    return {"success_criteria": criteria, "plan": plan, "risks": risks or ["No risks detected from the data"]}


# ── Contract ──────────────────────────────────────────────────────────────────


def contract(s, deal: Dict[str, Any]) -> Dict[str, Any]:
    committee = _committee(s, deal["id"])
    order = {"economic_buyer": 0, "champion": 1, "technical_evaluator": 2, "influencer": 3, "user": 4, "blocker": 5}
    chain = sorted(committee, key=lambda p: (0 if "sign-off" in (p["budget_authority"] or "").lower() else 1,
                                            order.get(p["role"], 9), p["hierarchy_level"] or 9))
    lobs = _q(s, """SELECT l.id, l.lob_name, count(p.id) AS contacts FROM lobs l LEFT JOIN personas p ON p.lob_id = l.id
                    WHERE l.account_id = :a AND l.id IS DISTINCT FROM :lob GROUP BY l.id ORDER BY 3 DESC, 2 LIMIT 5""",
             a=deal["account_id"], lob=deal.get("lob_id"))
    return {"approval_chain": [{"name": p["name"], "title": p["title"], "role": p["role"], "budget": p["budget_authority"]} for p in chain],
            "procurement_prep": ["Security questionnaire and InfoSec review pack ready", "Data processing / residency terms",
                                 "Vendor onboarding documents (insurance, certifications)", "Commercial terms and approval thresholds",
                                 "Legal redlines owner and turnaround agreed"],
            "expansion": {"other_offerings": [{"key": k, "label": v} for k, v in OFFERINGS.items() if k not in (deal["offerings"] or [])],
                          "other_lobs": lobs}}


BUILDERS = {"intro": intro, "discovery": discovery, "proposal": proposal, "pilot": pilot, "contract": contract}
