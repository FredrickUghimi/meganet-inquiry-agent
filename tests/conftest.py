import itertools

import pytest

from sentinel import ai, create_app, whatsapp
from sentinel.models import db


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound WhatsApp traffic instead of calling Meta."""
    log = []
    ids = itertools.count(1)
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: log.append(("text", to, body)) or f"wamid.out{next(ids)}")
    monkeypatch.setattr(whatsapp, "send_template",
                        lambda to, name, lang, params: log.append(("template", to, name, list(params))) or f"wamid.out{next(ids)}")
    return log


@pytest.fixture
def claude(monkeypatch):
    """Scripted Claude: set claude['intent'] per test."""
    state = {"intent": "exploring", "product": "ehr_cloud", "name": None, "summary": "Clinic, 12 staff."}
    monkeypatch.setattr(ai, "classify", lambda lead, msgs: dict(state))
    monkeypatch.setattr(ai, "draft_reply", lambda lead, msgs, situation: f"reply:{situation}")
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
