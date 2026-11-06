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


def create_app(overrides=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    app.config.update(load_config())
    if overrides:
        app.config.update(overrides)

    _warn_missing_credentials(app)
    _warn_todo_facts()
    db.init_app(app)
    from . import dashboard, webhook
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(webhook.bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()

    if app.config["SCHEDULER_ENABLED"]:
        from . import scheduler
        scheduler.start(app)
    return app
