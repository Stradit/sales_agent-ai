"""Copilot actions: spreadsheet downloads and "create …" proposals (README §19).

Two intents are answered here, straight from the database (no LLM, no quota):
  export  "give me this in Excel", "download the BNY VPs as CSV" → a table the page offers as Excel / CSV
  create  "create a task for each of them", "add a deal for BNY" → a proposal card (extras.actions)

Nothing in this module writes CRM records. A proposal lists the people / accounts it would use; the user
reviews it in the chat and confirms, and the page then calls the normal CRM endpoints (tasks, deals,
introductions, wishlist), so the usual permission and validation rules apply to every record.
"""
import re
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text

EXPORT_RE = re.compile(r"\b(excel|xlsx|csv|spreadsheets?|download(?:able)?|export)\b", re.I)
# "open" is left out on purpose: "open deals at BNY" is a pipeline question
CREATE_RE = re.compile(r"\b(create|add|make|log|start|set ?up|raise|request|schedule)\b.{0,60}?"
                       r"\b(deals?|opportunit(?:y|ies)|introductions?|intros?|tasks?|to-?dos?|follow[- ]?ups?|wishlist)\b", re.I)
# "log a call with Robin", "I just met BNY", "add a note about the pilot" → an activity (call/meeting/email/…)
ACTIVITY_RE = re.compile(r"\b(log|record|add|save|track)\b.{0,30}?\b(phone call|call|meeting|e-?mail|linkedin|conversation|note)s?\b"
                         r"|\bI\s+(just\s+)?(called|met|emailed|spoke\s+(to|with)|had\s+(a|an)\s+(call|meeting))\b", re.I)
# "move the BNY pilot to Contract", "mark the BlackRock deal as won", "advance the DTCC deal to the next stage"
STAGE_KEYS = ["intro", "discovery", "proposal", "pilot", "contract", "won", "lost"]
STAGE_TARGET_RE = re.compile(r"\b(?:to|as|into)\s+(?:the\s+)?(?:stage\s+)?(?:closed[- ])?(" + "|".join(STAGE_KEYS) + r")\b", re.I)
MOVE_RE = re.compile(r"\b(move|advance|push|progress|bump|shift|mark|set|change|update)\b.{0,80}?"
                     r"(\b(?:to|as|into)\s+(?:the\s+)?(?:stage\s+)?(?:closed[- ])?(?:" + "|".join(STAGE_KEYS) + r")\b|\bnext stage\b)", re.I)
VERB_ONLY_RE = re.compile(r"\b(draft|write|compose)\b", re.I)
DRAFT_RE = re.compile(r"\b(draft|write|compose)\b|\b(intro(duction)?|follow[- ]?up|outreach)\s+(e-?mail|message|note)s?\b", re.I)
PLURAL_RE = re.compile(r"\b(each|every|all|them|these|those|everyone|everybody|list|people|contacts)\b", re.I)
MAX_ITEMS = 25

PEOPLE_COLUMNS = ["name", "title", "tier", "account", "email", "phone"]


def detect(q: str) -> Optional[str]:
    """'move_deal' | 'create' | 'export' | None. A stage change and logging an activity are checked first
    ("log a follow-up call" is an activity, not a task); asking to create something wins over a mention of a
    file; writing an email or message ("create an intro email for Robin") stays a draft."""
    from apps.sales_copilot import records      # one deal / introduction: details and field changes
    rec = records.detect(q)
    if rec:
        return rec
    if MOVE_RE.search(q):
        return "move_deal"
    if ACTIVITY_RE.search(q) and not VERB_ONLY_RE.search(q):
        return "create"
    if CREATE_RE.search(q) and not DRAFT_RE.search(q):
        return "create"
    if EXPORT_RE.search(q):
        return "export"
    return None


def create_kind(q: str) -> str:
    """Which record a "create" turn is about. "a task in Introductions for each of them" means introductions."""
    ql = q.lower()
    if ACTIVITY_RE.search(q) and not re.search(r"\b(tasks?|to-?dos?)\b", ql):
        return "activity"
    if re.search(r"\b(introductions?|intros?|wishlist|introduce)\b", ql):
        return "introduction"
    if re.search(r"\b(deals?|opportunit(y|ies))\b", ql):
        return "deal"
    return "task"


