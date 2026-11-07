"""The approved first product reply (both products): confirmed features only, the name, the product's own video link inserted
by code exactly as stored, the Wednesday live-demo offer recorded in the lead's demo state, and a Claude-led conversation after."""
import re

import pytest

from replay import Chat, ScriptedClaude, cl, chat, turn  # noqa: F401
from sentinel import ai, business_facts, knowledge, service
from sentinel.models import DemoRequest, Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, post, text_value

EHR_LINK = "https://youtu.be/21ltXgmzlRY?si=CIKUp-bSXQSN4ow7"
ERP_LINK = "https://youtu.be/zBbRNTfcozk?si=h8oPYGu5gZUQyTei"
LINKS = {"erp_cloud": ERP_LINK, "ehr_cloud": EHR_LINK}
OTHER = {"erp_cloud": EHR_LINK, "ehr_cloud": ERP_LINK}
WEDNESDAY = "Personalised live demos are held every Wednesday at 10:00 AM (WAT), subject to confirmation."

# The exact feature sentence of each approved reply: every item was checked against the product code on 2026-11-06.
FEATURES = {
    "erp_cloud": "It combines accounting and financial reports, sales and invoicing, inventory, purchasing and suppliers, "
                 "customer records, HR and payroll, project costing, and Business Sentinel, which checks your records for "
                 "unusual items whenever you run a scan.",
    "ehr_cloud": "It combines patient records, appointments, doctor consultations, nursing workflows, prescriptions, laboratory "
                 "and radiology, pharmacy, inventory, billing and insurance claims, reports, and Financial Sentinel, which "
                 "flags possible billing problems for you to review.",
}
# Claims the code review found NOT BUILT, UNKNOWN or only PARTLY true: never in a first reply.
NEVER = re.compile(r"\b(branch(es)?|tasks?|time tracking|android|iphone|app store|AI|artificial intelligence|pipeline|"
                   r"reminders?|SMS|24/7|guarantee[sd]?|comprehensive|entire operations|all-in-one|all in one|offline|"
                   r"portal|facilities (cannot|can't)|every facility)\b", re.I)


def questions(text):
    """Question marks in the text, not counting the ?si= in the video link."""
    return re.sub(r"https?://\S+", "", text).count("?")


def sent_text(product, name="Amaka", link=True, disclosed=False):
    text = knowledge.first_reply(product, name, disclosed)
    return text.replace(knowledge.VIDEO_PLACEHOLDER, LINKS[product]) if link else text


# --- the approved texts ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("product", ["erp_cloud", "ehr_cloud"])
def test_approved_reply_features_are_exactly_the_confirmed_ones(product):
    text = knowledge.first_reply(product, "Amaka")
    assert FEATURES[product] in text
    assert not NEVER.findall(text.replace("flow into the accounting automatically", "").replace(knowledge.AI_INTRO, ""))
    for banned in knowledge.BANNED_PHRASES:
        assert banned not in text.lower()


@pytest.mark.parametrize("product", ["erp_cloud", "ehr_cloud"])
def test_approved_reply_name_words_question_video_and_wednesday_offer(product):
    text = sent_text(product, "Amaka Obi")
    assert text.startswith(f"Hello Amaka! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. Thank you for your interest in "
                           f"Mega-Net {'ERP' if product == 'erp_cloud' else 'EHR'} Cloud.")
    assert "Obi" not in text and "{" not in text
    assert len(text.split()) <= knowledge.FIRST_REPLY_MAX_WORDS == 165
    assert questions(text) == 1 and text.rstrip().endswith("?")
    assert LINKS[product] in text and text.count("http") == 1 and OTHER[product] not in text
    assert "Watch our product demonstration:\n" + LINKS[product] in text
    assert WEDNESDAY in text
    assert knowledge.first_reply(product, None).startswith("Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. Thank you")
    assert knowledge.first_reply(product, "+2348012345678").startswith("Hello! I'm Mega-Net Lead Sentinel")  # a number is not a name
    # once the bot has introduced itself (for example on the welcome menu) the introduction is not repeated
    again = knowledge.first_reply(product, "Amaka", disclosed=True)
    assert again.startswith("Hello Amaka! Thank you for your interest") and "Lead Sentinel" not in again
    assert knowledge.first_reply("unsure", "Amaka") is None


