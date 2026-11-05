"""First-contact tap menu: greeting detection, taps, typed replies, fallback, cooldown, display."""
import logging
from datetime import timedelta

import pytest

from sentinel import ai, menu, whatsapp
from sentinel.models import Lead, Message, db, utcnow
from tests.test_webhook_inbound import LEAD, OWNER, post, text_value


def interactive_value(sender, choice_id, title, mid="wamid.tap1", kind="button_reply"):
    v = text_value(sender, mid=mid)
    v["messages"][0].pop("text")
    v["messages"][0]["type"] = "interactive"
    v["messages"][0]["interactive"] = {"type": kind, kind: {"id": choice_id, "title": title}}
    return v


@pytest.fixture(autouse=True)
def no_claude_opener(monkeypatch):
    monkeypatch.setattr(ai, "is_generic_opener", lambda text: False)


def test_menu_constants_fit_whatsapp_limits():
    assert len(menu.MENU_BODY) <= 1024 and all(len(t) <= 20 for _, t in menu.MENU_BUTTONS)
    assert [i for i, _ in menu.MENU_BUTTONS] == ["ehr_cloud", "erp_cloud", "other"]


def test_greeting_sends_menu_not_draft(app, sent, claude, caplog):
    caplog.set_level(logging.INFO)
    post(app, text_value(LEAD, body="Hello"))
    assert sent == [("buttons", LEAD, menu.MENU_BODY, ["ehr_cloud", "erp_cloud", "other"])]
    lead = Lead.query.one()
    assert lead.awaiting_choice and lead.menu_sent_at
    out = Message.query.filter_by(direction="outbound").one()
    assert out.external_id == "wamid.out1" and out.body == menu.MENU_BODY
    assert any("menu lead=" in r.getMessage() and "sent via=buttons" in r.getMessage() for r in caplog.records)
    assert "Hello" not in "".join(r.getMessage() for r in caplog.records if "menu" in r.getMessage())


@pytest.mark.parametrize("body", ["hi", "Good morning", "are you there?", "Anyone", "I need help", "hey there!"])
def test_generic_openers_get_menu(app, sent, claude, body):
    post(app, text_value(LEAD, body=body))
    assert sent and sent[0][0] == "buttons"


@pytest.mark.parametrize("body", ["Hello, I need EHR Cloud", "hi do you do ERP?", "Hello how much is it",
                                   "Hi we run a clinic with 12 staff"])
def test_product_or_question_skips_menu(app, sent, claude, body):
    post(app, text_value(LEAD, body=body))
    assert sent and sent[0][0] == "text"
    assert not Lead.query.one().awaiting_choice


def test_uncertain_opener_asks_claude(app, sent, claude, monkeypatch):
    monkeypatch.setattr(ai, "is_generic_opener", lambda text: True)
    post(app, text_value(LEAD, body="Hello good people of Mega"))
    assert sent[0][0] == "buttons"


