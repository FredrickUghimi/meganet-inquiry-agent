"""Safeguards before template follow-ups go live: opt-out, follow-up safety rules, the preview, template failure handling and
delivery status. Mocked Claude (ScriptedClaude) and mocked Meta (the `sent` fixture patches whatsapp.send_*)."""
import importlib.util
import logging
import os
from datetime import timedelta

import pytest

from replay import Chat, chat, cl, turn  # noqa: F401  (fixtures)
from sentinel import ai, service, whatsapp
from sentinel.models import Lead, Message, db, utcnow
from test_webhook_inbound import OURS, post, text_value

CONFIRMATION = "Understood. We won't message you again. If you ever want to talk, just message this number."
OWNER = "2340000000"



def _csrf_rejected(resp):
    """CSRF failures redirect to the login page (never a raw 400) and do nothing."""
    return resp.status_code == 302 and resp.headers["Location"].endswith("/login")

@pytest.fixture(autouse=True)
def fixed_followup_line(monkeypatch):
    monkeypatch.setattr(ai, "draft_followup_line", lambda lead, msgs, stage, prev: f"line{stage}")


def bot_texts(c):
    return c.bot_messages()


def templates(sent):
    return [s for s in sent if s[0] == "template" and s[1] != OWNER]


def start_lead(chat, cl, text="Tell me about EHR Cloud", reply="EHR Cloud keeps patient records, billing, lab and pharmacy in one place."):
    cl.classification.update(product="ehr_cloud", topic="general")
    chat.say(text, turn(reply))
    return chat.lead


def make_due(lead, days=2):
    """The lead went quiet `days` ago (24-hour window closed) and its next follow-up is due now."""
    for m in Message.query.filter_by(lead_id=lead.id):
        m.sent_at = utcnow() - timedelta(days=days, hours=1)
    lead.last_inbound_at = utcnow() - timedelta(days=days, hours=1)
    lead.followup_stage = 0
    lead.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()


def age_followups(hours):
    for m in Message.query.filter_by(is_followup=True):
        m.sent_at -= timedelta(hours=hours)
    db.session.commit()


