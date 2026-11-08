"""Follow-ups after the real eval run: the FAILED SCENARIOS summary and --only / --out, eval checks that ignore the one-time
disclosure line, no unrequested demo video, the retired-report wording, and the partnership terms (20% of the initial total
cost, once, no recurring payments; everything undecided goes to the team). Only the Anthropic API is mocked."""
import importlib.util
import os
import re

import pytest

from replay import Chat, chat, cl, turn  # noqa: F401  (fixtures)
from sentinel import ai, business_facts as bf, knowledge_base, service
from sentinel.models import Lead, OpenQuestion, db
from test_knowledge_base import system_of

spec = importlib.util.spec_from_file_location(
    "eval_replies", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "eval_replies.py"))
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

INTRO = "Hello Amaka! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant."
EHR_LINK = bf.video_for("ehr_cloud")[1]
ERP_LINK = bf.video_for("erp_cloud")[1]
PROGRAMME = "Mega-Net Referral Partnership Programme"
PARTNER_REPLY = ("Yes, we have the Mega-Net Referral Partnership Programme. A partner introduces potential customers, and when a "
                 "customer comes through your referral and makes the initial payment you earn 20% of the initial total cost, paid "
                 "once, with no recurring payments. Our team handles the demo, proposal and closing. Fredrick or a member of our "
                 "team confirms the remaining terms personally. Could you tell me briefly who you are and which organisations you "
                 "could introduce?")


def scenario(name):
    return next(s for s in ev.SCENARIOS if s["name"] == name)


def number(name):
    return next(n for n, s in enumerate(ev.SCENARIOS, 1) if s["name"] == name)


def failed_names(sc, reply, prior=()):
    return {n for n, ok, _ in ev.check_reply(reply, sc, list(prior)) if not ok}


def failed_on_content(sc, reply, prior=()):
    """The same, without the disclosure check (a reply written without the opener fails that one by design)."""
    return failed_names(sc, reply, prior) - {"AI disclosure once, never repeated"}


# --- 1. FAILED SCENARIOS, --only, --out ---------------------------------------------------------------------------------------

def run_eval(app, scenarios, numbers=None):
    raw = []
    ev.evaluate(ev.make_eval_app(), scenarios, out=raw.append, numbers=numbers)
    return [line for chunk in raw for line in chunk.split("\n")]  # one entry per printed line


