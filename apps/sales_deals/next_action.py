"""Next best action (#12): ONE suggestion per deal, plus an optional AI-drafted email for it.

pick() is pure rules over the deal-room payload (no database, no LLM): the first rule that matches
wins, most urgent first. It returns what to do, why, who it's with, the deal-room fix that does it
(the same codes as GAP_ACTIONS in deals/main.js) and whether an email draft makes sense.

draft_prompt() builds the LLM prompt for that one action from a deliberately small slice of the
deal: names, titles, stage, pain/metrics and the plan — never the deal value or contact details.
"""
from datetime import date
from typing import Any, Dict, List, Optional

LATE_STAGES = ("proposal", "pilot", "contract")
STAGE_NAMES = {"intro": "Intro", "discovery": "Discovery", "proposal": "Proposal", "pilot": "Pilot", "contract": "Contract"}


def _person(stakeholders: List[Dict[str, Any]], *roles: str) -> Optional[Dict[str, Any]]:
    for role in roles:
        for p in stakeholders:
            if p.get("role") == role and p.get("sentiment") != "negative":
                return p
    return None


def _target(p: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not p:
        return None
    return {"persona_id": p.get("persona_id"), "name": p.get("name"), "title": p.get("title"),
            "role": p.get("role"), "has_email": bool(p.get("email"))}


def _d(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def pick(d: Dict[str, Any], today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """The single most useful next action for an open deal, or None for a closed one."""
    if d.get("stage") not in STAGE_NAMES:
        return None
    today = today or date.today()
    h = d.get("health") or {}
    codes = set(h.get("gap_codes") or [])
    people = d.get("stakeholders") or []
    champ = _person(people, "champion")
    eb = _person(people, "economic_buyer")
    friendly = champ or eb or _person(people, "technical_evaluator", "influencer", "user")
    stage = d["stage"]
    plan = d.get("close_plan") or []
    qual = d.get("qualification") or {}

    def act(code, title, why, target=None, fix=None, draft=True, **extra):
        out = {"code": code, "title": title, "why": why, "target": _target(target), "fix": fix,
               "can_draft": bool(draft and target)}
        out.update(extra)
        return out

    close = _d(d.get("expected_close"))
    if close and close < today:
        return act("new_close_date", f"Agree a new close date{' with ' + friendly['name'] if friendly else ''}",
                   f"The expected close ({close.isoformat()}) has passed — the forecast is wrong until it's reset.",
                   friendly, "close_date_passed")
    late_steps = [p for p in plan if p.get("overdue")]
    if late_steps:
        step = late_steps[0]
        who = friendly if step.get("side") in ("customer", "both") else None
        return act("plan_unblock", f"Get “{step['title']}” moving",
                   f"It was due {step.get('due_date')} and is the first overdue step of the close plan"
                   + (f" ({step['owner_name']} owns it)." if step.get("owner_name") else "."),
                   who, "plan_overdue", step_title=step["title"])
    if "next_step_overdue" in codes:
        return act("next_step_overdue", f"Do or reschedule: {d.get('next_step')}",
                   f"The next step was due {d.get('next_step_due')}.", friendly, "next_step_overdue")
    if not people:
        return act("add_people", "Add the people you're talking to",
                   "Nobody is on the buying committee yet, so nothing else can be judged.", None, "no_champion", draft=False)
    if "quiet" in codes or "no_activity" in codes:
        gap = next((g for g, c in zip(h.get("gaps") or [], h.get("gap_codes") or []) if c in ("quiet", "no_activity")), "")
        return act("re_engage", f"Re-engage {friendly['name'] if friendly else 'the customer'}",
                   f"{gap or 'No recent activity'} — deals that go quiet for two weeks rarely close on time.", friendly, "quiet")
    if "no_champion" in codes and stage != "intro":
        return act("find_champion", "Find a champion",
                   "No one is selling this internally for you. Ask your best contact who else cares about the outcome — "
                   "or look for a warm introduction.", friendly, "no_champion")
    if "no_economic_buyer" in codes and stage in LATE_STAGES:
        return act("reach_eb", f"Get access to the economic buyer{' through ' + champ['name'] if champ else ''}",
                   f"The deal is at {STAGE_NAMES[stage]} and no one with budget authority is engaged.", champ or friendly,
                   "no_economic_buyer")
    if stage in LATE_STAGES and not plan:
        return act("agree_plan", f"Agree a close plan{' with ' + friendly['name'] if friendly else ''}",
                   "There's a proposal on the table but no dated steps to signature — slips stay invisible until it's too late.",
                   friendly, "plan_overdue")
    if stage in ("discovery", "proposal") and not (qual.get("identify_pain") and qual.get("metrics")):
        missing = [n for k, n in (("identify_pain", "the pain"), ("metrics", "the success metrics")) if not qual.get(k)]
        return act("confirm_value", f"Confirm {' and '.join(missing)}{' with ' + friendly['name'] if friendly else ''}",
                   "Without an agreed pain and a measurable outcome the proposal can't be priced on value.", friendly, None,
                   tab="toolkit")
    if "exit_criteria" in codes:
        return act("exit_criteria", f"Finish the {STAGE_NAMES[stage]} exit criteria",
                   "Less than half of this stage's exit criteria are done.", None, "exit_criteria", draft=False)
    if "no_next_step" in codes or not d.get("next_step"):
        return act("set_next_step", "Set a dated next step", "Every open deal needs one concrete next step with a date.",
                   None, "no_next_step", draft=False)
    return act("keep_momentum", f"Confirm the next step{' with ' + friendly['name'] if friendly else ''}: {d.get('next_step')}",
               "The deal is healthy — keep the agreed next step on track.", friendly, None)


def draft_prompt(d: Dict[str, Any], action: Dict[str, Any], sender: str) -> Dict[str, str]:
    t = action.get("target") or {}
    qual = d.get("qualification") or {}
    plan = [f"- {p['title']} ({p.get('side')}, due {p.get('due_date') or 'no date'}, {p.get('status')})"
            for p in (d.get("close_plan") or [])[:8]]
    facts = [f"Customer company: {d.get('account_name')}",
             f"Recipient: {t.get('name')}{', ' + t['title'] if t.get('title') else ''} (their role in the deal: {(t.get('role') or '').replace('_', ' ')})",
             f"Deal: {d.get('name')} — stage {STAGE_NAMES.get(d.get('stage'), d.get('stage'))}",
             f"Our next step: {d.get('next_step') or 'none set'}" + (f" (due {d.get('next_step_due')})" if d.get("next_step_due") else "")]
    for k, label in (("identify_pain", "Their pain"), ("metrics", "Success metrics"), ("decision_process", "Decision process")):
        if qual.get(k):
            facts.append(f"{label}: {str(qual[k])[:300]}")
    if plan:
        facts.append("Close plan:\n" + "\n".join(plan))
    system = ("You write short, warm, specific B2B sales emails for StradIT (an IT services firm). Plain text, no "
              "placeholders in square brackets, no invented facts, no prices. 60-140 words. Reply ONLY with JSON: "
              '{"subject": "...", "body": "..."}')
    user = (f"Goal of this email: {action['title']}.\nWhy now: {action['why']}\n\nWhat we know:\n" + "\n".join(facts)
            + f"\n\nSign it from {sender}. Write the email.")
    return {"system": system, "user": user}
