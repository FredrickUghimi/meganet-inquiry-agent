"""Reply quality from real conversations: the after-hours note only on escalations, no repeated closing questions, a sentence-level
repeat guard, at most one question. Multi-turn transcripts through the real pipeline; only the Anthropic API is mocked."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from replay import Chat, chat, cl, turn  # noqa: F401  (fixtures)
from sentinel import ai, business_facts as bf, demo, knowledge_base
from sentinel.models import Lead, OpenQuestion, db, utcnow
from test_knowledge_base import system_of

NOTE_RE = re.compile(r"Our team will reply personally from (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) at \d{1,2}:\d{2} (?:AM|PM) WAT\.")
ORG_Q = "Which organisations could you introduce to us?"
INTRO = "Hello Amaka! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant."


@pytest.fixture
def after_hours(monkeypatch):
    """The team is outside business hours (the conftest default is inside)."""
    monkeypatch.setattr(demo, "office_open", lambda now: False)


def msgs(*bodies):
    """Bot messages, oldest first, as the repeat guard sees them."""
    return [SimpleNamespace(direction="outbound", status="sent", channel="whatsapp", sender=None, body=b, sent_at=None)
            for b in bodies]


def bot_texts(chat):
    return chat.bot_messages()


# --- 1. the after-hours note ---------------------------------------------------------------------------------------------------

def test_the_four_turn_partnership_conversation_asks_about_organisations_once_and_adds_the_note_once(chat, cl, after_hours):
    cl.classification.update(product="unsure", topic="partnership")
    t1 = chat.say("Do you pay people who refer customers?",
                  turn("Yes, we have the Mega-Net Referral Partnership Programme. You earn 20% of the referred customer's initial "
                       f"payment, once. {ORG_Q}", answered_last_question=None))
    t2 = chat.say("What about refunds?",
                  turn("No commission is earned on refunded transactions. The written partnership policy covers the rest. "
                       f"{ORG_Q} Our team will reply personally from Monday at 9:00 AM WAT.", answered_last_question=False))
    t3 = chat.say("Do I earn on renewals?",
                  turn("No. Renewals and later subscription payments earn no commission. Which organisations can you introduce to "
                       "us? Our team will reply personally from Monday at 9:00 AM WAT.", answered_last_question=False))
    cl.classification.update(topic="general")
    t4 = chat.say("I used the Daily Sales Report before. Can I still get it?",
                  turn("The Daily Sales Report is no longer offered. Mega-Net ERP Cloud is our current business management "
                       "product. What kind of business do you run, and what do you want to improve?", answered_last_question=False))
    bots = [t1[0], t2[0], t3[0], t4[0]]
    assert sum("organisations" in b for b in bots) == 1, bots  # the question appears at most once in the whole conversation
    assert NOTE_RE.search(t1[0]) and not any(NOTE_RE.search(b) for b in bots[1:])  # only the turn that escalated
    assert sum("Our team will reply personally" in b for b in bots) == 1
    assert "Monday at 9:00 AM WAT" not in " ".join(bots[1:])  # Claude's own copies of the line never reach the customer
    assert len(cl.calls) == 4  # the repeated closings were cut from the end of the replies; nothing needed a second draft
    assert "no longer offered" in t4[0] and not NOTE_RE.search(t4[0]) and "reply personally" not in t4[0]
    assert OpenQuestion.query.count() == 1 and len(chat.owner_texts()) == 1  # one alert for the whole conversation


def test_the_daily_sales_report_answer_has_no_note_and_no_follow_up_promise(chat, cl, after_hours):
    cl.classification.update(product="unsure", topic="general")
    r = chat.say("I used the Daily Sales Report before. Can I still get it?",
                 turn("The Daily Sales Report is no longer offered. Mega-Net ERP Cloud is our current business management "
                      "product. Our team will follow up on Monday at 9:00 AM.", answered_last_question=None),
                 turn("The Daily Sales Report is no longer offered. Mega-Net ERP Cloud is our current business management "
                      "product. What kind of business do you run, and what do you want to improve?"))
    assert not NOTE_RE.search(r[0]) and "follow up" not in r[0] and "reply personally" not in r[0]
    assert "Monday" not in r[0] and r[0].count("?") == 1
    assert chat.owner_texts() == [] and OpenQuestion.query.count() == 0 and chat.lead.after_hours_note_at is None
    assert "keep your reports in a separate setup" not in r[0]


def test_inside_business_hours_no_response_time_is_invented(chat, cl):
    cl.classification.update(product="unsure", topic="partnership")
    r = chat.say("Do you pay referrers?", turn("Yes, there is a programme. The team will reply within 2 hours."),
                 turn("Yes, there is a programme. The team shares the written policy."))
    assert not NOTE_RE.search(r[0]) and "within 2 hours" not in r[0]  # inside hours: nothing is promised, even for an escalation
    assert len(cl.calls) == 2 and "said when the team will reply" in system_of(cl.calls[1])
    assert chat.lead.after_hours_note_at is None and len(chat.owner_texts()) == 1


def test_the_approved_response_promises_are_not_treated_as_invented():
    assert not ai.states_reply_time(bf.DEMO_CONFIRMATION_PROMISE) and not ai.states_reply_time(bf.COMPLAINT_RESPONSE_PROMISE)
    for invented in ("Our team will reply personally from Monday at 9:00 AM WAT.", "Fredrick will get back to you within 3 days.",
                     "The team will be in touch tomorrow.", "Someone will contact you first thing on Monday."):
        assert ai.states_reply_time(invented), invented
    for fine in ("The team will confirm it personally.", "Fredrick will reply personally soon.", "Our office hours are Monday to Friday."):
        assert not ai.states_reply_time(fine), fine


def test_a_real_escalation_outside_business_hours_gets_the_note_once(chat, cl, after_hours):
    cl.classification.update(product="ehr_cloud", topic="general")
    r1 = chat.say("Can the team send me a written quote for a custom report?", turn("I have passed your request to the team.",
                                                                                   needs_human="wants a written custom quote"))
    assert NOTE_RE.search(r1[0]) and r1[0].count("Our team will reply personally") == 1
    note_at = chat.lead.after_hours_note_at
    assert note_at is not None
    r2 = chat.say("Thanks. What modules does EHR Cloud have?", turn("It covers patient records, appointments, billing, lab and pharmacy."))
    assert not NOTE_RE.search(r2[0])  # an ordinary informational answer
    r3 = chat.say("And do you have a lab module?", turn("Yes, the laboratory module handles orders, specimens and results."))
    assert not NOTE_RE.search(r3[0])
    assert chat.lead.after_hours_note_at == note_at


def test_a_new_escalation_inside_24_hours_gets_the_note_again_and_an_old_open_follow_up_after_24_hours(chat, cl, after_hours):
    cl.classification.update(product="ehr_cloud", topic="general")
    chat.say("Can the team send me a written quote for a custom report?", turn("I have passed your request to the team.",
                                                                                needs_human="wants a written custom quote"))
    cl.classification.update(flag="complaint", question="Nobody called me back")
    r2 = chat.say("This is poor service, nobody called me back", turn("I am sorry about that. Fredrick will look into it."))
    assert NOTE_RE.search(r2[0])  # a new escalation (a complaint) inside 24 hours is allowed to carry the note again
    cl.classification.pop("flag"), cl.classification.pop("question")
    cl.classification.update(product="unsure", topic="partnership")
    lead = chat.lead
    lead.after_hours_note_at = utcnow() - timedelta(hours=25)
    db.session.commit()
    r3 = chat.say("Any news on the referral programme?", turn("The programme exists; the team shares the written policy."))
    assert NOTE_RE.search(r3[0])  # an open follow-up (partner enquiry) with no note for more than 24 hours
    r4 = chat.say("OK, noted.", turn("Great."))
    assert not NOTE_RE.search(r4[0])


def test_a_complaint_demo_request_and_ready_lead_are_escalations(chat, cl, after_hours):
    cl.classification.update(product="ehr_cloud", topic="general", intent="ready_demo")
    r = chat.say("Please book a demo for me", turn("Happy to arrange it.", "wants_demo"))
    assert NOTE_RE.search(r[0])  # the lead became ready: Fredrick was alerted


def test_the_after_hours_note_function_rules(monkeypatch):
    monkeypatch.setattr(demo, "office_open", lambda now: demo.in_business_hours(now))  # the real rule, not the test default
    sat_night = utcnow().replace(year=2026, month=11, day=7, hour=21, minute=0)  # Saturday 22:00 Lagos
    note = demo.after_hours_note(sat_night, None, True)
    assert note == "Our team will reply personally from Monday at 9:00 AM WAT."
    assert demo.after_hours_note(sat_night, sat_night - timedelta(hours=2), False) == ""
    assert demo.after_hours_note(sat_night, sat_night - timedelta(hours=2), True) == note
    assert demo.after_hours_note(sat_night, sat_night - timedelta(hours=25), False) == note


# --- 2. repeated closings: question memory and the sentence-level guard ------------------------------------------------------------

def test_the_last_question_is_remembered_and_ages(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    chat.say("Tell me about the company", turn(f"We build software for businesses. {ORG_Q}"))
    assert chat.lead.last_question == ORG_Q and chat.lead.last_question_age == 0
    chat.say("What do you build?", turn("Business and healthcare software.", answered_last_question=False))
    assert chat.lead.last_question == ORG_Q and chat.lead.last_question_age == 1
    chat.say("And for clinics?", turn("EHR Cloud is for clinics.", answered_last_question=False))
    assert chat.lead.last_question_age == 2


def test_an_unanswered_question_is_not_asked_again_in_the_next_two_replies(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    chat.say("Tell me about the company", turn(f"We build software for businesses. {ORG_Q}"))
    r2 = chat.say("What is EHR Cloud?",
                  turn("EHR Cloud is for clinics and hospitals. Could you tell me which organisations you might introduce?",
                       answered_last_question=False),
                  turn("EHR Cloud is for clinics and hospitals.", answered_last_question=False))
    assert "organisations" not in r2[0] and len(cl.calls) == 3
    assert "Do not ask it again" in system_of(cl.calls[1]) and ORG_Q in system_of(cl.calls[1])  # Claude is told what it asked
    assert "has not answered" in system_of(cl.calls[2])
    r3 = chat.say("And ERP Cloud?", turn("ERP Cloud is for business accounting. What do you want to improve first?",
                                         answered_last_question=False))
    assert r3[0].endswith("What do you want to improve first?")  # a different relevant question is fine
    assert chat.lead.last_question == "What do you want to improve first?"


def test_after_two_replies_the_question_may_be_asked_again_and_an_answer_clears_it(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    chat.say("Tell me about the company", turn(f"We build software for businesses. {ORG_Q}"))
    for q, a in (("What is EHR Cloud?", "EHR Cloud is for clinics and hospitals."), ("What is ERP Cloud?", "ERP Cloud is for business accounting."),
                 ("What about PharmacyPro?", "PharmacyPro manages pharmacy stock, dispensing and suppliers.")):
        chat.say(q, turn(a, answered_last_question=False))
    assert chat.lead.last_question_age == 3  # more than the two quiet replies, and no longer in the last three bot messages
    r5 = chat.say("What about HospitalPro?", turn(f"HospitalPro is installed on a local network. {ORG_Q}", answered_last_question=False))
    assert "organisations" in r5[0] and len(cl.calls) == 5
    chat.say("A hospital group in Abuja", turn("Thank you, noted.", answered_last_question=True))
    assert chat.lead.last_question is None and chat.lead.last_question_age == 0


def test_a_reply_that_fully_answers_may_end_without_a_question_and_two_questions_are_regenerated(chat, cl):
    cl.classification.update(product="ehr_cloud", topic="general")
    r = chat.say("Does EHR Cloud have a lab module?", turn("Yes. The laboratory module handles test orders, specimens and results."))
    assert r[0].endswith("results.") and "?" not in r[0] and len(cl.calls) == 1
    r2 = chat.say("And pharmacy?", turn("Yes, there is a pharmacy module. How many staff do you have? And which clinic is it?"),
                  turn("Yes, there is a pharmacy module for prescriptions and dispensing."))
    assert len(cl.calls) == 3 and "?" not in r2[0] and "more than one question" in system_of(cl.calls[2])


def test_the_prompt_rules_say_at_most_one_question_and_none_required():
    assert "Ask at most ONE short question, and only if it helps" in ai.REPLY_RULES
    assert "a reply that fully answers the lead may end without a question" in " ".join(ai.REPLY_RULES.split())
    assert "never repeat a sentence you used in your last three messages" in ai.REPLY_RULES
    assert "one or two" not in ai.REPLY_RULES and "At most two questions" not in " ".join(ai.SITUATIONS.values())


def test_the_daily_sales_report_closing_question_is_narrowed(app):
    core = " ".join(knowledge_base.CORE.split())
    assert ("If you ask anything after that, ask only what kind of business they run and what they want to improve, or nothing."
            in core)
    assert "separate setup" not in core


# --- the sentence-level repeat guard -------------------------------------------------------------------------------------------------

def test_a_sentence_of_six_words_repeated_verbatim_or_nearly_is_found():
    prev = msgs("Revenue Guard finds revenue leakage in hospitals. What kind of business do you run?")
    assert ai.repeated_sentences("What kind of business do you run?", prev) == ["What kind of business do you run?"]
    assert ai.repeated_sentences("what kind of business do you run", prev)  # case and punctuation do not matter
    assert ai.repeated_sentences("Revenue Guard finds revenue leakage in hospitals!", prev)
    assert ai.repeated_sentences("Revenue Guard finds revenue leakages in hospitals.", prev)  # similarity above 0.9
    assert ai.repeated_sentences("What kind of company do you work in?", prev) == []  # a different sentence


def test_short_sentences_the_disclosure_and_older_messages_do_not_count():
    prev = msgs("Thanks for asking. Pharmacy stock is tracked per product.")
    assert ai.repeated_sentences("Thanks for asking.", prev) == []  # under six words
    prev2 = msgs(f"{INTRO} We build software for hospitals and businesses across Nigeria.")
    assert ai.repeated_sentences(f"{INTRO} Something else entirely is said here.", prev2) == []
    old = msgs("Pharmacy stock is tracked per product in every branch.", "Second.", "Third.", "Fourth.")
    assert ai.repeated_sentences("Pharmacy stock is tracked per product in every branch.", list(old)) == []  # four messages back
    recent = msgs("Pharmacy stock is tracked per product in every branch.", "Second.", "Third.")
    assert ai.repeated_sentences("Pharmacy stock is tracked per product in every branch.", recent)  # within the last three


def test_only_bot_messages_are_compared():
    other = [SimpleNamespace(direction="inbound", status="received", channel="whatsapp", sender=None, body="What kind of business do you run?"),
             SimpleNamespace(direction="outbound", status="sent", channel="manual", sender=None, body="What kind of business do you run?"),
             SimpleNamespace(direction="outbound", status="failed", channel="whatsapp", sender=None, body="What kind of business do you run?"),
             SimpleNamespace(direction="outbound", status="sent", channel="whatsapp", sender="owner", body="What kind of business do you run?")]
    assert ai.repeated_sentences("What kind of business do you run?", other) == []


SAFETY = [
    ("official contact", f"You can reach the team on WhatsApp at {bf.OFFICIAL_WHATSAPP_NUMBER} any time you like."),
    ("official link", f"Message us here: {bf.OFFICIAL_WHATSAPP_LINK} whenever you are ready to talk."),
    ("honest AI answer", "Yes, I'm an AI assistant, not a person, and a member of the team follows up personally."),
    ("pricing policy", bf.PRICING_POLICY),
    ("demo confirmation promise", bf.DEMO_CONFIRMATION_PROMISE),
    ("complaint promise", bf.COMPLAINT_RESPONSE_PROMISE),
    ("support hours", f"Support hours: {bf.SUPPORT_HOURS}"),
    ("support route", bf.SUPPORT_ROUTE_FOR_EXISTING_CLIENTS),
    ("pencilled demo", "I have pencilled in Wednesday at 10:00 AM. It is not confirmed yet: the team will confirm it."),
    ("SMS stance", "SMS is built into EHR Cloud but is not active by default and needs a gateway."),
    ("policy hand-over", "The written partnership policy covers that, and the team shares it with registered partners personally."),
    ("renewals", "Renewals and later subscription payments earn no commission at all."),
    ("custom work", "Our technical team will assess every custom development request first."),
    ("demo offer", "Would you also like a personalised demonstration with our team?"),
]


@pytest.mark.parametrize("name,sentence", SAFETY)
def test_safety_sentences_may_repeat(name, sentence):
    assert ai.is_safety_sentence(sentence), name
    assert ai.repeated_sentences(sentence, msgs(sentence)) == [], name


def test_the_safety_list_is_documented_in_code():
    labels = [label for label, _ in ai.SAFETY_SENTENCES]
    assert len(labels) == 9 and any("official WhatsApp" in l for l in labels) and any("AI assistant" in l for l in labels)
    assert any("pricing" in l for l in labels) and any("partnership hand-over" in l for l in labels)


def test_a_repeated_closing_sentence_is_removed_and_the_reply_stays_complete():
    prev = msgs("Business Sentinel flags unusual items. What kind of business do you run and what do you want to improve?")
    text = "ERP Cloud handles sales, stock and payroll in one system.\n\nWhat kind of business do you run and what do you want to improve?"
    new, removed = ai.drop_repeated_sentences(text, prev)
    assert removed == ["What kind of business do you run and what do you want to improve?"]
    assert new == "ERP Cloud handles sales, stock and payroll in one system."


def test_removing_a_sentence_from_the_middle_or_leaving_too_little_means_regenerate():
    prev = msgs("ERP Cloud handles sales, stock and payroll in one system.")
    mid = "ERP Cloud handles sales, stock and payroll in one system. It also has a dashboard for owners."
    assert ai.drop_repeated_sentences(mid, prev) == (mid, ["ERP Cloud handles sales, stock and payroll in one system."])
    short = "Thanks. ERP Cloud handles sales, stock and payroll in one system."
    assert ai.drop_repeated_sentences(short, prev)[0] == short  # only "Thanks." would be left


def test_in_the_pipeline_a_repeated_closing_question_is_removed(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    closing = "What kind of business do you run, and what do you want to improve?"
    chat.say("Tell me about ERP Cloud", turn(f"ERP Cloud is business accounting software for small businesses. {closing}"))
    r2 = chat.say("Does it do payroll?", turn(f"Yes, ERP Cloud runs monthly payroll with PAYE and pension schedules. {closing}"))
    assert len(cl.calls) == 2 and r2[0].endswith("pension schedules.") and "?" not in r2[0]  # trimmed, not regenerated


def test_in_the_pipeline_a_repeated_sentence_in_the_middle_is_regenerated(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    chat.say("Tell me about ERP Cloud", turn("ERP Cloud is business accounting software for small and medium businesses. Is it for you?"))
    r2 = chat.say("Does it do payroll?",
                  turn("ERP Cloud is business accounting software for small and medium businesses. It also runs monthly payroll."),
                  turn("Yes, it runs monthly payroll with PAYE and pension schedules."))
    assert len(cl.calls) == 3 and "repeated a sentence" in system_of(cl.calls[2])
    assert r2[0].endswith("pension schedules.") and "small and medium" not in r2[0]


def test_the_approved_first_reply_the_menu_and_holding_replies_are_not_trimmed(chat, cl):
    from sentinel import knowledge
    cl.classification["product"] = "ehr_cloud"
    r = chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    assert "Is yours a clinic or a hospital" in r[0]


# --- the eval script: multi-turn scenarios and the cross-reply checks ----------------------------------------------------------------

import importlib.util  # noqa: E402
import os  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "eval_replies", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "eval_replies.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)
NOTE = "Our team will reply personally from Monday at 9:00 AM WAT."


def conv(name):
    return next(s for s in ev.SCENARIOS if s["name"] == name)


def failed_conv(replies, turns):
    return {n for n, ok, _ in ev.conversation_checks(replies, turns) if not ok}


def test_the_eval_has_multi_turn_scenarios_for_partnership_informational_and_complaint_conversations():
    names = [s["name"] for s in ev.SCENARIOS if "turns" in s]
    assert len(names) == 3 and any("partnership" in n for n in names) and any("complaint" in n for n in names)
    four = conv("multi-turn partnership: no repeated closing question or note")
    assert [t["say"] for t in four["turns"]][-1].startswith("I used the Daily Sales Report") and len(four["turns"]) == 4
    assert four["turns"][0]["escalates"] and not any(t.get("escalates") for t in four["turns"][1:])
    assert all(s["history"][-1][0] == "in" for s in ev.SCENARIOS)  # --list still works for every scenario


def test_the_cross_reply_checks_catch_a_repeated_sentence_a_repeated_question_and_a_stray_note():
    turns = [dict(escalates=True), dict(), dict()]
    good = ["The programme pays 20% of the first payment, once. Which organisations could you introduce to us?",
            "No commission is earned on refunded transactions.", "Renewals earn no commission at all."]
    assert failed_conv(good, turns) == set()
    repeated = [good[0], "No commission is earned on refunded transactions. The programme pays 20% of the first payment, once.", good[2]]
    assert "no sentence repeated across consecutive replies" in failed_conv(repeated, turns)
    asked = [good[0], "No commission is earned on refunded transactions. Could you tell me which organisations you could introduce?", good[2]]
    assert "no question asked twice in the conversation" in failed_conv(asked, turns)
    stray = [good[0] + " " + NOTE, good[1] + " " + NOTE, good[2]]
    assert "after-hours note only on turns that escalate" in failed_conv(stray, turns)
    twice = [good[0] + " " + NOTE, good[1], good[2] + " " + NOTE]
    assert "after-hours note not repeated without a new escalation" in failed_conv(twice, [dict(escalates=True), dict(), dict()]) or \
        "after-hours note only on turns that escalate" in failed_conv(twice, [dict(escalates=True), dict(), dict()])
    assert failed_conv([good[0] + " " + NOTE, good[1], good[2]], turns) == set()  # the note on the escalating turn is fine


def test_safety_sentences_may_repeat_across_replies_in_the_eval_too():
    replies = [f"You can reach the team on WhatsApp at {bf.OFFICIAL_WHATSAPP_NUMBER} any time you like.",
               f"Of course. You can reach the team on WhatsApp at {bf.OFFICIAL_WHATSAPP_NUMBER} any time you like."]
    assert failed_conv(replies, [dict(), dict()]) == set()


def test_a_good_multi_turn_conversation_passes_through_the_real_pipeline_in_dry_run(app, sent, cl):
    sc = conv("multi-turn product questions: informational answers carry no follow-up promise")
    cl.drafts.extend([turn("Yes. The laboratory module handles test orders, specimens and result entry."),
                      turn("Yes, there is a pharmacy module for the drug formulary, prescriptions and dispensing."),
                      turn("Roles include nurse, physician, front desk, billing clerk and administrator.")])
    lines = []
    passed, total = ev.evaluate(ev.make_eval_app(), [sc], out=lines.append, numbers=[44])
    text = "\n".join(lines)
    assert (passed, total) == (1, 1), text
    assert "[44] multi-turn product questions" in text and "turn 3 customer: What roles" in text and "FAILED SCENARIOS" not in text


def test_a_conversation_with_a_follow_up_promise_on_an_informational_turn_fails_and_is_summarised(app, sent, cl):
    sc = conv("multi-turn product questions: informational answers carry no follow-up promise")
    cl.drafts.extend([turn("Yes. The laboratory module handles test orders, specimens and result entry. Our team will get back to you."),
                      turn("Yes, there is a pharmacy module for the drug formulary, prescriptions and dispensing."),
                      turn("Roles include nurse, physician, front desk, billing clerk and administrator.")])
    lines = []
    passed, _ = ev.evaluate(ev.make_eval_app(), [sc], out=lines.append, numbers=[44])
    text = "\n".join(lines)
    assert passed == 0 and "FAILED SCENARIOS" in text
    section = text.split("FAILED SCENARIOS", 1)[1]
    assert "[44] multi-turn product questions" in section and "turn 1: latest message addressed" in section
    assert "reply (first 300 characters): Hello Amaka!" in section or "reply (first 300 characters): Yes." in section


def test_the_eval_carries_the_saved_state_between_turns(app, sent, cl):
    sc = conv("multi-turn partnership: no repeated closing question or note")
    results = []
    cl.drafts.extend([turn(f"Yes, we have the {bf.PARTNERSHIP_PROGRAMME_NAME}. You earn 20% of the first payment, once. {ORG_Q}")] +
                     [turn("No commission is earned on refunded transactions. The written partnership policy covers the rest.")] * 3)
    lines = []
    ev.evaluate(ev.make_eval_app(), [sc], out=lines.append, numbers=[43])
    assert any("system" in c and ORG_Q in c["system"] for c in cl.calls[1:2]), "turn 2 must know the question it asked in turn 1"
