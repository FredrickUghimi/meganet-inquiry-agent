"""Safe lead deletion: scripts/manage_leads.py, dashboard confirmation, cleanup, fresh lead after delete."""
import csv
import importlib.util
import logging
import os
from types import SimpleNamespace

import pytest

from sentinel import service
from sentinel.models import DemoRequest, Lead, Message, OpenQuestion, db, utcnow
from test_webhook_inbound import LEAD, post, text_value

spec = importlib.util.spec_from_file_location(
    "manage_leads", os.path.join(os.path.dirname(__file__), "..", "scripts", "manage_leads.py"))
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)

SECRET = "SECRETBODY-do-not-print"


def make_lead(phone, name, messages=2, demos=1, questions=1):
    lead = Lead(phone=phone, name=name)
    db.session.add(lead)
    db.session.flush()
    for i in range(messages):
        db.session.add(Message(lead_id=lead.id, direction="inbound", channel="whatsapp", body=f"{SECRET} {i}",
                               external_id=f"wamid.{phone}.{i}", sent_at=utcnow()))
    for _ in range(demos):
        db.session.add(DemoRequest(lead_id=lead.id, status="tentative", requested_text=SECRET))
    for _ in range(questions):
        db.session.add(OpenQuestion(lead_id=lead.id, question=SECRET))
    db.session.commit()
    return lead.id


@pytest.fixture
def three(app):
    return [make_lead("2348011111111", "Amaka"), make_lead("2348022222222", "Bola", messages=3, demos=0, questions=0),
            make_lead("2348033333333", "Chidi", messages=1, demos=2, questions=0)]


def run(argv, capsys):
    capsys.readouterr()
    args = build_args(argv)
    args.fn(args)
    return capsys.readouterr().out


def build_args(argv):
    """Parse like main() does, without creating a second app."""
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list").set_defaults(fn=ml.cmd_list)
    ex = sub.add_parser("export")
    ex.add_argument("--out", default="/tmp/leads_export.csv")
    ex.set_defaults(fn=ml.cmd_export)
    de = sub.add_parser("delete")
    de.add_argument("--ids")
    de.add_argument("--all", action="store_true")
    de.add_argument("--all-except")
    de.add_argument("--yes", action="store_true")
    de.set_defaults(fn=ml.cmd_delete)
    return ap.parse_args(argv)


def totals():
    return Lead.query.count(), Message.query.count(), DemoRequest.query.count(), OpenQuestion.query.count()


# --- script ----------------------------------------------------------------------------------

def test_list_masks_phone_and_shows_counts_without_message_text(app, three, capsys):
    out = run(["list"], capsys)
    assert "234801*****11" in out and "2348011111111" not in out
    assert SECRET not in out and "Amaka" in out and "3 lead(s)" in out
    line = [l for l in out.splitlines() if "Bola" in l][0]
    assert line.split()[-2:] == ["3", "0"]  # messages, demos
    ids = [int(l.split()[0]) for l in out.splitlines()[1:4]]
    assert ids == sorted(ids, reverse=True)  # newest first (same created second: by id)


def test_mask_keeps_first_six_and_last_two():
    assert service.mask_phone_detail("2348011111111") == "234801*****11"
    assert service.mask_phone_detail("12345678") == "********"
    assert service.mask_phone_detail(None) == "-"


def test_delete_is_a_dry_run_by_default(app, three, capsys):
    before = totals()
    out = run(["delete", "--ids", f"{three[0]},{three[1]}"], capsys)
    assert totals() == before
    assert "DRY RUN" in out and f"lead {three[0]}: 2 message(s), 1 demo request(s), 1 open question(s)" in out
    assert f"lead {three[1]}: 3 message(s), 0 demo request(s)" in out
    assert "Total: 5 message(s), 1 demo request(s), 1 open question(s)." in out and "Nothing was deleted" in out
    assert SECRET not in out


