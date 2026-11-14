"""Outbound prospecting, Phase 1 (foundations): tables, kill switch, audit, single suppression list, settings, email lock,
and 'a message to the official line is a service reply only'. Nothing here sends anything."""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

from sentinel import create_app, service
from sentinel.models import Lead, OptOut, db, utcnow
from sentinel.outbound import approval, audit, campaigns, consent, counters, gate, settings, spend
from sentinel.outbound.models import (AuditEvent, Campaign, ConsentRecord, Draft, OutboundSetting, OutboundSpend,
                                      Prospect, ProspectSource, ReviewReport)
from test_webhook_inbound import post, text_value

PHONE = "2348011111111"
# Tuesday 17 Nov 2026, 10:00 in Lagos (UTC+1) = 09:00 UTC
TUE_10 = datetime(2026, 11, 17, 9, 0)



def _csrf_rejected(resp):
    """CSRF failures redirect to the login page (never a raw 400) and do nothing."""
    return resp.status_code == 302 and resp.headers["Location"].endswith("/login")

def campaign(key="erp"):
    return Campaign.query.filter_by(key=key).one()


def prospect(phone=PHONE, key="erp", name="Acme Pharmacy"):
    p = Prospect(campaign_id=campaign(key).id, org_name=name, business_phone=phone)
    db.session.add(p)
    db.session.commit()
    return p


def draft(p=None, channel="wa_manual", kind="first_touch", status="approved", **kw):
    p = p or prospect()
    kw.setdefault("approved_by", "owner" if status == "approved" else None)
    d = Draft(prospect_id=p.id, campaign_id=p.campaign_id, channel=channel, kind=kind, status=status, body="x", **kw)
    db.session.add(d)
    db.session.commit()
    return d


def sent_draft(p=None, kind="first_touch", mode="manual", channel="wa_manual", at=TUE_10):
    return draft(p, channel=channel, kind=kind, status="sent", sent_at=at, sent_mode=mode)


def turn_on(**more):
    settings.set_many(dict(enabled=True, **more))


def audit_actions():
    return [e.action for e in AuditEvent.query.order_by(AuditEvent.id)]


# --- 1. tables, settings, additive migration -----------------------------------------------------------------------------

def test_new_tables_exist_and_there_is_no_second_suppression_table(app):
    names = set(inspect(db.engine).get_table_names())
    assert {"outbound_campaigns", "outbound_sources", "outbound_prospects", "outbound_drafts", "outbound_consents",
            "outbound_settings", "outbound_audit", "outbound_review_reports", "outbound_spend", "opt_outs"} <= names
    assert not [n for n in names if "suppress" in n or "dnc" in n or "do_not" in n]


def test_phase_1_collects_no_prospect_data(app):
    for model in (Prospect, Draft, ConsentRecord, ProspectSource, ReviewReport, OutboundSpend):
        assert model.query.count() == 0


def test_two_inactive_campaigns_are_seeded_once_and_never_overwritten(app):
    assert {c.key for c in Campaign.query} == {"erp", "ehr"}
    assert all(c.active is False for c in Campaign.query)
    assert "Pharmacies" in json.loads(campaign("erp").segments) and "Clinics" in json.loads(campaign("ehr").segments)
    campaign("erp").segments = '["mine"]'
    db.session.commit()
    assert campaigns.ensure_campaigns() == 0 and json.loads(campaign("erp").segments) == ["mine"]


