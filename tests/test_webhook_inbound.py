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


# --- reply pipeline regressions ---------------------------------------------------------------

def reply_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("reply lead=")]


def test_existing_lead_new_message_gets_reply_with_start_and_end_logs(app, sent, claude, caplog):
    post(app, text_value(LEAD, mid="wamid.a"))
    assert len(sent) == 1
    caplog.set_level(logging.INFO)
    post(app, text_value(LEAD, mid="wamid.b", body="we have 12 staff"))
    assert len(sent) == 2 and sent[1] == ("text", LEAD, "reply:continue")
    lines = reply_lines(caplog)
    assert "started" in lines[0]
    assert "finished outcome=replied reason=continue" in lines[-1]


def test_skipped_reply_logs_reason(app, sent, claude, caplog):
    post(app, text_value(LEAD, mid="wamid.a"))
    lead = Lead.query.one()
    lead.followup_paused = True
    db.session.commit()
    sent.clear()
    caplog.set_level(logging.INFO)
    post(app, text_value(LEAD, mid="wamid.b"))
    assert sent == []
    assert "finished outcome=skipped reason=follow-ups paused" in reply_lines(caplog)[-1]


def test_run_async_uses_own_thread_not_scheduler(app, sent, claude):
    import threading
    from sentinel.scheduler import run_async
    app.extensions["sentinel_scheduler"] = object()  # a dead/forked scheduler must not matter
    done = threading.Event()
    run_async(app, lambda: done.set())
    assert done.wait(5)
    app.extensions.pop("sentinel_scheduler")


def test_sweeper_answers_stuck_unhandled_message(app, sent, claude):
    from datetime import timedelta
    from sentinel import service
    lead = service.find_or_create_lead(phone=LEAD, name="A")
    service.record_inbound(lead, "hello", "whatsapp", external_id="w1", at=utcnow() - timedelta(minutes=10))
    service.run_unanswered()
    assert sent == [("text", LEAD, "reply:first_contact")]
    sent.clear()
    service.run_unanswered()
    assert sent == []


def test_retry_button_replies_flashes_and_requires_csrf(app, sent, claude):
    post(app, text_value(LEAD, mid="wamid.a"))
    lead = Lead.query.one()
    sent.clear()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "tok"
    assert c.post(f"/leads/{lead.id}/retry", data={}).status_code == 400
    r = c.post(f"/leads/{lead.id}/retry", data={"csrf": "tok"}, follow_redirects=True)
    assert b"Retry reply: replied" in r.data
    assert sent == [("text", LEAD, "reply:continue")]
    assert b"Retry reply</button>" in c.get(f"/leads/{lead.id}").data


def test_dry_run_sends_and_writes_nothing(app, sent, claude):
    from sentinel import service
    post(app, text_value(LEAD, mid="wamid.a"))
    sent.clear()
    lines = []
    out = service.handle_lead_reply(Lead.query.one().id, retry=True, dry_run=True, trace=lines.append)
    assert out[0] == "skipped" and "dry run" in out[1] and sent == []
    assert any("claude classify ok" in l for l in lines)


# --- classification: JSON parsing and the 400 from Anthropic ------------------------------------

from types import SimpleNamespace  # noqa: E402

import anthropic  # noqa: E402
try:  # newer anthropic releases ship their own httpx2
    import httpx2 as httpx  # noqa: E402
except ImportError:
    import httpx  # noqa: E402
import pytest  # noqa: E402

from sentinel import ai  # noqa: E402

TOOL_CHOICE_MSG = "tool_choice: type 'tool' and 'any' are not supported for this model"


def make_400():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": TOOL_CHOICE_MSG}}
    return anthropic.BadRequestError(TOOL_CHOICE_MSG, response=httpx.Response(400, request=req, json=body), body=body)


def fake_client(create):
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def text_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


GOOD = '{"intent": "ready_demo", "product": "ehr_cloud", "name": "Amaka", "summary": "Clinic, 12 staff, wants a demo."}'