def test_delete_ids_with_yes_removes_lead_and_everything_belonging_to_it(app, three, capsys, caplog):
    caplog.set_level(logging.INFO)
    a, b, c = three
    out = run(["delete", "--ids", f"{a},{b}", "--yes"], capsys)
    assert "Deleted 2 lead(s): 5 message(s), 1 demo request(s), 1 open question(s)." in out and SECRET not in out
    assert [l.id for l in Lead.query.all()] == [c]
    assert Message.query.filter(Message.lead_id.in_([a, b])).count() == 0
    assert DemoRequest.query.filter_by(lead_id=a).count() == 0 and OpenQuestion.query.filter_by(lead_id=a).count() == 0
    assert totals() == (1, 1, 2, 0)  # the surviving lead keeps all of its rows
    lines = [r.getMessage() for r in caplog.records if "lead deleted" in r.getMessage()]
    assert lines == [f"lead deleted id={a} messages=2 demo_requests=1 open_questions=1",
                     f"lead deleted id={b} messages=3 demo_requests=0 open_questions=0"]
    assert SECRET not in caplog.text and "Amaka" not in caplog.text


def test_all_except(app, three, capsys):
    a, b, c = three
    out = run(["delete", "--all-except", f"{a},{c}"], capsys)
    assert f"lead {b}:" in out and f"lead {a}:" not in out and f"lead {c}:" not in out and totals()[0] == 3
    run(["delete", "--all-except", f"{a},{c}", "--yes"], capsys)
    assert sorted(l.id for l in Lead.query.all()) == [a, c]


def test_all_with_yes_and_empty_database(app, three, capsys):
    run(["delete", "--all", "--yes"], capsys)
    assert totals() == (0, 0, 0, 0)
    out = run(["delete", "--all", "--yes"], capsys)
    assert "0 lead(s)" in out


def test_bad_arguments_change_nothing(app, three, capsys):
    before = totals()
    for argv in (["delete", "--yes"], ["delete", "--all", "--ids", "1", "--yes"], ["delete", "--ids", "9999", "--yes"],
                 ["delete", "--ids", "abc", "--yes"]):
        with pytest.raises(SystemExit):
            run(argv, capsys)
    assert totals() == before


def test_export_writes_all_three_record_types(app, three, tmp_path, capsys):
    out_file = tmp_path / "x.csv"
    out = run(["export", "--out", str(out_file)], capsys)
    assert "3 lead(s), 6 message(s), 3 demo request(s)" in out and SECRET not in out
    rows = list(csv.DictReader(open(out_file, encoding="utf-8")))
    kinds = [r["record_type"] for r in rows]
    assert (kinds.count("lead"), kinds.count("message"), kinds.count("demo_request")) == (3, 6, 3)
    assert any(r["body"].startswith(SECRET) for r in rows) and totals()[0] == 3  # export deletes nothing


def test_deleting_does_not_touch_other_tables(app, three, capsys):
    from sqlalchemy import inspect
    assert set(inspect(db.engine).get_table_names()) == {"leads", "messages", "demo_requests", "open_questions"}


# --- dashboard -------------------------------------------------------------------------------

def client(app, logged_in=True):
    c = app.test_client()
    if logged_in:
        with c.session_transaction() as s:
            s["auth"], s["csrf"] = True, "t"
    return c


def test_lead_page_has_delete_button_and_confirmation_page(app, three):
    a = three[0]
    c = client(app)
    assert f"/leads/{a}/delete" in c.get(f"/leads/{a}").data.decode() and "Delete lead" in c.get(f"/leads/{a}").data.decode()
    html = c.get(f"/leads/{a}/delete").data.decode()
    assert "Amaka" in html and "234801*****11" in html and "2348011111111" not in html
    assert "I understand this cannot be undone" in html and 'name="understand"' in html
    assert ">2<" in html and SECRET not in html


def test_delete_requires_the_checkbox(app, three):
    a = three[0]
    r = client(app).post("/leads/delete", data={"csrf": "t", "ids": [a]})
    assert r.status_code == 400 and b"I understand this cannot be undone" in r.data
    assert Lead.query.count() == 3
    ok = client(app).post("/leads/delete", data={"csrf": "t", "ids": [a], "understand": "1"}, follow_redirects=True)
    assert b"Deleted 1 lead(s): 2 message(s), 1 demo request(s), 1 open question(s)." in ok.data
    assert Lead.query.count() == 2 and db.session.get(Lead, a) is None


def test_delete_rejects_missing_csrf_and_login(app, three):
    assert client(app).post("/leads/delete", data={"ids": [three[0]], "understand": "1"}).status_code == 400
    assert client(app).post("/leads/delete", data={"csrf": "wrong", "ids": [three[0]], "understand": "1"}).status_code == 400
    assert client(app).post("/leads/delete/confirm", data={"ids": [three[0]]}).status_code == 400
    anon = client(app, logged_in=False)
    for r in (anon.get(f"/leads/{three[0]}/delete"),):
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    with anon.session_transaction() as s:
        s["csrf"] = "t"
    r = anon.post("/leads/delete", data={"csrf": "t", "ids": [three[0]], "understand": "1"})
    assert r.status_code == 302 and "/login" in r.headers["Location"] and Lead.query.count() == 3