def dashboard(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c


# --- 1. opt-out ----------------------------------------------------------------------------------------------------------------------

OPT_OUT_PHRASES = ["stop", "STOP", "Stop.", "unsubscribe", "Please unsubscribe me", "do not message me", "don't message me",
                   "Dont message me again", "leave me alone", "Remove me", "remove me from your list", "no more messages",
                   "stop messaging me", "Stop sending me messages", "abeg stop", "Abeg stop am", "no send me message again",
                   "Abeg no send me message again o", "no dey send me message", "i no wan hear from you again",
                   "make you no message me again", "comot me for your list", "no disturb me"]


@pytest.mark.parametrize("phrase", OPT_OUT_PHRASES)
def test_every_opt_out_phrase_opts_the_lead_out_and_sends_one_confirmation(chat, cl, sent, caplog, phrase):
    lead = start_lead(chat, cl)
    calls_before = len(cl.calls)
    caplog.set_level(logging.INFO)
    r = chat.say(phrase)
    assert r == [CONFIRMATION]  # one short free-text confirmation, nothing else
    assert len(cl.calls) == calls_before  # Claude did not draft anything
    db.session.expire_all()
    lead = chat.lead
    assert lead.opted_out is True and lead.opted_out_at is not None and lead.next_followup_at is None
    lines = [r_.getMessage() for r_ in caplog.records if "opted out" in r_.getMessage() and r_.name.startswith("sentinel.service")]
    assert lines and all("Amaka" not in l and chat.phone not in l and chat.phone[-4:] not in l for l in lines)  # no names or numbers
    assert len([l for l in lines if l.startswith("lead=")]) >= 1
    assert templates(sent) == []


def test_the_opt_out_is_recorded_the_moment_the_message_arrives(chat, cl, app, sent):
    """Before the reply pipeline even runs, so no follow-up can slip out in between."""
    lead = start_lead(chat, cl)
    msg = service.record_inbound(lead, "STOP", "whatsapp", external_id="wamid.stop1")
    assert msg is not None and lead.opted_out is True and lead.next_followup_at is None


def test_the_classifier_catches_an_opt_out_the_fixed_patterns_do_not(chat, cl):
    lead = start_lead(chat, cl)
    assert ai.optout_intent("I would rather we leave it there, thanks") is None
    cl.classification["contact_preference"] = "opt_out"
    r = chat.say("I would rather we leave it there, thanks")
    assert r == [CONFIRMATION]
    db.session.expire_all()
    assert chat.lead.opted_out is True and chat.lead.next_followup_at is None
    assert "contact_preference" in ai.CLASSIFY_INSTRUCTIONS and "Nigerian Pidgin" in ai.CLASSIFY_INSTRUCTIONS
    assert ai.parse_classification('{"intent": "exploring", "contact_preference": "opt_out"}')["contact_preference"] == "opt_out"
    assert "contact_preference" not in ai.parse_classification('{"intent": "exploring", "contact_preference": "dance"}')


@pytest.mark.parametrize("text", [
    "Can you stop by on Wednesday?", "What is the bus stop near your office?", "How do I stop a duplicate invoice in ERP Cloud?",
    "We want to stop using paper records. How long does onboarding take for a clinic with forty staff and two branches?",
    "Remove the duplicate patient record please", "I need to remove a user from the system", "Does EHR Cloud send messages to patients?",
    "I want to cancel my demo on Wednesday and rebook", "Please send me the details", "No more questions, thanks"])
def test_messages_that_only_contain_the_word_are_not_opt_outs(chat, cl, text):
    start_lead(chat, cl)
    assert ai.optout_intent(text) in (None, "not_interested")
    r = chat.say(text, turn("Of course. The team will help with that."))
    assert CONFIRMATION not in r
    db.session.expire_all()
    assert chat.lead.opted_out is False


def test_not_interested_only_stops_the_follow_ups_and_the_lead_stays_answerable(chat, cl):
    lead = start_lead(chat, cl)
    r = chat.say("I am not interested in the cloud version, but does HospitalPro install on our own network?",
                 turn("HospitalPro is installed on a facility's own local network."))
    assert "HospitalPro" in r[0] and CONFIRMATION not in r
    db.session.expire_all()
    lead = chat.lead
    assert lead.followups_off is True and lead.opted_out is False and lead.next_followup_at is None
    assert service.followup_block_reason(lead).kind == "permanent"
    r2 = chat.say("And does it do billing?", turn("Yes, HospitalPro covers billing too."))
    assert "billing" in r2[0]  # still answered
    make_due(lead)
    service.run_due_followups()
    assert Message.query.filter_by(lead_id=lead.id, is_followup=True).count() == 0


def test_an_opt_out_before_a_scheduled_day_1_means_no_template_is_ever_sent(chat, cl, sent):
    lead = start_lead(chat, cl)
    make_due(lead)
    assert service.followup_block_reason(lead) is None  # it would have gone out
    chat.say("Abeg stop")
    db.session.expire_all()
    lead = chat.lead
    sent.clear()
    lead.next_followup_at = utcnow() - timedelta(minutes=1)  # even if something put it back in the queue
    db.session.commit()
    service.run_due_followups()
    service.send_followup(lead)  # and even a direct call
    assert templates(sent) == [] and not [s for s in sent if s[0] == "text"]
    assert Message.query.filter_by(lead_id=lead.id, is_followup=True).count() == 0


def test_no_template_or_follow_up_can_be_sent_to_an_opted_out_number_by_any_path(chat, cl, sent):
    lead = start_lead(chat, cl)
    chat.say("STOP")
    db.session.expire_all()
    lead = chat.lead
    sent.clear()
    msg = service.send_whatsapp(lead, template="day1", params=["Amaka", "EHR Cloud", "x"], is_followup=True, stage=1)
    assert msg.status == "failed" and "opted out" in msg.error
    msg = service.send_whatsapp(lead, template="ack", params=["Amaka", "EHR Cloud"])
    assert msg.status == "failed"
    msg = service.send_whatsapp(lead, text="Hi Amaka. Just checking in.", is_followup=True, stage=1)
    assert msg.status == "failed"
    assert sent == []  # Meta was never called
    assert Message.query.filter_by(lead_id=lead.id, status="failed").count() == 0  # and nothing was stored


def test_a_later_genuine_message_is_answered_normally_without_a_second_confirmation(chat, cl):
    start_lead(chat, cl)
    assert chat.say("STOP") == [CONFIRMATION]
    r = chat.say("Actually, what does EHR Cloud do for a pharmacy?", turn("EHR Cloud has a pharmacy module for dispensing and stock."))
    assert "pharmacy module" in r[0] and CONFIRMATION not in r[0]
    db.session.expire_all()
    assert chat.lead.opted_out is True and chat.lead.next_followup_at is None  # still opted out: only a restart lifts it


def test_the_confirmation_is_not_sent_twice_for_a_repeated_stop(chat, cl):
    start_lead(chat, cl)
    assert chat.say("STOP") == [CONFIRMATION]
    assert chat.say("STOP") == []  # already confirmed


@pytest.mark.parametrize("restart", ["yes, send me information again", "Please start sending again", "you can message me again",
                                     "abeg send me info again", "resubscribe"])
def test_a_clear_request_to_restart_lifts_the_opt_out(chat, cl, restart):
    start_lead(chat, cl)
    chat.say("STOP")
    assert ai.optout_intent(restart) == "restart"
    r = chat.say(restart, turn("Welcome back, Amaka. Happy to send you more information."))
    assert "Welcome back" in r[0] and CONFIRMATION not in r[0]
    assert "asked to hear from us again" in cl.calls[-1]["system"]
    db.session.expire_all()
    lead = chat.lead
    assert lead.opted_out is False and lead.opted_out_at is None and lead.next_followup_at is not None
    make_due(lead)
    assert service.followup_block_reason(lead) is None  # follow-ups can run again


def test_a_restart_phrase_from_a_lead_who_never_opted_out_changes_nothing(chat, cl):
    lead = start_lead(chat, cl)
    r = chat.say("please send me information again", turn("Of course, here is a summary."))
    assert "asked to hear from us again" not in cl.calls[-1]["system"] and r[0].endswith("summary.")
    assert chat.lead.opted_out is False


def test_the_opted_out_label_is_on_the_lead_page_and_the_list(chat, cl, app):
    start_lead(chat, cl)
    chat.say("STOP")
    c = dashboard(app)
    page = c.get(f"/leads/{chat.lead.id}").data.decode()
    assert "Opted out" in page and "No template or follow-up will be sent to this number again" in page
    assert "Opted out" in c.get("/leads").data.decode()
    other = Chat(app, chat.sent, cl, "2348099999999")
    other.say("Hello there", turn("Hi."))
    assert "Opted out" not in c.get(f"/leads/{other.lead.id}").data.decode()


def test_the_opt_out_pattern_list_is_deterministic_for_every_phrase():
    for phrase in OPT_OUT_PHRASES:
        assert ai.optout_intent(phrase) == "opt_out", phrase
    assert ai.optout_intent("not interested, stop messaging me") == "opt_out"  # opt-out wins over not-interested
    assert ai.optout_intent("No thanks") == "not_interested" and ai.optout_intent("I no interested") == "not_interested"
    assert ai.optout_intent("") is None and ai.optout_intent("hello") is None


# --- 2. follow-up safety --------------------------------------------------------------------------------------------------------------

def blocked(lead):
    b = service.followup_block_reason(lead)
    return b.kind if b else None


def test_a_normal_active_lead_may_get_a_follow_up(chat, cl):
    lead = start_lead(chat, cl)
    make_due(lead)
    assert blocked(lead) is None


@pytest.mark.parametrize("change,why", [
    (lambda l: setattr(l, "followup_paused", True), "paused"),
    (lambda l: setattr(l, "opted_out", True), "opted out"),
    (lambda l: setattr(l, "followups_off", True), "not interested"),
    (lambda l: setattr(l, "status", "closed_won"), "closed"),
    (lambda l: setattr(l, "status", "closed_lost"), "closed"),
    (lambda l: setattr(l, "status", "ready"), "not new or engaged"),
    (lambda l: setattr(l, "status", "cold"), "not new or engaged"),
    (lambda l: setattr(l, "is_existing_client", True), "existing client"),
    (lambda l: setattr(l, "partner_enquiry", True), "partner enquiry"),
    (lambda l: setattr(l, "phone", None), "no phone"),
])
def test_follow_ups_are_never_sent_to_these_leads(chat, cl, sent, change, why):
    lead = start_lead(chat, cl)
    make_due(lead)
    change(lead)
    db.session.commit()
    assert why in service.followup_block_reason(lead).why
    sent.clear()
    service.run_due_followups()
    assert templates(sent) == [] and Message.query.filter_by(lead_id=lead.id, is_followup=True).count() == 0


def test_a_lead_marked_contacted_gets_no_follow_up(chat, cl, sent):
    lead = start_lead(chat, cl)
    service.mark_contacted(lead)
    lead.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert blocked(lead) == "permanent"
    sent.clear()
    service.run_due_followups()
    assert templates(sent) == []


def test_a_human_handled_lead_gets_no_follow_up_and_is_looked_at_again_later(chat, cl, sent):
    lead = start_lead(chat, cl)
    make_due(lead)
    db.session.add(Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body="Hi, Fredrick here.", sender="owner",
                           sent_at=utcnow() - timedelta(hours=1), handled=True))
    db.session.commit()
    assert blocked(lead) == "human"
    sent.clear()
    service.send_followup(lead)
    assert templates(sent) == []
    assert lead.next_followup_at > utcnow() + timedelta(hours=5)