def test_migration_is_additive_on_an_existing_database(tmp_path):
    """Rebuild the database as it was BEFORE this change (no outbound tables, no opt_outs, no new lead columns), with data in
    it, then start the app on it: the columns and tables are added and nothing is lost."""
    import sqlalchemy as sa
    uri = f"sqlite:///{tmp_path / 'old.db'}"
    eng = sa.create_engine(uri)
    db.metadata.create_all(eng)
    with eng.begin() as c:
        for table in [t for t in inspect(eng).get_table_names() if t.startswith("outbound_")] + ["opt_outs"]:
            c.execute(text(f"drop table {table}"))
        c.execute(text("alter table leads drop column origin"))
        c.execute(text("alter table leads drop column prospect_id"))
        c.execute(text("alter table messages add column note_for_test integer"))  # an extra column nobody asked us to drop
        c.execute(text("insert into leads (id, phone, name, product_interest, status, source, created_at, followup_stage, "
                       "followup_paused, followup_failures, reminder_count, awaiting_choice, needs_owner, demo_offer_open, "
                       "demo_offer_ignored, is_existing_client, upgrade_enquiry, partner_enquiry, last_question_age, "
                       "opted_out, followups_off) values (1, '2348011111111', 'Keep Me', 'unsure', 'new', 'whatsapp', "
                       "current_timestamp, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0)"))
        c.execute(text("create table keepme (x integer)"))
    assert "origin" not in {c["name"] for c in inspect(eng).get_columns("leads")}
    eng.dispose()
    cfg = {"SQLALCHEMY_DATABASE_URI": uri, "SCHEDULER_ENABLED": False, "TESTING": True}
    app = create_app(cfg)
    with app.app_context():
        cols = {c["name"] for c in inspect(db.engine).get_columns("leads")}
        assert {"origin", "prospect_id"} <= cols
        lead = Lead.query.one()
        assert lead.name == "Keep Me" and lead.origin == "inbound" and lead.prospect_id is None and lead.opted_out is True
        tables = set(inspect(db.engine).get_table_names())
        assert "keepme" in tables and "outbound_audit" in tables
        assert "note_for_test" in {c["name"] for c in inspect(db.engine).get_columns("messages")}
        assert OptOut.query.count() == 1  # the earlier backfill still ran on the old data
        create_app(cfg)  # a second start changes nothing
        assert Lead.query.count() == 1 and Campaign.query.count() == 2
        db.session.remove()
        db.drop_all()


def test_every_setting_default_from_the_brief(app):
    v = settings.all_values()
    assert (v["weekly_target_total"], v["weekly_target_erp"], v["weekly_target_ehr"]) == (20, 10, 10)
    assert (v["gate_count"], v["approval_required"], v["gate_counts"], v["gate_counts_manual"]) == (40, True, "first_touch", True)
    assert v["followups_need_approval"] is False and v["pause_automated_after_gate"] is True
    assert (v["daily_cap_total"], v["daily_cap_campaign"]) == (8, 4)
    assert (v["send_days"], v["send_start"], v["send_end"], v["timezone"]) == ("0,1,2,3,4", "09:00", "17:00", "Africa/Lagos")
    assert v["spend_cap_usd"] == 30.0 and v["retention_days"] == 90
    assert settings.APPROVERS == ("owner",)
    assert OutboundSetting.query.count() == 0  # defaults live in code; nothing is written until someone changes a setting


def test_settings_are_validated_and_saved_atomically(app):
    with pytest.raises(settings.SettingError):
        settings.set_many({"weekly_target_erp": 15})  # 15 + 10 != 20
    with pytest.raises(settings.SettingError):
        settings.set_many({"daily_cap_campaign": 9})  # above the total
    with pytest.raises(settings.SettingError):
        settings.set_many({"send_start": "18:00"})
    with pytest.raises(settings.SettingError):
        settings.set_many({"gate_count": 50, "spend_cap_usd": 0})  # second field bad: the first must not be saved either
    assert settings.get("gate_count") == 40 and OutboundSetting.query.count() == 0
    settings.set_many({"weekly_target_total": 30, "weekly_target_erp": 20, "weekly_target_ehr": 10})
    assert settings.get("weekly_target_erp") == 20
    with pytest.raises(settings.SettingError):
        settings.set_many({"review_ack_at": "x"})  # only the Reviewed button sets it


# --- 2. kill switch ------------------------------------------------------------------------------------------------------

def test_outbound_is_off_and_the_kill_switch_is_not_engaged_by_default(app):
    assert settings.get("enabled") is False and settings.get("kill") is False and settings.is_killed() is False
    d = draft(channel="wa_manual")
    r = gate.can_send(d, TUE_10)
    assert (r.ok, r.code) == (False, "disabled")


