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


def test_ready_alert_falls_back_to_escalation_template_when_window_closed(app, sent, claude, monkeypatch):
    from sentinel import whatsapp

    def closed_window(to, body):
        raise whatsapp.WhatsAppError("131047 re-engagement")
    monkeypatch.setattr(whatsapp, "send_text", closed_window)
    inbound(app)
    sent.clear()
    claude.update(intent="ready_demo", summary="Clinic, 12 staff, wants a demo.")
    inbound(app, text="Can we book a demo?", mid="wamid.in2")
    db.session.expire_all()
    assert Lead.query.one().status == "ready"
    alert = [s for s in sent if s[0] == "template" and s[1] == "2340000000"]
    assert len(alert) == 1 and alert[0][2] == "meganet_lead_alert"
    line, url = alert[0][3]
    assert "ready for a demo" in line and url == "https://sentinel.test/leads/1"


def test_ready_alert_without_escalation_template_is_not_sent_when_window_closed(app, sent, claude, monkeypatch):
    from sentinel import whatsapp
    app.config["WHATSAPP_TEMPLATE_ESCALATION"] = None  # operator opted out

    def closed_window(to, body):
        raise whatsapp.WhatsAppError("131047 re-engagement")
    monkeypatch.setattr(whatsapp, "send_text", closed_window)
    inbound(app)
    sent.clear()
    claude.update(intent="ready_demo")
    inbound(app, text="Can we book a demo?", mid="wamid.in2")
    db.session.expire_all()
    assert Lead.query.one().status == "ready"  # still flagged in the dashboard
    assert [s for s in sent if s[1] == "2340000000"] == []


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


def post_form_lead(app):
    client = app.test_client()
    client.post("/login", data={"password": "pw", "csrf": ""})  # no csrf yet: rejected
    with client.session_transaction() as s:
        s["auth"], s["csrf"] = True, "tok"
    return client.post("/new", data={"csrf": "tok", "name": "Chidi", "phone": "+234 802 333 3333",
                                     "email": "c@x.com", "source": "email", "message": "Need ERP info"})


def test_form_lead_gets_ack_template_first_message(app, sent, claude):
    assert post_form_lead(app).status_code == 302
    lead = Lead.query.one()
    assert lead.phone == "2348023333333" and lead.source == "email"
    assert service.window_open(lead) is False  # a form lead has never messaged on WhatsApp
    assert sent == [("template", "2348023333333", "meganet_inquiry_ack", ["Chidi", "EHR Cloud"])]  # claude fixture classifies as EHR
    assert Message.query.filter_by(lead_id=lead.id, direction="outbound", status="sent").count() == 1


def test_form_lead_without_ack_template_sends_nothing(app, sent, claude):
    app.config["WHATSAPP_TEMPLATE_ACK"] = None  # operator opted out
    assert post_form_lead(app).status_code == 302
    assert sent == []  # window closed and no template: nothing sent, nothing crashes


def test_template_defaults_match_what_submit_script_submits(app):
    from sentinel.templates_meta import TEMPLATES
    for t in TEMPLATES.values():
        assert app.config[t["env"]] == t["name"]


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


# --- deployment: blank credentials, /healthz ------------------------------------------------

def _blank_app(tmp_path, **extra):
    from sentinel import create_app
    cfg = {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'b.db'}", "SCHEDULER_ENABLED": False,
           "TESTING": True, "SESSION_COOKIE_SECURE": False, "DASHBOARD_PASSWORD": "pw",
           "WHATSAPP_TOKEN": None, "WHATSAPP_PHONE_NUMBER_ID": None, "WHATSAPP_APP_SECRET": None,
           "WHATSAPP_VERIFY_TOKEN": None, "ANTHROPIC_API_KEY": None, "OWNER_WHATSAPP_NUMBER": None}
    cfg.update(extra)
    return create_app(cfg)


def test_healthz_no_auth_no_db_writes(app):
    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.data == b"ok"
    assert Lead.query.count() == 0 and Message.query.count() == 0


def test_blank_credentials_boot_warn_and_banner(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        app = _blank_app(tmp_path)
    assert "WhatsApp not connected" in caplog.text and "ANTHROPIC_API_KEY empty" in caplog.text
    c = app.test_client()
    assert c.get("/healthz").status_code == 200
    with c.session_transaction() as sess:
        sess["csrf"] = "tok"
    assert c.post("/login", data={"password": "pw", "csrf": "tok"}).status_code == 302
    page = c.get("/leads")
    assert page.status_code == 200 and b"WhatsApp not connected" in page.data


def test_blank_credentials_skip_sending_without_crashing(tmp_path, monkeypatch):
    app = _blank_app(tmp_path)
    with app.app_context():
        lead = service.find_or_create_lead(phone="2348022222222", name="Ada", source="form")
        service.record_inbound(lead, "Interested in EHR", "form")
        service.handle_lead_reply(lead.id)  # Claude disabled too: must not raise
        assert Message.query.filter_by(direction="outbound").count() == 0
        service.run_due_followups()
        assert service.notify_owner(lead, "x", "a demo") is False


def test_banner_hidden_when_connected(app, monkeypatch):
    monkeypatch.setitem(app.config, "WHATSAPP_TOKEN", "t")
    monkeypatch.setitem(app.config, "WHATSAPP_PHONE_NUMBER_ID", "1")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"] = True
    assert b"WhatsApp not connected" not in c.get("/leads").data


def test_verify_works_with_only_verify_token(tmp_path):
    app = _blank_app(tmp_path, WHATSAPP_VERIFY_TOKEN="tok")
    c = app.test_client()
    assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=7").data == b"7"
    assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=7").status_code == 403
    assert _blank_app(tmp_path).test_client().get(
        "/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=7").status_code == 403


def test_database_url_postgres_prefix(monkeypatch):
    from sentinel.config import database_uri
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/d")
    assert database_uri() == "postgresql+psycopg2://u:p@h:5432/d"
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    assert database_uri() == "postgresql+psycopg2://u:p@h/d"
