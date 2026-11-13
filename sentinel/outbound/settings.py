"""Outbound settings: typed, validated, stored in the database (so they change without a redeploy) and audited.

Defaults live here. Reading an unset key returns its default. Outbound is OFF by default in two independent ways:
`enabled` is False, and `kill` (the emergency stop) is a separate switch that, when engaged, overrides everything."""
import json
import re
from datetime import date, datetime

from flask import current_app

from ..models import db, utcnow
from . import audit, safety
from .models import OutboundSetting


class SettingError(ValueError):
    """A setting value was refused. The message is shown to the owner as it is."""


class SettingCorrupt(SettingError):
    """A stored setting value is not valid JSON or has the wrong type. The gate treats this as an error and refuses."""


# key -> (type, default, label). type: bool / int / float / str / date
SPEC = {
    # master switches
    "enabled": ("bool", False, "Outbound master switch (off = nothing outbound runs)"),
    "kill": ("bool", False, "Emergency stop (on = every outbound action is refused)"),
    # targets and back-pressure
    "weekly_target_total": ("int", 20, "Qualified prospects per week (all campaigns)"),
    "weekly_target_erp": ("int", 10, "ERP Cloud campaign: per week"),
    "weekly_target_ehr": ("int", 10, "EHR Cloud campaign: per week"),
    "max_pending": ("int", 12, "Pause discovery when more prospects than this await review"),
    "draft_expiry_days": ("int", 3, "Drafts not acted on expire after this many days"),
    # approval and the review checkpoint
    "approval_required": ("bool", True, "Human approval needed for first-touch openers"),
    "gate_count": ("int", 40, "Openers sent before the review checkpoint"),
    "gate_counts": ("str", "first_touch", "What counts toward the checkpoint: first_touch or all"),
    "gate_counts_manual": ("bool", True, "Manual 'mark as sent' counts toward the checkpoint"),
    "followups_need_approval": ("bool", False, "Follow-ups (approved wording) need fresh approval"),
    "pause_automated_after_gate": ("bool", True, "After the checkpoint, pause automated sends until 'Reviewed'"),
    "review_ack_at": ("str", "", "When 'Reviewed' was tapped (set by the button only)"),
    # caps and hours
    "daily_cap_total": ("int", 8, "Most messages per day, all campaigns"),
    "daily_cap_campaign": ("int", 4, "Most messages per day, per campaign"),
    "send_days": ("str", "0,1,2,3,4", "Send and call days (0=Mon ... 6=Sun)"),
    "send_start": ("str", "09:00", "Send and call hours: from (HH:MM)"),
    "send_end": ("str", "17:00", "Send and call hours: until (HH:MM)"),
    "timezone": ("str", "Africa/Lagos", "Time zone for hours, days and caps"),
    # money and data
    "spend_cap_usd": ("float", 30.0, "Monthly Claude + WhatsApp spend cap in US$ (hard stop)"),
    "retention_days": ("int", 90, "Keep never-engaged prospect data this many days"),
    # channels
    "channel_manual": ("bool", True, "Manual channels (personal WhatsApp, manual email, calls)"),
    "channel_wa_cloud": ("bool", False, "WhatsApp Cloud API outbound (recorded marketing opt-in only)"),
    "email_auto": ("bool", False, "Automated email (LOCKED until legal advice is recorded)"),
    "legal_advice_date": ("date", None, "Legal advice received on (date)"),
    "legal_advice_note": ("str", "", "Legal advice: short note (who, what was advised)"),
}
INTERNAL = {"review_ack_at"}  # not editable through set_many
APPROVERS = ("owner",)  # who may approve drafts: fixed in code, not a setting
GATE_COUNTS_CHOICES = ("first_touch", "all")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse(key, raw):
    kind = SPEC[key][0]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise SettingError(f"{key}: expected on or off")
    if kind in ("int", "float"):
        try:
            value = int(raw) if kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise SettingError(f"{key}: expected a number") from None
        if value < 0:
            raise SettingError(f"{key}: cannot be negative")
        return value
    if kind == "date":
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            raw = raw.date()
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            raise SettingError(f"{key}: use a date like 2026-12-01") from None
    return "" if raw is None else str(raw).strip()


def _encode(value):
    return json.dumps(value.isoformat() if isinstance(value, date) else value)


def _decode(key, text):
    """Stored JSON -> typed value. Anything that is not exactly the declared type is corrupt (for example the string "false"
    for a switch, which would otherwise count as on)."""
    kind = SPEC[key][0]
    value = json.loads(text)
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if kind == "str" and isinstance(value, str):
        return value
    if kind == "date":
        if value is None:
            return None
        if isinstance(value, str):
            return date.fromisoformat(value)
    raise SettingCorrupt(f"the stored value of {key} is corrupt")


def _load(strict):
    """(values, corrupt_keys). strict=True raises SettingCorrupt on the first bad row; strict=False uses the default for a bad
    row and reports its key, so the owner page can still open and the value can be overwritten."""
    rows = {r.key: r for r in OutboundSetting.query.all()}
    out, bad = {}, []
    for key, (_, default, _) in SPEC.items():
        if key not in rows:
            out[key] = default
            continue
        try:
            out[key] = _decode(key, rows[key].value)
        except Exception as exc:
            if strict:
                raise SettingCorrupt(f"the stored value of {key} is corrupt") from exc
            out[key] = default
            bad.append(key)
    return out, bad


def all_values(strict=True):
    """Every setting with its current value (defaults filled in). Strict by default: a corrupt value raises."""
    return _load(strict)[0]