def test_tap_sets_product_and_sends_qualifying_question(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    claude["product"] = "unsure"  # prove the tap, not the classifier, sets the product
    post(app, interactive_value(LEAD, "erp_cloud", "ERP Cloud"))
    lead = Lead.query.one()
    assert lead.product_interest == "erp_cloud" and lead.status == "engaged" and not lead.awaiting_choice
    assert sent[-1] == ("text", LEAD, "reply:qualify")
    inbound = Message.query.filter_by(direction="inbound").order_by(Message.id.desc()).first()
    assert inbound.body == "ERP Cloud" and inbound.choice_id == "erp_cloud"


def test_other_flags_lead_and_alerts_owner_once(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, interactive_value(LEAD, "other", "Something else"))
    lead = Lead.query.one()
    assert lead.needs_owner and lead.status == "engaged"
    assert ("text", LEAD, menu.OTHER_REPLY) in sent
    alerts = [s for s in sent if s[0] == "text" and s[1] == OWNER]
    assert len(alerts) == 1 and "Something else" in alerts[0][2]
    from sentinel import service
    assert service.wait_label(lead) is not None  # shows under "Needs your reply" until Fredrick answers


def test_list_reply_is_handled_like_button(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, interactive_value(LEAD, "ehr_cloud", "EHR Cloud", kind="list_reply"))
    assert Lead.query.one().product_interest == "ehr_cloud"


def test_meta_rejection_falls_back_to_text(app, sent, claude, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def reject(*a, **k):
        raise whatsapp.WhatsAppError("HTTP 400: nope")
    monkeypatch.setattr(whatsapp, "send_buttons", reject)
    post(app, text_value(LEAD, body="hello"))
    assert sent == [("text", LEAD, menu.MENU_FALLBACK_TEXT)]
    assert Lead.query.one().awaiting_choice
    assert Message.query.filter_by(direction="outbound").one().external_id == "wamid.out1"
    assert any("fallback used" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("reply,product", [("1", "ehr_cloud"), ("2", "erp_cloud"), ("ERP", "erp_cloud"),
                                            ("ehr", "ehr_cloud")])
def test_typed_choice_works_like_a_tap(app, sent, claude, reply, product):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    claude["product"] = "unsure"
    post(app, text_value(LEAD, body=reply, mid="wamid.b"))
    assert Lead.query.one().product_interest == product
    assert sent[-1] == ("text", LEAD, "reply:qualify")


def test_typed_3_means_other(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, text_value(LEAD, body="3", mid="wamid.b"))
    assert Lead.query.one().needs_owner and ("text", LEAD, menu.OTHER_REPLY) in sent


def test_normal_message_ignores_menu(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, text_value(LEAD, body="We run a pharmacy and need stock control", mid="wamid.b"))
    lead = Lead.query.one()
    assert not lead.awaiting_choice and not lead.needs_owner
    assert sent[-1] == ("text", LEAD, "reply:continue")


def test_digit_without_menu_is_not_a_choice(app, sent, claude):
    post(app, text_value(LEAD, body="2", mid="wamid.a"))
    assert sent[-1] == ("text", LEAD, "reply:first_contact")


def test_no_second_menu_within_six_hours(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, text_value(LEAD, body="hello", mid="wamid.b"))
    assert [s[0] for s in sent].count("buttons") == 1
    lead = Lead.query.one()
    assert not menu_allowed_after(lead, hours=1) and menu_allowed_after(lead, hours=7)


def menu_allowed_after(lead, hours):
    from sentinel import service
    lead.menu_sent_at = utcnow() - timedelta(hours=hours)
    lead.awaiting_choice = False
    return service.menu_allowed(lead)


@pytest.mark.parametrize("setup", ["ready", "paused", "closed_lost"])
def test_no_menu_for_ready_paused_or_closed(app, sent, claude, setup):
    lead = Lead(phone=LEAD, status=setup if setup != "paused" else "engaged", followup_paused=setup == "paused")
    db.session.add(lead)
    db.session.commit()
    post(app, text_value(LEAD, body="hi"))
    assert not any(s[0] == "buttons" for s in sent)


def test_menu_not_sent_to_existing_conversation(app, sent, claude):
    post(app, text_value(LEAD, body="Tell me about EHR Cloud", mid="wamid.a"))
    post(app, text_value(LEAD, body="hi", mid="wamid.b"))
    assert not any(s[0] == "buttons" for s in sent)


def test_interactive_reply_stored_not_dropped_and_shown(app, sent, claude):
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, interactive_value(LEAD, "ehr_cloud", "EHR Cloud"))
    assert Message.query.filter_by(direction="inbound").count() == 2
    lead = Lead.query.one()
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"] = True
    html = c.get(f"/leads/{lead.id}").data.decode()
    assert "Chose: EHR Cloud" in html


def test_owner_reply_clears_needs_owner(app, sent, claude):
    from sentinel import service
    post(app, text_value(LEAD, body="hi", mid="wamid.a"))
    post(app, interactive_value(LEAD, "other", "Something else"))
    lead = Lead.query.one()
    service.mark_contacted(lead)
    assert not lead.needs_owner and service.wait_label(lead) is None
