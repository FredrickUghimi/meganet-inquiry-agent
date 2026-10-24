import hashlib
import hmac
import json
from datetime import timedelta

from types import SimpleNamespace

from sentinel import ai, service
from sentinel.knowledge import EHR_CLOUD_KNOWLEDGE, ERP_CLOUD_KNOWLEDGE, PRODUCT_NOTES, VOICE
from sentinel.models import Lead, Message, db, utcnow


def inbound(app, phone="2348011111111", text="Hi, interested in EHR for my clinic", mid="wamid.in1", ts=None):
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": phone, "profile": {"name": "Amaka"}}],
        "messages": [{"from": phone, "id": mid, "timestamp": str(ts or int(utcnow().timestamp())),
                      "type": "text", "text": {"body": text}}]}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
    return app.test_client().post("/webhook", data=raw, content_type="application/json",
                                  headers={"X-Hub-Signature-256": sig})


def test_verify_handshake(app):
    c = app.test_client()
    assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=42").data == b"42"
    assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=42").status_code == 403


def test_bad_signature_rejected(app):
    r = app.test_client().post("/webhook", json={}, headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 403


def test_first_contact_creates_lead_and_auto_replies(app, sent, claude):
    assert inbound(app).status_code == 200
    lead = Lead.query.one()
    assert (lead.name, lead.phone, lead.source) == ("Amaka", "2348011111111", "whatsapp")
    assert lead.product_interest == "ehr_cloud" and lead.status == "new"
    assert sent == [("text", "2348011111111", "reply:first_contact")]
    assert lead.next_followup_at == lead.last_inbound_at + timedelta(days=1)


def test_duplicate_webhook_delivery_ignored(app, sent, claude):
    inbound(app)
    inbound(app)
    assert Message.query.filter_by(direction="inbound").count() == 1
    assert len(sent) == 1


def test_ready_lead_alerts_owner_and_stops_sequence(app, sent, claude):
    inbound(app)
    claude.update(intent="ready_pricing", summary="Clinic, 12 staff, wants pricing.")
    inbound(app, text="What does it cost?", mid="wamid.in2")
    lead = Lead.query.one()
    assert lead.status == "ready" and lead.next_followup_at is None
    alert = [s for s in sent if s[1] == "2340000000"]
    assert len(alert) == 1 and "https://sentinel.test/leads/1" in alert[0][2] and "pricing" in alert[0][2]


def test_followup_sequence_day1_3_7_then_cold(app, sent, claude):
    inbound(app)
    sent.clear()
    lead = Lead.query.one()
    t0 = lead.last_inbound_at
    for stage in (1, 3, 7):
        # 25+ hours since their last message, so we are outside the free window: templates only
        for m in Message.query.filter_by(direction="inbound"):
            m.sent_at = utcnow() - timedelta(days=stage, hours=1)
        lead.last_inbound_at = utcnow() - timedelta(days=stage, hours=1)
        lead.next_followup_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        service.run_due_followups()
        kind, to, name, params = sent[-1]
        assert (kind, name) == ("template", f"meganet_followup_day{stage}")
        assert params == ["Amaka", "EHR Cloud", f"line{stage}"]
        assert lead.followup_stage == stage
    assert lead.status == "cold" and lead.next_followup_at is None
    n = len(sent)
    service.run_due_followups()
    assert len(sent) == n  # never messages a cold lead again


def test_reply_stops_sequence_and_revives_cold(app, sent, claude):
    inbound(app)
    lead = Lead.query.one()
    lead.status, lead.followup_stage, lead.next_followup_at = "cold", 7, None
    db.session.commit()
    inbound(app, text="Sorry, been busy. Still keen.", mid="wamid.in2")
    assert lead.status == "engaged" and lead.followup_stage == 0 and lead.next_followup_at is not None


def test_send_failure_does_not_stop_other_leads(app, sent, claude, monkeypatch):
    from sentinel import whatsapp
    inbound(app, phone="2348011111111", mid="a")
    inbound(app, phone="2348022222222", mid="b", text="ERP please")
    sent.clear()
    for m in Message.query.filter_by(direction="inbound"):
        m.sent_at = utcnow() - timedelta(days=2)
    for l in Lead.query.all():
        l.last_inbound_at = utcnow() - timedelta(days=2)
        l.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    def flaky(to, name, lang, params):
        if to == "2348011111111":
            raise whatsapp.WhatsAppError("boom")
        sent.append(("template", to, name, params))
    monkeypatch.setattr(whatsapp, "send_template", flaky)
    service.run_due_followups()
    first, second = Lead.query.order_by(Lead.id).all()
    assert first.followup_stage == 0 and first.followup_failures == 1
    assert Message.query.filter_by(lead_id=first.id, status="failed").count() == 1
    assert second.followup_stage == 1 and len(sent) == 1


def test_form_lead_does_not_open_whatsapp_window(app, sent, claude):
    client = app.test_client()
    client.post("/login", data={"password": "pw", "csrf": ""})  # no csrf yet: rejected
    with client.session_transaction() as s:
        s["auth"], s["csrf"] = True, "tok"
    r = client.post("/new", data={"csrf": "tok", "name": "Chidi", "phone": "+234 802 333 3333",
                                  "email": "c@x.com", "source": "email", "message": "Need ERP info"})
    assert r.status_code == 302
    lead = Lead.query.one()
    assert lead.phone == "2348023333333" and lead.source == "email"
    assert sent == []  # no ack template configured, window closed: nothing sent, nothing crashes
    assert service.window_open(lead) is False


def test_dashboard_requires_login_and_actions(app, sent, claude):
    inbound(app)
    client = app.test_client()
    assert client.get("/leads").status_code == 302
    with client.session_transaction() as s:
        s["auth"], s["csrf"] = True, "tok"
    page = client.get("/leads").data.decode()
    assert "Mega-Net Lead Sentinel" in page and "Amaka" in page
    assert "reply:first_contact" in client.get("/leads/1").data.decode()
    assert client.post("/leads/1/action", data={"action": "pause"}).status_code == 400  # csrf missing
    client.post("/leads/1/action", data={"action": "pause", "csrf": "tok"})
    lead = db.session.get(Lead, 1)
    assert lead.followup_paused and lead.next_followup_at is None
    client.post("/leads/1/action", data={"action": "resume", "csrf": "tok"})
    db.session.refresh(lead)
    assert not lead.followup_paused and lead.next_followup_at is not None
    client.post("/leads/1/action", data={"action": "closed_won", "csrf": "tok"})
    db.session.refresh(lead)
    assert lead.status == "closed_won" and lead.next_followup_at is None


def test_login_wrong_and_right_password(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    assert c.post("/login", data={"password": "bad", "csrf": "tok"}).status_code == 200
    assert c.post("/login", data={"password": "pw", "csrf": "tok"}).status_code == 302


# --- product knowledge in the reply prompt ---------------------------------------------------

def fake_anthropic(monkeypatch, product="unsure"):
    """Replace the Anthropic client; record every system prompt. Classification returns `product`."""
    prompts = []

    def create(**kw):
        prompts.append(kw["system"])
        if "tools" in kw:
            block = SimpleNamespace(type="tool_use", input={"intent": "exploring", "product": product, "summary": "s"})
        else:
            block = SimpleNamespace(type="text", text="ok")
        return SimpleNamespace(content=[block])

    monkeypatch.setattr(ai, "_get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    return prompts


def assert_baseline(system):
    assert VOICE in system and PRODUCT_NOTES in system


def test_ehr_lead_prompt_has_ehr_facts_only(app):
    system = ai._system(Lead(product_interest="ehr_cloud"), "x")
    assert_baseline(system)
    assert EHR_CLOUD_KNOWLEDGE in system and ERP_CLOUD_KNOWLEDGE not in system
    assert "the EHR Cloud knowledge above" in system and "ERP Cloud knowledge" not in system


def test_erp_lead_prompt_has_erp_facts_only(app):
    system = ai._system(Lead(product_interest="erp_cloud"), "x")
    assert_baseline(system)
    assert ERP_CLOUD_KNOWLEDGE in system and EHR_CLOUD_KNOWLEDGE not in system
    assert "the ERP Cloud knowledge above" in system and "EHR Cloud knowledge" not in system


def test_unsure_or_undetermined_lead_prompt_has_neither(app):
    for interest in ("unsure", None, "something_else"):
        system = ai._system(Lead(product_interest=interest), "x")
        assert_baseline(system)
        assert EHR_CLOUD_KNOWLEDGE not in system and ERP_CLOUD_KNOWLEDGE not in system
        assert "from the voice guide and product notes above" in system


def test_every_draft_path_uses_the_leads_product(app, monkeypatch):
    prompts = fake_anthropic(monkeypatch)
    lead = Lead(product_interest="erp_cloud")
    ai.classify(lead, [])
    ai.draft_reply(lead, [], "continue")
    ai.draft_followup_line(lead, [], 3, [])
    assert len(prompts) == 3
    assert all(ERP_CLOUD_KNOWLEDGE in p and EHR_CLOUD_KNOWLEDGE not in p for p in prompts)


def test_lead_picks_up_detailed_facts_once_product_is_known(app, sent, monkeypatch):
    prompts = fake_anthropic(monkeypatch, product="ehr_cloud")
    inbound(app, text="Hi, can we talk?")
    lead = Lead.query.one()
    assert lead.product_interest == "ehr_cloud"
    # classify() ran while the lead was still "unsure"; the reply drafted after it already uses EHR facts
    assert EHR_CLOUD_KNOWLEDGE not in prompts[0]
    assert EHR_CLOUD_KNOWLEDGE in prompts[1] and ERP_CLOUD_KNOWLEDGE not in prompts[1]