def test_the_link_is_never_in_a_template_or_a_prompt(app):
    for text in (knowledge.ERP_FIRST_REPLY, knowledge.EHR_FIRST_REPLY, knowledge.first_reply_brief("erp_cloud", "A"),
                 knowledge.first_reply_brief("ehr_cloud", "A"), ai.system_text(ai._system(Lead(product_interest="erp_cloud"), "x")),
                 ai.TURN_SPEC, ai.REPLY_RULES):
        assert "youtu" not in text and "?si=" not in text
    assert knowledge.VIDEO_PLACEHOLDER in knowledge.ERP_FIRST_REPLY and knowledge.VIDEO_PLACEHOLDER in knowledge.first_reply_brief("ehr_cloud")


def test_the_wednesday_line_comes_from_the_business_facts(monkeypatch):
    monkeypatch.setattr(business_facts, "DEMO_WEEKDAY", "Thursday")
    monkeypatch.setattr(business_facts, "DEMO_START_TIME", "14:30")
    assert "every Thursday at 2:30 PM (WAT)" in knowledge.first_reply("erp_cloud", "A")


def test_the_unconfirmed_template_lines_were_softened():
    erp, ehr = knowledge.first_reply("erp_cloud", "A"), knowledge.first_reply("ehr_cloud", "A")
    for gone in ("project tracking", "scans your business activity", "issues that need attention", "on a computer or phone"):
        assert gone not in erp
    assert "project costing" in erp and "whenever you run a scan" in erp
    assert "phone" not in erp.lower() and "runs in a web browser" in erp
    assert "flags unusual billing activity" not in ehr and "flags possible billing problems for you to review" in ehr
    assert "each facility's data stays separate" not in ehr and "each organisation's data stays separate" in ehr


def test_knowledge_summaries_have_verdicts_and_never_claim_lists():
    for text in (knowledge.ERP_CLOUD_KNOWLEDGE, knowledge.EHR_CLOUD_KNOWLEDGE):
        assert "Customer-safe module summary" in text and "Never claim (NOT BUILT or UNKNOWN)" in text
        assert "CONFIRMED" in text and "PARTLY" in text
    erp, ehr = knowledge.ERP_CLOUD_KNOWLEDGE, knowledge.EHR_CLOUD_KNOWLEDGE
    for line in ("Project tracking: PARTLY", "Business Sentinel: PARTLY", "Where it runs: CONFIRMED",
                 "Accounting and financial reports: CONFIRMED", "Sales and purchases flowing into the accounts automatically: CONFIRMED"):
        assert line in erp, line
    for line in ("Patient queues: CONFIRMED", "Online patient booking: CONFIRMED", "NHIS and HMO claims", "Nursing workflows: CONFIRMED",
                 "Separation of each facility's data: PARTLY", "Where it runs: CONFIRMED",
                 "Cloud use with no server: CONFIRMED"):
        assert line in ehr, line
    flat = " ".join(ehr.split())
    assert "no on/off switch" in flat and "fixed template wording" in flat  # booking needs availability; Sentinel without AI


# --- the first reply in the pipeline ---------------------------------------------------------------------------------

