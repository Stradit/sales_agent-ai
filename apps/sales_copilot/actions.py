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
DRAFT_RE = re.compile(r"\b(draft|write|compose)\b|\b(intro(duction)?|follow[- ]?up|outreach)\s+(e-?mail|message|note)s?\b", re.I)
PLURAL_RE = re.compile(r"\b(each|every|all|them|these|those|everyone|everybody|list|people|contacts)\b", re.I)
MAX_ITEMS = 25

PEOPLE_COLUMNS = ["name", "title", "tier", "account", "email", "phone"]


def detect(q: str) -> Optional[str]:
    """'create' | 'export' | None. Asking to create something wins over a mention of a file;
    writing an email or message ("create an intro email for Robin") stays a draft."""
    if CREATE_RE.search(q) and not DRAFT_RE.search(q):
        return "create"
    if EXPORT_RE.search(q):
        return "export"
    return None


def create_kind(q: str) -> str:
    """Which record a "create" turn is about. "a task in Introductions for each of them" means introductions."""
    ql = q.lower()
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
            it["title_text"] = f"Reach out to {it['name']}" + (f" ({it['title'][:80]})" if it["title"] else "")
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
