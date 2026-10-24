"""One in-process APScheduler: the follow-up tick, plus a place to run webhook work off the request."""
import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import service
from .models import utcnow

log = logging.getLogger(__name__)


def run_async(app, fn, *args):
    """Run fn(*args) in the scheduler's thread pool (so the webhook can answer Meta fast).
    With no scheduler (tests, SCHEDULER_ENABLED=0) it runs inline."""
    def job():
        with app.app_context():
            try:
                fn(*args)
            except Exception:
                log.exception("Background job %s failed", getattr(fn, "__name__", fn))

    scheduler = app.extensions.get("sentinel_scheduler")
    if scheduler:
        scheduler.add_job(job)
    else:
        job()


def start(app):
    def tick():
        with app.app_context():
            service.run_due_followups()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(tick, "interval", minutes=app.config["FOLLOWUP_CHECK_MINUTES"], id="followups",
                      max_instances=1, coalesce=True, next_run_time=utcnow() + timedelta(seconds=30))
    scheduler.start()
    app.extensions["sentinel_scheduler"] = scheduler
    return scheduler
