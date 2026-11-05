"""WhatsApp Cloud API webhook: verification handshake and inbound messages."""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, request

from . import service
from .models import Lead, Message, db
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


def _log_event(kind, sender, phone_id, outcome):
    """One INFO line per webhook event. Never includes tokens, secrets or message text."""
    log.info("webhook event kind=%s sender=%s phone_number_id=%s outcome=%s",
             kind, service.mask_phone(sender), phone_id or "-", outcome)


def _ids_match(seen, configured):
    return str(seen).strip() == str(configured).strip()


@bp.post("/webhook")
def receive():
    raw = request.get_data()
    if not _signature_ok(raw):
        log.warning("webhook rejected: invalid or missing X-Hub-Signature-256 (403)")
        return "Invalid signature", 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        log.info("webhook event kind=other sender=- phone_number_id=- outcome=ignored (body is not a JSON object)")
        return "OK", 200
    cfg = current_app.config
    owner = cfg["OWNER_WHATSAPP_NUMBER"]
    configured_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID")
    to_handle = {}
    seen_any = False

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = (value.get("metadata") or {}).get("phone_number_id")
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            id_mismatch = bool(configured_id and phone_id is not None and not _ids_match(phone_id, configured_id))

            for m in value.get("messages") or []:
                seen_any = True
                sender = m.get("from")
                try:
                    phone = service.normalize_phone(sender)
                    if not phone:
                        _log_event("message", sender, phone_id, "ignored (no sender number)")
                    elif id_mismatch:
                        log.warning("webhook message addressed to phone_number_id=%s but this service is "
                                    "WHATSAPP_PHONE_NUMBER_ID=%s", phone_id, configured_id)
                        _log_event("message", phone, phone_id, "ignored (phone_number_id is not ours)")
                    elif phone == owner and m.get("type") == "text" and service.is_owner_command(_body_of(m)):
                        # signature already verified; only the exact owner number can run commands
                        _log_event("message", phone, phone_id,
                                   f"owner command {_body_of(m).split()[0].lower().rstrip(':')}")
                        run_async(current_app._get_current_object(), service.run_owner_command, _body_of(m))
                    elif phone == owner and not cfg.get("OWNER_AS_LEAD"):
                        _log_event("message", phone, phone_id,
                                   "ignored (sender is OWNER_WHATSAPP_NUMBER; set OWNER_AS_LEAD=1 to test)")
                    elif m.get("type") == "reaction":
                        _log_event("message", phone, phone_id, "ignored (reaction, not a message)")
                    else:
                        existed = Lead.query.filter_by(phone=phone).first() is not None
                        lead = service.find_or_create_lead(phone=phone, name=names.get(sender), source="whatsapp")
                        at = datetime.fromtimestamp(int(m["timestamp"]), timezone.utc).replace(tzinfo=None) \
                            if m.get("timestamp") else None
                        if service.record_inbound(lead, _body_of(m), "whatsapp", external_id=m.get("id"), at=at):
                            to_handle[lead.id] = (phone, phone_id)
                            _log_event("message", phone, phone_id,
                                       f"{'updated' if existed else 'created'} lead {lead.id}")
                        else:
                            _log_event("message", phone, phone_id, f"ignored (duplicate delivery of {m.get('id')})")
                except Exception:
                    db.session.rollback()
                    log.exception("webhook message from %s failed", service.mask_phone(sender))
                    _log_event("message", sender, phone_id, "failed (exception, see traceback above)")
                    raise

            for s in value.get("statuses") or []:
                seen_any = True
                recipient = s.get("recipient_id")
                status = s.get("status")
                outcome = f"ignored (delivery status '{status}')"
                if status == "failed":
                    msg = Message.query.filter_by(external_id=s.get("id")).first()
                    if msg:
                        err = (s.get("errors") or [{}])[0]
                        msg.status = "failed"
                        msg.error = f"{err.get('title') or err.get('message')} (code {err.get('code')})"[:500]
                        db.session.commit()
                        log.warning("WhatsApp delivery failed for message %s: %s", msg.id, msg.error)
                        outcome = f"failed (delivery failure recorded on message {msg.id}, code {err.get('code')})"
                    else:
                        outcome = "ignored (failed status for an unknown message id)"
                _log_event("status", recipient, phone_id, outcome)

    if not seen_any:
        log.info("webhook event kind=other sender=- phone_number_id=- outcome=ignored (no messages or statuses)")

    for lead_id in to_handle:
        run_async(current_app._get_current_object(), service.handle_lead_reply, lead_id)
    return "OK", 200