@pytest.mark.parametrize("product,word", [("erp_cloud", "ERP Cloud"), ("ehr_cloud", "EHR Cloud")])
def test_first_message_gets_the_approved_reply_with_the_video_inserted_by_code_and_state_recorded(chat, cl, product, word):
    cl.classification["product"] = product
    before = utcnow()
    r = chat.say(f"Hi, I am interested in {word}", turn(knowledge.first_reply(product, "Amaka")))  # Claude keeps the placeholder
    reply = r[0]
    assert reply == sent_text(product)
    assert reply.count(LINKS[product]) == 1 and OTHER[product] not in reply and "{video_url}" not in reply
    assert WEDNESDAY in reply and questions(reply) == 1 and reply.rstrip().endswith("?") and len(reply.split()) <= 165
    assert reply.startswith("Hello Amaka!")
    system = cl.calls[0]["system"]
    assert "APPROVED FIRST REPLY" in system and "youtu" not in system and "{video_url}" in system
    # state recorded at send time: the video (in the sent message, with product and time) and the live-demo offer
    lead = chat.lead
    assert lead.demo_state == "asked" and lead.demo_offer_open and lead.demo_offer_ignored == 0
    assert before <= lead.demo_offer_asked_at <= utcnow()
    sent_at = service._video_sent_at(lead, LINKS[product])
    assert sent_at is not None and service._video_sent_at(lead, OTHER[product]) is None and before <= sent_at <= utcnow()


def test_a_name_is_replaced_by_hello_when_unknown(app, sent, cl):
    cl.classification["product"] = "erp_cloud"
    value = text_value(LEAD, mid="wamid.n1", body="Hi, ERP Cloud please")
    value["contacts"] = []
    cl.drafts.append(turn(knowledge.first_reply("erp_cloud", None)))
    post(app, value)
    assert sent[-1][2].startswith(
        "Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. Thank you for your interest in Mega-Net ERP Cloud.")


def test_claude_cannot_write_or_change_the_link(chat, cl):
    cl.classification["product"] = "erp_cloud"
    typed = knowledge.first_reply("erp_cloud", "Amaka").replace(
        knowledge.VIDEO_PLACEHOLDER, "https://youtu.be/zBbRNTfcozk (short link) and https://example.com/other")
    r = chat.say("Hi, ERP Cloud please", turn(typed))
    assert "example.com" not in r[0] and "zBbRNTfcozk" not in r[0]  # anything but the exact stored link is removed
    assert ERP_LINK not in r[0]  # and the code does not guess a replacement for a link Claude wrote itself


def test_a_video_sent_in_the_last_24_hours_is_not_sent_again(app, sent, cl):
    lead = Lead(phone=LEAD, name="Amaka", product_interest="erp_cloud")
    db.session.add(lead)
    db.session.flush()
    db.session.add(Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=f"Watch: {ERP_LINK}", status="sent",
                           sent_at=utcnow()))
    db.session.commit()
    text, inserted = service.fill_video_placeholder(lead, knowledge.first_reply("erp_cloud", "Amaka"))
    assert not inserted and ERP_LINK not in text and "Watch our product demonstration" not in text
    assert WEDNESDAY in text and text.rstrip().endswith("?")  # the rest of the reply is untouched
    Message.query.filter_by(lead_id=lead.id).one().sent_at = utcnow().replace(year=utcnow().year - 1)
    db.session.commit()
    text, inserted = service.fill_video_placeholder(lead, knowledge.first_reply("erp_cloud", "Amaka"))
    assert inserted and text.count(ERP_LINK) == 1


def test_a_first_message_that_is_a_demo_request_keeps_the_same_structure_without_a_second_video(chat, cl):
    cl.classification["product"] = "ehr_cloud"
    r = chat.say("Hello, can I get a demo of EHR Cloud?",
                 turn(knowledge.first_reply("ehr_cloud", "Amaka"), "wants_demo", share_video="ehr_cloud"))
    assert r[0].count(EHR_LINK) == 1 and "Watch our product demonstration" in r[0] and WEDNESDAY in r[0]
    assert chat.lead.demo_state == "asked"


# --- what happens next: Claude-led, no scripted path ----------------------------------------------------------------

