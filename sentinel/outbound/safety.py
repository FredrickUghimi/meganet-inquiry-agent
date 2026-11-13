"""Fail-closed helpers. A check that a sender relies on must never raise to the sender and never answer "yes" because
something broke. On any error these log the error TYPE only (database error texts can echo the SQL parameters, which may be
phone numbers or message text) and return the refusing answer."""
import functools
import logging

from ..models import db

log = logging.getLogger("sentinel.outbound.safety")


def attr(obj, name, default=None):
    """getattr that also survives a detached or expired object whose refresh would hit a broken database."""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def reset_session():
    """After a database error the session cannot be used until it is rolled back. Never raises."""
    try:
        db.session.rollback()
    except Exception:
        pass


def log_failure(what, exc):
    log.error("outbound check %s failed with %s; refusing (details withheld)", what, type(exc).__name__)


def fail_closed(refusal, what):
    """Decorator for yes/no checks. `refusal` is the answer returned on ANY exception (the safe one). The undecorated function
    stays available as `.raw` for the gate, which reports a real error as gate_error instead of a misleading reason."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                log_failure(what, exc)
                reset_session()
                return refusal
        wrapper.raw = fn
        return wrapper
    return decorate