def test_the_kill_switch_stops_everything_immediately(app):
    turn_on()
    d = draft(channel="wa_manual")
    assert gate.can_send(d, TUE_10).ok is True
    settings.engage_kill("owner")
    assert gate.can_send(d, TUE_10).code == "killed"
    assert gate.can_send(draft(channel="call"), TUE_10).code == "killed"
    settings.release_kill("owner")
    assert gate.can_send(d, TUE_10).ok is True
    assert "kill_switch.engaged" in audit_actions() and "kill_switch.released" in audit_actions()


def test_the_environment_kill_flag_overrides_the_database(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'k.db'}", "SCHEDULER_ENABLED": False,
                      "TESTING": True, "OUTBOUND_KILL": True})
    with app.app_context():
        turn_on()
        assert settings.get("kill") is False and settings.is_killed() is True
        p = prospect()
        assert gate.can_send(draft(p), TUE_10).code == "killed"
        db.session.remove()
        db.drop_all()


def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c


def test_the_owner_page_has_the_emergency_stop(app):
    assert app.test_client().get("/outbound").status_code == 302  # login required
    c = client(app)
    html = c.get("/outbound").data.decode()
    assert "EMERGENCY STOP" in html and "OFF (default)" in html and "owner only" in html
    assert _csrf_rejected(c.post("/outbound/kill", data={"action": "engage"}))  # CSRF is enforced
    assert c.post("/outbound/kill", data={"action": "engage", "csrf": "t"}).status_code == 302
    assert settings.is_killed()
    assert "Release emergency stop" in c.get("/outbound").data.decode()
    c.post("/outbound/kill", data={"action": "release", "csrf": "t"})
    assert not settings.is_killed()


# --- 3. audit log --------------------------------------------------------------------------------------------------------

def test_setting_changes_and_gate_refusals_are_audited(app):
    settings.set_many({"daily_cap_total": 9})
    e = AuditEvent.query.filter_by(action="setting.changed").one()
    assert e.actor == "owner" and e.detail_dict == {"key": "daily_cap_total", "old": 8, "new": 9}
    gate.can_send(draft(channel="wa_manual"), TUE_10)  # outbound is off, so this is refused
    blocked = AuditEvent.query.filter_by(action="gate.blocked").one()
    assert blocked.detail_dict["code"] == "disabled" and blocked.entity == "draft"


def test_audit_never_holds_personal_data_or_message_text(app):
    with pytest.raises(ValueError):
        audit.record("x", "system", detail={"phone": PHONE})
    with pytest.raises(ValueError):
        audit.record("x", "system", detail={"Body": "hello"})
    with pytest.raises(ValueError):
        audit.record("x", "nobody")
    settings.set_many({"legal_advice_date": "2026-11-01", "legal_advice_note": "Counsel said X"})
    gate.can_send(draft(), TUE_10)
    everything = " ".join(e.detail for e in AuditEvent.query)
    assert PHONE not in everything and "Counsel said X" not in everything and "Acme" not in everything


def test_audit_rows_cannot_be_changed_or_deleted(app):
    e = audit.record("test.event", "system")
    e.action = "tampered"
    with pytest.raises(RuntimeError):
        db.session.commit()
    db.session.rollback()
    db.session.delete(AuditEvent.query.filter_by(action="test.event").one())
    with pytest.raises(RuntimeError):
        db.session.commit()
    db.session.rollback()
    assert AuditEvent.query.filter_by(action="test.event").one().action == "test.event"


def test_approval_consent_and_spend_are_audited(app):
    d = draft(status="draft", approved_by=None)
    approval.approve(d, "owner")
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form", "owner")
    spend.record_spend("claude", 0.12, "research")
    counters_ok = ["draft.approved", "consent.opt_in_recorded", "spend.recorded"]
    assert all(a in audit_actions() for a in counters_ok)


