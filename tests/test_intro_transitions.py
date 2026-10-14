"""Manual status rules for introductions (pure — no database)."""
import pytest
from fastapi import HTTPException

from apps.sales_crm.introductions import check_transition, missing_for


def ok(cur, new, **kw):
    kw.setdefault("has_account", True)
    kw.setdefault("has_contact", True)
    check_transition(cur, new, **kw)


def bad(cur, new, match, **kw):
    with pytest.raises(HTTPException) as e:
        ok(cur, new, **kw)
    assert e.value.status_code == 400
    assert match.lower() in e.value.detail.lower()


def test_one_step_forward_with_criteria():
    ok("proposed", "requested", has_account=False, has_contact=False)
    ok("requested", "accepted", has_account=False)
    ok("accepted", "intro_made")
    ok("intro_made", "meeting_held")


def test_forward_needs_stage_criteria():
    bad("requested", "accepted", "contact", has_contact=False)
    bad("accepted", "intro_made", "account", has_account=False)
    assert missing_for("meeting_held", False, False) == ["link it to an account", "choose the contact being introduced"]
    assert missing_for("requested", False, False) == []


def test_no_skipping_either_way():
    bad("proposed", "accepted", "one stage at a time")
    bad("proposed", "meeting_held", "one stage at a time")
    bad("meeting_held", "requested", "one stage at a time", reason="wrong stage")


def test_one_step_back_needs_reason():
    bad("intro_made", "accepted", "reason")
    ok("intro_made", "accepted", reason="Contact asked to reschedule")


def test_converted_only_via_convert_and_locked():
    bad("meeting_held", "converted", "use convert")
    bad("converted", "meeting_held", "converted introduction", reason="x")
    bad("converted", "declined", "converted introduction", reason="x")


def test_closing_needs_reason_from_any_open_stage():
    bad("requested", "declined", "reason")
    bad("requested", "stale", "reason")
    ok("proposed", "declined", reason="Not a fit", has_account=False, has_contact=False)
    ok("meeting_held", "stale", reason="Gone quiet")


def test_reopen_returns_to_previous_stage_with_reason():
    bad("declined", "accepted", "reason", reopen_to="accepted")
    bad("declined", "proposed", "accepted", reason="They came back", reopen_to="accepted")
    ok("declined", "accepted", reason="They came back", reopen_to="accepted")
    ok("stale", "proposed", reason="Revived", reopen_to=None)


def test_unknown_status():
    bad("proposed", "won", "unknown")