@pytest.mark.parametrize("product,word", [("erp_cloud", "ERP Cloud"), ("ehr_cloud", "EHR Cloud")])
def test_yes_wednesday_after_the_first_reply_is_acceptance_without_a_second_video_or_offer(chat, cl, product, word):
    cl.classification["product"] = product
    chat.say(f"Hi, {word} please", turn(knowledge.first_reply(product, "Amaka")))
    asked_at = chat.lead.demo_offer_asked_at
    r = chat.say("Yes, Wednesday works. I'd like a live demo",
                 turn("Lovely. To arrange it, what is your organisation called, and would you prefer a WhatsApp video call or "
                      "Google Meet?", "accepted_personalised", share_video=product, asked_personalised_offer=True))
    assert len(cl.calls) == 2 and "ACCEPTS it" in cl.calls[1]["system"] and "Do not send the video again" in cl.calls[1]["system"]
    assert chat.lead.demo_state == "accepted" and chat.lead.demo_offer_asked_at == asked_at and not chat.lead.demo_offer_open
    assert "youtu" not in r[0] and "personalised" not in r[0].lower() and "Wednesday" not in r[0]  # no video, no second offer
    assert len([m for m in chat.bot_messages() if LINKS[product] in m]) == 1


def test_collecting_details_then_a_tentative_demo_after_the_first_reply(chat, cl):
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    chat.say("yes please", turn("Great. Organisation and channel?", "accepted_personalised"))
    slot = __import__("sentinel.demo", fromlist=["x"]).next_slots(utcnow(), 1)[0]
    chat.say("Grace Clinic, Google Meet", turn("Thanks.", "gave_details", fields={"organisation": "Grace Clinic", "channel": "meet"}))
    chat.say("that Wednesday is fine", turn("Noted.", "proposed_time", proposed_time=f"{slot:%A} {slot.day} {slot:%B} 10:00"))
    req = DemoRequest.query.one()
    assert req.status == "tentative" and req.organisation == "Grace Clinic"  # never confirmed by the bot


def test_a_thank_you_or_a_question_after_the_first_reply_is_answered_not_met_with_the_offer(chat, cl):
    cl.classification["product"] = "erp_cloud"
    chat.say("Hi, ERP Cloud please", turn(knowledge.first_reply("erp_cloud", "Amaka")))
    asked_at = chat.lead.demo_offer_asked_at
    r = chat.say("Thanks, that is helpful", turn(f"You're welcome! Personalised live demos are held every Wednesday. Would you like one?",
                                                 asked_personalised_offer=True))
    assert "personalised" not in r[0].lower() and chat.lead.demo_offer_asked_at == asked_at
    assert chat.lead.demo_state == "asked" and chat.lead.demo_offer_ignored == 1
    r = chat.say("Does it handle payroll for 40 staff?", turn("Yes. Payroll runs monthly with PAYE, pension and NHF, and payslips can be downloaded."))
    assert "personalised" not in r[0].lower() and "youtu" not in r[0] and len(cl.calls) == 3


def test_a_price_question_after_the_first_reply_is_answered_without_repeating_the_offer(chat, cl):
    cl.classification["product"] = "ehr_cloud"
    chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    r = chat.say("Thank you. How much does it cost?",
                 turn(f"You're welcome! {business_facts.PRICING_POLICY}", asked_personalised_offer=True))
    assert business_facts.PRICING_POLICY in r[0] and "youtu" not in r[0]
    assert "personalised" not in r[0].lower() and "Wednesday" not in r[0]
    assert "You must NOT ask the personalised-demo question" in cl.calls[1]["system"]


def test_the_second_reply_is_a_normal_short_reply(chat, cl):
    cl.classification["product"] = "erp_cloud"
    chat.say("Hi, ERP Cloud please", turn(knowledge.first_reply("erp_cloud", "Amaka")))
    chat.say("We run a shop", turn("Good to know. Does your shop sell on credit as well?"))
    assert "APPROVED FIRST REPLY" in cl.calls[0]["system"] and "APPROVED FIRST REPLY" not in cl.calls[1]["system"]


def test_unknown_product_is_asked_not_given_a_made_up_pitch(chat, cl):
    cl.classification["product"] = "unsure"
    chat.say("Hello, I want your software", turn("Hello Amaka! Is it EHR Cloud for clinics or ERP Cloud for business accounts?"))
    assert "APPROVED FIRST REPLY" not in cl.calls[0]["system"]