def test_the_failed_scenarios_section_comes_last_with_number_title_failed_checks_and_300_characters(app, sent, cl):
    scs = [scenario("BizLens"), scenario("thanks plus price (the real transcript)")]
    nums = [number("BizLens"), number("thanks plus price (the real transcript)")]
    long_wrong = "I am happy to help with that. " + "Please tell me more about your clinic and what you need. " * 12  # > 300 chars
    cl.drafts.extend([turn(long_wrong), turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick.")])
    lines = run_eval(app, scs, nums)
    text = "\n".join(lines)
    assert lines.index("FAILED SCENARIOS") > max(i for i, l in enumerate(lines) if l.startswith("SPEED:"))
    assert any(l.startswith("FAIL: 1/2 scenarios passed") for l in lines)
    section = lines[lines.index("FAILED SCENARIOS"):]
    assert section[1] == f"[{nums[0]:02d}] BizLens"
    assert section[2].startswith("     failed checks: ") and "latest message addressed" in section[2]
    assert section[3].startswith("     reply (first 300 characters): ")
    shown = section[3].split(": ", 1)[1]
    assert shown == ("Hello Amaka! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. " + long_wrong)[:300] and len(shown) == 300
    assert len(section) == 4, section  # the passing scenario is not listed
    assert "SPEED:" in text and "bot:      " in text  # the existing output is still there


def test_a_scenario_with_no_reply_is_listed_too(app, sent, cl, monkeypatch):
    def no_reply(sc, n):
        no_reply.stats = ev.collect_stats(0.1, None)
        return None, "skipped", "WhatsApp not configured"
    monkeypatch.setattr(ev, "run_scenario", no_reply)
    lines = run_eval(app, [scenario("BizLens")], [28])
    assert "FAILED SCENARIOS" in lines and "[28] BizLens" in lines
    assert "     failed checks: no reply drafted" in lines
    assert "     reply (first 300 characters): (skipped: WhatsApp not configured)" in lines


def test_no_failed_section_when_everything_passes(app, sent, cl):
    cl.drafts.append(turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick, and I can "
                          "arrange a demo whenever you like."))
    lines = run_eval(app, [scenario("thanks plus price (the real transcript)")])
    assert "FAILED SCENARIOS" not in lines and lines[-1].startswith("PASS: 1/1")


def test_only_accepts_numbers_ranges_lists_and_names_and_keeps_the_original_numbers():
    assert [n for n, _ in ev.select_scenarios("28")] == [28]
    assert [n for n, _ in ev.select_scenarios("28,31")] == [28, 31]
    assert [n for n, _ in ev.select_scenarios("28-30")] == [28, 29, 30]
    assert [n for n, _ in ev.select_scenarios(" 3 , 5-6,28 ")] == [3, 5, 6, 28]
    assert [n for n, _ in ev.select_scenarios("")] == list(range(1, len(ev.SCENARIOS) + 1))
    named = ev.select_scenarios("price")
    assert named and all("price" in sc["name"].lower() for _, sc in named) and named[0][0] == 5  # numbers of the full list
    assert ev.SCENARIOS[27]["name"] == "BizLens" and ev.SCENARIOS[28]["name"] == "Daily Sales Report retired"
    with pytest.raises(ValueError, match="no scenario number"):
        ev.select_scenarios("999")
    assert ev.select_scenarios("no such scenario name") == []


def test_main_runs_only_the_chosen_scenarios_prints_their_original_numbers_and_writes_the_out_file(app, sent, cl, monkeypatch,
                                                                                                   capsys, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cl.drafts.append(turn("No, we don't sell BizLens separately. It's now called Business Sentinel, a feature inside Mega-Net "
                          "ERP Cloud. Are you looking at ERP Cloud for a business or EHR Cloud for a clinic or hospital?"))
    target = tmp_path / "run.txt"
    code = ev.main(["--only", "28", "--out", str(target)])
    printed = capsys.readouterr().out
    written = target.read_text(encoding="utf-8")
    assert code == 0 and "[28] BizLens" in printed and "[01]" not in printed and "1/1 scenarios passed" in printed
    assert written.strip() == printed.strip()  # the file has the full output, the console too
    assert "SPEED:" in written and "bot:      " in written


def test_main_rejects_an_unknown_number_and_lists_with_original_numbers(capsys):
    assert ev.main(["--only", "999"]) == 2
    assert "no scenario number" in capsys.readouterr().out
    assert ev.main(["--only", "28,29", "--list"]) == 0
    listed = capsys.readouterr().out.strip().splitlines()
    assert listed[0].startswith("28 BizLens") and listed[1].startswith("29 Daily Sales Report retired")


# --- 2. eval checks and the disclosure line -----------------------------------------------------------------------------------

BIZLENS_ANSWER = ("No, we don't sell BizLens separately. It's now called Business Sentinel and it's a feature inside Mega-Net "
                  "ERP Cloud that flags unusual items. Are you looking at ERP Cloud for a business or EHR Cloud for a clinic or "
                  "hospital?")


def test_the_bizlens_scenario_passes_with_the_disclosure_line_as_its_own_paragraph():
    sc = scenario("BizLens")
    assert failed_names(sc, f"{INTRO}\n\n{BIZLENS_ANSWER}") == set()
    assert failed_on_content(sc, BIZLENS_ANSWER) == set()  # and without it: the same verdict on the answer
    assert failed_names(sc, BIZLENS_ANSWER) == {"AI disclosure once, never repeated"}
    assert failed_names(sc, f"{INTRO} {BIZLENS_ANSWER}") == set()


def test_the_cause_of_the_bizlens_failure_was_the_pattern_not_the_opener():
    """The old pattern wanted 'not/n't sold separately'. 'we don't sell BizLens separately' says the same thing and
    did not match, however the opener was laid out."""
    old = r"(?:not|n't) sold separately"
    assert re.search(old, BIZLENS_ANSWER, re.I | re.S) is None
    assert re.search(old, f"{INTRO}\n\n{BIZLENS_ANSWER}", re.I | re.S) is None
    assert any(re.search(p, BIZLENS_ANSWER, re.I | re.S) for p in scenario("BizLens")["expect"] if "separately" in p)


def test_the_bizlens_scenario_still_fails_a_reply_that_does_not_answer():
    sc = scenario("BizLens")
    assert "latest message addressed" in failed_names(sc, f"{INTRO}\n\nThanks for getting in touch. How can I help you today?")
    assert "latest message addressed" in failed_names(sc, INTRO)  # the opener alone is not an answer
    assert "latest message addressed" in failed_names(sc, f"{INTRO}\n\nBizLens is sold on its own. Is it for a business?")


def test_the_word_cap_ignores_the_opener_and_links_but_not_real_length():
    sc = dict(expect=[r"."])
    answer88 = " ".join(["word"] * 88)
    assert failed_names(sc, f"{INTRO}\n\n{answer88}") == set()  # 88 + 11 opener words > 90 before this fix
    assert failed_names(sc, f"{INTRO}\n\n{answer88} {EHR_LINK}") == set()  # a link is not a word
    assert any(n.startswith("under 90") for n in failed_names(sc, f"{INTRO}\n\n" + " ".join(["word"] * 95)))
    assert any(n.startswith("under 90") for n in failed_names(sc, " ".join(["word"] * 95)))


def test_expected_and_forbidden_patterns_read_the_answer_after_the_opener():
    sc = dict(expect=[r"Lead Sentinel"], forbid=[])
    assert "latest message addressed" in failed_names(sc, f"{INTRO}\n\nSure.")  # the opener cannot satisfy an expectation
    sc2 = dict(expect=[r"."], forbid=[r"AI assistant"])
    assert failed_names(sc2, f"{INTRO}\n\nHappy to help.") == set()  # and cannot trip a forbidden pattern
    assert "latest message addressed" in failed_names(sc2, f"{INTRO}\n\nI am an AI assistant, as I said.")


def test_checks_that_catch_real_problems_still_do():
    sc = dict(expect=[r"."])
    assert "no currency amounts" in failed_names(sc, f"{INTRO}\n\nIt costs ₦500,000.")
    assert "no promises" in failed_names(sc, f"{INTRO}\n\nI guarantee it will work.")
    assert "no promises" in failed_names(sc, f"{INTRO}\n\nYour demo is confirmed for Wednesday.")
    assert "no repeated bot text" in failed_names(sc, f"{INTRO}\n\nPricing is shared by Fredrick.", ["Pricing is shared by Fredrick."])
    assert "AI disclosure once, never repeated" in failed_names(sc, "Pricing is shared by Fredrick.")
    assert "AI disclosure once, never repeated" in failed_names(sc, f"{INTRO}\n\nHi.", [f"{INTRO} Earlier."])
    assert "no number, e-mail or website except the official WhatsApp contact" in failed_names(sc, f"{INTRO}\n\nCall 08031234567.")


def test_other_scenario_patterns_no_longer_fail_good_replies():
    custom = scenario("custom software")
    ok = f"{INTRO}\n\nFeel free to share details. Our technical team will assess it first. What should the app do?"
    assert failed_names(custom, ok) == set()  # 'free' in 'feel free' used to trip the forbidden pattern
    assert "latest message addressed" in failed_names(custom, f"{INTRO}\n\nWe will build it for free. The technical team agrees.")
    assert "latest message addressed" in failed_names(custom, f"{INTRO}\n\nThe technical team will assess it; it takes 6 weeks.")
    mind = scenario("changes their mind")
    assert failed_names(mind, f"{INTRO} Okay, no worries. Message us whenever you are ready.") == set()
    prog = scenario("partnership programme")
    good = (f"{INTRO}\n\n{PARTNER_REPLY}")
    assert failed_names(prog, good) == set()
    assert failed_names(prog, good.replace("introduce?", "introduce. We'll reply personally.")) == {"latest message addressed"}  # needs a question


def test_the_user_example_partnership_reply_is_within_the_normal_word_cap():
    words = len(ai.strip_intro(f"{INTRO}\n\n{PARTNER_REPLY}").split())
    assert 60 <= words <= ev.MAX_WORDS, words


# --- 3. no unrequested video ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "I used the Daily Sales Report before. Can I still get it?",
    "What does ERP Cloud cost?", "Thanks", "Where are you based?", "Do you do payroll?",
])
def test_claude_asking_for_a_video_does_not_attach_one_when_the_customer_did_not_ask(chat, cl, caplog, text):
    import logging
    caplog.set_level(logging.INFO)
    cl.classification.update(product="erp_cloud", topic="general")
    r = chat.say(text, turn("Mega-Net ERP Cloud is our current business management product.", "wants_demo", share_video="erp_cloud"))
    assert "youtu" not in r[0] and ERP_LINK not in r[0]
    assert any("video not attached" in rec.getMessage() for rec in caplog.records)


