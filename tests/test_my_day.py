"""Command Center · My Day (services/my_day_service.py) — which reason a deal shows and how the queue is ordered.
Pure logic, no database."""
from datetime import date

from services import my_day_service as md

TODAY = date(2026, 11, 1)


def deal(**kw):
    base = {"id": 1, "name": "Demo · BNY — Pilot", "account_name": "BNY", "account_id": 3, "stage": "pilot", "stage_label": "Pilot",
            "value_amount": 320000, "next_step": "CIO readout", "next_step_due": date(2026, 10, 19),
            "last_customer_touch": "2026-10-20T10:00:00+00:00", "committee_gaps": [], "health": {"score": 65}}
    base.update(kw)
    return base


def test_overdue_next_step_comes_first():
    item = md._deal_item(deal(), TODAY)
    assert item["kind"] == "deal_next_step" and item["overdue_days"] == 13
    assert item["deal"] == "BNY — Pilot"                      # demo prefix dropped
    assert item["actions"][0]["label"] == "Draft email" and "deal_id=1" in item["actions"][0]["href"]


def test_quiet_late_stage_deal():
    item = md._deal_item(deal(next_step_due=date(2026, 11, 20), last_customer_touch="2026-10-06"), TODAY)
    assert item["kind"] == "deal_quiet" and "26 days" in item["title"]


def test_missing_economic_buyer_then_no_step_then_nothing():
    gap = md._deal_item(deal(next_step_due=None, last_customer_touch="2026-10-30", committee_gaps=["economic_buyer"]), TODAY)
    assert gap["kind"] == "deal_committee"
    assert md._deal_item(deal(next_step=None, next_step_due=None, last_customer_touch="2026-10-30", stage="intro"), TODAY)["kind"] == "deal_no_step"
    assert md._deal_item(deal(next_step_due=date(2026, 11, 20), last_customer_touch="2026-10-30"), TODAY) is None


def test_early_stage_quiet_deal_is_not_flagged():
    assert md._deal_item(deal(stage="discovery", next_step_due=date(2026, 11, 20), last_customer_touch="2026-09-01"), TODAY) is None


def test_urgency_order():
    overdue_task = {"kind": "task", "overdue_days": 15, "priority": "high", "value": 450000}
    overdue_step = {"kind": "deal_next_step", "overdue_days": 13, "value": 320000}
    intro = {"kind": "intro", "overdue_days": 20}
    no_step = {"kind": "deal_no_step", "overdue_days": 0, "value": 150000}
    ranked = sorted([no_step, intro, overdue_step, overdue_task], key=lambda q: -md._urgency(q))
    assert ranked == [overdue_task, overdue_step, intro, no_step]


def test_row_actions_resolve_in_place():
    assert [a.get("deal_action") for a in md._deal_item(deal(), TODAY)["actions"]] == [None, "reschedule"]
    quiet = md._deal_item(deal(next_step_due=date(2026, 11, 20), last_customer_touch="2026-10-06"), TODAY)
    assert quiet["actions"][1]["deal_action"] == "log_call" and "next step" in quiet["extra"]
    gap = md._deal_item(deal(next_step_due=None, last_customer_touch="2026-10-30", committee_gaps=["economic_buyer"]), TODAY)
    assert gap["actions"][0]["href"] == "/deals?deal=1&tab=committee"
    no_step = md._deal_item(deal(next_step=None, next_step_due=None, last_customer_touch="2026-10-30", stage="intro"), TODAY)
    assert no_step["actions"][0]["deal_action"] == "set_step"


def test_short_gap_text():
    assert md._short_gap("No champion on the buying committee", TODAY) == "No champion"
    assert md._short_gap("No economic buyer identified", TODAY) == "No economic buyer"
    assert md._short_gap("Next step overdue since 2026-10-20", TODAY) == "next step 12 days late"
