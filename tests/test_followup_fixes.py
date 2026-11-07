"""Demo capacity (confirmed only), confirm guards, existing clients support vs sales."""
import re
from datetime import timedelta

import pytest

from sentinel import ai, business_facts, demo, knowledge, service
from sentinel.models import DemoRequest, Lead, db, utcnow
from replay import Chat, cl, turn  # noqa: F401
from test_webhook_inbound import LEAD, OWNER, post, text_value

STAFF = "2347000000001"
OTHER = "2348022222222"


def say(app, body, mid, sender=LEAD):
    post(app, text_value(sender, mid=mid, body=body))
    db.session.expire_all()


def to(sent, number):
    return [s[2] for s in sent if s[1] == number]


def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c


def pencil_for(app, sent, cl, sender, tag):
    """A customer pencils in the next Wednesday through the soft demo flow (structured drafts)."""
    chat = Chat(app, sent, cl, sender)
    slot = demo.next_slots(utcnow(), 1)[0]
    chat.say("Can I get a demo of EHR Cloud?", turn("Sure.", "wants_demo", share_video="ehr_cloud"))
    chat.say("yes", turn("Great. Organisation and channel?", "accepted_personalised"))
    chat.say(f"Org {tag}, Google Meet", turn("Thanks.", "gave_details", fields={"organisation": "Org " + tag, "channel": "meet"}))
    chat.say("that Wednesday is fine", turn("Noted.", "proposed_time", proposed_time=f"{slot:%A} {slot.day} {slot:%B} 10:00"))
    return DemoRequest.query.filter_by(status="tentative").order_by(DemoRequest.id.desc()).first()


# --- 1. capacity: only confirmed demos fill a Wednesday ------------------------------------------

def test_tentative_requests_do_not_block_each_other_or_the_slot(app, sent, cl):
    a = pencil_for(app, sent, cl, LEAD, "a")
    b = pencil_for(app, sent, cl, OTHER, "b")
    assert a.start_at == b.start_at and a.id != b.id  # same Wednesday, both pencilled
    assert demo.validate(a.start_at, utcnow(), lead_id=999)[0] is True  # still bookable by a third lead
    assert a.start_at in demo.next_slots(utcnow(), 2)


def test_confirm_refused_when_wednesday_full_and_decline_works(app, sent, cl):
    app.config["STAFF_WHATSAPP_NUMBERS"] = (STAFF,)
    a = pencil_for(app, sent, cl, LEAD, "a")
    b = pencil_for(app, sent, cl, OTHER, "b")
    sent.clear()
    say(app, f"confirm {a.id}", "wamid.c1", OWNER)
    assert DemoRequest.query.get(a.id).status == "confirmed"
    sent.clear()
    say(app, f"confirm {b.id}", "wamid.c2", STAFF)  # staff gets the same refusal
    msg = to(sent, STAFF)[0]
    assert "already full of confirmed demos" in msg and f"decline {b.id} [new time]" in msg
    assert DemoRequest.query.get(b.id).status == "tentative" and to(sent, OTHER) == []
    sent.clear()
    say(app, f"confirm {b.id}", "wamid.c3", OWNER)
    assert "already full" in to(sent, OWNER)[0]
    sent.clear()
    say(app, f"decline {b.id}", "wamid.c4", STAFF)
    assert DemoRequest.query.get(b.id).status == "declined"
    offered = "".join(to(sent, OTHER)).split("The next available are", 1)[1]
    assert demo.fmt(a.start_at) not in offered  # offered other Wednesdays, not the full one


def test_demos_page_groups_tentative_per_wednesday(app, sent, cl):
    a = pencil_for(app, sent, cl, LEAD, "a")
    pencil_for(app, sent, cl, OTHER, "b")
    html = client(app).get("/demos").data.decode()
    assert "By Wednesday" in html and "confirmed 0/1" in html and "tentative 2" in html
    assert f"#{a.id}" in html


def test_prompt_never_says_a_day_is_full_because_of_others(app):
    prompt = ai.system_text(ai._system(Lead(product_interest="ehr_cloud"), "x"))
    assert "Never tell a prospect that a day or slot is full or taken because of someone else's request" in prompt
    assert "Never say a demo day is full or taken because of another person's request" in ai.REPLY_RULES


# --- 2. confirm guards ---------------------------------------------------------------------------

def make_demo(app, start):
    lead = Lead(phone="2340099")
    db.session.add(lead)
    db.session.flush()
    d = DemoRequest(lead_id=lead.id, start_at=start, status="tentative")
    db.session.add(d)
    db.session.commit()
    return d


