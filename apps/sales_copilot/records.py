"""Copilot: one deal or introduction — its details, and changes to it (README §19).

  deal_info     "show the BNY pilot deal", "what's the status of the BlackRock deal?"
  intro_info    "status of the introduction to Robin Vince", "show the intro via Priya"
  update_deal   "set the BNY pilot value to 300k", "change the close date of the DTCC deal to 15 Dec"
  update_intro  "mark the intro to Robin as accepted", "update the next step of the intro to Robin to …"

Details come straight from the database (no LLM). Changes are proposals (extras.actions) the user reviews and
confirms in the chat; the page then calls PATCH /api/deals/{id} or PATCH /api/crm/introductions/{id}, so the
usual permission and stage rules apply. Stage moves of a deal are actions.move_turn.
"""
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from apps.sales_copilot import actions

INTRO_WORD = r"\b(introductions?|intros?)\b(?!\s+(e-?mails?|messages?|notes?|calls?))"
INTRO_STATUS = {"proposed": "proposed", "requested": "requested", "accepted": "accepted", "intro made": "intro_made",
                "introduced": "intro_made", "meeting held": "meeting_held", "met": "meeting_held", "converted": "converted",
                "declined": "declined", "stale": "stale"}
INTRO_TARGET_RE = re.compile(r"\b(?:to|as|into)\s+(?:the\s+)?(" + "|".join(sorted(INTRO_STATUS, key=len, reverse=True)) + r")\b", re.I)
INFO_RE = re.compile(r"\b(show|open|view|details?|status|summary|summari[sz]e|overview|tell me about|where are we|how is|"
                     r"how's|what'?s happening|what is happening|look up|find)\b", re.I)
DEAL_WORD = r"\b(deal|opportunity)\b"          # singular on purpose: "deals" is the pipeline list (chat._answer_deals)
UPDATE_VERB = r"\b(update|change|set|edit|make|bump|increase|reduce|lower|raise|rename|move)\b"
DEAL_FIELDS = {
    "value_amount": r"\b(value|amount|size|worth|price)\b",
    "expected_close": r"\b(close date|expected close|closing date|close)\b",
    "next_step_due": r"\b(next step due|due date)\b",
    "next_step": r"\bnext step\b",
    "probability": r"\b(probability|likelihood|chance)\b",
    "forecast_category": r"\b(forecast|category)\b",
    "name": r"\b(rename|deal name)\b",
}
FIELD_LABEL = {"value_amount": "Value", "expected_close": "Expected close", "next_step": "Next step",
               "next_step_due": "Next step due", "probability": "Probability %", "forecast_category": "Forecast category",
               "name": "Deal name"}
FORECAST = {"commit": "commit", "best case": "best_case", "best-case": "best_case", "pipeline": "pipeline", "omitted": "omitted"}


def detect(q: str) -> Optional[str]:
    """One record: 'update_intro' | 'update_deal' | 'intro_info' | 'deal_info' | None."""
    intro = re.search(INTRO_WORD, q, re.I)
    if intro and (INTRO_TARGET_RE.search(q) and re.search(r"\b(mark|move|set|change|update)\b", q, re.I)
                  or re.search(UPDATE_VERB + r".{0,60}\bnext step\b", q, re.I)):
        return "update_intro"
    if not intro and re.search(UPDATE_VERB, q, re.I) and any(re.search(p, q, re.I) for p in DEAL_FIELDS.values()) \
            and (re.search(DEAL_WORD, q, re.I) or re.search(r"\brename\b", q, re.I)) and not actions.STAGE_TARGET_RE.search(q):
        return "update_deal"
    if INFO_RE.search(q):
        if intro:
            return "intro_info"
        if re.search(DEAL_WORD, q, re.I):
            return "deal_info"
    return None


# ── Finding the record ────────────────────────────────────────────────────────

STOP = actions.NAME_STOP | {"show", "open", "view", "details", "detail", "status", "summary", "overview", "tell", "about",
                            "where", "are", "how", "what", "whats", "happening", "look", "find", "introduction",
                            "introductions", "intro", "intros", "via", "from", "who", "value", "amount", "close", "date",
                            "expected", "step", "probability", "forecast", "category", "rename", "name", "edit",
                            "increase", "reduce", "lower", "raise", "make", "proposed", "requested", "accepted",
                            "declined", "stale", "converted", "made", "held", "meeting", "of", "is"}


