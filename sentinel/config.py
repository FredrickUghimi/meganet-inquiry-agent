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
        # Dashboard
        "DASHBOARD_PASSWORD": _env("DASHBOARD_PASSWORD"),
        "DISPLAY_TZ": _env("DISPLAY_TZ", "UTC"),
        "PUBLIC_BASE_URL": (_env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:5000").rstrip("/"),
        # Anthropic
        "ANTHROPIC_API_KEY": _env("ANTHROPIC_API_KEY"),
        "ANTHROPIC_MODEL": _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
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
        # Scheduler
        "SCHEDULER_ENABLED": _flag("SCHEDULER_ENABLED", True),
        "FOLLOWUP_CHECK_MINUTES": int(_env("FOLLOWUP_CHECK_MINUTES", "5")),
    }
