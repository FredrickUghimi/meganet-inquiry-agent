"""Thin WhatsApp Cloud API client (Meta Graph API)."""
import logging

import requests
from flask import current_app

log = logging.getLogger(__name__)


class WhatsAppError(Exception):
    pass


def is_configured():
    """True when both credentials needed to send are present."""
    cfg = current_app.config
    return bool(cfg.get("WHATSAPP_TOKEN") and cfg.get("WHATSAPP_PHONE_NUMBER_ID"))


def _post(payload):
    cfg = current_app.config
    token, phone_id = cfg["WHATSAPP_TOKEN"], cfg["WHATSAPP_PHONE_NUMBER_ID"]
    if not token or not phone_id:
        raise WhatsAppError("WhatsApp is not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)")
    url = f"https://graph.facebook.com/{cfg['WHATSAPP_API_VERSION']}/{phone_id}/messages"
    try:
        resp = requests.post(url, json={"messaging_product": "whatsapp", **payload},
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException as exc:
        log.warning("whatsapp send failed: network error %s", type(exc).__name__)
        raise WhatsAppError(f"network error: {exc}") from exc
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
            detail = f"{err.get('message')} (code {err.get('code')})"
        except ValueError:
            detail = resp.text[:200]
        log.warning("whatsapp send failed: HTTP %s %s", resp.status_code, detail)
        raise WhatsAppError(f"HTTP {resp.status_code}: {detail}")
    log.info("whatsapp send ok: HTTP %s", resp.status_code)
    try:
        return resp.json()["messages"][0]["id"]
    except (ValueError, KeyError, IndexError):
        return None


def send_text(to, body, reply_to=None):
    """Free-form text. Only delivered inside the 24h customer service window.
    reply_to: wamid of an inbound message to quote (WhatsApp reply context)."""
    payload = {"to": to, "type": "text", "text": {"body": body, "preview_url": False}}
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    return _post(payload)


def send_template(to, name, lang, params):
    """Pre-approved template with positional body variables {{1}}, {{2}}, ..."""
    template = {"name": name, "language": {"code": lang}}
    if params:
        template["components"] = [{"type": "body",
                                   "parameters": [{"type": "text", "text": p} for p in params]}]
    return _post({"to": to, "type": "template", "template": template})