def test_an_existing_client_who_is_a_sales_lead_may_still_get_a_follow_up(chat, cl):
    lead = start_lead(chat, cl)
    make_due(lead)
    lead.is_existing_client, lead.upgrade_enquiry = True, True
    db.session.commit()
    assert blocked(lead) is None


def test_never_more_than_one_follow_up_in_20_hours(chat, cl, sent):
    lead = start_lead(chat, cl)
    make_due(lead)
    service.run_due_followups()
    assert len(templates(sent)) == 1
    lead = db.session.get(Lead, lead.id)
    assert lead.next_followup_at >= utcnow() + timedelta(hours=19)  # the next stage is already held back
    lead.next_followup_at = utcnow() - timedelta(minutes=1)  # but suppose it were due again straight away
    db.session.commit()
    assert blocked(lead) == "gap"
    service.run_due_followups()
    assert len(templates(sent)) == 1
    assert lead.next_followup_at > utcnow() + timedelta(hours=15)  # pushed to 20 hours after the last one
    age_followups(21)
    lead.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    service.run_due_followups()
    assert len(templates(sent)) == 2 and templates(sent)[-1][2] == "meganet_followup_day3"


def test_never_more_than_three_follow_ups_in_total(chat, cl, sent):
    lead = start_lead(chat, cl)
    for i in range(3):  # an earlier sequence of three that the lead then answered; they went quiet again
        db.session.add(Message(lead_id=lead.id, direction="outbound", channel="whatsapp", body=f"old follow-up {i}", is_followup=True,
                               followup_stage=(1, 3, 7)[i], template="day1", sent_at=utcnow() - timedelta(days=10 + i), handled=True))
    db.session.commit()
    make_due(lead)
    for m in Message.query.filter_by(lead_id=lead.id, is_followup=True):
        m.sent_at = utcnow() - timedelta(days=10)
    db.session.commit()
    assert blocked(lead) == "permanent" and "3 follow-ups already sent" in service.followup_block_reason(lead).why
    sent.clear()
    service.run_due_followups()
    assert templates(sent) == [] and db.session.get(Lead, lead.id).next_followup_at is None


