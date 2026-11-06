"""List, export and safely delete leads (for clearing test data). Run in the Render Shell or locally:

    python scripts/manage_leads.py list
    python scripts/manage_leads.py export --out /tmp/leads_export.csv     # keep a copy first
    python scripts/manage_leads.py delete --ids 2,3                       # dry run: prints what would go, changes nothing
    python scripts/manage_leads.py delete --all-except 4,5 --yes          # really deletes, in one transaction

Deleting removes each lead with its messages, demo requests and open questions, and touches nothing else.
Message text is never printed: only ids and counts."""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import create_app, service  # noqa: E402
from sentinel.models import DemoRequest, Lead, Message, db  # noqa: E402


def parse_ids(text):
    try:
        return sorted({int(p) for p in text.replace(" ", "").split(",") if p})
    except ValueError:
        sys.exit(f"Not a comma-separated list of lead ids: {text!r}")


def cmd_list(args):
    leads = Lead.query.order_by(Lead.created_at.desc(), Lead.id.desc()).all()
    counts = service.lead_counts([l.id for l in leads])
    print(f"{'id':>4}  {'name':<22} {'phone':<16} {'status':<11} {'created':<10} {'msgs':>5} {'demos':>5}")
    for l in leads:
        c = counts[l.id]
        print(f"{l.id:>4}  {(l.name or '-')[:22]:<22} {service.mask_phone_detail(l.phone):<16} {l.status:<11} "
              f"{l.created_at:%Y-%m-%d} {c['messages']:>5} {c['demos']:>5}")
    print(f"{len(leads)} lead(s)")


LEAD_FIELDS = ("name", "phone", "email", "product_interest", "status", "source", "created_at", "is_existing_client")
MSG_FIELDS = ("direction", "channel", "body", "sent_at", "status", "external_id", "error", "choice_id", "sender")
DEMO_FIELDS = ("requested_text", "start_at", "duration", "channel", "status", "created_at", "confirmed_by",
               "contact_name", "organisation", "contact_phone")


def cmd_export(args):
    columns = ["record_type", "lead_id"] + sorted(set(LEAD_FIELDS) | set(MSG_FIELDS) | set(DEMO_FIELDS))
    n = {"lead": 0, "message": 0, "demo_request": 0}
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for kind, rows, fields in (("lead", Lead.query.order_by(Lead.id).all(), LEAD_FIELDS),
                                   ("message", Message.query.order_by(Message.lead_id, Message.id).all(), MSG_FIELDS),
                                   ("demo_request", DemoRequest.query.order_by(DemoRequest.lead_id, DemoRequest.id).all(),
                                    DEMO_FIELDS)):
            for r in rows:
                w.writerow({"record_type": kind, "lead_id": r.id if kind == "lead" else r.lead_id,
                            **{f: getattr(r, f) for f in fields}})
                n[kind] += 1
    print(f"Wrote {args.out}: {n['lead']} lead(s), {n['message']} message(s), {n['demo_request']} demo request(s).")
    print("The file contains message text and phone numbers. Keep it private.")


def choose_ids(args):
    everyone = [i for (i,) in db.session.query(Lead.id).order_by(Lead.id)]
    if args.ids is not None:
        ids = parse_ids(args.ids)
        missing = [i for i in ids if i not in everyone]
        if missing:
            sys.exit(f"No lead with id(s): {', '.join(map(str, missing))}. Nothing deleted.")
        return ids
    if args.all_except is not None:
        keep = set(parse_ids(args.all_except))
        return [i for i in everyone if i not in keep]
    return everyone


def cmd_delete(args):
    chosen = [bool(x is not None and x is not False) for x in (args.ids, args.all_except, args.all or None)]
    if sum(chosen) != 1:
        sys.exit("Choose exactly one of --ids, --all, --all-except.")
    ids = choose_ids(args)
    counts = service.lead_counts(ids)
    print(f"{'DELETING' if args.yes else 'DRY RUN (nothing will change)'}: {len(ids)} lead(s)")
    for i in ids:
        c = counts[i]
        print(f"  lead {i}: {c['messages']} message(s), {c['demos']} demo request(s), {c['questions']} open question(s)")
    print(f"Total: {sum(c['messages'] for c in counts.values())} message(s), "
          f"{sum(c['demos'] for c in counts.values())} demo request(s), "
          f"{sum(c['questions'] for c in counts.values())} open question(s).")
    if not ids:
        return
    if not args.yes:
        print("Nothing was deleted. Run `export` to keep a copy, then repeat this command with --yes to delete.")
        return
    print(service.describe_deleted(service.delete_leads(ids)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list leads, newest first").set_defaults(fn=cmd_list)
    ex = sub.add_parser("export", help="write leads, messages and demo requests to a CSV")
    ex.add_argument("--out", default="/tmp/leads_export.csv")
    ex.set_defaults(fn=cmd_export)
    de = sub.add_parser("delete", help="delete leads (dry run unless --yes)")
    de.add_argument("--ids", help="comma-separated lead ids, e.g. 2,3")
    de.add_argument("--all", action="store_true", help="every lead")
    de.add_argument("--all-except", help="every lead except these ids, e.g. 4,5")
    de.add_argument("--yes", action="store_true", help="really delete (default is a dry run)")
    de.set_defaults(fn=cmd_delete)
    args = ap.parse_args(argv)
    app = create_app({"SCHEDULER_ENABLED": False})  # never start the scheduler from a shell
    with app.app_context():
        args.fn(args)


if __name__ == "__main__":
    main()
