"""The 40-message checkpoint: how many openers have actually been sent, and the owner's 'Reviewed' tap."""
from ..models import utcnow
from . import audit, safety, settings
from .models import Draft


def counted_sends():
    """Number of messages ACTUALLY SENT that count toward the checkpoint. Drafts, rejections, expiries and prospects never
    count. Follow-ups count only if gate_counts is 'all'; manual 'mark as sent' counts unless gate_counts_manual is off."""
    query = Draft.query.filter(Draft.status == "sent", Draft.sent_at.isnot(None))
    if settings.get("gate_counts") == "first_touch":
        query = query.filter(Draft.kind == "first_touch")
    if not settings.get("gate_counts_manual"):
        query = query.filter(Draft.sent_mode == "auto")
    return query.count()


@safety.fail_closed(True, "review checkpoint check")
def review_pending():
    """True once the checkpoint count is reached and the owner has not yet tapped Reviewed."""
    return counted_sends() >= settings.get("gate_count") and not settings.get("review_ack_at")


def acknowledge_review(actor="owner"):
    """The owner taps Reviewed. Lifts the pause on AUTOMATED sends only. Approval stays required (approval_required is not
    touched) and nothing else is loosened."""
    if actor != "owner":
        raise settings.SettingError("Only the owner can mark the review as done.")
    if not review_pending():
        raise settings.SettingError("There is nothing to review yet.")
    settings.set_internal("review_ack_at", utcnow().isoformat(), actor)
    audit.record("review.acknowledged", actor, "review", None, {"counted_sends": counted_sends()})
