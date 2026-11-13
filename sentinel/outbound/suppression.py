"""The do-not-contact check for senders. The single suppression list is the existing `opt_outs` register (service.is_opted_out);
this is its fail-closed form: if the register cannot be read, the number counts as suppressed."""
from .. import service
from . import safety


@safety.fail_closed(True, "do-not-contact check")
def is_suppressed(phone):
    return service.is_opted_out(phone)
