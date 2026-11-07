"""SMS stance: built into EHR Cloud, off by default, works only once a gateway is connected; team confirms; no promises."""
import re
from types import SimpleNamespace

from sentinel import ai, business_facts, knowledge
from sentinel.models import Lead, Message
from test_webhook_inbound import LEAD, post, scripted_client, text_value, GOOD

STANCE = ("SMS (patient messages and appointment reminders) is built into Mega-Net EHR Cloud but is not active by default. "
          "It works only once an SMS gateway is connected for the organisation. Setup, availability and any SMS costs are "
          "confirmed by the team. Never promise it, a date or a price")
HONEST = ("Yes, SMS reminders are built into EHR Cloud, but they are not active by default and work only once an SMS "
          "gateway is connected. Our team will confirm setup, availability and any costs. How many patients do you see a day?")


def flat(text):
    return re.sub(r"\s+", " ", text)


def test_ehr_knowledge_has_the_stance_and_the_restored_facts():
    k = flat(knowledge.EHR_CLOUD_KNOWLEDGE)
    assert STANCE in k
    assert "Appointment reminder SMS: built, off by default for every organisation, and needs a connected SMS gateway" in k
    assert "Patient SMS: built, and stays in simulated mode (nothing real is sent) until a real SMS gateway is connected" in k
    assert "Radiology critical-finding SMS escalation: inactive. Only the in-app critical-finding alert works." in k


def test_no_contradictory_or_denying_sms_line_anywhere():
    texts = {"knowledge": open(knowledge.__file__).read(), "facts": open(business_facts.__file__).read(),
             "ai": open(ai.__file__).read()}
    for name, text in texts.items():
        for m in re.finditer(r"[^.]*\b(SMS|reminders?)\b[^.]*\.", flat(text.replace('"', ""))):
            sentence = m.group(0).lower()
            if "owner" in sentence or "reminder_" in sentence:  # the owner-reminder feature is unrelated
                continue
            if "never say" in sentence:  # an instruction forbidding such wording is not a denial
                continue
            for bad in ("currently unavailable", "does not exist", "doesn't exist", "not built", "never offer",
                        "no sms", "sms of any kind", "not available"):
                assert bad not in sentence, (name, m.group(0))


def test_ai_text_agrees_and_ehr_only():
    assert "built into EHR Cloud but is not active by default and works only once an SMS gateway is connected" in ai.REPLY_RULES
    assert "never say it does not exist" in ai.REPLY_RULES and "It is not part of ERP Cloud" in ai.REPLY_RULES
    assert "sms" not in knowledge.ERP_CLOUD_KNOWLEDGE.lower() and "reminder" not in knowledge.ERP_CLOUD_KNOWLEDGE.lower()
    assert ("SMS features (patient SMS and appointment reminders) are not active out of the box. They work only once an "
            "SMS gateway is connected.") in business_facts.NOT_OFFERED
    assert "unavailable" not in business_facts.NOT_OFFERED.lower()


def test_no_gateway_provider_is_named():
    for text in (knowledge.EHR_CLOUD_KNOWLEDGE, business_facts.NOT_OFFERED, ai.REPLY_RULES):
        assert not re.search(r"twilio|termii|africa'?s talking|infobip|vonage|nexmo|bulksms", text, re.I)


def test_prospect_asking_about_sms_reminders_gets_the_honest_facts_and_answer(app, sent, monkeypatch):
    seen = []
    scripted_client(monkeypatch, [(HONEST, "end_turn")], classify_text=GOOD.replace("ready_demo", "exploring"), seen=seen)
    post(app, text_value(LEAD, mid="wamid.q1", body="Do you send SMS reminders to patients? We use EHR Cloud for our clinic"))
    draft = [k for k in seen if "ONE JSON object" not in k["system"]][-1]
    system = draft["system"]
    assert STANCE in flat(system)  # what the model is told is the true stance
    assert "SMS features (patient SMS and appointment reminders) are not active out of the box" in system
    assert "If asked about SMS, patient messages or appointment reminders" in system
    assert "Do you send SMS reminders to patients?" in draft["messages"][0]["content"]
    reply = sent[-1][2]
    assert reply == knowledge.ai_opening("Amaka") + " " + HONEST  # the first message also says it is the AI assistant
    low = reply.lower()
    assert "gateway" in low and "team will confirm" in low and "built into" in low
    assert not re.search(r"does not exist|doesn't exist|unavailable|will be (ready|live)|works today|₦|\$|free", low)


def test_erp_lead_prompt_has_no_sms_facts(app):
    prompt = ai.system_text(ai._system(Lead(product_interest="erp_cloud"), "x"))
    assert "Appointment reminder SMS: built" not in prompt
