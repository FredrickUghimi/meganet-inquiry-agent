"""Regression: production once replied to "Can EHR Cloud send SMS to patients?" with the retired scripted line
"I shared the demo video with you earlier in this chat. Would you also like a personalised demonstration with our team?"
(the old _demo_flow / _video_text path, removed in dad4799). These tests replay that conversation through the real webhook,
classification, draft and send path with only Anthropic mocked, for every stored demo state old or new, and make sure no
stored state, earlier message or database value can bring fixed text back."""
import logging
import os
import re

import pytest

from replay import Chat, cl, chat, turn  # noqa: F401
from sentinel import ai, business_facts, knowledge, service
from sentinel.models import DemoRequest, Lead, Message, db, utcnow

RETIRED = "I shared the demo video with you earlier in this chat. Would you also like a personalised demonstration with our team?"
FRAGMENTS = ("shared the demo video", "earlier in this chat", "demo video with you earlier")
EHR_LINK = business_facts.video_for("ehr_cloud")[1]
QUESTION = "Can EHR Cloud send SMS to patients?"
SMS_ANSWER = ("Good question. SMS for patients is built into EHR Cloud, but it is not active by default: it works only once an "
              "SMS gateway is connected for your organisation. The team will confirm setup, availability and any costs.")
# every value lead.demo_state has ever held: the old scripted flow's (offered, collecting, choosing), the current ones, and none
STATES = [None, "offered", "collecting", "choosing", "asked", "accepted", "declined"]


def first_reply_message_1(chat_, cl_, shape):
    """Message 1: the first reply with the video and the Wednesday offer, in two shapes of earlier bot message."""
    cl_.classification["product"] = "ehr_cloud"
    if shape == "approved":
        chat_.say("Hi, I am interested in EHR Cloud", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    else:  # an older style of reply that says "demo video" in plain words, and records no state at all
        chat_.say("Hi, I am interested in EHR Cloud",
                  turn(f"Hello Amaka! Here is our demo video: {EHR_LINK}\n\nWould you also like a personalised demonstration "
                       "with our team?"))


@pytest.mark.parametrize("shape", ["approved", "old-style"])
@pytest.mark.parametrize("state", STATES)
def test_the_sms_question_is_answered_by_claude_whatever_state_the_lead_is_stored_in(chat, cl, state, shape):
    first_reply_message_1(chat, cl, shape)
    assert len(cl.calls) == 1
    lead = chat.lead
    lead.demo_state = state  # data as old releases (or any release) may have left it
    if state in (None, "offered", "collecting", "choosing"):
        lead.demo_offer_asked_at, lead.demo_offer_open = None, False  # old rows carried no offer bookkeeping
    db.session.commit()

    r = chat.say(QUESTION, turn(SMS_ANSWER))

    assert len(cl.calls) == 2, "Claude must run on the SMS question; a stored state may not short-circuit it"
    assert r == [SMS_ANSWER]  # the reply is Claude's answer, not fixed text
    low = r[0].lower()
    assert "gateway" in low and "team will confirm" in low and "built into" in low
    assert not any(f in low for f in FRAGMENTS) and "personalised demonstration" not in low
    assert QUESTION in cl.calls[1]["user"]  # the latest message is what Claude was answering
    assert "built into Mega-Net EHR Cloud but is not active by default" in " ".join(cl.calls[1]["system"].split())
    assert EHR_LINK not in r[0]  # no second video
    assert DemoRequest.query.count() == 0 or state in ("accepted", "collecting", "choosing")
    bot = chat.bot_messages()
    assert not any(ai.is_retired(m) for m in bot) and bot[-1] == SMS_ANSWER


def test_the_exact_old_text_is_blocked_even_if_claude_copies_it_from_the_history(chat, cl, caplog):
    caplog.set_level(logging.WARNING)
    cl.classification["product"] = "ehr_cloud"
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    # the production database already holds the retired line as an earlier bot message
    lead = chat.lead
    db.session.add(Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=RETIRED, status="sent",
                           sent_at=utcnow(), handled=True))
    lead.demo_state = "offered"
    db.session.commit()
    r = chat.say(QUESTION, turn(RETIRED), turn(SMS_ANSWER))  # Claude copies it once; the guard regenerates
    assert r == [SMS_ANSWER] and len(cl.calls) == 3
    assert any("failed a guard (retired" in rec.getMessage() or "retired" in rec.getMessage() for rec in caplog.records)
    # and the retired line is hidden from the prompt so Claude is not taught to copy it
    assert "shared the demo video" not in cl.calls[1]["user"] and "(an earlier automatic message)" in cl.calls[1]["user"]