def _words(t: Dict[str, Any]) -> List[str]:
    acct = {w for a in t["accounts"] for w in re.findall(r"[a-z0-9]{3,}", a["name"].lower())}
    q = re.sub(r"\b(to|as|into)\s+.*$", "", t["q"].lower()) if re.search(UPDATE_VERB, t["q"], re.I) else t["q"].lower()
    return [w for w in re.findall(r"[a-z0-9]{3,}", q) if w not in STOP and w not in acct]


def find_intros(session, t: Dict[str, Any], recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Introductions the user might mean, best first: by the contact in focus, then words of the contact, connector
    or account name. Only introductions the user can see (the Introductions page's scope)."""
    import auth
    from apps.sales_crm import introductions as intro_api
    rows = intro_api._q(session, intro_api.BASE_SQL + f" WHERE {intro_api._internal_scope_sql()} ORDER BY i.updated_at DESC LIMIT 300",
                        acl=auth.account_scope(session, t["user"]))
    ids = {a["id"] for a in actions._scope_accounts(t, recent)}
    pid = t["persona"]["id"] if t["persona"] else None
    words = _words(t)
    scored = []
    for r in rows:
        v = intro_api.internal_view(r)
        contact = v.get("persona_name") or (v.get("submitted") or {}).get("contact_name") or ""
        hay = " ".join([contact, v["connector"]["name"] or "", v.get("account_name") or ""]).lower()
        score = (6 if pid and v.get("persona_id") == pid else 0) + sum(2 for w in words if w in hay) \
            + (1 if v.get("account_id") in ids else 0) + (1 if v["status"] in intro_api.OPEN_STATUSES else 0)
        if score > 1:
            scored.append((score, {**v, "contact": contact or "Contact not chosen"}))
    scored.sort(key=lambda x: -x[0])
    if scored:
        scored = [x for x in scored if x[0] >= scored[0][0] - 1]
    return [v for _, v in scored[:5]]


def _deal_detail(session, t: Dict[str, Any], deal_id: int) -> Dict[str, Any]:
    from apps.sales_deals import api as deals_api
    return deals_api._deal_payload(session, deals_api._get_deal_row(session, deal_id, t["user"]), full=True)


def _money(v, cur="USD") -> str:
    return f"{v:,.0f} {cur}" if v is not None else "—"


def _day(d) -> str:
    return date.fromisoformat(str(d)[:10]).strftime("%d %b %Y") if d else "—"


# ── Details ───────────────────────────────────────────────────────────────────

def deal_info_turn(session, t: Dict[str, Any]) -> None:
    recent = actions.recent_extras(session, t["sid"])
    found = actions.find_deals(session, t, recent)
    if not found:
        t["answer"] = "I couldn't find that deal. Name it or mention its account with **@**, or ask *my open deals*."
        t["extras"]["followups"] = ["My open deals"]
        return
    if len(found) > 1 and not t.get("deal_id"):
        t["extras"]["table"] = {"columns": ["deal", "account", "stage"], "kind": "deals",
                                "rows": [{"deal": d["name"], "account": d["account"], "stage": actions.STAGE_LABEL.get(d["from"]),
                                          "deal_id": d["deal_id"]} for d in found]}
        t["answer"] = f"**{len(found)}** deals could match — which one? Ask again with more of its name."
        t["extras"]["followups"] = [f"Show the {d['name']} deal" for d in found[:2]]
        return
    d = _deal_detail(session, t, found[0]["deal_id"])
    h = d.get("health") or {}
    overdue = d.get("next_step_due") and str(d["next_step_due"])[:10] < date.today().isoformat()
    lines = [f"**{d['name']}** · {d['account_name']}", "",
             f"- **Stage:** {d['stage_label']}" + (f" · {h['days_in_stage']} days in stage" if h.get("days_in_stage") is not None else "")
             + f" · **Value:** {_money(d.get('value_amount'), d.get('currency') or 'USD')} · **Expected close:** {_day(d.get('expected_close'))}",
             f"- **Next step:** {d.get('next_step') or '—'}" + (f" (due {_day(d['next_step_due'])}{', overdue' if overdue else ''})"
                                                                 if d.get("next_step_due") else "")]
    if h.get("score") is not None:
        lines.append(f"- **Health:** {h['score']}/100" + (" — " + "; ".join(h.get("gaps", [])[:2]) if h.get("gaps") else ""))
    lines.append(f"- **Buying committee:** {len(d.get('stakeholders') or [])} people"
                 + (f" (missing {', '.join(d['committee_gaps'])})" if d.get("committee_gaps") else "")
                 + f" · **Open tasks:** {d.get('open_tasks') or 0}"
                 + (f" · **Owner:** {d['owner']['name']}" if (d.get("owner") or {}).get("name") else ""))
    t["answer"] = "\n".join(lines)
    t["extras"]["record"] = {"type": "deal", "id": d["id"], "href": f"/deals?deal={d['id']}", "label": "Open deal room"}
    short = d["name"]
    nxt = actions.STAGES[actions.STAGES.index(d["stage"]) + 1] if d["stage"] in actions.STAGES[:-1] else None
    t["extras"]["next_actions"] = ([f"Move the {short} deal to {actions.STAGE_LABEL[nxt]}"] if nxt else []) + \
        [f"Update the next step of the {short} deal"]


def intro_info_turn(session, t: Dict[str, Any]) -> None:
    from apps.sales_crm import introductions as intro_api
    recent = actions.recent_extras(session, t["sid"])
    found = find_intros(session, t, recent)
    if not found:
        t["answer"] = "I couldn't find that introduction. Name the contact or connector, or mention the account with **@**."
        return
    if len(found) > 1:
        t["extras"]["table"] = {"columns": ["contact", "account", "connector", "status"], "kind": "intros",
                                "rows": [{"contact": v["contact"], "account": v.get("account_name"), "connector": v["connector"]["name"],
                                          "status": v["status_label"]} for v in found]}
        t["answer"] = f"**{len(found)}** introductions could match — which one? Ask again with the contact's name."
        return
    v = found[0]
    deal = v.get("deal")
    lines = [f"**Introduction to {v['contact']}** at {v.get('account_name') or '—'} via **{v['connector']['name']}**", "",
             f"- **Status:** {v['status_label']} · {v.get('days_in_status', 0)} days" + (f" · **Owner:** {v['owner_name']}" if v.get("owner_name") else ""),
             f"- **Next step:** {v.get('next_step') or '—'}"]
    if v.get("context"):
        lines.append(f"- **Why:** {v['context'][:240]}")
    if deal:
        lines.append(f"- **Deal:** {deal.get('name')} · {deal.get('stage')} · {_money(deal.get('value_amount'), deal.get('currency') or 'USD')}")
    t["answer"] = "\n".join(lines)
    t["extras"]["record"] = {"type": "introduction", "id": v["id"], "href": f"/introductions?intro={v['id']}", "label": "Open introduction"}
    board = intro_api.STATUSES[:6]
    nxt = board[board.index(v["status"]) + 1] if v["status"] in board[:5] else None
    first = v["contact"].split(" ")[0] if v["contact"] else "them"
    t["extras"]["next_actions"] = ([f"Mark the intro to {v['contact']} as {intro_api.STATUS_LABEL[nxt].lower()}"]
                                   if nxt and nxt != "converted" else []) + [f"Update the next step of the intro to {first}"]


# ── Changes ───────────────────────────────────────────────────────────────────

MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
        "cr": 1e7, "crore": 1e7, "lakh": 1e5}


def _after(q: str, field_pat: str) -> Optional[str]:
    """Text after "<field> to|=|: …" — "set the value to 300k" → "300k"."""
    m = re.search(field_pat + r".{0,40}?\b(?:to|=|as|on|by|:)\s+(.+?)\s*[.?!]*$", q, re.I)
    return m.group(m.lastindex).strip() if m else None


def _parse_date(s: Optional[str]) -> str:
    if not s:
        return ""
    try:
        from dateutil import parser
        d = parser.parse(s, fuzzy=True, default=datetime(date.today().year, date.today().month, 1)).date()
        if not re.search(r"\b\d{1,2}\b|\d{4}", s):           # "December" alone → its last day is a guess; leave it to the user
            return ""
        if d < date.today() and not re.search(r"\d{4}", s):  # "15 Dec" in late December next year
            d = d.replace(year=d.year + 1)
        return d.isoformat()
    except (ValueError, OverflowError):
        return ""


def deal_changes(q: str) -> Dict[str, Any]:
    """Field → proposed new value parsed from the question ('' = asked to change it, value not given)."""
    out: Dict[str, Any] = {}
    for key, pat in DEAL_FIELDS.items():
        if not re.search(pat, q, re.I) or (key == "expected_close" and re.search(DEAL_FIELDS["next_step_due"], q, re.I)):
            continue
        if key == "next_step" and re.search(DEAL_FIELDS["next_step_due"], q, re.I):
            continue
        raw = _after(q, pat)
        if key == "value_amount":
            m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|mn|mm|million|thousand|bn|b|cr|crore|lakh)?\b", raw or "", re.I)
            out[key] = float(m.group(1).replace(",", "")) * MULT.get((m.group(2) or "").lower(), 1) if m else ""
        elif key in ("expected_close", "next_step_due"):
            out[key] = _parse_date(raw)
        elif key == "probability":
            m = re.search(r"(\d{1,3})", raw or "")
            out[key] = min(100, int(m.group(1))) if m else ""
        elif key == "forecast_category":
            m = re.search(r"\b(commit|best[- ]case|pipeline|omitted)\b", q, re.I)
            out[key] = FORECAST.get(m.group(1).lower().replace("-", " ")) if m else ""
        elif key == "name":
            out[key] = (raw or "")[:200]
        else:
            out[key] = (raw or "")[:500]
    return out


