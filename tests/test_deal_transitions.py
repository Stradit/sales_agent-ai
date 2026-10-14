"""Deal stage transition rules tests."""
import pytest
from fastapi import HTTPException
from apps.sales_deals.api import STAGES, CLOSED, WON_FROM


def check_deal_stage_transition(cur: str, new: str, reason: str = ""):
    if new and new != cur:
        if new not in STAGES + CLOSED:
            raise HTTPException(400, "Unknown stage.")
        if new == "won" and cur not in WON_FROM:
            raise HTTPException(400, "Move the deal to Proposal or later before marking it won.")
        if new == "lost" and not reason:
            raise HTTPException(400, "Please give a reason when marking a deal as lost.")
        if new in STAGES and cur in STAGES:
            step = STAGES.index(new) - STAGES.index(cur)
            if abs(step) > 1:
                raise HTTPException(400, "Move one stage at a time.")
            if step == -1 and not (reason or "").strip():
                raise HTTPException(400, "Please give a reason for moving back a stage.")


def test_deal_one_step_forward():
    check_deal_stage_transition("intro", "discovery")
    check_deal_stage_transition("discovery", "proposal")
    check_deal_stage_transition("proposal", "pilot")
    check_deal_stage_transition("pilot", "contract")


def test_deal_no_skipping_stages():
    with pytest.raises(HTTPException) as exc:
        check_deal_stage_transition("intro", "proposal")
    assert "Move one stage at a time" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        check_deal_stage_transition("contract", "intro", reason="jump")
    assert "Move one stage at a time" in exc.value.detail


def test_deal_moving_back_requires_reason():
    with pytest.raises(HTTPException) as exc:
        check_deal_stage_transition("discovery", "intro")
    assert "Please give a reason for moving back a stage" in exc.value.detail

    # With reason, it succeeds
    check_deal_stage_transition("discovery", "intro", reason="Client requested to start fresh")
    check_deal_stage_transition("proposal", "discovery", reason="Need more requirements")


def test_deal_won_rules():
    with pytest.raises(HTTPException) as exc:
        check_deal_stage_transition("intro", "won")
    assert "Move the deal to Proposal or later" in exc.value.detail

    check_deal_stage_transition("proposal", "won")
    check_deal_stage_transition("contract", "won")


def test_deal_lost_requires_reason():
    with pytest.raises(HTTPException) as exc:
        check_deal_stage_transition("proposal", "lost")
    assert "Please give a reason when marking a deal as lost" in exc.value.detail

    check_deal_stage_transition("proposal", "lost", reason="Budget cancelled")
