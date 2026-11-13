import logging

from flask import Flask

from .config import load_config
from .models import db

log = logging.getLogger(__name__)


def _warn_todo_facts():
    from . import business_facts
    todo = business_facts.todo_names()
    if todo:
        log.warning("business_facts.py still has TODO values (the bot will say the team will confirm these): %s",
                    ", ".join(todo))


def _warn_missing_credentials(app):
    cfg = app.config
    missing = [k for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET") if not cfg.get(k)]
    if missing:
        log.warning("WhatsApp not connected (%s empty): the dashboard works, but no messages will be sent "
                    "and inbound webhooks are rejected until these are set.", ", ".join(missing))
    if not cfg.get("WHATSAPP_VERIFY_TOKEN"):
        log.warning("WHATSAPP_VERIFY_TOKEN empty: Meta's GET /webhook verification will be refused.")
    if not cfg.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY empty: Claude is disabled; leads are classified as 'exploring' "
                    "and replies use fallback copy.")
    if not cfg.get("DASHBOARD_PASSWORD"):
        log.warning("DASHBOARD_PASSWORD empty: nobody can log in to the dashboard.")


NEW_COLUMNS = (  # added after the first deploy; db.create_all() never alters existing tables
    ("leads", "ai_note", "VARCHAR(400)"),
    ("leads", "last_hold_reply_at", "TIMESTAMP"),
    ("leads", "last_owner_alert_at", "TIMESTAMP"),
    ("leads", "reminder_count", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "last_reminder_at", "TIMESTAMP"),
    ("leads", "reminder_episode_at", "TIMESTAMP"),
    ("messages", "sender", "VARCHAR(8)"),
    ("leads", "menu_sent_at", "TIMESTAMP"),
    ("leads", "awaiting_choice", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "needs_owner", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("messages", "choice_id", "VARCHAR(32)"),
    ("leads", "handback_at", "TIMESTAMP"),
    ("leads", "demo_state", "VARCHAR(12)"),
    ("leads", "demo_offer_asked_at", "TIMESTAMP"),
    ("leads", "demo_offer_open", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "demo_offer_ignored", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "is_existing_client", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "existing_alerted_at", "TIMESTAMP"),
    ("leads", "upgrade_enquiry", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "opted_out", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "opted_out_at", "TIMESTAMP"),
    ("leads", "followups_off", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "template_error", "VARCHAR(500)"),
    ("leads", "template_error_at", "TIMESTAMP"),
    ("messages", "template", "VARCHAR(40)"),
    ("messages", "delivery_status", "VARCHAR(10)"),
    ("leads", "last_question", "VARCHAR(300)"),
    ("leads", "last_question_age", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "after_hours_note_at", "TIMESTAMP"),
    ("leads", "referrer_name", "VARCHAR(120)"),
    ("leads", "referrer_phone", "VARCHAR(40)"),
    ("leads", "partner_enquiry", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("demo_requests", "contact_name", "VARCHAR(120)"),
    ("demo_requests", "organisation", "VARCHAR(160)"),
    ("demo_requests", "contact_phone", "VARCHAR(40)"),
    ("opt_outs", "needs_review", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("leads", "origin", "VARCHAR(10) NOT NULL DEFAULT 'inbound'"),  # inbound / outbound (outbound prospecting)
    ("leads", "prospect_id", "INTEGER"),
)


def _add_missing_columns():
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    have = {}
    for table, name, ddl in NEW_COLUMNS:
        if table not in have:
            have[table] = {c["name"] for c in inspector.get_columns(table)}
        if name not in have[table]:
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            db.session.commit()
            log.info("Added %s.%s column", table, name)


def _backfill_opt_outs():
    """The opt_outs table is created by create_all(). Copy opt-outs that so far lived only on lead rows into it. Adds rows only
    (idempotent: a second start adds nothing), never touches leads, logs a count and no numbers."""
    from . import service
    try:
        added = service.backfill_opt_outs()
        active, review = service.register_counts()
        log.info("Opt-out register: backfilled %d opt-out(s) from existing leads; %d active in total, %d flagged for review "
                 "(country unclear)", added, active, review)
    except Exception:
        db.session.rollback()
        log.exception("Opt-out backfill failed; the service keeps running and will retry on the next start")


def create_app(overrides=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)

    _warn_missing_credentials(app)
    _warn_todo_facts()
    log.info("Lead Sentinel starting, commit=%s", app.config.get("GIT_COMMIT") or "unknown")
    db.init_app(app)
    from . import dashboard, outbound, webhook  # noqa: F401  (importing outbound registers its tables)
    from .outbound import views as outbound_views
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(webhook.bp)
    app.register_blueprint(outbound_views.bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _backfill_opt_outs()
        from .outbound import campaigns
        campaigns.ensure_campaigns()  # two inactive configuration rows; no prospect data

    if app.config["SCHEDULER_ENABLED"]:
        from . import scheduler
        scheduler.start(app)
    return app
