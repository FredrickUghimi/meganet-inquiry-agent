"""Password-protected dashboard (single user) and the manual intake form."""
import hmac
import secrets
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for)

from . import service
from .models import Lead, Message, db, utcnow
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


@bp.app_context_processor
def inject():
    return {"csrf_token": csrf_token, "PRODUCT_NAMES": PRODUCT_NAMES}


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


@bp.get("/leads")
@login_required
def leads():
    status = request.args.get("status")
    query = Lead.query
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(Lead.last_contact_at.desc().nullslast(), Lead.created_at.desc()).all()
    rows.sort(key=lambda l: l.status != "ready")  # stable: ready leads float to the top
    return render_template("leads.html", leads=rows, statuses=STATUS_ORDER, current=status)


@bp.get("/leads/<int:lead_id>")
@login_required
def lead_detail(lead_id):
    lead = db.session.get(Lead, lead_id) or abort(404)
    return render_template("lead.html", lead=lead)


ACTIONS = {"contacted", "closed_won", "closed_lost", "pause", "resume", "reopen"}


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
        db.session.add(Message(lead_id=lead.id, direction="outbound", channel="manual", sent_at=now,
                               body="Marked as contacted by Fredrick.", handled=True))
        lead.last_contact_at = now
        lead.followup_paused = True
        if lead.status == "new":
            lead.status = "engaged"
        lead.next_followup_at = None
    elif action in ("closed_won", "closed_lost"):
        lead.status = action
        lead.next_followup_at = None
    elif action == "pause":
        lead.followup_paused = True
        lead.next_followup_at = None
    elif action in ("resume", "reopen"):
        lead.followup_paused = False
        lead.followup_failures = 0
        if action == "reopen" or lead.status == "cold":
            lead.status, lead.followup_stage = "engaged", 0
            lead.last_inbound_at = now  # restart the Day 1/3/7 clock from today
        lead.next_followup_at = service.next_followup_time(lead)
    db.session.commit()
    flash(f"Updated: {action.replace('_', ' ')}.")
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
