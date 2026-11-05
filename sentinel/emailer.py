"""Optional email copy of owner alerts. Entirely off unless all six SMTP/ALERT settings are present."""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)
REQUIRED = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM")


def is_configured():
    cfg = current_app.config
    return all(cfg.get(k) for k in REQUIRED)


def send_alert(subject, body):
    """Returns True if sent, False if it failed, None if email is not configured (nothing is logged then)."""
    if not is_configured():
        return None
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject[:200], cfg["ALERT_EMAIL_FROM"], cfg["ALERT_EMAIL_TO"]
    msg.set_content(body)
    try:
        port = cfg["SMTP_PORT"]
        if port == 465:
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=15)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=15)
        with server:
            if port != 465:
                server.starttls()
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.send_message(msg)
        return True
    except Exception as exc:  # never log the password or the exception text (some servers echo credentials)
        log.warning("Alert email to %s failed: %s", cfg["ALERT_EMAIL_TO"], type(exc).__name__)
        return False
