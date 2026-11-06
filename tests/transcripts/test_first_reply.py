"""The approved first product reply: only confirmed features, the lead's name, within 150 words, one question."""
import re

import pytest

from replay import Chat, ScriptedClaude, cl, chat, turn  # noqa: F401
from sentinel import ai, knowledge, menu, service
from sentinel.models import Lead, db
from test_webhook_inbound import LEAD, post, text_value

# What each approved reply may promise (one entry per bullet), checked against the product code on 2026-11-06.
ERP_BULLETS = [
    "Accounting and financial reports", "Sales, invoicing and payments", "Inventory and stock management",
    "Purchasing and suppliers", "Customer records, with follow-up dates and notes", "HR and payroll",
    "Project costing, to see the cost and profit of each job",
    "Business Sentinel, which checks your records for unusual items whenever you run a scan",
]
EHR_BULLETS = [
    "Patient registration and records", "Appointments, with an online booking page for each facility",
    "Billing, payments and insurance claims, built around Nigerian health insurance (NHIS)",
    "Lab, radiology and pharmacy", "Inventory and purchase orders", "Wards, admissions and discharge",
    "Reports and dashboards, and Financial Sentinel, which flags possible billing problems for you to review",
]
# Claims the code review found NOT BUILT, UNKNOWN or only PARTLY true: they must never appear in a first reply.
NEVER = re.compile(r"\b(branch(es)?|multi-?branch|tasks?|time tracking|android|iphone|app store|AI|artificial intelligence|"
                   r"pipeline|reminders?|SMS|24/7|guarantee[sd]?|comprehensive|entire operations|all-in-one|all in one|"
                   r"automatic(ally)? (follow|remind|scan)|offline|portal)\b", re.I)


def bullets(text):
    return [line[1:].strip() for line in text.splitlines() if line.startswith("•")]


@pytest.mark.parametrize("product,expected", [("erp_cloud", ERP_BULLETS), ("ehr_cloud", EHR_BULLETS)])
def test_approved_reply_lists_only_confirmed_features(product, expected):
    text = knowledge.first_reply(product, "Amaka")
    assert bullets(text) == expected
    for banned in knowledge.BANNED_PHRASES:
        assert banned not in text.lower()
    # "automatically" appears once in ERP: sales and purchases posting to the accounts, which the code does confirm
    forbidden = NEVER.findall(text.replace("flow into the accounting automatically", ""))
    assert not forbidden, forbidden


@pytest.mark.parametrize("product", ["erp_cloud", "ehr_cloud"])
def test_approved_reply_uses_the_name_stays_within_150_words_and_ends_with_one_question(product):
    text = knowledge.first_reply(product, "Amaka Obi")
    assert text.startswith("Hello Amaka! Thank you for your interest in Mega-Net ")
    assert "Obi" not in text and "{name}" not in text
    assert len(text.split()) <= knowledge.FIRST_REPLY_MAX_WORDS == 150
    assert text.count("?") == 1 and text.rstrip().endswith("?")
    assert knowledge.first_reply(product, None).startswith("Hello there!")
    assert knowledge.first_reply(product, "+2348012345678").startswith("Hello there!")  # a bare number is not a name
    assert knowledge.first_reply("unsure", "Amaka") is None


def test_the_erp_template_lines_that_were_not_confirmed_were_softened():
    erp = knowledge.first_reply("erp_cloud", "Amaka")
    for gone in ("Project tracking", "scans your business activity", "Customer records and follow-ups",
                 "on a computer or phone"):
        assert gone not in erp
    assert "Project costing" in erp and "whenever you run a scan" in erp and "follow-up dates and notes" in erp
    assert "The modules work together, so your sales and purchases flow into the accounting automatically." in erp


def test_knowledge_has_a_customer_safe_summary_with_never_claim_lists():
    for text in (knowledge.ERP_CLOUD_KNOWLEDGE, knowledge.EHR_CLOUD_KNOWLEDGE):
        assert "Customer-safe module summary" in text and "Never claim (NOT BUILT or UNKNOWN)" in text
        assert "CONFIRMED" in text and "PARTLY" in text
    erp = knowledge.ERP_CLOUD_KNOWLEDGE
    for line in ("Project tracking: PARTLY", "Business Sentinel: PARTLY", "Sales pipeline: PARTLY", "Customer follow-ups: PARTLY",
                 "Use on a phone: PARTLY", "Low-stock monitoring: CONFIRMED", "Accounting and financial reports: CONFIRMED"):
        assert line in erp, line
    assert "multiple branches or locations" in erp and "it does not use AI" in erp and "Android or iPhone apps" in erp
    assert "Forgot-password by email (only" not in erp and "reset a forgotten password by email" in erp  # was stale
    assert "Use on a phone: UNKNOWN" in knowledge.EHR_CLOUD_KNOWLEDGE


