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


def test_phone_keys_are_e164():
    assert service.opt_key("2348011111111") == "+2348011111111"
    assert service.opt_key("+234 801 111 1111") == "+2348011111111"
    assert service.opt_key("08011111111") == "+2348011111111"
    assert service.opt_key("002348011111111") == "+2348011111111"
    assert service.opt_key("") is None and service.opt_key(None) is None


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
    assert service.is_opted_out(PHONE) and service.is_opted_out("+234 801 111 1111") and service.is_opted_out("08011111111")


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


def test_a_lead_object_with_a_different_phone_format_is_still_blocked(app, sent):
    lead = make_lead()
    ask_to_stop(lead)
    service.delete_leads([lead.id])
    other = make_lead(phone="08011111111")  # same person, local format, recreated by hand
    msg = service.send_whatsapp(other, template="ack", params=["Amaka", "EHR Cloud"])
    assert msg.status == "failed" and sent == []


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