def test_select_mode_and_delete_selected(app, three):
    a, b, c_ = three
    c = client(app)
    plain = c.get("/leads").data.decode()
    assert 'type="checkbox"' not in plain and "Select</a>" in plain
    html = c.get("/leads?select=1").data.decode()
    assert html.count('name="ids"') == 3 and "Delete selected" in html
    page = c.post("/leads/delete/confirm", data={"csrf": "t", "ids": [a, b]}).data.decode()
    assert "Delete 2 leads?" in page and "5 message(s)" in page and "I understand" in page
    r = c.post("/leads/delete", data={"csrf": "t", "ids": [a, b], "understand": "1"}, follow_redirects=True)
    assert b"Deleted 2 lead(s): 5 message(s)" in r.data and [l.id for l in Lead.query.all()] == [c_]
    empty = c.post("/leads/delete/confirm", data={"csrf": "t"}, follow_redirects=True)
    assert b"Select at least one lead" in empty.data


# --- no stale references ---------------------------------------------------------------------

def test_new_message_from_a_deleted_number_starts_a_fresh_lead_with_the_menu(app, sent, claude):
    post(app, text_value(LEAD, mid="wamid.old", body="Hello"))
    old = Lead.query.one()
    old_id = old.id
    assert old.awaiting_choice and old.menu_sent_at
    service.delete_leads([old_id])
    assert Lead.query.count() == 0 and Message.query.count() == 0
    sent.clear()
    post(app, text_value(LEAD, mid="wamid.new", body="Hello"))
    fresh = Lead.query.one()
    assert fresh.phone == LEAD and fresh.awaiting_choice and fresh.menu_sent_at
    assert Message.query.filter_by(lead_id=fresh.id, direction="inbound").count() == 1
    assert sent and sent[0][0] == "buttons"  # first-contact menu again, not "a repeat within 6 hours"


def test_same_message_id_after_delete_is_not_treated_as_duplicate(app, sent, claude):
    post(app, text_value(LEAD, mid="wamid.same", body="Hi, tell me about EHR Cloud"))
    service.delete_leads([Lead.query.one().id])
    post(app, text_value(LEAD, mid="wamid.same", body="Hi, tell me about EHR Cloud"))
    assert Lead.query.count() == 1 and Message.query.filter_by(direction="inbound").count() == 1


def test_status_event_for_a_deleted_message_is_ignored_quietly(app, sent, claude, caplog):
    post(app, text_value(LEAD, mid="wamid.a", body="Hi, tell me about EHR Cloud"))
    out_id = Message.query.filter_by(direction="outbound").one().external_id
    service.delete_leads([Lead.query.one().id])
    caplog.set_level(logging.INFO)
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"},
             "statuses": [{"id": out_id, "status": "failed", "recipient_id": LEAD,
                           "errors": [{"code": 131047, "title": "Re-engagement"}]}]}
    assert post(app, value).status_code == 200
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ignored (failed status for an unknown message id)" in r.getMessage() for r in caplog.records)
    assert Lead.query.count() == 0


def test_reminders_followups_and_sweeper_survive_deleted_leads(app, sent, claude):
    from datetime import timedelta
    post(app, text_value(LEAD, mid="wamid.a", body="Hi, EHR Cloud please"))
    lead = Lead.query.one()
    lead.followup_paused = True
    lead.next_followup_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    stale_id = lead.id
    service.delete_leads([stale_id])
    sent.clear()
    service.run_owner_reminders(now=utcnow() + timedelta(days=2))
    service.run_due_followups()
    service.run_unanswered(min_age=timedelta(0))
    assert service.handle_lead_reply(stale_id) == ("skipped", "lead not found")
    assert sent == []


def test_alert_and_demo_commands_for_a_deleted_lead_are_polite(app, sent, claude):
    lead_id = make_lead("2348099999999", "Gone")
    demo_id = DemoRequest.query.filter_by(lead_id=lead_id).one().id
    service.delete_leads([lead_id])
    assert "No demo with id" in service._demo_command("confirm", str(demo_id), "")
    assert "No lead with id" in service.owner_command(f"done {lead_id}")[1]