# --- 4. one suppression list: the existing opt_outs register --------------------------------------------------------------

def test_the_gate_refuses_a_number_on_the_opt_out_register(app):
    turn_on()
    p = prospect()
    d = draft(p)
    assert gate.can_send(d, TUE_10).ok is True
    service.record_opt_out(PHONE)
    db.session.commit()
    assert OptOut.query.count() == 1  # the register that already exists, not a new one
    r = gate.can_send(d, TUE_10)
    assert (r.ok, r.code) == (False, "suppressed")


def test_the_register_still_blocks_after_the_lead_is_deleted(app):
    turn_on()
    lead = Lead(phone=PHONE, opted_out=True, opted_out_at=utcnow())
    db.session.add(lead)
    db.session.commit()
    service.delete_leads([lead.id])
    assert gate.can_send(draft(), TUE_10).code == "suppressed"


def test_an_unclear_number_format_is_still_blocked_by_the_failsafe_match(app):
    turn_on()
    service.record_opt_out("08011111111")
    db.session.commit()
    assert gate.can_send(draft(prospect(phone="2348011111111")), TUE_10).code == "suppressed"


def test_a_contact_with_no_phone_or_an_email_channel_cannot_pass_the_check(app):
    turn_on()
    assert gate.can_send(draft(prospect(phone=None)), TUE_10).code == "no_contact"
    assert gate.can_send(draft(channel="email_manual"), TUE_10).code == "email_suppression_missing"


def test_approval_refuses_a_suppressed_contact(app):
    service.record_opt_out(PHONE)
    db.session.commit()
    d = draft(status="draft", approved_by=None)
    with pytest.raises(approval.ApprovalError):
        approval.approve(d, "owner")
    assert d.status == "blocked"


# --- 5. the rules from the brief -----------------------------------------------------------------------------------------

def test_only_the_owner_may_approve_drafts(app):
    for who in ("staff", "system", "bot", ""):
        d = draft(status="draft", approved_by=None)
        with pytest.raises(approval.ApprovalError):
            approval.approve(d, who)
        assert d.status == "draft"
    d = draft(status="draft", approved_by=None)
    approval.approve(d, "owner")
    assert (d.status, d.approved_by) == ("approved", "owner")
    assert "draft.approve_refused" in audit_actions()


def test_a_first_touch_opener_needs_owner_approval_but_a_followup_does_not(app):
    turn_on()
    opener = draft(status="draft", approved_by=None)
    assert gate.can_send(opener, TUE_10).code == "not_approved"
    self_approved = draft(status="approved", approved_by="staff")
    assert gate.can_send(self_approved, TUE_10).code == "not_approved"
    followup = draft(kind="followup", status="draft", approved_by=None)
    assert gate.can_send(followup, TUE_10).ok is True


def test_the_count_is_messages_actually_sent_manual_included_followups_excluded(app):
    p = prospect()
    draft(p, status="draft", approved_by=None)  # drafts do not count
    draft(p, status="rejected", approved_by=None)
    draft(p, status="expired", approved_by=None)
    assert counters.counted_sends() == 0
    sent_draft(p, mode="manual")
    sent_draft(p, mode="auto", channel="whatsapp_cloud")
    sent_draft(p, kind="followup", mode="manual")  # follow-ups do not count
    assert counters.counted_sends() == 2
    settings.set_many({"gate_counts_manual": False})
    assert counters.counted_sends() == 1
    settings.set_many({"gate_counts_manual": True, "gate_counts": "all"})
    assert counters.counted_sends() == 3