def test_parse_clean_json():
    r = ai.parse_classification(GOOD)
    assert r == {"intent": "ready_demo", "product": "ehr_cloud", "name": "Amaka",
                 "summary": "Clinic, 12 staff, wants a demo."}


def test_parse_json_in_code_fences_and_prose():
    assert ai.parse_classification(f"```json\n{GOOD}\n```")["intent"] == "ready_demo"
    assert ai.parse_classification(f"Sure! Here you go:\n{GOOD}\nHope that helps {{x}}")["product"] == "ehr_cloud"


def test_parse_invalid_field_values_fall_back_safely():
    r = ai.parse_classification('{"intent": "buy-now!!", "product": "crm", "name": "null", "summary": 5}')
    assert r == {"intent": "exploring", "product": "unsure", "name": None, "summary": ""}


@pytest.mark.parametrize("junk", ["", "I cannot help with that.", "{not json", '{"product": "ehr_cloud"}', "[1, 2]"])
def test_parse_junk_raises(junk):
    with pytest.raises(ai.ClassificationParseError):
        ai.parse_classification(junk)


def test_classify_sends_plain_request_without_tools_or_tool_choice(app, monkeypatch):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return text_response(GOOD)

    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    out = ai.classify(Lead(product_interest="unsure"), [])
    assert out["intent"] == "ready_demo"
    assert "tools" not in seen and "tool_choice" not in seen and "thinking" not in seen
    assert seen["model"] == app.config["ANTHROPIC_MODEL"]


def test_default_model_is_configurable_default(monkeypatch):
    from sentinel.config import load_config
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert load_config()["ANTHROPIC_MODEL"] == "claude-haiku-5-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-custom")
    assert load_config()["ANTHROPIC_MODEL"] == "claude-custom"


def test_anthropic_400_is_logged_as_error_and_shown_on_lead(app, sent, monkeypatch, caplog):
    def create(**kw):
        if "system" in kw and "ONE JSON object" in kw["system"]:
            raise make_400()
        return text_response("Thanks, tell me more.")

    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    post(app, text_value(LEAD))
    lead = Lead.query.one()
    # still replied (fallback to "exploring"), but loudly
    assert sent and sent[0][0] == "text"
    assert lead.ai_note and "BadRequestError http=400" in lead.ai_note and "tool_choice" in lead.ai_note
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("BadRequestError http=400" in e and "claude classify FAILED" in e for e in errors)
    assert "tok" not in caplog.text.replace("tool", "")

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "t"
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert "Claude classification failed" in page and "BadRequestError" in page


def test_successful_classification_clears_note_and_can_make_lead_ready(app, sent, monkeypatch):
    calls = {"n": 0}

    def create(**kw):
        if "ONE JSON object" in kw.get("system", ""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise make_400()
            return text_response("```json\n" + GOOD + "\n```")
        return text_response("Fredrick will call you.")

    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    post(app, text_value(LEAD, mid="wamid.a"))
    assert Lead.query.one().ai_note
    post(app, text_value(LEAD, mid="wamid.b", body="please book a demo"))
    lead = Lead.query.one()
    assert lead.ai_note is None and lead.status == "ready" and lead.product_interest == "ehr_cloud"
    assert any(s[1] == OWNER for s in sent)  # owner escalation alert went out


def test_dry_run_trace_shows_classification_and_draft_text(app, sent, monkeypatch):
    from sentinel import service
    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(
        lambda **kw: text_response(GOOD.replace("ready_demo", "exploring") if "ONE JSON object" in kw["system"]
                                   else "Hello from Mega-Net")))
    post(app, text_value(LEAD, mid="wamid.a"))
    sent.clear()
    lines = []
    service.handle_lead_reply(Lead.query.one().id, retry=True, dry_run=True, trace=lines.append)
    joined = "\n".join(lines)
    assert "intent=exploring" in joined and "Hello from Mega-Net" in joined and sent == []
