"""scripts/eval_replies.py: scenarios, automatic checks, dry-run safety and the PASS/FAIL summary (Claude mocked)."""
import importlib.util
import os

import pytest

from replay import ScriptedClaude, cl, turn  # noqa: F401
from sentinel import ai, business_facts, knowledge, whatsapp
from sentinel.models import Lead, Message

spec = importlib.util.spec_from_file_location(
    "eval_replies", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "eval_replies.py"))
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_at_least_fifteen_scenarios_covering_the_required_cases():
    names = " | ".join(s["name"].lower() for s in ev.SCENARIOS)
    assert len(ev.SCENARIOS) >= 15
    for needle in ("thanks plus price", "topic switch", "vague ok", "pidgin", "two questions", "tuesday", "complaint",
                   "voice note", "human", "repeated question", "long silence"):
        assert needle in names, needle
    for s in ev.SCENARIOS:
        assert s["history"][-1][0] == "in" and s["history"][-1][2] == 0  # the last entry is the customer's latest message


def test_automatic_checks_catch_each_failure():
    sc = dict(expect=[r"Wednesday"], forbid=[r"personalised demonstration"])
    good = ("Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. "
            "Live demos run on Wednesdays at 10:00 AM WAT. Which Wednesday suits you?")
    assert all(ok for _, ok, _ in ev.check_reply(good, sc, ["Something quite different was said before."]))

    def failed(text, prior=()):
        return {name for name, ok, _ in ev.check_reply(text, sc, list(prior)) if not ok}
    assert "no repeated bot text" in failed(good, [good.replace("Which", "Which one")])
    assert "AI disclosure once, never repeated" in failed(good.replace("Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. ", ""))
    assert "AI disclosure once, never repeated" in failed(good, ["Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. Hi."])
    assert "no number, e-mail or website except the official WhatsApp contact" in failed(good + " See www.example.com or call 08012345678.")
    assert "no number, e-mail or website except the official WhatsApp contact" not in failed(
        good + f" Message us on {business_facts.OFFICIAL_WHATSAPP_NUMBER} ({business_facts.OFFICIAL_WHATSAPP_LINK}).")
    assert "no currency amounts" in failed("Wednesday works and it costs ₦500,000.")
    assert "no promises" in failed("Wednesday works, I guarantee it.")
    assert "no promises" in failed("Wednesday works and your demo is confirmed for 10:00.")
    assert any(n.startswith("under") for n in failed("Wednesday " + "word " * 100))
    assert "latest message addressed" in failed("Sure, let me know.")
    assert "latest message addressed" in failed("Wednesday. Would you also like a personalised demonstration with our team?")