def test_after_the_40th_automated_sends_pause_until_reviewed_and_approval_stays_on(app):
    turn_on(channel_wa_cloud=True)
    p = prospect()
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    for _ in range(39):
        sent_draft(p, at=TUE_10 - timedelta(days=2))
    cloud = draft(p, channel="whatsapp_cloud")
    manual = draft(p, channel="wa_manual")
    assert counters.review_pending() is False and gate.can_send(cloud, TUE_10).ok is True
    sent_draft(p, at=TUE_10 - timedelta(days=2))  # the 40th
    assert counters.review_pending() is True
    assert gate.can_send(cloud, TUE_10).code == "review_pending"
    assert gate.can_send(manual, TUE_10).ok is True  # only AUTOMATED sends pause
    with pytest.raises(settings.SettingError):
        counters.acknowledge_review("staff")
    counters.acknowledge_review("owner")
    assert counters.review_pending() is False
    assert gate.can_send(cloud, TUE_10).ok is True
    assert settings.get("approval_required") is True  # still on: Reviewed loosens nothing else
    assert gate.can_send(draft(p, channel="whatsapp_cloud", status="draft", approved_by=None), TUE_10).code == "not_approved"
    assert "review.acknowledged" in audit_actions()


def test_reviewed_does_nothing_before_the_checkpoint(app):
    with pytest.raises(settings.SettingError):
        counters.acknowledge_review("owner")


def test_the_reviewed_button_on_the_page(app):
    c = client(app)
    p = prospect()
    for _ in range(40):
        sent_draft(p)
    assert "Reviewed" in c.get("/outbound").data.decode()
    c.post("/outbound/reviewed", data={"csrf": "t"})
    assert counters.review_pending() is False


def test_daily_caps_total_8_and_per_campaign_4(app):
    turn_on()
    erp, ehr = prospect(key="erp"), prospect(phone="2348022222222", key="ehr")
    for _ in range(4):
        sent_draft(erp)
    assert gate.can_send(draft(erp), TUE_10).code == "cap_campaign"
    assert gate.can_send(draft(ehr), TUE_10).ok is True  # the other campaign still has room
    for _ in range(4):
        sent_draft(ehr)
    assert gate.can_send(draft(ehr), TUE_10).code == "cap_total"
    next_day = TUE_10 + timedelta(days=1)
    assert gate.can_send(draft(ehr), next_day).ok is True  # a new Lagos day


def test_the_lagos_day_starts_at_midnight_lagos_not_utc(app):
    turn_on()
    p = prospect()
    for _ in range(4):
        sent_draft(p, at=datetime(2026, 11, 16, 23, 30))  # 00:30 Tue in Lagos = Tuesday's cap
    assert gate.can_send(draft(p), TUE_10).code == "cap_campaign"


@pytest.mark.parametrize("when,ok", [
    (datetime(2026, 11, 17, 7, 59), False),   # Tue 08:59 Lagos
    (datetime(2026, 11, 17, 8, 0), True),     # Tue 09:00 Lagos
    (datetime(2026, 11, 17, 15, 59), True),   # Tue 16:59
    (datetime(2026, 11, 17, 16, 0), False),   # Tue 17:00 Lagos
    (datetime(2026, 11, 14, 9, 0), False),    # Saturday
    (datetime(2026, 11, 15, 9, 0), False),    # Sunday
    (datetime(2026, 11, 16, 9, 0), True),     # Monday
])
def test_send_and_call_hours_mon_to_fri_9_to_5_lagos(app, when, ok):
    turn_on()
    assert gate.can_send(draft(channel="call"), when).ok is ok


def test_the_monthly_spend_cap_is_a_hard_stop_for_automated_sends(app):
    turn_on(channel_wa_cloud=True)
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    cloud = draft(channel="whatsapp_cloud")
    assert gate.can_send(cloud, TUE_10).ok is True
    spend.record_spend("claude", 20.0)
    spend.record_spend("whatsapp", 9.99)
    assert spend.spend_allowed() and not spend.spend_allowed(extra=0.5)
    spend.record_spend("whatsapp", 0.01)
    assert not spend.spend_allowed()
    assert gate.can_send(cloud, TUE_10).code == "spend_cap"
    with pytest.raises(settings.SettingError):
        settings.set_many({"spend_cap_usd": 0})  # cannot be switched off by setting it to zero


def test_spend_is_counted_per_month(app):
    spend.record_spend("claude", 29.0)
    db.session.add(OutboundSpend(kind="claude", usd=50.0, at=utcnow() - timedelta(days=40)))
    db.session.commit()
    assert round(spend.month_total(), 2) == 29.0