def test_the_daily_cap_limits_template_sends_and_the_rest_wait_for_tomorrow(app, sent, cl):
    app.config["FOLLOWUP_DAILY_CAP"] = 2
    leads = []
    for i in range(3):
        c = Chat(app, sent, cl, f"23480111111{i:02d}")
        leads.append(start_lead(c, cl))
    for l in leads:
        make_due(l)
    sent.clear()
    service.run_due_followups()
    assert len(templates(sent)) == 2 and service.template_followups_today() == 2
    waiting = [l for l in Lead.query.all() if l.followup_stage == 0]
    assert len(waiting) == 1 and waiting[0].next_followup_at is not None  # still queued, not dropped
    assert service.followup_block_reason(waiting[0]).kind == "cap"
    for m in Message.query.filter_by(is_followup=True):  # a day later
        m.sent_at -= timedelta(days=1)
    db.session.commit()
    service.run_due_followups()
    assert len(templates(sent)) == 3


def test_followups_enabled_zero_sends_nothing_but_everything_else_works(app, sent, cl):
    app.config["FOLLOWUPS_ENABLED"] = False
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    make_due(lead)
    sent.clear()
    service.run_due_followups()
    service.send_followup(lead)
    assert sent == [] and service.followup_block_reason(lead).kind == "disabled"
    assert lead.next_followup_at is not None  # stays queued, so switching it back on sends it
    assert service.upcoming_followups(24) == []
    r = c.say("What about the pharmacy module?", turn("EHR Cloud has a pharmacy module for dispensing."))  # replies still work
    assert "pharmacy module" in r[0]
    app.config["FOLLOWUPS_ENABLED"] = True
    make_due(db.session.get(Lead, lead.id))
    service.run_due_followups()
    assert len(templates(sent)) == 1


