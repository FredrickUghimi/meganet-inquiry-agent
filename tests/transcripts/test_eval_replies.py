"""scripts/eval_replies.py: scenarios, automatic checks, dry-run safety and the PASS/FAIL summary (Claude mocked)."""
import importlib.util
import os

import pytest

from replay import ScriptedClaude, cl, turn  # noqa: F401
from sentinel import ai, whatsapp
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
    assert "[01] thanks plus price" in text and "bot:      You're welcome!" in text
    with eval_app.app_context():  # the throwaway database: dry run wrote no replies either
        assert Message.query.filter_by(sender=None, direction="outbound", channel="whatsapp").filter(
            Message.external_id.is_(None), Message.body.contains("Pricing is tailored")).count() == 0


def test_a_good_reply_passes_and_the_summary_says_pass(app, sent, cl, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda payload: pytest.fail("eval must never send"))
    cl.drafts.append(turn("You're welcome! Pricing is tailored to your needs and shared personally by Fredrick, and I can "
                          "arrange a demo whenever you like."))
    lines = []
    passed, total = ev.evaluate(ev.make_eval_app(), ev.SCENARIOS[:1], out=lines.append)
    assert (passed, total) == (1, 1) and lines[-1].strip() == "PASS: 1/1 scenarios passed"


def test_without_an_api_key_it_says_so_and_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ev.main([]) == 0
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
    assert ev.main(["--list"]) == 0 and "thanks plus price" in capsys.readouterr().out