def recent_extras(session, sid: str, n: int = 4) -> List[Dict[str, Any]]:
    """Extras of the last few answers in this chat, newest first ("each of them" refers back to these)."""
    rows = session.execute(text("""SELECT extras FROM copilot_messages WHERE session_id = :s AND role = 'assistant'
                                   ORDER BY id DESC LIMIT :n"""), {"s": sid, "n": n}).fetchall()
    return [r[0] for r in rows if r[0]]


def _people_table(extras: Dict[str, Any]) -> List[Dict[str, Any]]:
    tbl = extras.get("table") or {}
    return (tbl.get("rows") or []) if tbl.get("kind") == "people" else []


def _scope_accounts(t: Dict[str, Any], recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if t["accounts"]:
        return t["accounts"]
    for ex in recent:
        accts = ((ex.get("entities") or {}).get("accounts")) or []
        if accts:
            return accts
    return []


def people_for(session, t: Dict[str, Any], recent: List[Dict[str, Any]],
               list_people: Callable[..., List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The people a request is about → {rows, source}. In order: the person in focus (unless the question is
    about several people), the last people table in this chat, then the people at the account(s) in scope —
    filtered by the question's words ("VPs in technology")."""
    q = t["q"]
    has_filter = bool(re.search(r"\b(vps?|vice presidents?|directors?|c-?suite|cxos?|chiefs?|executives|managers|"
                                r"decision[- ]makers|technology|engineering|risk|compliance|operations|finance|data)\b", q, re.I))
    if t["persona"] and not PLURAL_RE.search(q):
        p = t["persona"]
        return {"rows": [{"id": p["id"], "name": p["name"], "title": p.get("title"), "account": p.get("account"),
                          "account_id": p.get("account_id")}], "source": "person"}
    if not has_filter:
        for ex in recent:
            rows = _people_table(ex)
            if rows:
                return {"rows": rows, "source": "previous"}
    accounts = _scope_accounts(t, recent)
    if accounts:
        return {"rows": list_people(session, t["acl"], q, accounts), "source": "account", "accounts": accounts}
    return {"rows": [], "source": "none"}


def export_turn(session, t: Dict[str, Any], list_people) -> None:
    """A spreadsheet of what the user asked for: the last table in this chat, or the people in scope."""
    recent = recent_extras(session, t["sid"])
    extras = t["extras"]
    table = None
    if not t["accounts"] and not t["persona"]:
        table = next((ex["table"] for ex in recent if (ex.get("table") or {}).get("rows")), None)
    if table is None:
        found = people_for(session, t, recent, list_people)
        if found["rows"]:
            table = {"columns": PEOPLE_COLUMNS, "rows": found["rows"][:500], "kind": "people"}
    if not table:
        t["answer"] = ("I can export a list as Excel or CSV — tell me which one, e.g. *export the BNY VPs to Excel* or "
                       "*download all contacts at BlackRock*.")
        extras["followups"] = ["Which BNY VPs work in technology?", "List the C-suite at BlackRock"]
        return
    extras["table"] = table
    extras["download"] = True
    n = len(table["rows"])
    what = {"people": "people", "deals": "deals", "changes": "changes"}.get(table.get("kind"), "rows")
    t["answer"] = (f"Here's the list as a spreadsheet — **{n}** {what}. Use **Download Excel** or **CSV** below; "
                   "contact details come from the database.")
    extras["followups"] = (["Create a task for each of them", "Add them to the introduction wishlist"]
                           if table.get("kind") == "people" else [])


def _account_rows(session, acl: List[int], ids: List[int]) -> List[Dict[str, Any]]:
    rows = session.execute(text("SELECT id, display_name FROM accounts WHERE id = ANY(:ids) AND id = ANY(:acl) ORDER BY 2"),
                           {"ids": ids, "acl": acl}).fetchall()
    return [{"account_id": r[0], "account": r[1]} for r in rows]


def create_turn(session, t: Dict[str, Any], list_people) -> None:
    """A proposal card: the records this would create, for the user to review and confirm in the chat."""
    q, extras = t["q"], t["extras"]
    kind = create_kind(q)
    recent = recent_extras(session, t["sid"])
    if kind == "activity":
        _activity_proposal(session, t, recent, list_people)
        return
    if kind == "deal":
        ids = [a["id"] for a in _scope_accounts(t, recent)]
        if not ids and t["persona"]:
            ids = [t["persona"]["account_id"]]
        if not ids:
            ids = sorted({r["account_id"] for r in people_for(session, t, recent, list_people)["rows"] if r.get("account_id")})
        items = _account_rows(session, t["acl"], ids)[:MAX_ITEMS]
        if not items:
            t["answer"] = "Which account is the deal for? Mention it with **@**, e.g. *create a deal for @BNY*."
            return
        for it in items:
            it["name"] = f"{it['account']} — new opportunity"
        extras["actions"] = {"kind": "deal", "items": items}
        t["answer"] = (f"Here's the deal{'s' if len(items) > 1 else ''} I'd create — check the name and value, then "
                       f"**Create**. {'It starts' if len(items) == 1 else 'They start'} in the Intro stage.")
        return

    found = people_for(session, t, recent, list_people)
    rows = [r for r in found["rows"] if r.get("id") and r.get("account_id")]
    if not rows:
        t["answer"] = ("Who should I use? Mention a person or account with **@**, or ask for a list first — e.g. "
                       "*list the C-suite at BNY*, then *create a task for each of them*.")
        extras["followups"] = ["List the C-suite at BNY", "Who are the decision-makers at BlackRock?"]
        return
    if EXPORT_RE.search(q):   # "the list in Excel … and create a task for each": the spreadsheet too
        extras["table"] = {"columns": PEOPLE_COLUMNS, "rows": rows[:500], "kind": "people"}
        extras["download"] = True
    more = len(rows) - MAX_ITEMS
    items = [{"persona_id": r["id"], "name": r["name"], "title": r.get("title") or "", "account": r.get("account") or "",
              "account_id": r["account_id"]} for r in rows[:MAX_ITEMS]]
    if kind == "task":
        for it in items:
            it["title_text"] = _task_title(q, it, single=len(items) == 1)
        extras["actions"] = {"kind": "task", "items": items}
        what = "a task"
    else:
        extras["actions"] = {"kind": "introduction", "items": items,
                             "account_ids": sorted({it["account_id"] for it in items})}
        what = "an introduction request"
    source = {"previous": "from the list above", "account": "at " + ", ".join(a["name"] for a in found.get("accounts", [])),
              "person": ""}.get(found["source"], "")
    t["answer"] = (f"I've prepared {what} for **{len(items)}** {'person' if len(items) == 1 else 'people'} {source}".rstrip()
                   + ". Untick anyone to skip, adjust the details, then confirm — nothing is created until you do."
                   + (f" (Showing the first {MAX_ITEMS}; {more} more not included.)" if more > 0 else ""))


def _task_title(q: str, it: Dict[str, Any], single: bool) -> str:
    """"create a task to send Robin the deck" → "Send Robin the deck"; a follow-up → "Follow up with Robin";
    otherwise "Reach out to Robin (title)"."""
    if single:
        m = re.search(r"\bto\s+((?!each\b|all\b|every\b)\w.{3,150}?)\s*[.?!]*$", q, re.I)
        if m and not re.match(r"(do|my|the|a|an)\b", m.group(1), re.I):
            return m.group(1)[0].upper() + m.group(1)[1:]
    if re.search(r"\bfollow[- ]?ups?\b", q, re.I):
        return f"Follow up with {it['name']}"
    return f"Reach out to {it['name']}" + (f" ({it['title'][:80]})" if it.get("title") else "")


# ── Activities ────────────────────────────────────────────────────────────────

ACTIVITY_TYPES = [("meeting", r"\b(meeting|met)\b"), ("email", r"\b(e-?mail(ed)?)\b"), ("linkedin", r"\blinkedin\b"),
                  ("note", r"\bnotes?\b"), ("call", r"\b(call(ed)?|phone|spoke)\b")]
TYPE_LABEL = {"meeting": "Meeting", "email": "Email", "linkedin": "LinkedIn message", "note": "Note", "call": "Call"}


def _activity_type(q: str) -> str:
    return next((k for k, pat in ACTIVITY_TYPES if re.search(pat, q, re.I)), "call")


def _activity_proposal(session, t: Dict[str, Any], recent: List[Dict[str, Any]], list_people) -> None:
    """"log a call with Robin about pricing" → one activity: type, subject, notes, who took part, linked to the account."""
    q, extras = t["q"], t["extras"]
    typ = _activity_type(q)
    if t["persona"] and not PLURAL_RE.search(q):
        p = t["persona"]
        people = [{"id": p["id"], "name": p["name"], "title": p.get("title"), "account": p.get("account"),
                   "account_id": p.get("account_id")}]
    elif PLURAL_RE.search(q):
        people = [r for r in people_for(session, t, recent, list_people)["rows"] if r.get("id")][:10]
    else:
        people = []
    accounts = _scope_accounts(t, recent)
    account_id = (people[0]["account_id"] if people else None) or (accounts[0]["id"] if accounts else None)
    if not account_id:
        t["answer"] = ("Who was it with? Mention the person or account with **@**, e.g. *log a call with @Robin Vince "
                       "about pricing*.")
        return
    account = (people[0].get("account") if people else None) or next((a["name"] for a in accounts if a["id"] == account_id), "")
    names = ", ".join(p["name"] for p in people[:3]) + (f" +{len(people) - 3}" if len(people) > 3 else "")
    m = re.search(r"\b(?:about|regarding|re:?|discussed|discussing|covering)\s+(.{3,400}?)\s*[.?!]*$", q, re.I)
    extras["actions"] = {
        "kind": "activity", "type": typ, "account_id": account_id, "account": account, "deal_id": t.get("deal_id"),
        "subject": f"{TYPE_LABEL[typ]} with {names or account}"[:300], "summary": m.group(1) if m else "",
        "items": [{"persona_id": p["id"], "name": p["name"], "title": p.get("title") or "", "account": p.get("account") or "",
                   "account_id": p["account_id"]} for p in people]}
    who = f"with **{names}**" if names else f"at **{account}**"
    t["answer"] = (f"Here's the {TYPE_LABEL[typ].lower()} {who} I'd log. Check the time and notes, then **Log** — it goes on "
                   f"{'their' if people else 'the account'} timeline for the team.")


# ── Moving a deal ─────────────────────────────────────────────────────────────

STAGES = ["intro", "discovery", "proposal", "pilot", "contract"]
WON_FROM = ["proposal", "pilot", "contract"]
STAGE_LABEL = {"intro": "Intro", "discovery": "Discovery", "proposal": "Proposal", "pilot": "Pilot", "contract": "Contract",
               "won": "Won", "lost": "Lost"}
NAME_STOP = set(STAGE_KEYS) | {"move", "advance", "push", "progress", "bump", "shift", "mark", "set", "change", "update",
                               "the", "deal", "deals", "opportunity", "stage", "next", "closed", "into", "back", "forward",
                               "please", "our", "my", "this", "that", "for", "with", "and", "can", "you"}


def find_deals(session, t: Dict[str, Any], recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deals the user might mean, best first: the deal the chat was opened from, words of the deal name in the
    question, then the account(s) in scope. Only deals the user can see (reps: their own, as on /deals)."""
    from apps.sales_deals import api as deals_api
    ids = [a["id"] for a in _scope_accounts(t, recent)]
    account_words = {w for a in t["accounts"] for w in re.findall(r"[a-z0-9]{3,}", a["name"].lower())}
    words = [w for w in re.findall(r"[a-z0-9]{3,}", t["q"].lower()) if w not in NAME_STOP and w not in account_words]
    sql = """SELECT d.id, d.name, d.stage, d.account_id, a.display_name FROM deals d JOIN accounts a ON a.id = d.account_id
             WHERE d.account_id = ANY(:acl) AND d.stage <> 'won'"""
    params: Dict[str, Any] = {"acl": t["acl"]}
    if deals_api.own_deals_only(t["user"]):
        sql += " AND d.owner_user_id = :u"
        params["u"] = t["user"].id
    rows = session.execute(text(sql + " ORDER BY d.updated_at DESC NULLS LAST LIMIT 300"), params).fetchall()
    scored = []
    for r in rows:
        name = (r[1] or "").lower()
        score = sum(2 for w in words if w in name) + (5 if r[0] == t.get("deal_id") else 0) + (1 if r[3] in ids else 0)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 2:            # a name word or the deal in context: keep the close matches only
        scored = [x for x in scored if x[0] >= scored[0][0] - 1]
    return [{"deal_id": r[0], "name": r[1], "from": r[2], "account_id": r[3], "account": r[4]} for _, r in scored[:5]]


def _target_stage(q: str) -> Optional[str]:
    m = STAGE_TARGET_RE.search(q)
    return m.group(1).lower() if m else None    # None → the next stage


def move_turn(session, t: Dict[str, Any]) -> None:
    """"move the BNY pilot to Contract" → a card with the deal, from → to, and what the move needs (a reason to move
    back, why it was lost). The server's stage rules still decide: one stage at a time, won from Proposal on."""
    from apps.sales_deals import api as deals_api
    recent = recent_extras(session, t["sid"])
    deals = find_deals(session, t, recent)
    if not deals:
        t["answer"] = ("Which deal? Name it or mention its account with **@**, e.g. *move the @BNY pilot deal to Contract*. "
                       "Ask *my open deals* to see them.")
        t["extras"]["followups"] = ["My open deals", "Which deals are stuck?"]
        return
    target = _target_stage(t["q"])
    items = []
    for n, d in enumerate(deals):
        cur, to, note, blocked = d["from"], target, "", False
        if to is None:
            to = STAGES[STAGES.index(cur) + 1] if cur in STAGES[:-1] else None
            if to is None:
                note, blocked = ("Contract is the last stage — mark it won or lost." if cur == "contract"
                                 else "Reopen it by naming a stage, e.g. *move it back to Discovery*."), True
        elif to in STAGES and cur in STAGES and abs(STAGES.index(to) - STAGES.index(cur)) > 1:
            step = STAGES[STAGES.index(cur) + (1 if STAGES.index(to) > STAGES.index(cur) else -1)]
            note = (f"Deals move one stage at a time — this moves it to {STAGE_LABEL[step]}; "
                    f"ask again to go on to {STAGE_LABEL[to]}.")
            to = step
        if to == cur:
            note, blocked = f"Already in {STAGE_LABEL[cur]}.", True
        elif to == "won" and cur not in WON_FROM:
            note, blocked = "A deal can be marked won from Proposal onwards.", True
        back = bool(to and cur in STAGES and to in STAGES and STAGES.index(to) < STAGES.index(cur))
        items.append({**d, "from_label": STAGE_LABEL.get(cur, cur), "to": to, "to_label": STAGE_LABEL.get(to, to or "—"),
                      "back": back, "note": note, "blocked": blocked, "skip": blocked or n > 0})
    t["extras"]["actions"] = {"kind": "move_deal", "items": items,
                              "lost_categories": [{"key": k, "label": v} for k, v in deals_api.LOST_CATEGORIES.items()]}
    first = items[0]
    if len(items) == 1:
        t["answer"] = (f"Move **{first['name']}** ({first['account']}) from **{first['from_label']}** to **{first['to_label']}**?"
                       if not first["blocked"] else f"**{first['name']}** ({first['account']}): {first['note']}")
    else:
        t["answer"] = f"I found **{len(items)}** deals that could match — the closest is ticked. Check it, then confirm."


# ── Suggested next steps after an answer ──────────────────────────────────────

NO_NEXT = {"create", "move_deal", "export", "moderated", "smalltalk", "clarify", "remember", "list_people", "deals",
           "deal_info", "intro_info", "update_deal", "update_intro"}


def next_actions(session, t: Dict[str, Any]) -> List[str]:
    """One-tap next steps under briefs and drafts ("Create a follow-up task for Robin Vince", "Log a call with …").
    Each is a question the user sends; it comes back as a proposal card, so nothing happens without a confirm."""
    if t["intent"] in NO_NEXT or t["extras"].get("moderated") or t["extras"].get("actions"):
        return []
    p, accounts = t["persona"], t["accounts"]
    if p:
        n = p["name"]
        if t["intent"] == "draft":
            return [f"Log an email to {n}", f"Create a follow-up task for {n}"]
        return [f"Create a follow-up task for {n}", f"Log a call with {n}"]
    if accounts:
        a = accounts[0]
        has_open = session.execute(text("SELECT 1 FROM deals WHERE account_id = :a AND stage NOT IN ('won', 'lost') LIMIT 1"),
                                   {"a": a["id"]}).fetchone()
        return [f"Show open deals at {a['name']}" if has_open else f"Add a deal for {a['name']}",
                f"Log a meeting with {a['name']}"]
    return []
