"""WhatsApp Cloud API webhook: verification handshake and inbound messages."""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, request

from . import service
from .models import Message, db
from .scheduler import run_async

log = logging.getLogger(__name__)
bp = Blueprint("webhook", __name__)


def _signature_ok(raw):
    cfg = current_app.config
    secret = cfg["WHATSAPP_APP_SECRET"]
    if not secret:
        if cfg["WHATSAPP_SKIP_SIGNATURE"]:
            return True
        log.error("WHATSAPP_APP_SECRET not set: rejecting webhook (set WHATSAPP_SKIP_SIGNATURE=1 for local testing)")
        return False
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def _body_of(m):
    kind = m.get("type")
    if kind == "text":
        return m["text"]["body"]
    if kind == "button":
        return m["button"].get("text", "[button]")
    if kind == "interactive":
        inter = m["interactive"]
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "[interactive reply]")
    if kind in ("image", "video", "document"):
        caption = m.get(kind, {}).get("caption")
        return f"[{kind}] {caption}" if caption else f"[{kind}]"
    if kind == "audio":
        return "[voice note]"
    return f"[{kind or 'unsupported'} message]"


@bp.get("/webhook")
def verify():
    expected = current_app.config["WHATSAPP_VERIFY_TOKEN"]
    token = request.args.get("hub.verify_token", "")
    if (request.args.get("hub.mode") == "subscribe" and expected
            and hmac.compare_digest(token, expected)):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@bp.post("/webhook")
def receive():
    raw = request.get_data()
    if not _signature_ok(raw):
        return "Invalid signature", 403
    payload = request.get_json(silent=True) or {}
    owner = current_app.config["OWNER_WHATSAPP_NUMBER"]
    to_handle = set()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}

            for m in value.get("messages", []):
                phone = service.normalize_phone(m.get("from"))
                if not phone or phone == owner:
                    continue  # Fredrick texting the business number is not a lead
                lead = service.find_or_create_lead(phone=phone, name=names.get(m.get("from")), source="whatsapp")
                at = datetime.fromtimestamp(int(m["timestamp"]), timezone.utc).replace(tzinfo=None) \
                    if m.get("timestamp") else None
                if service.record_inbound(lead, _body_of(m), "whatsapp", external_id=m.get("id"), at=at):
                    to_handle.add(lead.id)

            for s in value.get("statuses", []):
                if s.get("status") == "failed":
                    msg = Message.query.filter_by(external_id=s.get("id")).first()
                    if msg:
                        err = (s.get("errors") or [{}])[0]
                        msg.status = "failed"
                        msg.error = f"{err.get('title') or err.get('message')} (code {err.get('code')})"[:500]
                        db.session.commit()
                        log.warning("WhatsApp delivery failed for message %s: %s", msg.id, msg.error)

    for lead_id in to_handle:
        run_async(current_app._get_current_object(), service.handle_lead_reply, lead_id)
    return "OK", 200
