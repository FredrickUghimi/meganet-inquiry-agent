"""scripts/eval_replies.py: scenarios, automatic checks, dry-run safety and the PASS/FAIL summary (Claude mocked)."""
import importlib.util
import os

import pytest

from replay import ScriptedClaude, cl, turn  # noqa: F401
from sentinel import ai, knowledge, whatsapp
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
    good = "Live demos run on Wednesdays at 10:00 AM WAT. Which Wednesday suits you?"
    assert all(ok for _, ok, _ in ev.check_reply(good, sc, ["Something quite different was said before."]))

    def failed(text, prior=()):
        return {name for name, ok, _ in ev.check_reply(text, sc, list(prior)) if not ok}
    assert "no repeated bot text" in failed(good, [good.replace("Which", "Which one")])
    assert "no currency amounts" in failed("Wednesday works and it costs ₦500,000.")
    assert "no promises" in failed("Wednesday works, I guarantee it.")
    assert "no promises" in failed("Wednesday works and your demo is confirmed for 10:00.")
    assert any(n.startswith("under") for n in failed("Wednesday " + "word " * 100))
    assert "latest message addressed" in failed("Sure, let me know.")
    assert "latest message addressed" in failed("Wednesday. Would you also like a personalised demonstration with our team?")


def test_evaluate_runs_the_real_pipeline_in_dry_run_and_sends_and_stores_nothing(app, sent, cl, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda payload: pytest.fail("eval must never send"))
    cl.drafts.extend([turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick.")] * len(ev.SCENARIOS))
    real_leads = Lead.query.count()
    lines = []
    eval_app = ev.make_eval_app()
    passed, total = ev.evaluate(eval_app, ev.SCENARIOS, out=lines.append)
    text = "\n".join(lines)
    assert total == len(ev.SCENARIOS) and passed < total  # a generic reply cannot satisfy every scenario
    assert "FAIL: " in lines[-1] and "scenarios passed" in lines[-1]
    assert len(cl.calls) == total  # Claude (mocked) was called once per scenario, for real, through the pipeline
    assert sent == [] and Lead.query.count() == real_leads  # nothing sent, nothing stored in the app's database
    assert "[04] thanks plus price" in text and "bot:      You're welcome!" in text
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
    assert {s["first_reply"] for s in firsts} == {"erp_cloud", "ehr_cloud"} and len(firsts) >= 3
    for sc in [s for s in firsts if "bare" in s["name"]]:
        results = ev.check_reply(knowledge.first_reply(sc["first_reply"], "Amaka"), sc, [])
        assert all(ok for _, ok, _ in results), results
        assert {"uses the customer's name", "at most one question", "only confirmed features"} <= {n for n, _, _ in results}


def test_first_reply_checks_catch_each_failure():
    sc = [s for s in ev.SCENARIOS if s["name"] == "first ERP reply, bare inquiry"][0]
    good = knowledge.first_reply("erp_cloud", "Amaka")

    def failed(text):
        return {n for n, ok, _ in ev.check_reply(text, sc, []) if not ok}
    assert not failed(good)
    assert "uses the customer's name" in failed(good.replace("Amaka", "there"))
    assert "at most one question" in failed(good.replace("software to solve?", "software to solve? And your staff size?"))
    assert "only confirmed features" in failed(good.replace("HR and payroll", "Branch management across all your locations"))
    assert "only confirmed features" in failed(good.replace("HR and payroll", "AI that runs your business"))
    assert "only confirmed features" in failed(good.replace("one place", "one comprehensive place"))
    assert any(n.startswith("under 150") for n in failed(good + " word" * 20))
    assert ev.check_reply(good, sc, [])[3][0] == "under 150 words"  # the first reply gets 150, others stay at 90
