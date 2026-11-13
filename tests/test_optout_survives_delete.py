"""The opt-out register is keyed by phone number, not by lead: deleting or recreating a lead never loses an opt-out."""
import importlib.util
import os
from datetime import timedelta

import pytest

from sentinel import service
from sentinel.models import Lead, Message, OptOut, db, utcnow

PHONE = "2348011111111"

spec = importlib.util.spec_from_file_location(
    "audit_optouts", os.path.join(os.path.dirname(__file__), "..", "scripts", "audit_optouts.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def make_lead(phone=PHONE, **kw):
    lead = Lead(phone=phone, name="Amaka", status="engaged", product_interest="ehr_cloud", **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def ask_to_stop(lead):
    service.record_inbound(lead, "STOP", "whatsapp", external_id="wamid.stop1")
    db.session.commit()


def row():
    return OptOut.query.filter_by(phone="+" + PHONE).first()


def test_webhook_digits_and_plus_numbers_give_the_same_key():
    for raw in ("2348011111111", "+2348011111111", "+234 801 111 1111", "002348011111111", "234-801-111-1111"):
        assert service.phone_key(raw) == ("+2348011111111", False), raw
    assert service.phone_key("14155550123") == ("+14155550123", False)  # another country: no Nigeria assumption
    assert service.phone_key("") == (None, False) and service.phone_key(None) == (None, False)


def test_numbers_without_a_clear_country_are_stored_as_given_and_flagged_not_guessed():
    for raw in ("08011111111", "0801 111 1111", "8011111111", "12345"):
        key, review = service.phone_key(raw)
        assert review is True and key == "?" + "".join(c for c in raw if c.isdigit()), raw
        assert service.opt_key(raw) is None  # never turned into an E.164 key
    assert not any(service.phone_key(r)[0].startswith("+234") for r in ("08011111111", "8011111111"))


def test_an_unclear_number_that_opts_out_is_kept_flagged_and_still_blocks(app, sent):
    lead = make_lead(phone="08011111111")
    service.record_opt_out(lead.phone)
    db.session.commit()
    r = OptOut.query.one()
    assert r.phone == "?08011111111" and r.needs_review is True and r.lifted_at is None
    assert service.register_counts() == (1, 1)
    service.delete_leads([lead.id])
    assert OptOut.query.count() == 1
    assert service.send_whatsapp(make_lead(phone="08011111111"), template="ack", params=["A", "EHR Cloud"]).status == "failed"
    # fail-safe: the same person arriving later as +234 8011111111 is still protected (over-blocking is the safe error)
    assert service.is_opted_out("2348011111111")
    assert sent == []


def test_an_unrelated_number_is_not_blocked_by_an_unclear_row(app):
    service.record_opt_out("08011111111")
    db.session.commit()
    assert not service.is_opted_out("2348099999999") and not service.is_opted_out("14155550123")


def test_stop_writes_the_register_and_deleting_the_lead_keeps_it(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    before = row()
    assert before is not None and before.lifted_at is None
    stamp = before.opted_out_at
    service.delete_leads([lead.id])
    assert Lead.query.count() == 0
    after = row()
    assert after is not None and after.lifted_at is None and after.opted_out_at == stamp
    assert service.is_opted_out(PHONE) and service.is_opted_out("+234 801 111 1111")


def test_a_legacy_opt_out_flag_is_saved_to_the_register_before_the_lead_is_deleted(app):
    lead = make_lead(opted_out=True, opted_out_at=utcnow())  # opted out under the old code: flag only, no register row
    assert row() is None
    service.delete_leads([lead.id])
    assert row() is not None and row().source == "delete_guard"


def test_a_recreated_lead_gets_no_template_or_followup(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    service.delete_leads([lead.id])
    sent.clear()
    again = service.find_or_create_lead(phone=PHONE, name="Amaka", source="whatsapp")
    db.session.commit()
    assert again.opted_out is True  # born opted out
    again.opted_out = False  # even if that flag were wrong, the register decides at send time
    db.session.commit()
    for kwargs in (dict(template="ack", params=["Amaka", "EHR Cloud"]),
                   dict(template="day1", params=["Amaka", "EHR Cloud", "x"], is_followup=True, stage=1),
                   dict(text="Hi Amaka, checking in", is_followup=True, stage=1)):
        msg = service.send_whatsapp(again, **kwargs)
        assert msg.status == "failed" and msg.error.startswith("blocked:")
    assert sent == []
    again.last_inbound_at = utcnow() - timedelta(days=2)
    assert service.followup_block_reason(again).kind == "permanent"
    service.send_followup(again)
    assert sent == []


def test_a_recreated_lead_with_a_plus_or_spaced_number_is_still_blocked(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    service.delete_leads([lead.id])
    other = make_lead(phone="+234 801 111 1111")  # same person, typed with a plus and spaces
    msg = service.send_whatsapp(other, template="ack", params=["Amaka", "EHR Cloud"])
    assert msg.status == "failed" and sent == []
    # national format is not resolved to a country, but the fail-safe cross-match still blocks it
    local = make_lead(phone="08011111111")
    assert service.send_whatsapp(local, template="ack", params=["Amaka", "EHR Cloud"]).status == "failed" and sent == []


def test_a_customer_who_asks_to_start_again_lifts_it_and_the_row_is_kept(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    service._lift_opt_out(lead, "test")
    assert row() is not None and row().lifted_at is not None
    assert not service.is_opted_out(PHONE)
    again = make_lead(phone="2348022222222")
    assert service.send_whatsapp(again, template="ack", params=["B", "EHR Cloud"]).status == "sent"


def test_normal_leads_still_receive_messages(app, sent):
    stopped = make_lead()
    ask_to_stop(stopped)
    normal = make_lead(phone="2348033333333")
    msg = service.send_whatsapp(normal, template="ack", params=["Chidi", "EHR Cloud"])
    assert msg.status == "sent"
    msg = service.send_whatsapp(normal, text="Hello Chidi", is_followup=True, stage=1)
    assert msg.status == "sent"
    assert [e[1] for e in sent] == ["2348033333333", "2348033333333"]
    service.delete_leads([stopped.id])  # deleting the opted-out one changes nothing for the other
    assert service.send_whatsapp(normal, text="Again", is_followup=True, stage=3).status == "sent"


def test_owner_replies_and_in_window_answers_still_work_for_an_opted_out_number(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    # existing policy kept: only business-initiated sends (templates, follow-ups) are blocked; Fredrick may still answer
    assert service.send_whatsapp(lead, text="Sure, here you go", sender="owner").status == "sent"


def test_backfill_copies_existing_opt_outs_once_and_changes_nothing_else(app):
    a = make_lead(opted_out=True, opted_out_at=utcnow() - timedelta(days=5))
    b = make_lead(phone="2348022222222")
    db.session.add(Message(lead_id=a.id, direction="inbound", channel="whatsapp", body="stop", external_id="w1"))
    db.session.commit()
    assert service.backfill_opt_outs() == 1
    assert service.backfill_opt_outs() == 0  # idempotent
    assert OptOut.query.count() == 1 and row().source == "backfill"
    assert Lead.query.count() == 2 and Message.query.count() == 1
    assert db.session.get(Lead, b.id).opted_out is False


def test_startup_runs_the_backfill(tmp_path):
    from sentinel import create_app
    uri = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = {"SQLALCHEMY_DATABASE_URI": uri, "SCHEDULER_ENABLED": False, "TESTING": True}
    app = create_app(cfg)
    with app.app_context():
        db.session.add(Lead(phone=PHONE, opted_out=True, opted_out_at=utcnow()))
        OptOut.query.delete()
        db.session.commit()
    app2 = create_app(cfg)  # a restart / deploy
    with app2.app_context():
        assert row() is not None
        db.session.remove()
        db.drop_all()


def test_audit_script_counts_only_and_never_prints_numbers(app, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audit.db'}")
    log = tmp_path / "app.log"
    log.write_text("\n".join([
        "INFO lead=1 opted out (keyword, recorded on arrival)", "INFO lead deleted id=1 messages=2 demo_requests=0 open_questions=0",
        "INFO lead=2 opted out (keyword, recorded on arrival)", "INFO lead=2 opt-out lifted (keyword)",
        "INFO lead deleted id=2 messages=1 demo_requests=0 open_questions=0",
        "INFO lead=3 opted out (classifier)", "INFO lead deleted id=4 messages=1 demo_requests=0 open_questions=0",
    ]))
    assert audit.possibly_lost(log.read_text().splitlines()) == 1
    audit.main(["--log", str(log)])  # builds its own app on a throwaway database; only counts are printed
    out = capsys.readouterr().out
    assert "may already have been lost (estimate from the log): 1" in out
    assert PHONE not in out and "+234" not in out


# --- the real inbound path, same transaction, raw delete ---------------------------------------------------------------------

from test_webhook_inbound import post, text_value  # noqa: E402


def committed_view(phone_key):
    """What a SEPARATE connection sees: only committed data. (lead flag, register row exists)."""
    from sqlalchemy import text
    with db.engine.connect() as conn:
        flag = conn.execute(text("select opted_out from leads where phone = :p"), {"p": PHONE}).scalar()
        reg = conn.execute(text("select count(*) from opt_outs where phone = :k"), {"k": phone_key}).scalar()
    return flag, reg


@pytest.mark.parametrize("body", ["STOP", "stop", "Unsubscribe", "please stop messaging me"])
def test_stop_through_the_webhook_writes_the_register_row_immediately(app, sent, claude, body):
    assert post(app, text_value(PHONE, mid="wamid.s1", body=body)).status_code == 200
    r = OptOut.query.one()
    assert r.phone == "+" + PHONE and r.lifted_at is None and r.needs_review is False and r.source == "inbound"
    assert Lead.query.one().opted_out is True


def test_the_register_row_and_the_lead_flag_are_committed_together(app, sent, claude, monkeypatch):
    """Stop the reply pipeline from running at all: the opt-out must already be committed by record_inbound itself."""
    from sentinel import webhook
    monkeypatch.setattr(webhook, "run_async", lambda *a, **k: None)
    post(app, text_value(PHONE, mid="wamid.s2", body="STOP"))
    assert committed_view("+" + PHONE) == (1, 1)  # both visible to another connection


def test_a_failure_after_the_opt_out_cannot_leave_the_lead_flag_without_the_register_row(app, sent):
    lead = make_lead()
    service.record_inbound(lead, "STOP", "whatsapp", external_id="wamid.s3")
    db.session.rollback()  # nothing is pending: it was all committed together
    assert committed_view("+" + PHONE) == (1, 1)


def test_the_classifier_opt_out_path_also_writes_the_register(app, sent, claude, monkeypatch):
    """A phrase the fixed patterns miss but the classifier catches goes through _opt_out_and_confirm."""
    lead = make_lead()
    db.session.add(Message(lead_id=lead.id, direction="inbound", channel="whatsapp", body="x", external_id="w9"))
    db.session.commit()
    service._opt_out_and_confirm(lead, "classifier", False, lambda *a, **k: None)
    assert OptOut.query.one().phone == "+" + PHONE
    assert committed_view("+" + PHONE) == (1, 1)


def test_a_raw_delete_of_the_lead_row_leaves_the_register_row(app, sent, claude):
    post(app, text_value(PHONE, mid="wamid.s4", body="STOP"))
    assert OptOut.query.count() == 1
    from sqlalchemy import text
    db.session.execute(text("delete from messages"))
    db.session.execute(text("delete from leads"))  # raw SQL, bypassing delete_leads and every guard
    db.session.commit()
    assert Lead.query.count() == 0
    assert OptOut.query.one().phone == "+" + PHONE and service.is_opted_out(PHONE)


def test_after_a_raw_delete_the_returning_customer_gets_no_template(app, sent, claude):
    post(app, text_value(PHONE, mid="wamid.s5", body="STOP"))
    from sqlalchemy import text
    db.session.execute(text("delete from messages"))
    db.session.execute(text("delete from leads"))
    db.session.commit()
    sent.clear()
    post(app, text_value(PHONE, mid="wamid.s6", body="Hello, a question about EHR"))
    lead = Lead.query.one()
    assert lead.opted_out is True  # born opted out from the register
    assert service.send_whatsapp(lead, template="ack", params=["A", "EHR Cloud"]).status == "failed"
    assert not [e for e in sent if e[0] == "template"]


def test_the_startup_line_is_counts_only(tmp_path, caplog):
    import logging
    from sentinel import create_app
    caplog.set_level(logging.INFO)
    cfg = {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 's.db'}", "SCHEDULER_ENABLED": False, "TESTING": True}
    app = create_app(cfg)
    with app.app_context():
        db.session.add_all([Lead(phone=PHONE, opted_out=True, opted_out_at=utcnow()),
                            Lead(phone="08022222222", opted_out=True, opted_out_at=utcnow())])
        OptOut.query.delete()
        db.session.commit()
    caplog.clear()
    app2 = create_app(cfg)
    lines = [r.getMessage() for r in caplog.records if "Opt-out register" in r.getMessage()]
    assert lines == ["Opt-out register: backfilled 2 opt-out(s) from existing leads; 2 active in total, 1 flagged for review "
                     "(country unclear)"]
    assert PHONE not in lines[0] and "08022222222" not in lines[0]
    with app2.app_context():
        db.session.remove()
        db.drop_all()
