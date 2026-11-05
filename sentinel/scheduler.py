"""One in-process APScheduler: the follow-up tick, plus a place to run webhook work off the request."""
import logging
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import service
from .models import utcnow

log = logging.getLogger(__name__)


def run_async(app, fn, *args):
    """Run fn(*args) on its own daemon thread so the webhook can answer Meta fast.

    Deliberately NOT the APScheduler pool: a scheduler whose thread is dead (for example after gunicorn forks a
    --preload'ed app) accepts jobs and never runs them, silently. A plain thread cannot fail that way.
    With no scheduler configured (tests, SCHEDULER_ENABLED=0) it runs inline."""
    name = getattr(fn, "__name__", str(fn))

    def job():
        with app.app_context():
            try:
                fn(*args)
            except Exception:
                log.exception("Background job %s failed", name)

    if app.extensions.get("sentinel_scheduler"):
        threading.Thread(target=job, name=f"sentinel-{name}", daemon=True).start()
        log.info("background job %s started on its own thread args=%s", name, args)
    else:
        job()


def start(app):
    def tick():
        with app.app_context():
            service.run_unanswered()
            service.run_due_followups()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(tick, "interval", minutes=app.config["FOLLOWUP_CHECK_MINUTES"], id="followups",
                      max_instances=1, coalesce=True, next_run_time=utcnow() + timedelta(seconds=30))
    scheduler.start()
    app.extensions["sentinel_scheduler"] = scheduler
    return scheduler