# --- the guards -----------------------------------------------------------------------------------------------------

def test_a_reply_with_banned_words_too_long_or_two_questions_is_regenerated(chat, cl):
    cl.classification["product"] = "erp_cloud"
    good = knowledge.first_reply("erp_cloud", "Amaka")
    bad_words = good.replace("brings your day-to-day operations into one place", "is a comprehensive all-in-one system for your entire operations")
    r = chat.say("Hi, ERP Cloud please", turn(bad_words), turn(good))
    assert r == [sent_text("erp_cloud")] and len(cl.calls) == 2
    chat2 = Chat(chat.app, chat.sent, cl, "2348055555555")
    long_reply = good + " " + "Extra words that go on and on. " * 12
    assert len(long_reply.split()) > knowledge.FIRST_REPLY_MAX_WORDS
    assert chat2.say("Hi, ERP Cloud please", turn(long_reply), turn(good)) == [sent_text("erp_cloud")]
    chat3 = Chat(chat.app, chat.sent, cl, "2348066666666")
    two_q = good.replace("software to solve?", "software to solve? And how many staff do you have?")
    assert chat3.say("Hi, ERP Cloud please", turn(two_q), turn(good)) == [sent_text("erp_cloud")]


def test_if_claude_keeps_breaking_the_rules_the_approved_text_itself_is_sent_and_noted(chat, cl):
    cl.classification["product"] = "ehr_cloud"
    bad = "Hello Amaka! This comprehensive all-in-one system runs your entire operations. Which clinic is it?"
    r = chat.say("Hi, EHR Cloud please", turn(bad), turn(bad))
    assert r == [sent_text("ehr_cloud")]  # the approved text with the link inserted by code
    assert "fixed fallback" in chat.lead.ai_note and chat.lead.demo_state == "asked"


def test_tapping_a_product_button_sends_the_approved_first_reply_with_link_and_state(app, sent, cl):
    cl.classification["product"] = "erp_cloud"
    post(app, text_value(LEAD, mid="wamid.a", body="hi"))
    cl.drafts.append(knowledge.first_reply("erp_cloud", "Amaka", disclosed=True))  # the tap draft is plain text
    inter = text_value(LEAD, mid="wamid.b")
    inter["messages"][0].pop("text")
    inter["messages"][0]["type"] = "interactive"
    inter["messages"][0]["interactive"] = {"type": "button_reply", "button_reply": {"id": "erp_cloud", "title": "ERP Cloud"}}
    post(app, inter)
    db.session.expire_all()
    assert sent[-1][2] == sent_text("erp_cloud", disclosed=True)  # the welcome menu already introduced the AI assistant
    lead = Lead.query.one()
    assert lead.product_interest == "erp_cloud" and lead.demo_state == "asked" and lead.demo_offer_open
    assert service._video_sent_at(lead, ERP_LINK) is not None
    assert "APPROVED FIRST REPLY" in cl.calls[-1]["system"] and "youtu" not in cl.calls[-1]["system"]


def test_tap_with_claude_down_sends_the_approved_text_itself(app, sent, cl):
    post(app, text_value(LEAD, mid="wamid.a", body="hi"))
    cl.fail = True
    inter = text_value(LEAD, mid="wamid.b")
    inter["messages"][0].pop("text")
    inter["messages"][0]["type"] = "interactive"
    inter["messages"][0]["interactive"] = {"type": "button_reply", "button_reply": {"id": "ehr_cloud", "title": "EHR Cloud"}}
    post(app, inter)
    assert sent[-1][2] == sent_text("ehr_cloud", disclosed=True)


def test_banned_phrases_are_in_the_reply_rules():
    assert "comprehensive" in ai.REPLY_RULES and "all-in-one" in ai.REPLY_RULES and str(knowledge.FIRST_REPLY_MAX_WORDS) in ai.REPLY_RULES
