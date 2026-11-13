"""Count opt-outs that may already have been lost to lead deletion. Prints counts only, never phone numbers or lead ids.

    python scripts/audit_optouts.py                      # what the database holds now
    python scripts/audit_optouts.py --log app.log        # also estimate losses from a saved application log

The database cannot say how many opt-outs were lost: a deleted lead takes its opt-out with it. The only trace is in the
application log, which records "lead=<id> opted out", "lead=<id> opt-out lifted" and "lead deleted id=<id>". With a log file
(for example exported from Render) this counts lead ids that opted out, were later deleted, and were never lifted. That is an
ESTIMATE: it covers only the period the log reaches, and a lead may have been re-created and opted out again.
Reads only; sends nothing and changes nothing."""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import create_app  # noqa: E402
from sentinel.models import Lead, OptOut  # noqa: E402

OPTED = re.compile(r"lead=(\d+) opted out")
LIFTED = re.compile(r"lead=(\d+) opt-out lifted")
DELETED = re.compile(r"lead deleted id=(\d+)")


def possibly_lost(lines):
    """Number of lead ids whose last opt-out event was followed by a deletion with no lift in between."""
    state = {}
    for line in lines:
        for rx, what in ((OPTED, "opted"), (LIFTED, "lifted"), (DELETED, "deleted")):
            m = rx.search(line)
            if m:
                lid = m.group(1)
                if what == "deleted":
                    if state.get(lid) == "opted":
                        state[lid] = "lost"
                else:
                    state[lid] = what
    return sum(1 for v in state.values() if v == "lost")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", help="a saved application log to scan for opt-out and delete events")
    args = ap.parse_args(argv)
    app = create_app({"SCHEDULER_ENABLED": False})
    with app.app_context():
        print(f"Opt-outs in the register now: {OptOut.query.filter_by(lifted_at=None).count()}")
        print(f"  of which flagged for review (country unclear): {OptOut.query.filter_by(lifted_at=None, needs_review=True).count()}")
        print(f"Leads currently flagged opted out: {Lead.query.filter_by(opted_out=True).count()}")
    if args.log:
        with open(args.log, errors="replace") as fh:
            print(f"Opt-outs that may already have been lost (estimate from the log): {possibly_lost(fh)}")
    else:
        print("Opt-outs possibly lost earlier cannot be counted from the database. Pass --log <file> to estimate from a log.")


if __name__ == "__main__":
    main()