@pytest.mark.parametrize("stages,expected,cold_after", [
    ("1,3,7", ["meganet_followup_day1", "meganet_followup_day3", "meganet_followup_day7"], 7),
    ("1,3", ["meganet_followup_day1", "meganet_followup_day3"], 3),
    ("1", ["meganet_followup_day1"], 1),
    ("3, 1", ["meganet_followup_day1", "meganet_followup_day3"], 3),
    ("1,5,9,x", ["meganet_followup_day1"], 1)])
def test_followup_stages_chooses_which_days_run(app, sent, cl, stages, expected, cold_after):
    app.config["FOLLOWUP_STAGES"] = stages
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    sent.clear()
    for _ in range(4):
        lead = db.session.get(Lead, lead.id)
        if lead.next_followup_at is None:
            break
        make_due_keep_stage(lead)
        age_followups(21)
        service.run_due_followups()
    assert [t[2] for t in templates(sent)] == expected
    lead = db.session.get(Lead, lead.id)
    assert lead.status == "cold" and lead.followup_stage == cold_after and lead.next_followup_at is None


def make_due_keep_stage(lead):
    stage = lead.followup_stage
    make_due(lead)
    lead.followup_stage = stage
    db.session.commit()


def test_the_default_stages_and_the_settings_page_document_the_cost(app):
    assert service.followup_stages() == (1, 3, 7) and app.config["FOLLOWUP_DAILY_CAP"] == 30 and app.config["FOLLOWUPS_ENABLED"] is True
    html = dashboard(app).get("/settings").data.decode()
    assert "FOLLOWUP_STAGES" in html and "FOLLOWUP_DAILY_CAP" in html and "FOLLOWUPS_ENABLED" in html
    readme = open(os.path.join(os.path.dirname(__file__), "..", "..", "README.md"), encoding="utf-8").read()
    deploy = open(os.path.join(os.path.dirname(__file__), "..", "..", "DEPLOY.md"), encoding="utf-8").read()
    for doc in (readme, deploy):
        assert "FOLLOWUP_STAGES" in doc and "Marketing" in doc and "FOLLOWUP_DAILY_CAP" in doc and "FOLLOWUPS_ENABLED" in doc


# --- 3. the preview ---------------------------------------------------------------------------------------------------------------------