# --- the pipeline uses it -------------------------------------------------------------------------------------------

def test_first_erp_message_gets_the_approved_reply_brief_and_claudes_adapted_answer(chat, cl):
    cl.classification["product"] = "erp_cloud"
    adapted = ("Hello Amaka! Thank you for your interest in Mega-Net ERP Cloud. Stock going missing in a pharmacy is exactly "
               "what the inventory side is for: it tracks every sale, purchase and return, and low-stock alerts show what to "
               "reorder. The same modules cover your accounting, invoicing and payroll.")
    r = chat.say("Hi, I run a pharmacy in Lagos and my stock keeps getting lost. I am interested in ERP Cloud", turn(adapted))
    assert r == [adapted] and "?" not in r[0]  # the closing question was skipped: they already gave business and problem
    system = cl.calls[0]["system"]
    assert "APPROVED FIRST REPLY" in system and knowledge.first_reply("erp_cloud", "Amaka") in system
    assert "at most ONE question" in system and "150 words" in system


def test_the_second_reply_is_a_normal_short_reply(chat, cl):
    cl.classification["product"] = "erp_cloud"
    chat.say("Hi, ERP Cloud please", turn(knowledge.first_reply("erp_cloud", "Amaka")))
    chat.say("We run a shop", turn("Good to know. Does your shop sell on credit as well?"))
    assert "APPROVED FIRST REPLY" in cl.calls[0]["system"] and "APPROVED FIRST REPLY" not in cl.calls[1]["system"]


def test_unknown_product_is_asked_not_given_a_made_up_pitch(chat, cl):
    cl.classification["product"] = "unsure"
    chat.say("Hello, I want your software", turn("Hello Amaka! Is it EHR Cloud for clinics or ERP Cloud for business accounts?"))
    assert "APPROVED FIRST REPLY" not in cl.calls[0]["system"]


def test_a_reply_with_banned_words_too_long_or_two_questions_is_regenerated(chat, cl):
    cl.classification["product"] = "erp_cloud"
    good = knowledge.first_reply("erp_cloud", "Amaka")
    bad_words = good.replace("brings your day-to-day operations into one place", "is a comprehensive all-in-one system for your entire operations")
    r = chat.say("Hi, ERP Cloud please", turn(bad_words), turn(good))
    assert r == [good] and len(cl.calls) == 2
    chat2 = Chat(chat.app, chat.sent, cl, "2348055555555")
    long_reply = good + " " + "Extra words that go on and on. " * 12
    assert len(long_reply.split()) > 150
    assert chat2.say("Hi, ERP Cloud please", turn(long_reply), turn(good)) == [good]
    chat3 = Chat(chat.app, chat.sent, cl, "2348066666666")
    two_q = good.replace("software to solve?", "software to solve? And how many staff do you have?")
    assert chat3.say("Hi, ERP Cloud please", turn(two_q), turn(good)) == [good]


def test_if_claude_keeps_breaking_the_rules_the_approved_text_itself_is_sent(chat, cl):
    cl.classification["product"] = "ehr_cloud"
    bad = "Hello Amaka! This comprehensive all-in-one system runs your entire operations. Which clinic is it?"
    r = chat.say("Hi, EHR Cloud please", turn(bad), turn(bad))
    assert r == [knowledge.first_reply("ehr_cloud", "Amaka")]
    assert "fixed fallback" in chat.lead.ai_note


def test_tapping_a_product_button_sends_the_approved_first_reply(app, sent, claude):
    post(app, text_value(LEAD, mid="wamid.a", body="hi"))
    inter = text_value(LEAD, mid="wamid.b")
    inter["messages"][0].pop("text")
    inter["messages"][0]["type"] = "interactive"
    inter["messages"][0]["interactive"] = {"type": "button_reply", "button_reply": {"id": "erp_cloud", "title": "ERP Cloud"}}
    post(app, inter)
    assert claude["calls"][-1]["situation"] == "first_product_reply"
    assert "APPROVED FIRST REPLY" in claude["calls"][-1]["context"] and "Mega-Net ERP Cloud" in claude["calls"][-1]["context"]
    assert Lead.query.one().product_interest == "erp_cloud"


def test_banned_phrases_are_in_the_reply_rules_and_voice():
    assert "comprehensive" in ai.REPLY_RULES and "all-in-one" in ai.REPLY_RULES
    assert f"{knowledge.FIRST_REPLY_MAX_WORDS}" in ai.REPLY_RULES
