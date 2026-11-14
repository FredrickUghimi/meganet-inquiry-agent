"""Chat-style lead page: composer below the thread, Waiting labels, quoted replies."""
from datetime import timedelta

from sentinel import service, whatsapp
from sentinel.models import Lead, Message, db, utcnow
from test_webhook_inbound import LEAD, post, text_value


REAL_SEND_TEXT = whatsapp.send_text  # captured before the `sent` fixture replaces it


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body



def _csrf_rejected(resp):
    """CSRF failures redirect to the login page (never a raw 400) and do nothing."""
    return resp.status_code == 302 and resp.headers["Location"].endswith("/login")

def capture_graph(monkeypatch, reject_context=False):
    calls = []
    monkeypatch.setattr(whatsapp, "send_text", REAL_SEND_TEXT)  # exercise the real request building

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if reject_context and "context" in json:
            return FakeResp(400, {"error": {"message": "Message to reply to not found", "code": 131009}})
        return FakeResp(200, {"messages": [{"id": f"wamid.OUT{len(calls)}"}]})

    monkeypatch.setattr(whatsapp.requests, "post", fake_post)
    return calls


def ready_lead_with_messages(app, sent, claude):
    claude["intent"] = "exploring"
    post(app, text_value(LEAD, mid="wamid.IN1", body="First question about EHR"))
    post(app, text_value(LEAD, mid="wamid.IN2", body="Second message " + "z" * 120))
    db.session.expire_all()
    lead = Lead.query.one()
    lead.followup_paused = True  # human-handled: only Fredrick can answer
    db.session.commit()
    sent.clear()
    return lead


def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"], sess["csrf"] = True, "t"
    return c


def test_composer_is_below_the_thread_oldest_to_newest(app, sent, claude):
    lead = ready_lead_with_messages(app, sent, claude)
    page = client(app).get(f"/leads/{lead.id}").data.decode()
    first, second = page.index("First question about EHR"), page.index("Second message")
    reply_bot = page.index("reply:first_product_reply")
    composer = page.index('id="composer"')
    assert page.index("Conversation") < first < second < composer
    assert first < reply_bot < composer or reply_bot < composer
    assert page.index('id="newest"') < composer
    # the window line lives with the composer, after it starts
    assert page.index("Reply window open until") > composer
    assert "Reply to " not in page[:page.index("Conversation")]  # no composer above the thread
    assert "position:sticky" in page and "scrollIntoView" in page


def test_unanswered_inbound_messages_are_labelled_waiting(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    c = client(app)
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert page.count(">Waiting<") == 2
    capture_graph(monkeypatch)
    for m in Message.query.filter_by(direction="inbound"):
        m.sent_at = utcnow() - timedelta(minutes=5)
    db.session.commit()
    c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "On it"})
    assert ">Waiting<" not in c.get(f"/leads/{lead.id}").data.decode()


def test_reply_link_sets_quote_banner_with_cancel(app, sent, claude):
    lead = ready_lead_with_messages(app, sent, claude)
    c = client(app)
    msg = Message.query.filter_by(external_id="wamid.IN2").one()
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert f"quote={msg.id}#composer" in page
    quoted = c.get(f"/leads/{lead.id}?quote={msg.id}").data.decode()
    assert "Replying to: Second message " in quoted and "..." in quoted
    assert f'name="quote" value="{msg.id}"' in quoted and ">Cancel<" in quoted
    plain = c.get(f"/leads/{lead.id}").data.decode()
    assert "Replying to:" not in plain


def test_quoted_send_puts_context_in_the_request_and_stores_wamid(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    calls = capture_graph(monkeypatch)
    msg = Message.query.filter_by(external_id="wamid.IN2").one()
    r = client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "Answering this", "quote": msg.id},
                         follow_redirects=True)
    assert b"Reply sent" in r.data and b"without the quote" not in r.data
    assert len(calls) == 1
    assert calls[0]["context"] == {"message_id": "wamid.IN2"}
    assert calls[0]["text"]["body"] == "Answering this" and calls[0]["to"] == LEAD
    out = Message.query.filter_by(sender="owner", direction="outbound").one()
    assert out.external_id == "wamid.OUT1"
    assert Lead.query.one().followup_paused