def test_if_claude_keeps_returning_the_retired_line_the_fallback_is_not_the_retired_line(chat, cl):
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    r = chat.say(QUESTION, turn(RETIRED), turn(RETIRED))
    assert r and not ai.is_retired(r[0]) and "gateway" not in r[0].lower()
    assert "fixed fallback" in chat.lead.ai_note  # visible on the lead page


def test_the_send_time_guard_also_blocks_the_retired_line_on_any_path(chat, cl):
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    lead = chat.lead
    cl.regens.append("Thanks for asking. The team will come back to you shortly on that.")
    msg = service.send_whatsapp(lead, text=RETIRED)
    assert msg.body == "Thanks for asking. The team will come back to you shortly on that." and not ai.is_retired(msg.body)
    cl.regens.append(RuntimeError("down"))
    msg = service.send_whatsapp(lead, text=RETIRED)
    assert not ai.is_retired(msg.body)  # the acknowledgement fallback, never the retired line


@pytest.mark.parametrize("state", STATES)
def test_claude_down_still_never_sends_the_retired_line_for_any_state(chat, cl, state, caplog):
    first_reply_message_1(chat, cl, "old-style")
    lead = chat.lead
    lead.demo_state = state
    db.session.commit()
    cl.fail = True
    r = chat.say(QUESTION)
    assert r and not any(f in r[0].lower() for f in FRAGMENTS) and "personalised demonstration" not in r[0].lower()
    assert any("fixed fallback" in rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING")


def test_a_reply_that_is_only_the_offer_when_asking_is_not_allowed_gets_a_real_answer_not_filler(chat, cl):
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))  # the offer was just made
    cl.regens.append(SMS_ANSWER)
    r = chat.say(QUESTION, turn("Would you also like a personalised demonstration with our team?", asked_personalised_offer=True))
    assert r == [SMS_ANSWER]
    assert "anything else you would like to know" not in r[0]


# --- nothing in the code or its data can hold the text ---------------------------------------------------------------

def test_no_fragment_of_the_retired_text_exists_in_any_production_file():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    hits = []
    for base in ("sentinel", "scripts", "app.py", "render.yaml", "requirements.txt"):
        path = os.path.join(root, base)
        files = [path] if os.path.isfile(path) else [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
        for f in files:
            if f.endswith((".pyc", ".db")) or "__pycache__" in f:
                continue
            text = open(f, encoding="utf-8", errors="ignore").read().lower()
            for frag in FRAGMENTS:
                if frag in text:
                    hits.append((os.path.relpath(f, root), frag))
    # the one allowed home is the block-list in ai.py that stops the line from ever being sent
    assert {h[0] for h in hits} <= {"sentinel/ai.py"}, hits
    ai_lines = [ln for ln in open(os.path.join(root, "sentinel", "ai.py"), encoding="utf-8").read().splitlines()
                if any(f in ln.lower() for f in FRAGMENTS)]
    assert len(ai_lines) == 1 and ai_lines[0].lstrip().startswith("RETIRED_FRAGMENTS"), ai_lines
    src = open(os.path.join(root, "sentinel", "service.py")).read()
    for gone in ("_demo_flow", "_video_text", "PERSONALISED_QUESTION", "DEMO_STATE_TTL"):
        assert gone not in src


def test_old_demo_state_values_are_mapped_never_acted_on_as_a_script(app):
    for old, new in (("offered", "asked"), ("collecting", "accepted"), ("choosing", "accepted"), (None, None), ("asked", "asked")):
        assert service.personalised_state(Lead(demo_state=old)) == new


def test_the_settings_page_shows_the_running_commit_so_a_stale_deploy_is_visible(app):
    app.config["GIT_COMMIT"] = "134014bb28dd0ce63ee70dc465d2916e87fb049a"
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    html = c.get("/settings").data.decode()
    assert "Running commit" in html and "134014b" in html
    app.config["GIT_COMMIT"] = ""
    assert "unknown" in c.get("/settings").data.decode()


def test_a_question_mark_inside_a_video_link_is_not_counted_as_a_question():
    reply = f"Hello Amaka! Watch our product demonstration:\n{EHR_LINK}\n\nIs yours a clinic or a hospital?"
    assert reply.count("?") == 2 and ai.question_count(reply) == 1
    assert ai.question_count("One? Two?") == 2 and ai.question_count(None) == 0