def test_retention_and_expiry_defaults_are_settings(app):
    assert settings.get("retention_days") == 90 and settings.get("draft_expiry_days") == 3 and settings.get("max_pending") == 12
    settings.set_many({"retention_days": 30})
    assert settings.get("retention_days") == 30


def test_an_expired_or_already_sent_draft_is_refused(app):
    turn_on()
    assert gate.can_send(draft(expires_at=TUE_10 - timedelta(hours=1)), TUE_10).code == "expired"
    assert gate.can_send(sent_draft(), TUE_10).code == "already_sent"


def test_the_settings_form_saves_and_shows_errors(app):
    c = client(app)
    form = {"csrf": "t", "enabled": "on", "approval_required": "on", "gate_counts_manual": "on", "channel_manual": "on",
            "pause_automated_after_gate": "on"}
    for key in ("weekly_target_total", "weekly_target_erp", "weekly_target_ehr", "max_pending", "draft_expiry_days", "gate_count",
                "gate_counts", "daily_cap_total", "daily_cap_campaign", "send_days", "send_start", "send_end", "timezone",
                "spend_cap_usd", "retention_days", "legal_advice_date", "legal_advice_note"):
        form[key] = str(settings.SPEC[key][1] if settings.SPEC[key][1] is not None else "")
    form["daily_cap_total"] = "10"
    assert c.post("/outbound/settings", data=form).status_code == 302
    assert settings.get("enabled") is True and settings.get("daily_cap_total") == 10
    form["weekly_target_erp"] = "15"
    c.post("/outbound/settings", data=form)
    assert settings.get("weekly_target_erp") == 10  # refused: split no longer adds up


# --- 6. automated email is locked until legal advice is recorded ---------------------------------------------------------

def test_email_auto_is_locked_until_a_dated_legal_advice_note_is_recorded(app):
    with pytest.raises(settings.SettingError, match="locked"):
        settings.set_many({"email_auto": True})
    with pytest.raises(settings.SettingError):
        settings.set_many({"email_auto": True, "legal_advice_date": "2026-11-01"})  # a date alone is not enough
    with pytest.raises(settings.SettingError):
        settings.set_many({"email_auto": True, "legal_advice_note": "Counsel OK"})  # nor a note alone
    assert settings.get("email_auto") is False
    with pytest.raises(settings.SettingError, match="future"):
        settings.set_many({"legal_advice_date": "2999-01-01", "legal_advice_note": "x"})
    with pytest.raises(settings.SettingError):
        settings.set_many({"legal_advice_date": "not a date"})
    settings.set_many({"email_auto": True, "legal_advice_date": "2026-11-01", "legal_advice_note": "Counsel OK for B2B email"})
    assert settings.get("email_auto") is True and settings.get("legal_advice_date") == date(2026, 11, 1)


def test_recording_legal_advice_does_not_switch_email_on_by_itself(app):
    settings.set_many({"legal_advice_date": "2026-11-01", "legal_advice_note": "Counsel OK"})
    assert settings.get("email_auto") is False and settings.email_auto_effective() is False


def test_removing_the_legal_advice_record_locks_email_again(app):
    turn_on()
    settings.set_many({"email_auto": True, "legal_advice_date": "2026-11-01", "legal_advice_note": "Counsel OK"})
    assert settings.email_auto_effective() is True
    settings.set_many({"legal_advice_note": ""})
    assert settings.get("email_auto") is False and settings.email_auto_effective() is False


def test_the_gate_refuses_automated_email_when_locked_and_even_when_unlocked(app):
    turn_on()
    e = draft(channel="email")
    assert gate.can_send(e, TUE_10).code == "email_locked"
    settings.set_many({"email_auto": True, "legal_advice_date": "2026-11-01", "legal_advice_note": "Counsel OK"})
    # unlocked, but the do-not-contact list cannot check email addresses yet, so it is still refused
    assert gate.can_send(e, TUE_10).code == "email_suppression_missing"


