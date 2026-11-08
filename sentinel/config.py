"""Configuration. Everything comes from environment variables; nothing is hardcoded."""
import logging
import os
import secrets

log = logging.getLogger(__name__)


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _flag(name, default=False):
    value = _env(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _numbers(name):
    """Comma separated phone numbers -> tuple of digit-only strings."""
    out = ("".join(c for c in part if c.isdigit()) for part in (_env(name) or "").split(","))
    return tuple(n for n in out if n)


def database_uri():
    url = _env("DATABASE_URL")
    if not url:
        return "sqlite:///sentinel.db"  # lands in ./instance/ for local dev
    # Render and Heroku hand out postgres:// ; SQLAlchemy wants an explicit driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def load_config():
    secret_key = _env("SECRET_KEY")
    if not secret_key:
        secret_key = secrets.token_hex(32)
        log.warning("SECRET_KEY not set: using a random one. Dashboard sessions reset on every restart.")

    return {
        "SECRET_KEY": secret_key,
        "SQLALCHEMY_DATABASE_URI": database_uri(),
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": _flag("SESSION_COOKIE_SECURE", True),
        # Which build is running (Render sets RENDER_GIT_COMMIT); shown on the Settings page and logged at start-up
        "GIT_COMMIT": (_env("RENDER_GIT_COMMIT") or _env("SOURCE_VERSION") or _env("GIT_COMMIT") or "")[:40],
        # Dashboard
        "DASHBOARD_PASSWORD": _env("DASHBOARD_PASSWORD"),
        "DISPLAY_TZ": _env("DISPLAY_TZ", "UTC"),
        "PUBLIC_BASE_URL": (_env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:5000").rstrip("/"),
        # Anthropic
        "ANTHROPIC_API_KEY": _env("ANTHROPIC_API_KEY"),
        "ANTHROPIC_MODEL": _env("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        # auto = switch off hidden reasoning where the model allows it (faster, no max_tokens hits); default = leave it alone
        "ANTHROPIC_THINKING": (_env("ANTHROPIC_THINKING", "auto") or "auto").lower(),
        # WhatsApp Cloud API
        "WHATSAPP_TOKEN": _env("WHATSAPP_TOKEN"),
        "WHATSAPP_PHONE_NUMBER_ID": _env("WHATSAPP_PHONE_NUMBER_ID"),
        "WHATSAPP_VERIFY_TOKEN": _env("WHATSAPP_VERIFY_TOKEN"),
        "WHATSAPP_APP_SECRET": _env("WHATSAPP_APP_SECRET"),
        "WHATSAPP_SKIP_SIGNATURE": _flag("WHATSAPP_SKIP_SIGNATURE", False),
        "WHATSAPP_API_VERSION": _env("WHATSAPP_API_VERSION", "v23.0"),
        "WHATSAPP_TEMPLATE_LANG": _env("WHATSAPP_TEMPLATE_LANG", "en"),
        "WHATSAPP_TEMPLATE_DAY1": _env("WHATSAPP_TEMPLATE_DAY1", "meganet_followup_day1"),
        "WHATSAPP_TEMPLATE_DAY3": _env("WHATSAPP_TEMPLATE_DAY3", "meganet_followup_day3"),
        "WHATSAPP_TEMPLATE_DAY7": _env("WHATSAPP_TEMPLATE_DAY7", "meganet_followup_day7"),
        # First message for form/email leads, and owner alerts outside the 24h window. On by default;
        # submit them with `scripts/submit_templates.py --all`. A send fails (and is logged) until Meta approves them.
        "WHATSAPP_TEMPLATE_ACK": _env("WHATSAPP_TEMPLATE_ACK", "meganet_inquiry_ack"),
        "WHATSAPP_TEMPLATE_ESCALATION": _env("WHATSAPP_TEMPLATE_ESCALATION", "meganet_lead_alert"),
        # Escalation target (digits only, with country code)
        "OWNER_WHATSAPP_NUMBER": "".join(c for c in (_env("OWNER_WHATSAPP_NUMBER") or "") if c.isdigit()) or None,
        # Testing only: treat messages from the owner number as a normal lead instead of ignoring them.
        "OWNER_AS_LEAD": _flag("OWNER_AS_LEAD", False),
        # Waiting prospects (ready / paused / contacted leads who write again)
        "HOLD_REPLY_COOLDOWN_HOURS": float(_env("HOLD_REPLY_COOLDOWN_HOURS", "4")),
        "OWNER_REPLY_REMINDER_MINUTES": float(_env("OWNER_REPLY_REMINDER_MINUTES", "60")),
        # Who handles a lead: the bot keeps answering ready leads unless a human has taken over
        "OWNER_TAKEOVER_HOURS": float(_env("OWNER_TAKEOVER_HOURS", "48")),
        "BOT_ANSWERS_AFTER_READY": _flag("BOT_ANSWERS_AFTER_READY", True),
        # Existing clients get the support route, not sales flows. Digits only, comma separated.
        "EXISTING_CLIENT_NUMBERS": _numbers("EXISTING_CLIENT_NUMBERS"),
        # Team members who are alerted about demo requests and may send confirm / decline (digits, comma separated)
        "STAFF_WHATSAPP_NUMBERS": _numbers("STAFF_WHATSAPP_NUMBERS"),
        # Optional email copy of every owner alert (all six must be set, else email is off)
        "SMTP_HOST": _env("SMTP_HOST"),
        "SMTP_PORT": int(_env("SMTP_PORT", "587")),
        "SMTP_USER": _env("SMTP_USER"),
        "SMTP_PASSWORD": _env("SMTP_PASSWORD"),
        "ALERT_EMAIL_TO": _env("ALERT_EMAIL_TO"),
        "ALERT_EMAIL_FROM": _env("ALERT_EMAIL_FROM"),
        # Scheduler
        "SCHEDULER_ENABLED": _flag("SCHEDULER_ENABLED", True),
        "FOLLOWUP_CHECK_MINUTES": int(_env("FOLLOWUP_CHECK_MINUTES", "5")),
        # Business-initiated follow-ups (WhatsApp templates). 0 = none are sent; everything else keeps working.
        "FOLLOWUPS_ENABLED": _flag("FOLLOWUPS_ENABLED", True),
        # Most follow-up template messages sent per day (Africa/Lagos calendar day), across all leads.
        "FOLLOWUP_DAILY_CAP": int(_env("FOLLOWUP_DAILY_CAP", "30")),
        # Which follow-up days run. "1,3" skips the Day 7 template. Day 3 and Day 7 are Marketing templates, which cost more
        # per message than the Utility Day 1 template.
        "FOLLOWUP_STAGES": _env("FOLLOWUP_STAGES", "1,3,7"),
    }