def update_deal_turn(session, t: Dict[str, Any]) -> None:
    recent = actions.recent_extras(session, t["sid"])
    found = actions.find_deals(session, t, recent)
    if not found:
        t["answer"] = "Which deal? Name it or mention its account with **@**, e.g. *set the @BNY pilot deal value to 300k*."
        return
    changes = deal_changes(t["q"]) or {"value_amount": "", "expected_close": "", "next_step": ""}
    d = _deal_detail(session, t, found[0]["deal_id"])
    fields = [{"key": k, "label": FIELD_LABEL[k], "current": d.get(k), "value": v} for k, v in changes.items()]
    t["extras"]["actions"] = {"kind": "update_deal", "currency": d.get("currency") or "USD", "fields": fields,
                              "items": [{**f, "from_label": actions.STAGE_LABEL.get(f["from"], f["from"]), "skip": n > 0}
                                        for n, f in enumerate(found)]}
    t["answer"] = (f"Here's the change for **{d['name']}** ({d['account_name']}) — check the new "
                   f"value{'s' if len(fields) > 1 else ''}, then **Save**."
                   + (" Other deals that could match are listed; tick the right one." if len(found) > 1 else ""))


def update_intro_turn(session, t: Dict[str, Any]) -> None:
    from apps.sales_crm import introductions as intro_api
    recent = actions.recent_extras(session, t["sid"])
    found = find_intros(session, t, recent)
    if not found:
        t["answer"] = "Which introduction? Name the contact or connector, e.g. *mark the intro to Robin Vince as accepted*."
        return
    m = INTRO_TARGET_RE.search(t["q"])
    target = INTRO_STATUS[m.group(1).lower()] if m and re.search(r"\b(mark|move|set|change)\b", t["q"], re.I) \
        and not re.search(r"\bnext step\b", t["q"], re.I) else None
    v = found[0]
    nxt = _after(t["q"], r"\bnext step\b") if re.search(r"\bnext step\b", t["q"], re.I) else None
    t["extras"]["actions"] = {
        "kind": "update_intro", "status": target or v["status"], "next_step": nxt if nxt is not None else (v.get("next_step") or ""),
        "statuses": [{"key": k, "label": intro_api.STATUS_LABEL[k]} for k in intro_api.STATUSES if k != "converted"],
        "items": [{"intro_id": x["id"], "name": x["contact"], "account": x.get("account_name") or "", "connector": x["connector"]["name"],
                   "from": x["status"], "from_label": x["status_label"], "next_step_now": x.get("next_step") or "", "skip": n > 0}
                  for n, x in enumerate(found)]}
    note = (" Converting creates a deal — do that from the introduction (Convert), so the deal gets its details."
            if target == "converted" else "")
    if target == "converted":
        t["extras"]["actions"]["status"] = v["status"]
    t["answer"] = (f"Here's the update for the introduction to **{v['contact']}** via {v['connector']['name']} "
                   f"(now **{v['status_label']}**) — check it, then **Save**.{note}")