@pytest.mark.parametrize("sender", [OWNER, STAFF])
def test_confirm_refused_when_start_has_passed(app, sent, claude, sender):
    app.config["STAFF_WHATSAPP_NUMBERS"] = (STAFF,)
    d = make_demo(app, demo.now_local(utcnow()) - timedelta(hours=1))
    say(app, f"confirm {d.id}", "wamid.p1", sender)
    assert "already passed" in to(sent, sender)[0]
    assert DemoRequest.query.get(d.id).status == "tentative"


@pytest.mark.parametrize("sender", [OWNER, STAFF])
def test_confirm_inside_notice_period_works_with_warning(app, sent, claude, sender):
    app.config["STAFF_WHATSAPP_NUMBERS"] = (STAFF,)
    d = make_demo(app, demo.now_local(utcnow()) + timedelta(minutes=30))
    say(app, f"confirm {d.id}", "wamid.w1", sender)
    reply = to(sent, sender)[0]
    assert reply.startswith(f"Confirmed demo {d.id}") and "Warning: it starts in under 2 hours" in reply
    assert DemoRequest.query.get(d.id).status == "confirmed"


def test_confirm_far_ahead_has_no_warning(app, sent, claude):
    d = make_demo(app, demo.now_local(utcnow()) + timedelta(days=3))
    say(app, f"confirm {d.id}", "wamid.f1", OWNER)
    assert "Warning" not in to(sent, OWNER)[0]


# --- 3. existing clients: support vs sales -------------------------------------------------------

def test_support_message_gets_support_route_one_alert_no_sales(app, sent, claude):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    claude["intent"] = "exploring"
    say(app, "My report shows an error, how do I fix it?", "wamid.s1")
    reply = to(sent, LEAD)[0]
    assert business_facts.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS in reply and business_facts.SUPPORT_HOURS in reply
    say(app, "It is still not working", "wamid.s2")
    assert len(to(sent, OWNER)) == 1
    lead = Lead.query.one()
    assert lead.is_existing_client and not lead.upgrade_enquiry and lead.status == "new"
    assert service.next_followup_time(lead) is None
    assert "Existing client</span>" in client(app).get(f"/leads/{lead.id}").data.decode()


@pytest.mark.parametrize("via_classifier", [True, False])
def test_sales_message_is_a_normal_sales_lead_with_label_and_alert(app, sent, claude, via_classifier):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    claude["intent"] = "exploring"
    if via_classifier:
        claude["client_topic"] = "sales"
    say(app, "We want to upgrade and move to the cloud with ERP Cloud", "wamid.u1")
    lead = Lead.query.one()
    assert lead.is_existing_client and lead.upgrade_enquiry
    assert to(sent, LEAD) == ["reply:first_product_reply"]  # normal sales reply, not the support route
    assert len(to(sent, OWNER)) == 1 and "upgrade enquiry" in to(sent, OWNER)[0]
    assert "Existing client, upgrade enquiry" in client(app).get(f"/leads/{lead.id}").data.decode()
    assert service.next_followup_time(lead) is not None  # a sales lead may get follow-ups


def test_pricing_or_demo_from_existing_client_is_sales_even_if_classifier_says_support(app, sent, claude):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    claude["client_topic"] = "support"
    say(app, "Can we get a demo of the new modules?", "wamid.u1")
    assert Lead.query.one().upgrade_enquiry


def test_product_name_alone_in_a_problem_is_still_support(app, sent, claude):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    claude["client_topic"] = "support"
    say(app, "My EHR Cloud login shows an error", "wamid.u1")
    assert not Lead.query.one().upgrade_enquiry and business_facts.SUPPORT_HOURS in to(sent, LEAD)[0]


def test_support_after_sales_clears_the_label_and_no_menu_for_clients(app, sent, claude):
    app.config["EXISTING_CLIENT_NUMBERS"] = (LEAD,)
    say(app, "How much to upgrade?", "wamid.u1")
    assert Lead.query.one().upgrade_enquiry
    say(app, "Hello", "wamid.u2")
    assert not any(s[0] == "buttons" for s in sent) and not Lead.query.one().upgrade_enquiry


def test_classifier_parse_client_topic():
    base = '{"intent": "exploring", "product": "unsure", "name": null, "summary": "s", "existing_client": true, '
    assert ai.parse_classification(base + '"client_topic": "sales"}')["client_topic"] == "sales"
    assert "client_topic" not in ai.parse_classification(base + '"client_topic": "other"}')
    assert ai.client_topic_hint("how much does it cost") == "sales" and ai.client_topic_hint("I forgot my password") == "support"
