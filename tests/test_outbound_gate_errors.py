"""The outbound gate and its yes/no checks fail CLOSED: any error becomes an explicit refusal ("gate_error"), never an exception
and never ok=True. Logs and audit entries never carry phone numbers or message text."""
import logging
from datetime import datetime

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from sentinel import service
from sentinel.models import db
from sentinel.outbound import audit, clock, consent, counters, gate, settings, spend, suppression
from sentinel.outbound.models import AuditEvent, Draft, OutboundSetting, Prospect
from test_outbound_phase1 import PHONE, TUE_10, audit_actions, campaign, draft, prospect, sent_draft, turn_on

SECRET = "SECRET-MESSAGE-TEXT-do-not-log"


def ready_cloud_draft():
    """A WhatsApp Cloud draft that would be ALLOWED if nothing were broken."""
    turn_on(channel_wa_cloud=True)
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    d = draft(prospect(), channel="whatsapp_cloud")
    assert gate.can_send(d, TUE_10).ok is True  # sanity: this is the normal, unchanged behaviour
    return d


def corrupt(key, value):
    row = db.session.get(OutboundSetting, key)
    if row is None:
        db.session.add(OutboundSetting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()


def assert_refused_as_error(result):
    assert result.ok is False and result.code == "gate_error" and result.code == gate.GATE_ERROR
    assert "refused" in result.reason


# --- the three cases asked for ---------------------------------------------------------------------------------------------

def test_a_missing_settings_table_gives_a_refusal_not_an_exception(app):
    d = ready_cloud_draft()
    db.session.execute(text("drop table outbound_settings"))
    db.session.commit()
    assert_refused_as_error(gate.can_send(d, TUE_10))
    assert "gate_error" in audit_actions()  # the audit table is still there, so the entry was written


def test_an_unreachable_database_gives_a_refusal_not_an_exception(app, caplog):
    d = ready_cloud_draft()
    caplog.set_level(logging.INFO)
    did = d.id

    def refuse_connection(conn, cursor, statement, parameters, context, executemany):
        # real driver errors echo the statement and its parameters (which can be phone numbers or message text)
        raise OperationalError(statement, {"phone": PHONE, "body": SECRET}, Exception(f"connection refused {PHONE} {SECRET}"))

    event.listen(db.engine, "before_cursor_execute", refuse_connection)
    try:
        result = gate.can_send(d, TUE_10)  # even the audit write fails; it must not raise
    finally:
        event.remove(db.engine, "before_cursor_execute", refuse_connection)
    assert_refused_as_error(result)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "OperationalError" in logged and PHONE not in logged and SECRET not in logged and "SELECT" not in logged
    assert AuditEvent.query.filter_by(action="gate_error").count() == 0  # nothing could be written, and nothing raised
    assert did  # (the draft itself is untouched)


@pytest.mark.parametrize("stored,key", [
    ("not json {", "enabled"),            # not even JSON
    ('"false"', "enabled"),               # the string "false" must NOT count as on
    ('"yes"', "enabled"),
    ("1", "enabled"),                      # a number is not a switch
    ('"abc"', "daily_cap_total"),         # text where a number is needed
    ("true", "daily_cap_total"),          # a bool is not a number
    ("12", "send_start"),                 # a number where text is needed
    ('"2026-13-45"', "legal_advice_date"),
    ('"abc"', "spend_cap_usd"),
])
def test_a_corrupt_setting_value_gives_a_refusal(app, stored, key):
    d = ready_cloud_draft()
    corrupt(key, stored)
    result = gate.can_send(d, TUE_10)
    assert_refused_as_error(result)


def test_a_corrupt_kill_switch_row_fails_closed_too(app):
    d = ready_cloud_draft()
    corrupt("kill", '"off"')
    assert_refused_as_error(gate.can_send(d, TUE_10))
    assert settings.is_killed() is True  # the public read says "stopped" when it cannot read the switch


# --- every internal check that can fail is covered, and none can produce ok=True ---------------------------------------------

@pytest.mark.parametrize("target,attr", [
    (service, "is_opted_out"),                  # the do-not-contact register
    (service, "phone_key"),                     # inside the marketing opt-in check
    (spend, "month_total"),                     # the spend hard stop
    (clock, "local"),                           # the send hours
    (counters, "counted_sends"),                # the 40-opener checkpoint
    (settings, "all_values"),                   # settings as a whole
    (settings, "get"),                          # the kill switch read
])
def test_any_failing_check_inside_the_gate_is_a_refusal(app, monkeypatch, target, attr):
    d = ready_cloud_draft()
    consent.record_opt_in  # (opt-in already recorded in ready_cloud_draft)

    def boom(*a, **k):
        raise RuntimeError(f"boom {PHONE} {SECRET}")

    monkeypatch.setattr(target, attr, boom)
    result = gate.can_send(d, TUE_10)
    assert_refused_as_error(result)


def test_a_failing_draft_object_is_still_a_refusal(app):
    class Broken:
        id = 5

        @property
        def channel(self):
            raise RuntimeError("cannot read")

    assert_refused_as_error(gate.can_send(Broken(), TUE_10))
    assert_refused_as_error(gate.can_send(None, TUE_10))
    assert_refused_as_error(gate.can_send(object(), TUE_10))


def test_the_audit_write_itself_can_never_raise(app, monkeypatch):
    d = ready_cloud_draft()
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    monkeypatch.setattr(settings, "all_values", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("settings down")))
    assert_refused_as_error(gate.can_send(d, TUE_10))  # error path with the audit write also failing
    monkeypatch.undo()
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    d2 = draft(prospect(phone="2348022222222"), channel="wa_manual", status="draft", approved_by=None)
    r = gate.can_send(d2, TUE_10)  # an ordinary refusal whose audit entry cannot be written keeps its own reason
    assert (r.ok, r.code) == (False, "not_approved")