def test_evaluate_runs_the_real_pipeline_in_dry_run_and_sends_and_stores_nothing(app, sent, cl, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda payload: pytest.fail("eval must never send"))
    drafts_needed = sum(len(sc.get("turns", [1])) for sc in ev.SCENARIOS)  # a multi-turn scenario drafts once per turn
    cl.drafts.extend([turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick.")] * (drafts_needed * 2))  # room for regenerations
    real_leads = Lead.query.count()
    lines = []
    eval_app = ev.make_eval_app()
    passed, total = ev.evaluate(eval_app, ev.SCENARIOS, out=lines.append)
    text = "\n".join(lines)
    assert total == len(ev.SCENARIOS) and passed < total  # a generic reply cannot satisfy every scenario
    summary = next(l for l in lines if "scenarios passed" in l)
    assert "FAIL: " in summary and lines[-1].startswith("     reply (first 300 characters)")  # FAILED SCENARIOS is last
    assert len(cl.calls) >= drafts_needed  # Claude (mocked) drafted for every customer message, through the real pipeline
    assert sent == [] and Lead.query.count() == real_leads  # nothing sent, nothing stored in the app's database
    idx = [i for i, s in enumerate(ev.SCENARIOS, 1) if s["name"].startswith("thanks plus price")][0]
    assert f"[{idx:02d}] thanks plus price" in text and "You're welcome!" in text
    with eval_app.app_context():  # the throwaway database: dry run wrote no replies either
        assert Message.query.filter_by(sender=None, direction="outbound", channel="whatsapp").filter(
            Message.external_id.is_(None), Message.body.contains("Pricing is tailored")).count() == 0


def test_a_good_reply_passes_and_the_summary_says_pass(app, sent, cl, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda payload: pytest.fail("eval must never send"))
    cl.drafts.append(turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick, and I can "
                          "arrange a demo whenever you like."))
    lines = []
    price = [s for s in ev.SCENARIOS if s["name"].startswith("thanks plus price")]
    passed, total = ev.evaluate(ev.make_eval_app(), price, out=lines.append)
    assert (passed, total) == (1, 1) and lines[-1].strip() == "PASS: 1/1 scenarios passed"


def test_without_an_api_key_it_says_so_and_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ev.main([]) == 0
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
    assert ev.main(["--list"]) == 0 and "thanks plus price" in capsys.readouterr().out


def test_first_reply_scenarios_exist_and_the_approved_reply_passes_every_check():
    firsts = [s for s in ev.SCENARIOS if s.get("first_reply")]
    assert {s["first_reply"] for s in firsts} == {"erp_cloud", "ehr_cloud"} and len(firsts) >= 4
    for sc in [s for s in firsts if s.get("first_strict")]:
        text = knowledge.first_reply(sc["first_reply"], "Amaka").replace(
            knowledge.VIDEO_PLACEHOLDER, business_facts.video_for(sc["first_reply"])[1])
        results = ev.check_reply(text, sc, [])
        assert all(ok for _, ok, _ in results), results
        names = {n for n, _, _ in results}
        assert {"uses the customer's name", "exactly one closing question", "own video link, exactly as stored",
                "no other product's video", "Wednesday live-demo offer", "only confirmed features",
                "approved feature sentence kept"} <= names


def test_first_reply_checks_catch_each_failure():
    sc = [s for s in ev.SCENARIOS if s["name"] == "first ERP reply, bare inquiry"][0]
    own, other = business_facts.video_for("erp_cloud")[1], business_facts.video_for("ehr_cloud")[1]
    good = knowledge.first_reply("erp_cloud", "Amaka").replace(knowledge.VIDEO_PLACEHOLDER, own)

    def failed(text):
        return {n for n, ok, _ in ev.check_reply(text, sc, []) if not ok}
    assert not failed(good)  # the ?si= in the link is not counted as a question
    assert "uses the customer's name" in failed(good.replace("Amaka", "there"))
    assert "exactly one closing question" in failed(good.replace("software to solve?", "software to solve? And your staff size?"))
    assert "exactly one closing question" in failed(good.replace("What type of business do you run, and what's the biggest challenge you want the software to solve?", "Tell me more."))
    assert "own video link, exactly as stored" in failed(good.replace(own, own.split("?")[0]))  # ?si= dropped
    assert "own video link, exactly as stored" in failed(good.replace(own, "https://youtu.be/zBbRNTfcozk?si=changed"))
    assert "own video link, exactly as stored" in failed(good.replace("Watch our product demonstration:\n" + own, ""))
    assert "no other product's video" in failed(good.replace(own, other))
    assert "Wednesday live-demo offer" in failed(good.replace(knowledge.demo_line(), ""))
    assert "only confirmed features" in failed(good.replace("HR and payroll", "Branch management across all your locations"))
    assert "only confirmed features" in failed(good.replace("project costing", "project tracking"))
    assert "only confirmed features" in failed(good.replace("one place", "one comprehensive place"))
    assert "approved feature sentence kept" in failed(good.replace("HR and payroll", "payroll"))
    assert any(n.startswith(f"under {knowledge.FIRST_REPLY_MAX_WORDS}") for n in failed(good + " word" * 40))
    assert ev.check_reply(good, sc, [])[3][0] == f"under {knowledge.FIRST_REPLY_MAX_WORDS} words"  # the first reply gets 165, others stay at 90


def test_first_reply_scenarios_pass_end_to_end_in_dry_run_with_the_link_inserted_by_code(app, sent, cl, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda payload: pytest.fail("eval must never send"))
    strict = [s for s in ev.SCENARIOS if s.get("first_strict") or s.get("demo_first")]
    assert len(strict) == 3
    for sc in strict:  # Claude keeps the placeholder; the pipeline (dry run) inserts the exact link and records nothing
        cl.drafts.append(turn(knowledge.first_reply(sc["first_reply"], "Amaka")))
    lines = []
    passed, total = ev.evaluate(ev.make_eval_app(), strict, out=lines.append)
    assert (passed, total) == (3, 3), "\n".join(lines)
    text = "\n".join(lines)
    assert business_facts.video_for("ehr_cloud")[1] in text and business_facts.video_for("erp_cloud")[1] in text
    assert "{video_url}" not in text and sent == []


# --- the demo-request-first scenario, and the speed report --------------------------------------------------------------

EHR_LINK = business_facts.video_for("ehr_cloud")[1]
DEMO_FIRST = next(s for s in ev.SCENARIOS if s.get("demo_first"))
SHORT_DEMO_REPLY = (f"Hello Amaka! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. Happy to show you Mega-Net EHR Cloud, our "
                    f"cloud system for clinics and hospitals. Watch our product demonstration:\n{EHR_LINK}\n\n"
                    f"We also hold personalised demos on Wednesdays at 10:00 AM (WAT). Is yours a clinic or a hospital?")


def failed_for(sc, reply):
    return {n for n, ok, _ in ev.check_reply(reply, sc, []) if not ok}


def test_a_correct_short_reply_to_a_demo_request_passes_without_the_approved_sentences():
    assert DEMO_FIRST["name"] == "first EHR reply, demo request as the first message"
    assert "It combines" not in SHORT_DEMO_REPLY and knowledge.demo_line() not in SHORT_DEMO_REPLY
    assert failed_for(DEMO_FIRST, SHORT_DEMO_REPLY) == set()


def test_the_demo_first_checks_still_catch_a_wrong_link_a_missing_time_two_questions_or_length():
    assert "own video link, exactly as stored" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY.replace(EHR_LINK, EHR_LINK.split("?")[0]))
    assert "Wednesday 10:00 demo offer" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY.replace("Wednesdays at 10:00 AM (WAT)", "Wednesdays"))
    assert "Wednesday 10:00 demo offer" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY.replace("Wednesdays", "Fridays"))
    assert "exactly one closing question" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY + " And how many staff?")
    assert "exactly one closing question" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY.replace("Is yours a clinic or a hospital?", ""))
    assert any(n.startswith("under") for n in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY + " word" * 200))
    assert "only confirmed features" in failed_for(DEMO_FIRST, SHORT_DEMO_REPLY.replace("clinics", "clinics with branch management"))