# --- 7. a message to the official line is a service reply only ------------------------------------------------------------

def test_an_inbound_whatsapp_message_never_creates_a_marketing_opt_in(app, sent, claude):
    assert post(app, text_value(PHONE, mid="wamid.i1", body="Hello, a question about ERP")).status_code == 200
    assert Lead.query.one().origin == "inbound"
    assert ConsentRecord.query.count() == 0
    assert consent.has_marketing_opt_in(PHONE) is False
    assert consent.INBOUND_MESSAGE_PURPOSE == "service_reply_only"


def test_after_an_inbound_message_the_cloud_api_gate_still_says_no_opt_in(app, sent, claude):
    turn_on(channel_wa_cloud=True)
    post(app, text_value(PHONE, mid="wamid.i2", body="Please send me information"))
    p = prospect()
    r = gate.can_send(draft(p, channel="whatsapp_cloud"), TUE_10)
    assert (r.ok, r.code) == (False, "no_opt_in") and "service reply only" in r.reason


def test_an_inbound_message_as_evidence_is_refused(app):
    for kind in ("inbound_message", "whatsapp_inbound", "service_reply", "replied", ""):
        with pytest.raises(consent.ConsentError, match="service reply only"):
            consent.record_opt_in(PHONE, "whatsapp", kind, "they messaged us")
    assert ConsentRecord.query.count() == 0


def test_an_explicit_recorded_opt_in_works_and_a_stop_overrides_it(app):
    with pytest.raises(consent.ConsentError):
        consent.record_opt_in(PHONE, "whatsapp", "form_tick", "")  # where/how is required
    with pytest.raises(consent.ConsentError):
        consent.record_opt_in("08011111111", "whatsapp", "form_tick", "Ticked box")  # country unclear
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    assert consent.has_marketing_opt_in(PHONE) and consent.has_marketing_opt_in("+234 801 111 1111")
    assert not consent.has_marketing_opt_in(PHONE, "email")
    service.record_opt_out(PHONE)
    db.session.commit()
    assert consent.has_marketing_opt_in(PHONE) is False
    with pytest.raises(consent.ConsentError):
        consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box again")


def test_withdrawal_puts_the_number_on_the_single_do_not_contact_list(app):
    consent.record_opt_in(PHONE, "whatsapp", "signed_form", "Signed at the trade fair")
    consent.record_withdrawal(PHONE, "whatsapp")
    assert consent.has_marketing_opt_in(PHONE) is False and service.is_opted_out(PHONE)
    assert OptOut.query.one().source == "withdrawal"


def test_a_recorded_opt_in_lets_the_cloud_gate_pass(app):
    turn_on(channel_wa_cloud=True)
    consent.record_opt_in(PHONE, "whatsapp", "explicit_yes_reply", "Replied YES to 'may we send you updates?'")
    assert gate.can_send(draft(channel="whatsapp_cloud"), TUE_10).ok is True


# --- guards that were part of Phase 1 scope ---------------------------------------------------------------------------------

def test_outbound_origin_leads_never_get_the_inquiry_followups(app, sent):
    lead = Lead(phone=PHONE, name="Amaka", status="new", origin="outbound", last_inbound_at=utcnow() - timedelta(days=2))
    db.session.add(lead)
    db.session.commit()
    assert service.next_followup_time(lead) is None
    assert service.followup_block_reason(lead).kind == "permanent"
    service.send_followup(lead)
    assert sent == []


def test_nothing_in_phase_1_sends_anything(app, sent, claude):
    turn_on(channel_wa_cloud=True)
    consent.record_opt_in(PHONE, "whatsapp", "form_tick", "Ticked the box on the demo form")
    for ch in ("whatsapp_cloud", "wa_manual", "call", "email", "email_manual"):
        gate.can_send(draft(channel=ch), TUE_10)
    approval.approve(draft(status="draft", approved_by=None), "owner")
    assert sent == []  # no WhatsApp call of any kind