def corrupt_keys():
    return _load(False)[1]


def get(key):
    if key not in SPEC:
        raise KeyError(key)
    row = db.session.get(OutboundSetting, key)
    return _decode(key, row.value) if row else SPEC[key][1]


@safety.fail_closed(True, "kill switch read")
def is_killed():
    """Emergency stop: the database switch OR the OUTBOUND_KILL environment/config flag. If it cannot be read the answer is
    "stopped"."""
    return bool(current_app.config.get("OUTBOUND_KILL")) or bool(get("kill"))


def legal_advice_recorded(values=None):
    values = values or all_values()
    return bool(values["legal_advice_date"] and values["legal_advice_note"])


@safety.fail_closed(False, "email switch read")
def email_auto_effective():
    """Automated email can run only if it is switched on AND legal advice is recorded AND outbound is on and not killed."""
    v = all_values()
    return bool(v["email_auto"] and legal_advice_recorded(v) and v["enabled"] and not is_killed())


def _validate(values, today=None):
    """Cross-field rules on the full, merged set of values."""
    today = today or utcnow().date()
    if values["weekly_target_erp"] + values["weekly_target_ehr"] != values["weekly_target_total"]:
        raise SettingError("The ERP and EHR targets must add up to the weekly total "
                           f"({values['weekly_target_erp']} + {values['weekly_target_ehr']} is not {values['weekly_target_total']}).")
    if values["gate_count"] < 1:
        raise SettingError("gate_count must be at least 1")
    if values["gate_counts"] not in GATE_COUNTS_CHOICES:
        raise SettingError("gate_counts must be first_touch or all")
    if values["daily_cap_campaign"] > values["daily_cap_total"]:
        raise SettingError("The per-campaign daily cap cannot be higher than the total daily cap.")
    for k in ("send_start", "send_end"):
        if not _HHMM.match(values[k]):
            raise SettingError(f"{k}: use HH:MM, for example 09:00")
    if values["send_start"] >= values["send_end"]:
        raise SettingError("Send hours: the start must be before the end.")
    days = [d for d in values["send_days"].replace(" ", "").split(",") if d != ""]
    if not days or any(not d.isdigit() or int(d) > 6 for d in days):
        raise SettingError("send_days: use numbers 0 (Mon) to 6 (Sun), for example 0,1,2,3,4")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(values["timezone"])
    except Exception:
        raise SettingError("timezone: not a known time zone") from None
    if values["spend_cap_usd"] <= 0:
        raise SettingError("The monthly spend cap must be above zero (it is a hard stop).")
    if values["legal_advice_date"] and values["legal_advice_date"] > today:
        raise SettingError("The legal-advice date cannot be in the future.")
    if values["email_auto"] and not (values["legal_advice_date"] and values["legal_advice_note"]):
        raise SettingError("Automated email stays locked until you record that legal advice was received "
                           "(a date and a short note).")


def set_many(changes, actor="owner", today=None):
    """Validate and save several settings atomically; audit each change. Raises SettingError (nothing is saved)."""
    unknown = [k for k in changes if k not in SPEC]
    if unknown:
        raise SettingError(f"unknown setting {unknown[0]}")
    locked = [k for k in changes if k in INTERNAL]
    if locked:
        raise SettingError(f"{locked[0]} is set by its own button, not here")
    current, bad = _load(False)
    merged = dict(current)
    for key, raw in changes.items():
        merged[key] = _parse(key, raw)
    # Removing the legal-advice record also locks automated email again (rather than refusing).
    if (not (merged["legal_advice_date"] and merged["legal_advice_note"]) and merged["email_auto"]
            and "email_auto" not in changes):
        merged["email_auto"] = False  # (an explicit request to switch email ON without the record is refused below)
    _validate(merged, today)
    return _save(current, merged, actor, force=bad)


def _save(current, merged, actor, force=()):
    changed = [k for k in merged if merged[k] != current[k] or k in force]
    try:
        for key in changed:
            row = db.session.get(OutboundSetting, key)
            if row is None:
                db.session.add(OutboundSetting(key=key, value=_encode(merged[key]), updated_by=actor, updated_at=utcnow()))
            else:
                row.value, row.updated_by, row.updated_at = _encode(merged[key]), actor, utcnow()
            if key == "legal_advice_note":  # the note text itself is not copied into the audit log
                detail = {"key": key, "old_length": len(current[key] or ""), "new_length": len(merged[key] or "")}
            else:
                detail = {"key": key, "old": _plain(current[key]), "new": _plain(merged[key])}
            audit.record("setting.changed", actor, "setting", key, detail, commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return changed


def _plain(value):
    return value.isoformat() if isinstance(value, date) else value


def set_internal(key, value, actor):
    """For the buttons that own an internal key (for example 'Reviewed')."""
    if key not in INTERNAL:
        raise SettingError(f"{key} is not an internal setting")
    current, bad = _load(False)
    merged = dict(current, **{key: _parse(key, value)})
    return _save(current, merged, actor, force=bad)


def engage_kill(actor="owner"):
    """Emergency stop on. Immediate: read by every gate check."""
    return _toggle_kill(True, actor)


def release_kill(actor="owner"):
    return _toggle_kill(False, actor)


def _toggle_kill(on, actor):
    current, bad = _load(False)
    merged = dict(current, kill=on)
    changed = _save(current, merged, actor, force=bad)
    audit.record("kill_switch.engaged" if on else "kill_switch.released", actor, "setting", "kill", {"changed": bool(changed)})
    return changed
