"""Owner-only Outbound page: status, settings, emergency stop, 'Reviewed'. The dashboard login is the owner (single user)."""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from .. import dashboard, service
from ..models import OptOut
from . import audit, counters, settings, spend
from .models import AuditEvent

bp = Blueprint("outbound", __name__)
bp.before_request(dashboard.check_csrf)

GROUPS = (
    ("Targets and queue", ("weekly_target_total", "weekly_target_erp", "weekly_target_ehr", "max_pending", "draft_expiry_days")),
    ("Approval and the 40-message checkpoint", ("approval_required", "gate_count", "gate_counts", "gate_counts_manual",
                                                 "followups_need_approval", "pause_automated_after_gate")),
    ("Caps and hours", ("daily_cap_total", "daily_cap_campaign", "send_days", "send_start", "send_end", "timezone")),
    ("Money and data", ("spend_cap_usd", "retention_days")),
    ("Channels", ("channel_manual", "channel_wa_cloud", "email_auto")),
    ("Legal advice (unlocks automated email)", ("legal_advice_date", "legal_advice_note")),
)


def _from_form(form):
    changes = {}
    for _, keys in GROUPS:
        for key in keys:
            kind = settings.SPEC[key][0]
            changes[key] = (form.get(key) == "on") if kind == "bool" else form.get(key, "")
    changes["enabled"] = form.get("enabled") == "on"
    return changes


def _try(fn, default):
    try:
        return fn()
    except Exception:
        return default


@bp.get("/outbound")
@dashboard.login_required
def page():
    values, corrupt = settings._load(False)  # lenient: a corrupt stored value shows its default and a warning, so it can be fixed
    return render_template(
        "outbound.html", values=values, corrupt=corrupt, spec=settings.SPEC, groups=GROUPS, killed=settings.is_killed(),
        env_kill=bool(current_app.config.get("OUTBOUND_KILL")), counted=_try(counters.counted_sends, 0),
        review_pending=counters.review_pending(), spent=_try(spend.month_total, 0.0), email_effective=settings.email_auto_effective(),
        legal_recorded=settings.legal_advice_recorded(values), approvers=", ".join(settings.APPROVERS),
        suppressed=OptOut.query.filter(OptOut.lifted_at.is_(None)).count(),
        events=AuditEvent.query.order_by(AuditEvent.id.desc()).limit(20).all())


@bp.post("/outbound/settings")
@dashboard.login_required
def save_settings():
    try:
        changed = settings.set_many(_from_form(request.form), "owner")
        flash(f"Saved: {', '.join(changed)}." if changed else "No changes.")
    except settings.SettingError as exc:
        flash(f"Not saved: {exc}")
    return redirect(url_for("outbound.page"))


@bp.post("/outbound/kill")
@dashboard.login_required
def kill():
    if request.form.get("action") == "release":
        settings.release_kill("owner")
        flash("Emergency stop released. Outbound still needs its master switch on.")
    else:
        settings.engage_kill("owner")
        flash("Emergency stop ON. Every outbound action is refused.")
    return redirect(url_for("outbound.page"))


@bp.post("/outbound/reviewed")
@dashboard.login_required
def reviewed():
    try:
        counters.acknowledge_review("owner")
        flash("Marked as reviewed. Automated sends can resume; approval is still required.")
    except settings.SettingError as exc:
        flash(str(exc))
    return redirect(url_for("outbound.page"))
