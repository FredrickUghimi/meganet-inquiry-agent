"""Realistic signed Meta payloads through POST /webhook: lead, reply and log behaviour."""
import hashlib
import hmac
import json
import logging

from sentinel.models import Lead, Message, db, utcnow

OURS = "123"  # WHATSAPP_PHONE_NUMBER_ID in the test app fixture
OWNER = "2340000000"
LEAD = "2348011111111"


def post(app, value, secret=b"shh"):
    payload = {"object": "whatsapp_business_account",
               "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return app.test_client().post("/webhook", data=raw, content_type="application/json",
                                  headers={"X-Hub-Signature-256": sig})


def text_value(sender, phone_id=OURS, mid="wamid.t1", body="Hello SECRETBODY about EHR"):
    return {"messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "2349000000000", "phone_number_id": phone_id},
            "contacts": [{"profile": {"name": "Amaka"}, "wa_id": sender}],
            "messages": [{"from": sender, "id": mid, "timestamp": str(int(utcnow().timestamp())),
                          "type": "text", "text": {"body": body}}]}


def webhook_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name.startswith("sentinel") and "webhook event" in r.getMessage()]


def test_normal_number_creates_lead_replies_and_logs(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    assert post(app, text_value(LEAD)).status_code == 200
    lead = Lead.query.one()
    assert lead.phone == LEAD
    assert sent == [("text", LEAD, "reply:first_contact")]
    lines = webhook_lines(caplog)
    assert len(lines) == 1
    assert "kind=message" in lines[0] and "sender=234********11" in lines[0]
    assert f"phone_number_id={OURS}" in lines[0] and f"created lead {lead.id}" in lines[0]
    assert any("outcome=replied" in r.getMessage() for r in caplog.records)
    text = caplog.text
    assert LEAD not in text and "SECRETBODY" not in text and "shh" not in text and "tok" not in text


def test_second_message_logs_updated_lead(app, sent, claude, caplog):
    post(app, text_value(LEAD, mid="wamid.a"))
    caplog.set_level(logging.INFO)
    post(app, text_value(LEAD, mid="wamid.b"))
    assert "updated lead" in webhook_lines(caplog)[0]
    assert Message.query.filter_by(direction="inbound").count() == 2


def test_owner_number_ignored_by_default_but_logged(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    assert post(app, text_value(OWNER)).status_code == 200
    assert Lead.query.count() == 0 and sent == []
    line = webhook_lines(caplog)[0]
    assert "ignored (sender is OWNER_WHATSAPP_NUMBER" in line and OWNER not in line


def test_owner_as_lead_flag_treats_owner_as_normal_lead(app, sent, claude):
    app.config["OWNER_AS_LEAD"] = True
    post(app, text_value(OWNER))
    assert Lead.query.one().phone == OWNER
    assert ("text", OWNER, "reply:first_contact") in sent


def test_status_update_creates_nothing_and_is_logged(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": OURS},
             "statuses": [{"id": "wamid.out1", "status": "delivered", "recipient_id": LEAD,
                           "timestamp": "1700000000"}]}
    assert post(app, value).status_code == 200
    assert Lead.query.count() == 0 and sent == []
    line = webhook_lines(caplog)[0]
    assert "kind=status" in line and "ignored (delivery status 'delivered')" in line


def test_failed_status_marks_message_failed(app, sent, claude, caplog):
    post(app, text_value(LEAD))
    msg = Message.query.filter_by(direction="outbound").one()
    caplog.set_level(logging.INFO)
    post(app, {"metadata": {"phone_number_id": OURS},
               "statuses": [{"id": msg.external_id, "status": "failed", "recipient_id": LEAD,
                             "errors": [{"code": 131047, "title": "Re-engagement message"}]}]})
    assert db.session.get(Message, msg.id).status == "failed"
    assert "kind=status" in webhook_lines(caplog)[0] and "failed" in webhook_lines(caplog)[0]


def test_other_phone_number_id_ignored_with_warning(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    assert post(app, text_value(LEAD, phone_id="999")).status_code == 200
    assert Lead.query.count() == 0 and sent == []
    assert "phone_number_id=999" in webhook_lines(caplog)[0]
    assert "phone_number_id is not ours" in webhook_lines(caplog)[0]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_phone_number_id_compare_tolerates_whitespace_and_int(app, sent, claude):
    app.config["WHATSAPP_PHONE_NUMBER_ID"] = " 123 "
    post(app, text_value(LEAD, phone_id=123))
    assert Lead.query.count() == 1


def test_reaction_is_not_a_lead_message(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    value = text_value(LEAD)
    value["messages"][0] = {"from": LEAD, "id": "wamid.r", "timestamp": "1700000000", "type": "reaction",
                            "reaction": {"message_id": "wamid.x", "emoji": "👍"}}
    post(app, value)
    assert Lead.query.count() == 0 and sent == []
    assert "ignored (reaction" in webhook_lines(caplog)[0]


def test_closed_lost_lead_gets_no_reply_and_log_says_why(app, sent, claude, caplog):
    post(app, text_value(LEAD, mid="wamid.a"))
    lead = Lead.query.one()
    lead.status = "closed_lost"
    db.session.commit()
    sent.clear()
    caplog.set_level(logging.INFO)
    post(app, text_value(LEAD, mid="wamid.b"))
    assert sent == []
    assert any("lead is closed_lost" in r.getMessage() for r in caplog.records)


def test_exception_is_logged_with_traceback(app, sent, claude, caplog, monkeypatch):
    from sentinel import service
    monkeypatch.setattr(service, "record_inbound", lambda *a, **k: 1 / 0)
    caplog.set_level(logging.INFO)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    r = post(app, text_value(LEAD))
    assert r.status_code == 500
    assert any(r.exc_info for r in caplog.records if "webhook message" in r.getMessage())
    assert "outcome=failed" in webhook_lines(caplog)[0]


def test_bad_signature_logged(app, caplog):
    caplog.set_level(logging.INFO)
    assert post(app, text_value(LEAD), secret=b"wrong").status_code == 403
    assert "invalid or missing X-Hub-Signature-256" in caplog.text