def preview_script():
    spec = importlib.util.spec_from_file_location(
        "manage_followups", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "manage_followups.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_preview_lists_only_the_leads_that_would_get_a_follow_up_and_sends_nothing(app, sent, cl, capsys):
    a = start_lead(Chat(app, sent, cl, "2348011111111"), cl)
    b = start_lead(Chat(app, sent, cl, "2348022222222"), cl)
    c_ = start_lead(Chat(app, sent, cl, "2348033333333"), cl)
    d = start_lead(Chat(app, sent, cl, "2348044444444"), cl)
    for lead in (a, b, c_, d):
        make_due(lead)
    b.opted_out = True
    c_.followup_paused = True
    d.next_followup_at = utcnow() + timedelta(days=3)  # not within 24 hours
    db.session.commit()
    sent.clear()
    rows = service.upcoming_followups(24)
    assert [r["lead"].id for r in rows] == [a.id]
    row = rows[0]
    assert row["stage"] == 1 and row["kind"] == "template" and row["template"] == "meganet_followup_day1"
    assert "Day 1 follow-up" in row["why"] and "not paused, opted out, closed" in row["why"] and "0 of 3 follow-ups sent" in row["why"]
    mod = preview_script()
    with app.app_context():
        mod.cmd_preview(type("A", (), {"hours": 24})())
    out = capsys.readouterr().out
    assert f"{a.id:>4}" in out and "234" in out and "*" in out and "2348011111111" not in out  # masked
    assert "meganet_followup_day1" in out and "Day 1 follow-up" in out and "Nothing was sent" in out and "Marketing" in out
    assert str(b.id) not in out.split("Nothing was sent")[0].split("\n", 3)[-1].replace(f"{a.id:>4}", "")
    assert sent == [] and Message.query.filter_by(is_followup=True).count() == 0


def test_the_preview_script_runs_from_the_command_line_entry_point(app, sent, cl, capsys, monkeypatch):
    start_lead(Chat(app, sent, cl, "2348011111111"), cl)
    mod = preview_script()
    monkeypatch.setattr(mod, "create_app", lambda overrides=None: app)
    mod.main(["preview", "--hours", "1"])
    assert "No follow-up would be sent in the next 1 hours." in capsys.readouterr().out


def test_the_preview_says_when_follow_ups_are_off(app, capsys):
    app.config["FOLLOWUPS_ENABLED"] = False
    with app.app_context():
        preview_script().cmd_preview(type("A", (), {"hours": 24})())
    assert "OFF (FOLLOWUPS_ENABLED=0)" in capsys.readouterr().out


def test_the_dashboard_lists_upcoming_follow_ups_with_a_pause_button(app, sent, cl):
    lead = start_lead(Chat(app, sent, cl, "2348011111111"), cl)
    make_due(lead)
    c = dashboard(app)
    page = c.get("/followups").data.decode()
    assert "Upcoming follow-ups" in page and f"#{lead.id}" in page and "meganet_followup_day1" in page and "Pause" in page
    assert "2348011111111" not in page and "Marketing" in page
    anon = app.test_client()
    assert anon.get("/followups").status_code == 302  # login required


def test_the_pause_button_pauses_the_lead_and_is_csrf_protected(app, sent, cl):
    lead = start_lead(Chat(app, sent, cl, "2348011111111"), cl)
    make_due(lead)
    c = dashboard(app)
    assert _csrf_rejected(c.post(f"/leads/{lead.id}/action", data={"action": "pause", "next": "followups"}))  # no token
    resp = c.post(f"/leads/{lead.id}/action", data={"csrf": "t", "action": "pause", "next": "followups"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/followups")
    db.session.expire_all()
    lead = db.session.get(Lead, lead.id)
    assert lead.followup_paused is True and lead.next_followup_at is None
    assert service.upcoming_followups(24) == []
    assert f"#{lead.id}</a>" not in c.get("/followups").data.decode()


def test_the_dashboard_follow_ups_page_warns_when_they_are_off(app):
    app.config["FOLLOWUPS_ENABLED"] = False
    assert "Follow-ups are switched off" in dashboard(app).get("/followups").data.decode()


# --- 4. template failures and delivery status ---------------------------------------------------------------------------------------------

META_ERROR = "HTTP 400: Template name does not exist in the translation (code 132001)"


def reject_templates(monkeypatch, sent, fail_times=99):
    state = {"n": 0}

    def send_template(to, name, lang, params):
        if to != OWNER:
            state["n"] += 1
            if state["n"] <= fail_times:
                raise whatsapp.WhatsAppError(META_ERROR)
        sent.append(("template", to, name, list(params)))
        return f"wamid.t{len(sent)}"
    monkeypatch.setattr(whatsapp, "send_template", send_template)
    return state


def test_a_rejected_template_records_meta_s_code_and_message_alerts_once_and_is_retried_once(app, sent, cl, monkeypatch):
    state = reject_templates(monkeypatch, sent)
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    make_due(lead)
    sent.clear()
    service.run_due_followups()
    lead = db.session.get(Lead, lead.id)
    assert "132001" in lead.template_error and "Template name does not exist" in lead.template_error
    assert lead.template_error.startswith("meganet_followup_day1:") and lead.template_error_at is not None
    failed = Message.query.filter_by(lead_id=lead.id, status="failed").all()
    assert len(failed) == 1 and failed[0].template == "day1" and failed[0].delivery_status == "failed" and "132001" in failed[0].error
    alerts = [s for s in sent if s[1] == OWNER]
    assert len(alerts) == 1 and "meganet_followup_day1" in alerts[0][2] and "132001" in alerts[0][2]
    assert lead.followup_failures == 1 and lead.followup_stage == 0 and lead.followup_paused is False
    assert lead.next_followup_at > utcnow() + timedelta(minutes=50)  # one retry, an hour later
    lead.next_followup_at = utcnow() - timedelta(minutes=1)  # the retry
    db.session.commit()
    service.run_due_followups()
    lead = db.session.get(Lead, lead.id)
    assert state["n"] == 2  # the same template was sent twice in all, never a third time
    assert lead.followup_paused is True and lead.next_followup_at is None
    service.run_due_followups()
    assert state["n"] == 2
    assert len([s for s in sent if s[1] == OWNER]) == 1  # still only one owner alert
    page = dashboard(app).get(f"/leads/{lead.id}").data.decode()
    assert "WhatsApp template problem" in page and "132001" in page and "color:#7f1d1d" in page


def test_a_retry_that_works_clears_the_red_note(app, sent, cl, monkeypatch):
    reject_templates(monkeypatch, sent, fail_times=1)
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    make_due(lead)
    service.run_due_followups()
    lead = db.session.get(Lead, lead.id)
    assert lead.template_error
    lead.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    service.run_due_followups()
    lead = db.session.get(Lead, lead.id)
    assert lead.template_error is None and lead.followup_stage == 1 and lead.followup_failures == 0
    assert "WhatsApp template problem" not in dashboard(app).get(f"/leads/{lead.id}").data.decode()


def status_event(wamid, status, recipient, errors=None):
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "2349000000000", "phone_number_id": OURS},
             "statuses": [dict({"id": wamid, "status": status, "recipient_id": recipient}, **({"errors": errors} if errors else {}))]}
    return value


def test_delivery_status_is_recorded_moves_only_forward_and_is_shown_on_the_lead_page(app, sent, cl):
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    make_due(lead)
    service.run_due_followups()
    msg = Message.query.filter_by(lead_id=lead.id, is_followup=True).one()
    assert msg.template == "day1" and msg.delivery_status == "sent" and msg.external_id
    page = lambda: dashboard(app).get(f"/leads/{lead.id}").data.decode()
    assert "template day1: <b>sent</b>" in page()
    post(app, status_event(msg.external_id, "delivered", "2348011111111"))
    db.session.expire_all()
    assert Message.query.filter_by(id=msg.id).one().delivery_status == "delivered" and "template day1: <b>delivered</b>" in page()
    post(app, status_event(msg.external_id, "read", "2348011111111"))
    post(app, status_event(msg.external_id, "delivered", "2348011111111"))  # a late duplicate must not move it back
    db.session.expire_all()
    assert Message.query.filter_by(id=msg.id).one().delivery_status == "read" and "template day1: <b>read</b>" in page()


def test_a_template_that_fails_after_being_accepted_is_recorded_alerted_once_and_pauses_the_sequence(app, sent, cl):
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    make_due(lead)
    service.run_due_followups()
    msg = Message.query.filter_by(lead_id=lead.id, is_followup=True).one()
    sent.clear()
    post(app, status_event(msg.external_id, "failed", "2348011111111", [{"code": 131026, "title": "Message undeliverable"}]))
    db.session.expire_all()
    lead = db.session.get(Lead, lead.id)
    msg = db.session.get(Message, msg.id)
    assert msg.status == "failed" and msg.delivery_status == "failed" and "131026" in msg.error
    assert "131026" in lead.template_error and "Message undeliverable" in lead.template_error
    assert lead.followup_paused is True and lead.next_followup_at is None
    assert len([s for s in sent if s[1] == OWNER]) == 1
    assert "template day1: <b>failed</b>" in dashboard(app).get(f"/leads/{lead.id}").data.decode()


def test_a_free_text_message_failure_status_does_not_pause_follow_ups(app, sent, cl):
    c = Chat(app, sent, cl, "2348011111111")
    lead = start_lead(c, cl)
    bot = Message.query.filter_by(lead_id=lead.id, direction="outbound").first()
    bot.external_id = "wamid.freetext1"
    db.session.commit()
    post(app, status_event("wamid.freetext1", "failed", "2348011111111", [{"code": 131047, "title": "Re-engagement message"}]))
    db.session.expire_all()
    assert db.session.get(Message, bot.id).status == "failed" and db.session.get(Lead, lead.id).followup_paused is False
    assert db.session.get(Lead, lead.id).template_error is None


def test_the_old_webhook_status_log_line_is_unchanged_for_unknown_messages(app, sent, cl, caplog):
    caplog.set_level(logging.INFO)
    post(app, status_event("wamid.unknown", "delivered", "2348011111111"))
    assert any("ignored (delivery status 'delivered')" in r.getMessage() for r in caplog.records)


# --- the eval script's opt-out scenarios ---------------------------------------------------------------------------------------------------

def _eval_module():
    spec = importlib.util.spec_from_file_location(
        "eval_replies", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "eval_replies.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_eval_has_opt_out_scenarios_and_the_fixed_patterns_pass_them_without_any_draft(app, sent, cl):
    ev = _eval_module()
    opt_outs = [(n, s) for n, s in enumerate(ev.SCENARIOS, 1) if s["name"].startswith("opt-out:") and "classifier" not in s["name"]]
    assert len(opt_outs) == 5 and any("Pidgin" in s["name"] for _, s in opt_outs)
    lines = []
    passed, total = ev.evaluate(ev.make_eval_app(), [s for _, s in opt_outs], out=lines.append, numbers=[n for n, _ in opt_outs])
    assert (passed, total) == (5, 5), "\n".join(lines)
    assert cl.calls == []  # a stop request never reaches Claude's drafting step
    assert all(CONFIRMATION in "\n".join(lines) for _ in [0])


def test_the_normal_answer_scenarios_after_an_opt_out_are_not_given_the_confirmation(app, sent, cl):
    ev = _eval_module()
    names = ["message after an opt-out is answered normally", "restart after an opt-out lifts it",
             "a product question with the word stop is not an opt-out", "not interested: follow-ups stop but the lead is still answered"]
    scs = [s for s in ev.SCENARIOS if s["name"] in names]
    assert len(scs) == 4
    cl.drafts.extend([turn("HospitalPro is installed on your own local network."),  # in scenario order
                      turn("In ERP Cloud you can void the invoice or issue a credit note; the team can confirm the exact steps."),
                      turn("Pricing is tailored to your needs and shared personally by Fredrick or the team."),
                      turn("Welcome back, Amaka. Glad to send you more information.")])
    lines = []
    passed, total = ev.evaluate(ev.make_eval_app(), scs, out=lines.append)
    assert (passed, total) == (4, 4), "\n".join(lines)
    assert len(cl.calls) == 4 and CONFIRMATION not in "\n".join(lines)