def test_the_retired_report_question_gets_no_video_and_the_new_wording(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    r = chat.say("I used the Daily Sales Report before. Can I still get it?",
                 turn("The Daily Sales Report is no longer offered. Mega-Net ERP Cloud is our current business management "
                      "product. Is it for a business?", "wants_demo", share_video="both"))
    assert "no longer offered" in r[0] and "ERP Cloud" in r[0] and "http" not in r[0]
    assert "won't be able to get it" not in r[0]


@pytest.mark.parametrize("text", [
    "Can I get a demo of EHR Cloud?", "How does it work?", "Please show me the product", "Abeg make I see demo",
    "Can you walk me through it?", "Do you have a video?", "I want to watch how it looks"])
def test_a_video_is_attached_when_the_customer_asks_for_a_demo_how_it_works_or_to_see_the_product(chat, cl, text):
    cl.classification.update(product="ehr_cloud", topic="general")
    r = chat.say(text, turn("Here is the demonstration video for EHR Cloud.", "wants_demo", share_video="ehr_cloud"))
    assert EHR_LINK in r[0]


def test_wants_demo_alone_with_a_real_request_still_defaults_to_the_leads_product(chat, cl):
    cl.classification.update(product="erp_cloud", topic="general")
    r = chat.say("Can I see a demo please?", turn("Happy to show you.", "wants_demo"))
    assert ERP_LINK in r[0]


def test_the_approved_first_product_reply_still_carries_its_video(chat, cl):
    from sentinel import knowledge
    cl.classification["product"] = "ehr_cloud"
    r = chat.say("Hi, EHR Cloud please", turn(knowledge.first_reply("ehr_cloud", "Amaka")))
    assert EHR_LINK in r[0]


def test_a_follow_up_in_a_demo_conversation_may_repeat_the_video_after_a_day(chat, cl):
    cl.classification.update(product="ehr_cloud", topic="general")
    chat.say("Can I get a demo of EHR Cloud?", turn("Here you go.", "wants_demo", share_video="ehr_cloud"))
    chat.age_bot(25)
    r = chat.say("Sorry, one more time please?", turn("Of course.", "wants_demo", share_video="ehr_cloud"))
    assert EHR_LINK in r[0]  # the earlier request for the demo is the context of "one more time"


# --- 4. the retired-report wording ----------------------------------------------------------------------------------------------

def test_the_knowledge_text_says_no_longer_offered_then_points_to_erp_cloud(app):
    core = " ".join(knowledge_base.CORE.split())
    assert ("The Daily Sales Report is no longer offered; say that, then point to Mega-Net ERP Cloud as the current business "
            "management product.") in core
    assert "won't be able to get it" not in core.replace("Do not say the customer won't be able to get it", "")
    for topic in (None, "erp", "academy"):
        system = " ".join(ai.system_text(ai._system(Lead(product_interest="unsure"), "x", topic)).split())
        assert "no longer offered; say that, then point to Mega-Net ERP Cloud" in system


def test_a_reply_telling_the_customer_they_cannot_get_the_report_is_regenerated(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    r = chat.say("Can I still get the Daily Sales Report?",
                 turn("It is no longer offered, so you won't be able to get it. Try ERP Cloud."),
                 turn("The Daily Sales Report is no longer offered. Mega-Net ERP Cloud is our current business management product."))
    assert len(cl.calls) == 2 and "won't be able" not in r[0] and "no longer offered" in r[0]
    assert "Do not tell the customer they cannot get it" in system_of(cl.calls[1])


def test_the_wording_guard_only_applies_to_the_retired_report(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    r = chat.say("Can I get a refund?", turn("Fredrick or a member of the team will look into it; I can't promise a refund."))
    assert len(cl.calls) == 1 and "can't promise" in r[0]
