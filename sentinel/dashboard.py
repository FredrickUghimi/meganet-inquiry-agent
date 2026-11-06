"""Password-protected dashboard (single user) and the manual intake form."""
import hmac
import secrets
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for)

from . import service, whatsapp
from .models import DemoRequest, Lead, Message, OpenQuestion, db, utcnow
from .scheduler import run_async

bp = Blueprint("dashboard", __name__)

STATUS_ORDER = ["ready", "new", "engaged", "cold", "closed_won", "closed_lost"]
PRODUCT_NAMES = {"ehr_cloud": "EHR Cloud", "erp_cloud": "ERP Cloud", "unsure": "Unsure"}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("dashboard.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@bp.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Bad CSRF token")


@bp.app_template_filter("local")
def local_time(dt):
    if not dt:
        return "—"
    tz = ZoneInfo(current_app.config["DISPLAY_TZ"])
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d %b %Y %H:%M")


@bp.app_template_filter("clock")
def clock(dt):
    from datetime import timezone
    if not dt:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(current_app.config["DISPLAY_TZ"])).strftime("%H:%M")


@bp.app_context_processor
def inject():
    return {"csrf_token": csrf_token, "PRODUCT_NAMES": PRODUCT_NAMES, "wait_label": service.wait_label,
            "whatsapp_connected": whatsapp.is_configured()}


@bp.get("/healthz")
def healthz():
    return "ok"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = current_app.config["DASHBOARD_PASSWORD"] or ""
        given = request.form.get("password", "")
        if expected and hmac.compare_digest(given.encode(), expected.encode()):
            session.clear()
            session["auth"] = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard.leads"))
        flash("Wrong password.")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard.login"))


@bp.get("/")
@login_required
def index():
    return redirect(url_for("dashboard.leads"))


def _ids_from(values):
    out = []
    for v in values:
        if str(v).isdigit():
            out.append(int(v))
    return sorted(set(out))


def _delete_page(ids, error=None):
    counts = service.lead_counts(ids)
    rows = []
    for lead_id in ids:
        lead = db.session.get(Lead, lead_id)
        if lead:
            rows.append({"id": lead.id, "name": lead.name or "(no name)", "phone": service.mask_phone_detail(lead.phone),
                         **counts[lead.id]})
    if not rows:
        flash("Nothing to delete: those leads no longer exist.")
        return redirect(url_for("dashboard.leads"))
    totals = {k: sum(r[k] for r in rows) for k in ("messages", "demos", "questions")}
    return render_template("delete_leads.html", rows=rows, totals=totals, error=error), (400 if error else 200)


@bp.get("/leads/<int:lead_id>/delete")
@login_required
def delete_lead_confirm(lead_id):
    db.session.get(Lead, lead_id) or abort(404)
    return _delete_page([lead_id])


@bp.post("/leads/delete/confirm")
@login_required
def delete_selected_confirm():
    ids = _ids_from(request.form.getlist("ids"))
    if not ids:
        flash("Select at least one lead first.")
        return redirect(url_for("dashboard.leads", select=1))
    return _delete_page(ids)


@bp.post("/leads/delete")
@login_required
def delete_leads():
    ids = _ids_from(request.form.getlist("ids"))
    if not ids:
        abort(400)
    if request.form.get("understand") != "1":
        return _delete_page(ids, error="Tick \"I understand this cannot be undone\" to delete.")
    totals = service.delete_leads(ids)
    flash(service.describe_deleted(totals))
    return redirect(url_for("dashboard.leads"))


@bp.get("/leads")
@login_required
def leads():
    status = request.args.get("status")
    query = Lead.query
    if status and status != "needs_reply":
        query = query.filter_by(status=status)
    rows = query.order_by(Lead.last_contact_at.desc().nullslast(), Lead.created_at.desc()).all()
    if status == "needs_reply":
        rows = [l for l in rows if service.reply_wait_since(l) is not None]
    rows.sort(key=lambda l: l.status != "ready")  # stable: ready leads float to the top
    rows.sort(key=lambda l: service.reply_wait_since(l) is None)  # then leads waiting for Fredrick above all
    return render_template("leads.html", leads=rows, statuses=STATUS_ORDER, current=status,
                           select=request.args.get("select") == "1")


@bp.get("/demos")
@login_required
def demos():
    from . import demo
    rows = DemoRequest.query.order_by(DemoRequest.id.desc()).all()
    days = {}
    for d in rows:
        if d.start_at and d.status in ("tentative", "confirmed"):
            days.setdefault(d.start_at.date(), []).append(d)
    groups = [(day, sorted(ds, key=lambda d: (d.status != "confirmed", d.id)),
               sum(1 for d in ds if d.status == "confirmed")) for day, ds in sorted(days.items())]
    return render_template("demos.html", demos=rows, groups=groups, capacity=demo.slots_per_session())