def test_the_strict_checks_stay_for_generic_interest_and_menu_taps():
    strict = next(s for s in ev.SCENARIOS if s.get("first_strict") and s["first_reply"] == "ehr_cloud")
    assert "approved feature sentence kept" in failed_for(strict, SHORT_DEMO_REPLY)
    assert "Wednesday live-demo offer" in failed_for(strict, SHORT_DEMO_REPLY)


def test_the_demo_first_scenario_runs_end_to_end_with_a_short_reply(app, sent, cl):
    cl.drafts.append(turn(SHORT_DEMO_REPLY.replace(EHR_LINK, "{video_url}")))
    lines = []
    assert ev.evaluate(ev.make_eval_app(), [DEMO_FIRST], out=lines.append)[0] == 1, "\n".join(lines)


def test_every_scenario_prints_seconds_and_tokens_and_the_run_ends_with_totals_and_a_hit_rate(app, sent, cl):
    cl.drafts.append(turn(SHORT_DEMO_REPLY.replace(EHR_LINK, "{video_url}")))
    lines = []
    ev.evaluate(ev.make_eval_app(), [DEMO_FIRST], out=lines.append)
    text = "\n".join(lines)
    assert "speed:" in text and "classify in/out/cache-read" in text and "draft in/out/cache-read" in text
    assert "max_tokens hits 0" in text and "SPEED:" in text and "average" in text and "cache hit rate" in text
    assert "max_tokens retries 0" in text


def test_summarise_counts_cache_hits_and_max_tokens_retries():
    base = dict(seconds=4.0, classify_in=100, classify_out=50, classify_read=900, classify_write=0, draft_in=200, draft_out=120,
                draft_read=1800, draft_write=0, draft_calls=1, max_tokens_hits=0, reply_chars=300, hidden_out_est=20)
    slow = dict(base, seconds=10.0, max_tokens_hits=1, draft_out=1500, hidden_out_est=1400)
    t = ev.summarise([base, slow])
    assert t["scenarios"] == 2 and t["total_seconds"] == 14.0 and t["avg_seconds"] == 7.0 and t["max_seconds"] == 10.0
    assert t["max_tokens_retries"] == 1 and t["hidden_out_est"] == 1420
    assert t["cache_read_tokens"] == 5400 and t["input_tokens"] == 600
    assert abs(t["cache_hit_rate"] - 5400 / 6000) < 1e-9
    assert ev.summarise([]) == {"scenarios": 0}
