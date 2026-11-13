"""Who may approve drafts: the owner only."""
from .. import service
from ..models import db, utcnow
from . import audit, settings
from .models import Draft, Prospect


class ApprovalError(PermissionError):
    pass


def can_approve(actor):
    return actor in settings.APPROVERS


def approve(draft, actor):
    if not can_approve(actor):
        audit.record("draft.approve_refused", "system", "draft", draft.id, {"reason": "not the owner", "by": str(actor)[:20]})
        raise ApprovalError("Only the owner can approve drafts.")
    if draft.status != "draft":
        raise ApprovalError(f"A {draft.status} draft cannot be approved.")
    prospect = db.session.get(Prospect, draft.prospect_id)
    if prospect is not None and prospect.business_phone and service.is_opted_out(prospect.business_phone):
        draft.status, draft.block_reason = "blocked", "on the do-not-contact list"
        audit.record("draft.blocked", "system", "draft", draft.id, {"reason": draft.block_reason})
        raise ApprovalError("This contact is on the do-not-contact list; the draft was blocked.")
    draft.status, draft.approved_at, draft.approved_by = "approved", utcnow(), "owner"
    audit.record("draft.approved", "owner", "draft", draft.id, {"channel": draft.channel, "kind": draft.kind})
    return draft


def reject(draft, actor, reason=""):
    if not can_approve(actor):
        raise ApprovalError("Only the owner can reject drafts.")
    if draft.status not in ("draft", "approved"):
        raise ApprovalError(f"A {draft.status} draft cannot be rejected.")
    draft.status = "rejected"
    audit.record("draft.rejected", "owner", "draft", draft.id, {"reason": (reason or "")[:80]})
    return draft