@bp.get("/settings")
@login_required
def settings():
    """Read-only view of the business facts the bot uses and how conversations are handled. No secrets."""
    from . import business_facts as bf
    cfg = current_app.config
    facts = [(label, bf.display(name), bf.is_todo(name)) for name, label in bf.FACTS]
    modes = [("Bot answers after a lead is ready", "yes" if cfg["BOT_ANSWERS_AFTER_READY"] else "no (old behaviour)"),
             ("Owner takeover window (hours)", cfg["OWNER_TAKEOVER_HOURS"])]
    todo = [label for label, _, is_todo in facts if is_todo]
    return render_template("settings.html", facts=facts, modes=modes, todo=todo)


@bp.get("/leads/<int:lead_id>")
@login_required
def lead_detail(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    quoted = None
    if request.args.get("quote", "").isdigit():
        q = db.session.get(Message, int(request.args["quote"]))
        if q and q.lead_id == lead.id and q.direction == "inbound":
            quoted = q
    quoted_text = ""
    if quoted:
        body = " ".join(quoted.body.split())
        quoted_text = body[:80] + ("..." if len(body) > 80 else "")
    return render_template("lead.html", lead=lead, demos=DemoRequest.query.filter_by(lead_id=lead.id).order_by(DemoRequest.id).all(),
                           questions=OpenQuestion.query.filter_by(lead_id=lead.id).order_by(OpenQuestion.id).all(),
                           human_handled=service.human_handled(lead), window_until=service.window_closes_at(lead),
                           waiting_ids=service.waiting_message_ids(lead), quoted=quoted, quoted_text=quoted_text)


@bp.post("/leads/<int:lead_id>/reply")
@login_required
def owner_reply(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    quote = None
    if request.form.get("quote", "").isdigit():
        quote = db.session.get(Message, int(request.form["quote"]))
    ok, detail = service.owner_reply(lead, request.form.get("text", ""), quote=quote)
    flash(detail)
    return redirect(url_for("dashboard.lead_detail", lead_id=lead.id))


ACTIONS = {"contacted", "closed_won", "closed_lost", "pause", "resume", "reopen", "existing_client"}


@bp.post("/leads/<int:lead_id>/action")
@login_required
def lead_action(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    action = request.form.get("action")
    if action not in ACTIONS:
        abort(400)
    now = utcnow()
    if action == "contacted":
        # You are talking to them now: log it, pause the bot's follow-ups and auto-replies.
        service.mark_contacted(lead)
    elif action == "existing_client":  # toggle: existing clients get the support route, not sales flows
        lead.is_existing_client = not lead.is_existing_client
        lead.next_followup_at = None if lead.is_existing_client else service.next_followup_time(lead)
    elif action in ("closed_won", "closed_lost"):
        lead.status = action
        lead.next_followup_at = None
    elif action == "pause":
        lead.followup_paused = True
        lead.next_followup_at = None
    elif action in ("resume", "reopen"):
        lead.followup_paused = False
        lead.handback_at = now  # the bot answers again, even if Fredrick wrote within the takeover window
        lead.followup_failures = 0
        if action == "reopen" or lead.status == "cold":
            lead.status, lead.followup_stage = "engaged", 0
            lead.last_inbound_at = now  # restart the Day 1/3/7 clock from today
        lead.next_followup_at = service.next_followup_time(lead)
    db.session.commit()
    flash(f"Updated: {action.replace('_', ' ')}.")
    return redirect(url_for("dashboard.lead_detail", lead_id=lead.id))


@bp.post("/leads/<int:lead_id>/retry")
@login_required
def retry_reply(lead_id):
    """Re-run the reply pipeline for the lead's latest inbound message, synchronously, and flash the outcome."""
    lead = db.session.get(Lead, lead_id) or abort(404)
    outcome, reason = service.handle_lead_reply(lead.id, retry=True)
    flash(f"Retry reply: {outcome}. {reason}")
    return redirect(url_for("dashboard.lead_detail", lead_id=lead.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_inquiry():
    """Manual intake, e.g. for an email inquiry Fredrick forwards in."""
    if request.method == "POST":
        name = request.form.get("name", "").strip() or None
        phone = service.normalize_phone(request.form.get("phone"))
        email = request.form.get("email", "").strip() or None
        body = request.form.get("message", "").strip()
        source = request.form.get("source") if request.form.get("source") in ("form", "email") else "form"
        if not body or not (phone or email):
            flash("A message and at least a phone number or email are required.")
            return render_template("new.html", form=request.form), 400
        lead = service.find_or_create_lead(phone=phone, email=email, name=name, source=source)
        # Not a WhatsApp message, so it must not count as opening the 24h window.
        service.record_inbound(lead, body, source)
        run_async(current_app._get_current_object(), service.handle_lead_reply, lead.id)
        flash("Inquiry logged.")
        return redirect(url_for("dashboard.lead_detail", lead_id=lead.id))
    return render_template("new.html", form={})
