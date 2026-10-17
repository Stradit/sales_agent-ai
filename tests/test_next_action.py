"""Next best action (#12): one suggestion per deal, most urgent rule first (pure — no database)."""
from datetime import date, timedelta

from apps.sales_deals.next_action import draft_prompt, pick

TODAY = date(2026, 10, 17)
CHAMP = {"persona_id": 1, "name": "Priya Shah", "title": "CDO", "role": "champion", "sentiment": "positive", "email": "p@x.com"}
EB = {"persona_id": 2, "name": "Sam Lee", "title": "CFO", "role": "economic_buyer", "sentiment": None, "email": None}


def deal(**kw):
    d = {"stage": "discovery", "name": "AI pilot", "account_name": "BNY", "next_step": "Workshop",
         "next_step_due": str(TODAY + timedelta(days=3)), "expected_close": str(TODAY + timedelta(days=40)),
         "health": {"gaps": [], "gap_codes": []}, "stakeholders": [CHAMP, EB], "close_plan": [],
         "qualification": {"identify_pain": "Manual reconciliations", "metrics": "Cut close time by 2 days"}}
    d.update(kw)
    return d


def test_closed_deal_has_no_action():
    assert pick(deal(stage="won"), TODAY) is None


def test_past_close_date_comes_first():
    a = pick(deal(expected_close=str(TODAY - timedelta(days=1)),
                  health={"gaps": ["Quiet for 20 days"], "gap_codes": ["quiet"]}), TODAY)
    assert a["code"] == "new_close_date" and a["target"]["name"] == "Priya Shah" and a["can_draft"]
    assert a["fix"] == "close_date_passed"


def test_overdue_customer_plan_step():
    plan = [{"title": "Security review", "side": "customer", "owner_name": "Their CISO", "due_date": "2026-10-01",
             "status": "todo", "overdue": True}]
    a = pick(deal(stage="proposal", close_plan=plan), TODAY)
    assert a["code"] == "plan_unblock" and "Security review" in a["title"] and a["target"]["persona_id"] == 1


def test_our_own_overdue_step_has_no_customer_email():
    plan = [{"title": "Send SOW", "side": "us", "due_date": "2026-10-01", "status": "todo", "overdue": True}]
    a = pick(deal(stage="proposal", close_plan=plan), TODAY)
    assert a["code"] == "plan_unblock" and a["target"] is None and not a["can_draft"]


def test_no_people_means_add_people_without_draft():
    a = pick(deal(stakeholders=[]), TODAY)
    assert a["code"] == "add_people" and not a["can_draft"]


def test_quiet_deal_re_engages_champion():
    a = pick(deal(health={"gaps": ["Quiet for 20 days"], "gap_codes": ["quiet"]}), TODAY)
    assert a["code"] == "re_engage" and "Quiet for 20 days" in a["why"]


def test_late_stage_without_plan_asks_for_one():
    a = pick(deal(stage="proposal"), TODAY)
    assert a["code"] == "agree_plan" and a["fix"] == "plan_overdue"


def test_missing_pain_in_discovery():
    a = pick(deal(qualification={}), TODAY)
    assert a["code"] == "confirm_value" and "the pain and the success metrics" in a["title"]


def test_negative_stakeholder_is_never_the_target():
    hostile = dict(CHAMP, sentiment="negative")
    a = pick(deal(stakeholders=[hostile, EB], health={"gaps": ["Quiet"], "gap_codes": ["quiet"]}), TODAY)
    assert a["target"]["persona_id"] == 2


def test_healthy_deal_keeps_momentum():
    a = pick(deal(), TODAY)
    assert a["code"] == "keep_momentum" and "Workshop" in a["title"]


def test_prompt_has_no_value_or_contact_details():
    d = deal(value_amount=250000, currency="USD")
    p = draft_prompt(d, pick(d, TODAY), "Janak")
    text = p["system"] + p["user"]
    assert "250000" not in text and "p@x.com" not in text and "Priya Shah" in text and "Janak" in text
