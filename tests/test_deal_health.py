"""Deal health: gap texts and their codes stay in step (pure — no database)."""
from datetime import date, datetime, timedelta, timezone

from apps.sales_deals.api import health

NOW = datetime.now(timezone.utc)


def deal(**kw):
    d = {"stage": "discovery", "next_step": "Call CDO", "next_step_due": date.today() + timedelta(days=3),
         "expected_close": date.today() + timedelta(days=30), "stage_changed_at": NOW - timedelta(days=5)}
    d.update(kw)
    return d


CHAMP = {"role": "champion", "sentiment": "positive"}
EB = {"role": "economic_buyer", "sentiment": None}


def codes(h):
    assert len(h["gaps"]) == len(h["gap_codes"])
    return h["gap_codes"]


def test_healthy_deal_has_no_gaps():
    h = health(deal(), [CHAMP, EB], [{"stage": "discovery", "done": True}], NOW - timedelta(days=2), 0)
    assert codes(h) == [] and h["level"] == "good"


def test_each_gap_has_its_code():
    h = health(deal(next_step=None, expected_close=date.today() - timedelta(days=1),
                    stage_changed_at=NOW - timedelta(days=60)),
               [{"role": "blocker", "sentiment": "negative"}],
               [{"stage": "discovery", "done": False}, {"stage": "discovery", "done": False}],
               NOW - timedelta(days=40), 0)
    assert codes(h) == ["no_champion", "no_economic_buyer", "no_next_step", "no_activity",
                        "close_date_passed", "exit_criteria", "blockers", "long_in_stage"]


def test_overdue_next_step_and_quiet():
    h = health(deal(next_step_due=date.today() - timedelta(days=2)), [CHAMP, EB], [], NOW - timedelta(days=20), 0)
    assert codes(h) == ["next_step_overdue", "quiet"]


def test_no_economic_buyer_not_required_at_intro():
    h = health(deal(stage="intro"), [CHAMP], [], NOW, 0)
    assert "no_economic_buyer" not in codes(h)


def test_closed_deal_has_no_gaps():
    h = health(deal(stage="won"), [], [], None, 0)
    assert h["gaps"] == [] and h["gap_codes"] == []


def test_overdue_close_plan_is_a_gap():
    h = health(deal(), [CHAMP, EB], [{"stage": "discovery", "done": True}], NOW - timedelta(days=2), 0, plan_overdue=2)
    assert codes(h) == ["plan_overdue"] and "2 milestones overdue" in h["gaps"][0]
    ok = health(deal(), [CHAMP, EB], [{"stage": "discovery", "done": True}], NOW - timedelta(days=2), 0, plan_overdue=0)
    assert h["score"] < ok["score"]
