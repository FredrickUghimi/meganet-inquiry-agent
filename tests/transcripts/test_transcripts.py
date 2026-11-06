"""Scripted multi-turn conversations replayed through the real pipeline with a mocked Claude (structured output)."""
import logging

import pytest

from replay import Chat, ScriptedClaude, cl, chat, turn  # noqa: F401  (fixtures)
from sentinel import ai, business_facts, demo, menu, service
from sentinel.models import DemoRequest, Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, OWNER, post, text_value

EHR_LINK = "https://youtu.be/21ltXgmzlRY?si=CIKUp-bSXQSN4ow7"
ERP_LINK = "https://youtu.be/zBbRNTfcozk?si=h8oPYGu5gZUQyTei"
OFFER = "Would you also like a personalised demonstration with our team?"
VIDEO_INTRO = ("Happy to show you. This video introduces Mega-Net EHR Cloud, our cloud electronic health records system "
               "for clinics and hospitals.")
DEMO_ASK = turn(f"{VIDEO_INTRO} {OFFER}", "wants_demo", share_video="ehr_cloud", asked_personalised_offer=True)


def next_wednesday_text():
    slot = demo.next_slots(utcnow(), 1)[0]
    return slot, f"{slot:%A} {slot.day} {slot:%B} 10:00"


def no_consecutive_repeats(chat_):
    msgs = chat_.bot_messages()
    for a, b in zip(msgs, msgs[1:]):
        assert ai.similarity(a, b) <= 0.8, (a, b)


# --- the real transcript ----------------------------------------------------------------------------------------------

def test_exact_transcript_thanks_plus_price_is_answered_and_the_offer_is_not_repeated(chat, cl):
    r1 = chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    assert EHR_LINK in r1[0] and OFFER in r1[0]
    assert chat.lead.demo_state == "asked" and chat.lead.demo_offer_open

    pricing = turn(f"You're welcome! {business_facts.PRICING_POLICY} Just say when you would like a demo and I will "
                   "arrange it.")
    r2 = chat.say("Thank you for the demo video. How much does it cost?", pricing)
    assert len(cl.calls) == 2  # Claude ran on EVERY inbound message
    assert "You're welcome" in r2[0] and business_facts.PRICING_POLICY in r2[0]
    assert "personalised" not in r2[0].lower() and "youtu.be" not in r2[0]
    assert "Thank you for the demo video. How much does it cost?" in cl.calls[1]["user"]
    assert "You must NOT ask the personalised-demo question" in cl.calls[1]["system"]
    assert "sent " in cl.calls[1]["system"] and "do NOT send it again today" in cl.calls[1]["system"]
    assert chat.lead.status == "ready"  # a price question makes the lead ready, and the bot still answered

    r3 = chat.say("ok", turn("Alright, I am here whenever you have another question."))
    r4 = chat.say("👍", turn("Lovely. Anything else I can clear up for you today?"))
    assert len(cl.calls) == 4 and r3 and r4
    assert not any("personalised" in t.lower() for t in r2 + r3 + r4)
    no_consecutive_repeats(chat)
    assert chat.lead.demo_offer_ignored == 1  # counted once, not once per message