def test_the_gate_error_audit_entry_has_no_personal_data(app, monkeypatch):
    d = ready_cloud_draft()
    monkeypatch.setattr(service, "is_opted_out", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"{PHONE} {SECRET}")))
    gate.can_send(d, TUE_10)
    e = AuditEvent.query.filter_by(action="gate_error").one()
    assert e.detail_dict == {"code": "gate_error", "error_type": "RuntimeError", "channel": "whatsapp_cloud"}
    assert e.entity == "draft" and e.entity_id == str(d.id) and e.actor == "system"
    assert PHONE not in e.detail and SECRET not in e.detail


def test_the_session_is_usable_again_after_an_error(app):
    d = ready_cloud_draft()
    corrupt("enabled", "garbage")
    assert_refused_as_error(gate.can_send(d, TUE_10))
    settings.set_many({"enabled": True})  # the owner overwrites the corrupt row
    assert gate.can_send(d, TUE_10).ok is True


# --- the other checks a sender would call --------------------------------------------------------------------------------------

def test_the_public_checks_fail_closed_when_the_database_is_unreachable(app):
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    assert consent.has_marketing_opt_in(PHONE) is True and suppression.is_suppressed(PHONE) is False
    assert spend.spend_allowed() is True and clock.in_send_hours(TUE_10) is True

    def refuse(conn, cursor, statement, parameters, context, executemany):
        raise OperationalError(statement, parameters, Exception("connection refused"))

    event.listen(db.engine, "before_cursor_execute", refuse)
    try:
        assert settings.is_killed() is True                  # cannot read the stop switch: treated as stopped
        assert settings.email_auto_effective() is False
        assert consent.has_marketing_opt_in(PHONE) is False  # cannot read consent: no opt-in
        assert suppression.is_suppressed(PHONE) is True      # cannot read the register: suppressed
        assert spend.spend_allowed() is False                # cannot read spend: hard stop
        assert counters.review_pending() is True             # cannot count sends: automated sends stay paused
        assert clock.in_send_hours(TUE_10) is False          # cannot read the hours setting: outside hours
    finally:
        event.remove(db.engine, "before_cursor_execute", refuse)


def test_the_public_checks_behave_normally_when_nothing_is_broken(app):
    assert settings.is_killed() is False
    assert settings.email_auto_effective() is False
    assert consent.has_marketing_opt_in(PHONE) is False
    assert suppression.is_suppressed(PHONE) is False
    service.record_opt_out(PHONE)
    db.session.commit()
    assert suppression.is_suppressed(PHONE) is True
    assert spend.spend_allowed() is True and counters.review_pending() is False
    assert clock.in_send_hours(TUE_10) is True and clock.in_send_hours(datetime(2026, 11, 14, 9, 0)) is False


def test_the_unwrapped_checks_still_raise_so_the_gate_can_name_the_error(app, monkeypatch):
    monkeypatch.setattr(service, "phone_key", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        consent.has_marketing_opt_in.raw(PHONE)
    assert consent.has_marketing_opt_in(PHONE) is False


# --- the owner page can still open and fix a corrupt value --------------------------------------------------------------------

def test_the_owner_page_opens_with_a_corrupt_value_and_saving_repairs_it(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    corrupt("enabled", '"false"')
    html = c.get("/outbound").data.decode()
    assert "Corrupt stored setting(s): enabled" in html
    form = {"csrf": "t", "approval_required": "on", "gate_counts_manual": "on", "channel_manual": "on",
            "pause_automated_after_gate": "on"}
    for key in ("weekly_target_total", "weekly_target_erp", "weekly_target_ehr", "max_pending", "draft_expiry_days", "gate_count",
                "gate_counts", "daily_cap_total", "daily_cap_campaign", "send_days", "send_start", "send_end", "timezone",
                "spend_cap_usd", "retention_days", "legal_advice_date", "legal_advice_note"):
        form[key] = str(settings.SPEC[key][1] if settings.SPEC[key][1] is not None else "")
    assert c.post("/outbound/settings", data=form).status_code == 302  # "enabled" unticked = off
    assert settings.corrupt_keys() == [] and settings.get("enabled") is False
