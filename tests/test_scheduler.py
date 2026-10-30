"""Scheduler (services/scheduler.py) — when a job is due and when it runs next. Pure logic, no database."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SCHEDULER_ENABLED", "0")

from services import scheduler   # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def test_never_run_is_due():
    assert scheduler.is_due(None, 6, NOW)
    assert scheduler.next_run_at(None, 6, True, NOW) == NOW


def test_due_after_the_interval():
    assert not scheduler.is_due(NOW - timedelta(hours=5, minutes=59), 6, NOW)
    assert scheduler.is_due(NOW - timedelta(hours=6), 6, NOW)


def test_catch_up_after_downtime():
    # The server was down for a day: the job is due now, not at some later 6-hour mark
    last = NOW - timedelta(hours=30)
    assert scheduler.is_due(last, 6, NOW)
    assert scheduler.next_run_at(last, 6, True, NOW) == NOW


def test_next_run_and_off():
    assert scheduler.next_run_at(NOW - timedelta(hours=2), 6, True, NOW) == NOW + timedelta(hours=4)
    assert scheduler.next_run_at(NOW, 6, False, NOW) is None


def test_jobs_and_channels():
    assert list(scheduler.JOBS) == ["sources_refresh", "command_center_refresh"]   # sources first, then the feeds
    assert scheduler.JOBS["sources_refresh"]["options"]["channels"] == ["sec"]       # free by default
    assert scheduler.CHANNELS["sec"] == "Free" and scheduler.CHANNELS["news"] != "Free"
