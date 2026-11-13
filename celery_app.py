"""
Celery Application Configuration — Distributed task queue with Redis broker.
Configures task routing, serializer, result backend, and Celery Beat cron schedule.
100% Dynamic, Zero Hardcoding.
"""

import sys
from pathlib import Path
from celery import Celery
from celery.schedules import crontab

# Ensure project root is on path
PIPELINE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_ROOT))

import config

app = Celery(
    "sales_ai_pipeline",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=[
        "tasks.refresh_tasks",
        "tasks.pipeline_tasks",
        "tasks.notifications",
    ]
)

# Celery Configuration Settings
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=1800,  # 30-minute max per task
    worker_prefetch_multiplier=1,  # Fair distribution
    task_acks_late=True,
)

# 15-Day Celery Beat Schedule (Runs on the 1st and 16th of every month at 02:00 UTC)
app.conf.beat_schedule = {
    "run-15-day-social-and-hierarchy-refresh": {
        "task": "tasks.refresh_tasks.task_scheduled_15day_refresh",
        "schedule": crontab(day_of_month="1,16", hour=2, minute=0),
        "args": (),
    },
    "deliver-pending-catalog-notifications": {
        "task": "tasks.deliver_pending_notifications",
        "schedule": crontab(minute="*/1"),  # every minute
        "args": (50,),
    },
}