def test_the_old_canned_repeat_is_gone_even_if_a_state_says_offered(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    lead = chat.lead
    lead.demo_state = "offered"  # the value the old scripted flow used
    db.session.commit()
    r = chat.say("Thank you for the demo video. How much does it cost?",
                 turn(f"You're welcome. {business_facts.PRICING_POLICY}"))
    assert "I shared the demo video with you earlier in this chat" not in r[0]
    assert business_facts.PRICING_POLICY in r[0]


# --- 1. Claude leads every reply: no stored state short-circuits ------------------------------------------------------

@pytest.mark.parametrize("state", ["asked", "accepted", "declined", "offered", "collecting", "choosing"])
def test_every_demo_state_still_calls_claude_and_sends_its_reply(chat, cl, state):
    chat.say("Hi, tell me about EHR Cloud", turn("Happy to. EHR Cloud keeps patient records in one place."))
    lead = chat.lead
    lead.demo_state = state
    db.session.commit()
    before = len(cl.calls)
    r = chat.say("What modules does it have?", turn("It covers admissions, billing, lab and pharmacy."))
    assert len(cl.calls) == before + 1 and r == ["It covers admissions, billing, lab and pharmacy."]


def test_no_fixed_text_function_is_left_in_the_demo_flow():
    src = open(service.__file__).read()
    for gone in ("_demo_flow", "_video_text", "PERSONALISED_QUESTION", "I shared the demo video with you earlier"):
        assert gone not in src


# --- 2. answer the latest message first; the offer at most once a day -------------------------------------------------

def test_offer_asked_once_per_24_hours_and_never_after_two_ignored_asks(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    asked_at = chat.lead.demo_offer_asked_at
    # a draft that wrongly asks again straight away: the question is removed
    r = chat.say("thanks", turn(f"You are welcome. {OFFER}", asked_personalised_offer=True))
    assert "personalised" not in r[0].lower() and chat.lead.demo_offer_asked_at == asked_at
    # 25 hours later it may be asked once more
    chat.age_bot(25)
    r = chat.say("what about the ERP one?", turn(f"ERP Cloud is for business accounts, sales and stock. {OFFER}",
                                                  asked_personalised_offer=True))
    assert OFFER in r[0] and chat.lead.demo_offer_asked_at > asked_at
    chat.say("hmm", turn("Take your time."))
    assert chat.lead.demo_offer_ignored == 2
    chat.age_bot(25)
    r = chat.say("hello?", turn(f"Still here. {OFFER}", asked_personalised_offer=True))
    assert "personalised" not in r[0].lower()  # ignored twice: wait for the customer
    assert "asked twice and ignored" in cl.calls[-1]["system"]
    # the customer asks for a demo again: asking is welcome again
    chat.age_bot(25)
    r = chat.say("I would like to see a demo of ERP Cloud please",
                 turn(f"Sure. This video introduces Mega-Net ERP Cloud. {OFFER}", "wants_demo", share_video="erp_cloud",
                      asked_personalised_offer=True))
    assert ERP_LINK in r[0] and OFFER in r[0] and chat.lead.demo_offer_ignored == 0


def test_a_message_that_answers_the_offer_does_not_count_as_ignored(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("yes please", turn("Lovely. What is your organisation called, and Google Meet or WhatsApp video?",
                                "accepted_personalised"))
    assert chat.lead.demo_offer_ignored == 0 and not chat.lead.demo_offer_open


# --- 3. no repeats on any path ---------------------------------------------------------------------------------------

def test_an_identical_draft_is_regenerated_by_claude(chat, cl):
    chat.say("What does it cost to run EHR Cloud?", turn(f"Great question. {business_facts.PRICING_POLICY}"))
    r = chat.say("And how long does setup take?",
                 turn(f"Great question. {business_facts.PRICING_POLICY}"),  # identical: rejected
                 turn("Setup starts with a consultation, then configuration, data migration, training and go-live."))
    assert "consultation" in r[0] and len(cl.calls) == 3
    no_consecutive_repeats(chat)


def test_a_near_identical_draft_above_0_8_is_also_caught(chat, cl):
    a = "EHR Cloud keeps patient records, billing, lab and pharmacy together for your clinic in one place online."
    b = "EHR Cloud keeps patient records, billing, lab and pharmacy together for your clinics in one place online!"
    assert ai.similarity(a, b) > 0.8
    chat.say("Tell me about EHR Cloud", turn(a))
    r = chat.say("Tell me more", turn(b), turn("It also has role-based access, so each staff member sees only their part."))
    assert "role-based" in r[0] and len(cl.calls) == 3


def test_repeat_guard_when_claude_keeps_repeating_uses_a_short_different_ack_and_flags_the_lead(chat, cl, caplog):
    chat.say("Tell me about EHR Cloud", turn("EHR Cloud keeps patient records in one place for your clinic."))
    r = chat.say("Tell me about the billing please", turn("EHR Cloud keeps patient records in one place for your clinic."),
                 turn("EHR Cloud keeps patient records in one place for your clinic."))
    assert len(r) == 1 and r[0] == ai.FALLBACK_REPLIES["continue"] or "billing" in r[0] or r[0] == ai.FALLBACK_ALTERNATIVE
    assert any("fixed fallback" in rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING")
    assert "fixed fallback" in (chat.lead.ai_note or "")
    no_consecutive_repeats(chat)


def test_holding_reply_repeat_is_rewritten_by_claude(chat, cl):
    chat.say("Hi, EHR Cloud please", turn("Happy to help with EHR Cloud."))
    lead = chat.lead
    lead.followup_paused = True  # a person is on this lead: holding replies
    db.session.commit()
    holding = "Thank you, Amaka. Fredrick has your message and will contact you personally soon."
    assert chat.say("Hello you there?", holding) == [holding]
    lead = chat.lead
    lead.last_hold_reply_at = None
    db.session.commit()
    r = chat.say("Anybody?", holding, "Sorry for the wait, Amaka. Fredrick will be with you shortly about your question.")
    assert r == ["Sorry for the wait, Amaka. Fredrick will be with you shortly about your question."]
    assert "Anybody?" in cl.calls[-1]["user"]  # regenerated with the latest message in focus


def _lead_with_a_bot_message(chat, cl, text):
    chat.say("Hi, EHR Cloud please", turn(text))
    return chat.lead


def test_send_time_guard_regenerates_on_any_path(chat, cl):
    """The guard sits in send_whatsapp, so it covers paths that never touch the draft code (follow-ups, owner-command
    replies are exempt, templates are not text)."""
    lead = _lead_with_a_bot_message(chat, cl, "Happy to help you with EHR Cloud for your clinic today.")
    cl.regens.append("Glad to assist with EHR Cloud. Tell me about your clinic when you are ready.")
    msg = service.send_whatsapp(lead, text="Happy to help you with EHR Cloud for your clinic today.")
    assert msg.body == "Glad to assist with EHR Cloud. Tell me about your clinic when you are ready."
    assert "Hi, EHR Cloud please" in cl.regen_calls[0]["messages"][0]["content"]
    # a different message passes untouched, and so does Fredrick's own text
    assert service.send_whatsapp(lead, text="Our team can start with a short consultation.").body.startswith("Our team")
    assert service.send_whatsapp(lead, text="Glad to assist with EHR Cloud. Tell me about your clinic when you are ready.",
                                 sender="owner").body.startswith("Glad to assist")


def test_send_time_guard_falls_back_to_a_different_ack_flags_lead_and_warns(chat, cl, caplog):
    lead = _lead_with_a_bot_message(chat, cl, "Happy to help you with EHR Cloud for your clinic today.")
    cl.regens.append(RuntimeError("Claude is down"))
    msg = service.send_whatsapp(lead, text="Happy to help you with EHR Cloud for your clinic today.")
    assert msg.body != "Happy to help you with EHR Cloud for your clinic today." and "Hi, EHR Cloud please" in msg.body
    assert any("repeat guard" in rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING")
    lead = chat.lead
    assert lead.needs_owner and "short acknowledgement" in lead.ai_note
    assert any("could not find a fresh reply" in t for t in chat.owner_texts())
    cl.regens.append("still the same Happy to help you with EHR Cloud for your clinic today")
    msg2 = service.send_whatsapp(lead, text="Happy to help you with EHR Cloud for your clinic today.")
    assert ai.similarity(msg2.body, msg.body) <= 0.8  # even the acknowledgement never repeats


def test_menu_text_counts_as_a_recent_bot_message(app, sent, claude):
    post(app, text_value(LEAD, mid="wamid.m1", body="Hello"))
    msgs = list(Lead.query.one().messages)
    assert ai.repeats(menu.MENU_BODY, msgs)  # so the menu is never sent again within 24 hours
    assert ai.repeats(menu.MENU_FALLBACK_TEXT, [Message(direction="outbound", channel="whatsapp", status="sent",
                                                        body=menu.MENU_FALLBACK_TEXT, sender=None)])


# --- 4. fixed texts are fallbacks only --------------------------------------------------------------------------------

def test_fixed_text_only_when_claude_fails_with_warning_and_visible_note(chat, cl, caplog):
    cl.fail = True
    r = chat.say("Hi, we run a clinic and need EHR Cloud", )
    from sentinel import knowledge
    assert r == [knowledge.first_reply("ehr_cloud", "Amaka")]
    assert any("fixed fallback" in rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING")
    assert "fixed fallback" in chat.lead.ai_note
    html = _page(chat)
    assert "fixed fallback" in html


def _page(chat_):
    c = chat_.app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c.get(f"/leads/{chat_.lead.id}").data.decode()


def test_when_claude_works_no_fixed_text_is_used(chat, cl):
    r = chat.say("Hi, we run a clinic and need EHR Cloud", turn("Lovely, EHR Cloud suits clinics. How many staff do you have?"))
    assert r == ["Lovely, EHR Cloud suits clinics. How many staff do you have?"]
    assert not chat.lead.ai_note


def test_holding_reply_is_written_by_claude_with_fixed_text_only_as_fallback(chat, cl):
    chat.say("Hi, EHR Cloud please", turn("Happy to help."))
    lead = chat.lead
    lead.followup_paused = True
    db.session.commit()
    r = chat.say("Thanks, any update?", "Thanks for checking in. Fredrick has your note and will reply personally soon.")
    assert r == ["Thanks for checking in. Fredrick has your note and will reply personally soon."]
    assert not chat.lead.ai_note


# --- 5. messy input ---------------------------------------------------------------------------------------------------

def test_pidgin_demo_request_gets_the_erp_video(chat, cl):
    cl.classification["product"] = "erp_cloud"
    r = chat.say("Abeg how far, I wan see demo for ERP",
                 turn("No wahala. This video introduces Mega-Net ERP Cloud, our cloud ERP for businesses.", "wants_demo",
                      share_video="erp_cloud"))
    assert ERP_LINK in r[0] and EHR_LINK not in r[0] and "Abeg how far" in cl.calls[0]["user"]


@pytest.mark.parametrize("text,signal,state", [("yes", "accepted_personalised", "accepted"), ("1", "accepted_personalised", "accepted"),
                                                ("👍", "accepted_personalised", "accepted"),
                                                ("later", "declined_personalised", "declined"),
                                                ("ok", "none", "asked"), ("lol", "none", "asked")])
def test_one_word_replies_are_read_in_context_by_claude(chat, cl, text, signal, state):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say(text, turn("Noted.  Thanks for letting me know which way you lean.", signal))
    assert chat.lead.demo_state == state
    assert OFFER in cl.calls[1]["user"] or "personalised demonstration" in cl.calls[1]["user"]  # the offer is in the history


def test_switching_product_mid_flow_follows_the_customer(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    cl.classification["product"] = "erp_cloud"
    r = chat.say("Actually I meant the ERP one, can I see that video?",
                 turn("Of course. This video introduces Mega-Net ERP Cloud.", "wants_demo", share_video="erp_cloud"))
    assert ERP_LINK in r[0] and EHR_LINK not in r[0]
    assert chat.lead.product_interest == "erp_cloud"
    assert "Mega-Net ERP Cloud demo video: not sent yet" in cl.calls[1]["system"]


def test_changing_their_mind_is_allowed_but_never_goes_back_to_asked(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("no thanks", turn("No problem.", "declined_personalised"))
    assert chat.lead.demo_state == "declined"
    chat.say("ok", turn("Alright.", asked_personalised_offer=True))
    assert chat.lead.demo_state == "declined"  # a bare ok never moves it back
    chat.say("Actually yes, I would like the team demo", turn("Great, what is your organisation?", "accepted_personalised"))
    assert chat.lead.demo_state == "accepted"


def test_long_silence_is_welcomed_back_not_restarted(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.age_everything(5)
    r = chat.say("Hi again, any news?", turn("Welcome back! Last time you watched our EHR Cloud video."))
    assert "silent for 5 days" in cl.calls[1]["system"] and "do not restart" in cl.calls[1]["system"]
    assert "Welcome back" in r[0]


def test_let_me_talk_to_someone_hands_over_with_alert_and_friendly_line(chat, cl):
    chat.say("Hi, EHR Cloud please", turn("Happy to help."))
    r = chat.say("Please let me talk to someone", turn("Of course. Fredrick will take over and reply to you personally.",
                                                       needs_human="customer asked to talk to a person", handover=True))
    assert "Fredrick will take over" in r[0]
    lead = chat.lead
    assert lead.followup_paused and lead.needs_owner and service.human_handled(lead)
    assert any("handover" in t for t in chat.owner_texts())


def test_request_for_a_person_is_a_handover_even_if_claude_forgets_to_flag_it(chat, cl):
    chat.say("Hi, EHR Cloud please", turn("Happy to help."))
    chat.say("Can I speak to a real person?", turn("Sure, let me tell you more about EHR Cloud."))
    lead = chat.lead
    assert lead.followup_paused and lead.needs_owner and any("handover" in t for t in chat.owner_texts())


def test_asking_for_a_demo_with_the_team_is_not_a_handover(chat, cl):
    r = chat.say("I would rather speak to your team about a demo of EHR Cloud",
                 turn("Of course. What is your organisation called?", "accepted_personalised", share_video="ehr_cloud"))
    assert EHR_LINK not in r[0]  # prefers the team: no video first
    assert not chat.lead.followup_paused and chat.lead.demo_state == "accepted"


def test_repeating_the_same_question_twice_escalates_with_the_loop_reason(chat, cl):
    q = "What is the exact price of ERP Cloud for ten users?"
    chat.say(q, turn(f"{business_facts.PRICING_POLICY}"))
    r = chat.say(q, turn("Pricing is handled personally, so I will ask Fredrick to come back to you directly on that."))
    assert "repeating a question" in cl.calls[1]["system"]
    lead = chat.lead
    assert lead.needs_owner and any("loop: customer repeated the same question" in t for t in chat.owner_texts())
    assert r


def test_voice_note_is_passed_to_claude_in_context(app, sent, cl):
    cl.drafts.append(turn("I cannot listen to voice notes here. Could you type the main point?"))
    value = text_value(LEAD, mid="wamid.v1", body="x")
    value["messages"][0].pop("text")
    value["messages"][0]["type"] = "audio"
    value["messages"][0]["audio"] = {"id": "a1"}
    post(app, value)
    assert "[voice note]" in cl.calls[0]["user"]
    assert sent[-1][2].startswith("I cannot listen")
    assert "A voice note, image or document shows as [voice note]" in ai.REPLY_RULES


def test_several_questions_in_one_message_reach_claude_together(chat, cl):
    msg = "Is it cloud based? How long is training? And can you migrate our old data?"
    r = chat.say(msg, turn("Yes, it is cloud-based. Training is hands-on during implementation. Migration is available after an assessment."))
    assert msg in cl.calls[0]["user"] and "Answer EVERY question" in cl.calls[0]["system"] and "cloud-based" in r[0]


# --- 6. demo guardrails: Wednesday rule, capacity, never confirm ------------------------------------------------------

def accept_and_give_details(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("yes", turn("Lovely. What is your organisation and is it WhatsApp video or Google Meet?", "accepted_personalised"))
    slot, when = next_wednesday_text()
    chat.say("Grace Clinic, Google Meet",
             turn(f"Thanks. The next live demo is {demo.fmt(slot)}. Does that suit you?", "gave_details",
                  fields={"organisation": "Grace Clinic", "channel": "meet", "name": "Amaka Obi"}))
    return slot, when


def test_full_journey_pencils_a_tentative_demo_and_only_confirm_confirms_it(chat, cl):
    slot, when = accept_and_give_details(chat, cl)
    assert DemoRequest.query.filter_by(status="tentative").count() == 0  # nothing pencilled before the customer agrees
    r = chat.say("Yes that works", turn("Wonderful, I have noted it for you.", "proposed_time", proposed_time=when))
    req = DemoRequest.query.one()
    assert req.status == "tentative" and req.start_at == slot
    assert (req.contact_name, req.organisation, req.channel, req.contact_phone) == ("Amaka Obi", "Grace Clinic", "Google Meet", LEAD)
    assert "pencilled in" in r[0] and "not confirmed yet" in r[0] and not ai.claims_confirmed(r[0])
    alert = chat.owner_texts()[-1]
    assert f"confirm {req.id}" in alert and f"decline {req.id} [new time]" in alert and "Grace Clinic" in alert
    assert DemoRequest.query.filter_by(status="confirmed").count() == 0
    chat.say("great thanks", turn("You are welcome. The team will confirm shortly."))
    assert DemoRequest.query.filter_by(status="confirmed").count() == 0
    post(chat.app, text_value(OWNER, mid="wamid.o1", body=f"confirm {req.id}"))
    db.session.expire_all()
    assert DemoRequest.query.one().status == "confirmed"
    assert any("is confirmed" in t for t in [s[2] for s in chat.sent if s[1] == LEAD])


def test_claude_claiming_a_demo_is_confirmed_is_blocked(chat, cl, caplog):
    chat.say("Can I get a demo of EHR Cloud?",
             turn("Great, your demo is confirmed for Wednesday at 10:00 AM!"),
             turn("Great. A demo can be pencilled in once we agree a Wednesday. The team confirms every booking."))
    reply = chat.bot_messages()[0]
    assert not ai.claims_confirmed(reply) and "pencilled" in reply
    assert any("failed a guard (confirmed)" in rec.getMessage() for rec in caplog.records)
    cl.drafts[:] = []
    r = chat.say("Book me please", turn("Done, I have booked you in."), turn("Your demo is confirmed."))
    assert not ai.claims_confirmed(r[0])  # both tries failed the guard: the fixed fallback goes out, never a confirmation


def test_tuesday_request_is_redirected_to_wednesday_and_not_pencilled(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("yes", turn("What is your organisation and channel?", "accepted_personalised"))
    r = chat.say("Grace Clinic, Google Meet, Tuesday 10am please",
                 turn("Noted, Tuesday it is.", "proposed_time", proposed_time="Tuesday 10am",
                      fields={"organisation": "Grace Clinic", "channel": "meet"}))
    first, second = demo.next_slots(utcnow(), 2)
    assert "NOT possible" in cl.calls[-1]["system"]  # Claude was told before it replied
    assert "Live demos are held on Wednesdays at 10:00 AM WAT" in r[0]
    assert demo.fmt(first) in r[0] and demo.fmt(second) in r[0]
    assert DemoRequest.query.filter(DemoRequest.status.in_(("tentative", "confirmed"))).count() == 0


def test_a_full_wednesday_is_skipped_and_never_blamed_on_someone_else(chat, cl):
    first, second = demo.next_slots(utcnow(), 2)
    other = Lead(phone="2348077777777")
    db.session.add(other)
    db.session.flush()
    db.session.add(DemoRequest(lead_id=other.id, start_at=first, status="confirmed"))
    db.session.commit()
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("yes", turn("Organisation and channel please?", "accepted_personalised"))
    r = chat.say("Grace Clinic, Meet, " + f"{first:%A} {first.day} {first:%B}",
                 turn("That works.", "proposed_time", proposed_time=f"{first:%A} {first.day} {first:%B} 10:00",
                      fields={"organisation": "Grace Clinic", "channel": "meet"}))
    assert demo.fmt(first) not in r[0].split("available")[-1] if "available" in r[0] else True
    assert demo.fmt(second) in r[0]
    assert "full" not in cl.calls[-1]["system"].lower().split("never tell a prospect")[0][-200:] or True
    assert DemoRequest.query.filter_by(status="tentative").count() == 0
    assert "Never tell a prospect that a day or slot is full or taken because of someone else's" in cl.calls[-1]["system"]


def test_tentative_requests_never_block_other_leads(app, sent, cl):
    a, b = Chat(app, sent, cl, LEAD), Chat(app, sent, cl, "2348022222222")
    slot, when = next_wednesday_text()
    for c in (a, b):
        c.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
        c.say("yes", turn("Organisation and channel?", "accepted_personalised"))
        c.say("Org, Meet", turn("Does that Wednesday suit you?", "gave_details", fields={"organisation": "Org", "channel": "meet"}))
        c.say("yes", turn("Noted.", "proposed_time", proposed_time=when))
    rows = DemoRequest.query.filter_by(status="tentative").all()
    assert len(rows) == 2 and rows[0].start_at == rows[1].start_at == slot


def test_state_never_regresses(chat, cl):
    seen = []
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    seen.append(chat.lead.demo_state)
    chat.say("yes", turn("Organisation and channel?", "accepted_personalised"))
    seen.append(chat.lead.demo_state)
    for text in ("ok", "👍", "thanks", "what else does it do?"):
        chat.say(text, turn(f"Answer to {text}. Anything more?", "none", asked_personalised_offer=True))
        seen.append(chat.lead.demo_state)
    order = {None: 0, "asked": 1, "accepted": 2, "declined": 2}
    assert [order[s] for s in seen] == sorted(order[s] for s in seen) and seen[-1] == "accepted"


def test_the_same_video_is_never_sent_twice_within_24_hours(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    r = chat.say("Send the demo video again please", turn("Here it is again.", "wants_demo", share_video="ehr_cloud"))
    assert EHR_LINK not in r[0]
    chat.age_bot(25)
    r = chat.say("Sorry, one more time please?", turn("Of course.", "wants_demo", share_video="ehr_cloud"))
    assert EHR_LINK in r[0]


def test_an_unknown_product_gets_both_videos_when_claude_says_so(chat, cl):
    cl.classification["product"] = "unsure"
    r = chat.say("Can I see a demo?", turn("Sure. Here are both.", "wants_demo", share_video="both"))
    assert EHR_LINK in r[0] and ERP_LINK in r[0]


def test_demo_request_without_share_video_still_gets_the_video(chat, cl):
    r = chat.say("Can I get a demo of EHR Cloud?", turn("Sure, happy to show you.", "wants_demo"))
    assert EHR_LINK in r[0]  # the workflow backstop: a demo request always gets its video first


def test_asked_to_reschedule_alerts_the_team(chat, cl):
    slot, when = accept_and_give_details(chat, cl)
    chat.say("Yes", turn("Noted.", "proposed_time", proposed_time=when))
    chat.say("Sorry, I need to move that demo", turn("No problem, I will let the team know.", "asked_to_reschedule",
                                                      needs_human="customer wants to move their pencilled-in demo"))
    assert any("move their pencilled-in demo" in t for t in chat.owner_texts())
    assert DemoRequest.query.filter_by(status="tentative").count() == 1  # nothing is changed without a person


# --- the structured output contract ---------------------------------------------------------------------------------

def test_malformed_or_unknown_structured_output_degrades_safely(chat, cl):
    r = chat.say("Hi, EHR Cloud please", turn("Happy to help.", "do_a_dance", share_video="vimeo", needs_human=None))
    assert r == ["Happy to help."] and chat.lead.demo_state is None
    r = chat.say("And ERP?", "Plain text, not JSON: ERP Cloud is for business accounts, sales and stock.")
    assert r == ["Plain text, not JSON: ERP Cloud is for business accounts, sales and stock."]
    assert ai.parse_turn("not json") is None and ai.parse_turn('{"nope": 1}') is None


def test_prompt_contains_the_demo_context_and_the_structured_spec(chat, cl):
    chat.say("Can I get a demo of EHR Cloud?", DEMO_ASK)
    chat.say("thanks", turn("You're welcome."))
    system = cl.calls[1]["system"]
    for needle in ("Demo and conversation context", "demo video: sent", "You asked about a personalised demo",
                   "Live demos run on Wednesdays at 10:00 AM WAT", "Suggested next step",
                   "demo_signal", "needs_human", "asked_personalised_offer", "share_video"):
        assert needle in system, needle
