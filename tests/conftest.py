import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "transcripts"))

from sentinel import ai, create_app, whatsapp
from sentinel.models import db


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound WhatsApp traffic instead of calling Meta."""
    log = []
    ids = itertools.count(1)
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: log.append(("text", to, body)) or f"wamid.out{next(ids)}")
    monkeypatch.setattr(whatsapp, "send_buttons",
                        lambda to, body, buttons: log.append(("buttons", to, body, [i for i, _ in buttons])) or f"wamid.out{next(ids)}")
    monkeypatch.setattr(whatsapp, "send_template",
                        lambda to, name, lang, params: log.append(("template", to, name, list(params))) or f"wamid.out{next(ids)}")
    return log


@pytest.fixture
def claude(monkeypatch):
    """Scripted Claude: set claude['intent'] per test."""
    state = {"intent": "exploring", "product": "ehr_cloud", "name": None, "summary": "Clinic, 12 staff."}
    monkeypatch.setattr(ai, "classify", lambda lead, msgs: dict(state))
    monkeypatch.setattr(ai, "draft_reply", lambda lead, msgs, situation: f"reply:{situation}")
    state["script"] = []  # structured turns, consumed one per drafted reply: dicts like {"reply": ..., "demo_signal": ...}
    state["calls"] = []  # every draft_turn call: {"situation", "context", "structured"}
    fixed_purpose = ("holding", "support_route", "other_choice")  # default mock: these use their fixed fallback text

    def default_turn(lead, msgs, situation, fallback_text):
        if situation in fixed_purpose:
            return dict(ai.blank_turn(fallback_text or ai.FALLBACK_REPLIES[situation]), fallback=True,
                        error="mock: fixed-purpose draft")
        return dict(ai.blank_turn(ai.draft_reply(lead, msgs, situation)), fallback=False, error=None)

    def scripted_turn(lead, msgs, situation, context="", fallback_text=None, structured=True):
        state["calls"].append({"situation": situation, "context": context, "structured": structured,
                               "lead_id": getattr(lead, "id", None)})
        if state["script"]:
            item = dict(state["script"].pop(0))
            base = ai.blank_turn(item.pop("reply", "ok"))
            fields = dict(base["fields"], **item.pop("fields", {}))
            return dict(base, **item, fields=fields, fallback=False, error=None)
        return default_turn(lead, msgs, situation, fallback_text)

    monkeypatch.setattr(ai, "draft_turn", scripted_turn)
    regen = {"n": 0}

    def fake_regen(lead, msgs, original):
        regen["n"] += 1
        return f"regenerated reply {regen['n']} about your latest message"

    monkeypatch.setattr(ai, "regenerate_message", fake_regen)
    monkeypatch.setattr(ai, "draft_followup_line", lambda lead, msgs, stage, prev: f"line{stage}")
    return state


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}",
        "SCHEDULER_ENABLED": False, "TESTING": True, "SESSION_COOKIE_SECURE": False,
        "DASHBOARD_PASSWORD": "pw", "WHATSAPP_VERIFY_TOKEN": "verify", "WHATSAPP_APP_SECRET": "shh",
        "WHATSAPP_TOKEN": "tok", "WHATSAPP_PHONE_NUMBER_ID": "123",
        "OWNER_WHATSAPP_NUMBER": "2340000000", "PUBLIC_BASE_URL": "https://sentinel.test",
    })
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()