def test_send_without_quote_has_no_context(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    calls = capture_graph(monkeypatch)
    client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "Plain"})
    assert "context" not in calls[0]


def test_inbound_without_wamid_sends_plain_with_note(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    form = service.record_inbound(lead, "Emailed question", "form")  # no WhatsApp id
    calls = capture_graph(monkeypatch)
    assert form.external_id is None
    r = client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "Hi", "quote": form.id},
                         follow_redirects=True)
    assert b"Reply sent" in r.data and b"has no WhatsApp id, so it was sent without the quote" in r.data
    assert len(calls) == 1 and "context" not in calls[0]


def test_meta_rejecting_the_context_falls_back_to_plain_send_with_note(app, sent, claude, monkeypatch, caplog):
    lead = ready_lead_with_messages(app, sent, claude)
    calls = capture_graph(monkeypatch, reject_context=True)
    msg = Message.query.filter_by(external_id="wamid.IN1").one()
    r = client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "SECRETTEXT", "quote": msg.id},
                         follow_redirects=True)
    assert b"Reply sent" in r.data and b"WhatsApp rejected the quote, so it was sent without it" in r.data
    assert len(calls) == 2 and "context" in calls[0] and "context" not in calls[1]
    out = Message.query.filter_by(sender="owner", direction="outbound").all()
    assert len(out) == 1 and out[0].status == "sent" and out[0].external_id == "wamid.OUT2"
    assert "SECRETTEXT" not in caplog.text


def test_quote_from_another_lead_is_refused(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    other = service.find_or_create_lead(phone="2348077777777", name="Other")
    foreign = service.record_inbound(other, "hello", "whatsapp", external_id="wamid.FOREIGN")
    calls = capture_graph(monkeypatch)
    r = client(app).post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "Hi", "quote": foreign.id},
                         follow_redirects=True)
    assert b"does not belong to this lead" in r.data and calls == []


def test_csrf_limit_and_window_still_enforced(app, sent, claude, monkeypatch):
    lead = ready_lead_with_messages(app, sent, claude)
    calls = capture_graph(monkeypatch)
    c = client(app)
    assert _csrf_rejected(c.post(f"/leads/{lead.id}/reply", data={"text": "x"}))
    assert b"too long" in c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "x" * 1001},
                                 follow_redirects=True).data
    for m in Message.query.filter_by(direction="inbound"):
        m.sent_at = utcnow() - timedelta(hours=30)
    db.session.commit()
    page = c.get(f"/leads/{lead.id}").data.decode()
    assert "Window closed. Message the lead from your own number." in page
    assert b"Window closed" in c.post(f"/leads/{lead.id}/reply", data={"csrf": "t", "text": "x"},
                                      follow_redirects=True).data
    assert calls == []


def test_send_button_is_an_icon_below_the_textbox_with_accessible_name(app, sent, claude):
    lead = ready_lead_with_messages(app, sent, claude)
    c = client(app)
    page = c.get(f"/leads/{lead.id}").data.decode()
    form = page[page.index('id="composer"'):]
    assert "Send as You" not in page
    assert form.index("<textarea") < form.index('aria-label="Send reply"')  # button under the text box
    assert 'title="Send reply"' in form and '<span class="sr-only">Send reply</span>' in form
    assert "<svg" in form and "http" not in form[form.index("<svg"):form.index("</svg>")]  # inline, no external file
    assert "flex-direction:column" in page and "width:100%" in page
    button = form[form.index("<button"):form.index("</button>")]
    assert "disabled" not in button  # window open
    for m in Message.query.filter_by(direction="inbound"):
        m.sent_at = utcnow() - timedelta(hours=30)
    db.session.commit()
    closed = c.get(f"/leads/{lead.id}").data.decode()
    closed_button = closed[closed.index('class="primary send-btn"'):]
    assert "disabled" in closed_button.split(">")[0]
