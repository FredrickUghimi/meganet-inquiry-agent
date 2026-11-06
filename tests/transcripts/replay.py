"""Replay scripted multi-turn conversations through the REAL pipeline (webhook -> classify -> draft -> guards -> send).
Only the Anthropic API is mocked: ScriptedClaude returns structured JSON drafts and classifications, so the real
draft_turn, parsing, guards and demo-signal handling all run."""
import json
import itertools
from datetime import timedelta

import pytest

from sentinel import ai
from sentinel.models import DemoRequest, Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, OWNER, fake_client, make_400, post, text_response, text_value


class ScriptedClaude:
    def __init__(self, monkeypatch):
        self.drafts = []  # queue: a dict (structured draft), a str (raw text) or an Exception to raise
        self.regens = []  # queue for the send-time repeat guard's regeneration
        self.classification = {"intent": "exploring", "product": "ehr_cloud", "name": None, "summary": "s"}
        self.calls = []  # every draft request: {"system", "user"}
        self.regen_calls = []
        self.fail = False
        monkeypatch.setattr(ai, "_get_client", lambda: fake_client(self.create))

    def create(self, **kw):
        system = kw["system"]
        if "ONE JSON object" in system:  # classification
            return text_response(json.dumps(self.classification))
        if "too similar to one you already sent" in system:  # the send-time repeat guard
            self.regen_calls.append(kw)
            item = self.regens.pop(0) if self.regens else RuntimeError("no regeneration scripted")
            if isinstance(item, Exception):
                raise item
            return text_response(item)
        self.calls.append({"system": system, "user": kw["messages"][0]["content"]})
        if self.fail:
            raise make_400()
        item = self.drafts.pop(0)
        if isinstance(item, Exception):
            raise item
        return text_response(item if isinstance(item, str) else json.dumps(item))


class Chat:
    """One customer talking to the bot through the webhook."""

    def __init__(self, app, sent, claude, phone=LEAD):
        self.app, self.sent, self.claude, self.phone = app, sent, claude, phone
        self._ids = itertools.count(1)

    def say(self, text, *drafts):
        self.claude.drafts.extend(drafts)
        before = len(self.sent)
        post(self.app, text_value(self.phone, mid=f"wamid.{self.phone}.{next(self._ids)}", body=text))
        db.session.expire_all()
        return [s[2] for s in self.sent[before:] if s[1] == self.phone]

    def owner_texts(self):
        return [s[2] for s in self.sent if s[1] == OWNER]

    @property
    def lead(self):
        return Lead.query.filter_by(phone=self.phone).one()

    def bot_messages(self):
        return [m.body for m in Message.query.filter_by(lead_id=self.lead.id, direction="outbound").order_by(Message.id)
                if m.status != "failed"]

    def age_bot(self, hours):
        """Make everything the bot said (and the offer timestamp) `hours` older. Customer messages stay fresh so the
        24-hour WhatsApp window is still open."""
        for m in Message.query.filter_by(lead_id=self.lead.id, direction="outbound"):
            m.sent_at -= timedelta(hours=hours)
        lead = self.lead
        if lead.demo_offer_asked_at:
            lead.demo_offer_asked_at -= timedelta(hours=hours)
        db.session.commit()

    def age_everything(self, days):
        for m in Message.query.filter_by(lead_id=self.lead.id):
            m.sent_at -= timedelta(days=days)
        db.session.commit()


def turn(reply, signal="none", **kw):
    """A structured draft as Claude would return it."""
    out = {"reply": reply, "demo_signal": signal}
    out.update(kw)
    return out


@pytest.fixture
def cl(monkeypatch):
    return ScriptedClaude(monkeypatch)


@pytest.fixture
def chat(app, sent, cl):
    return Chat(app, sent, cl)
