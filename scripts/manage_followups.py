"""See which follow-ups would go out next, without sending anything. Run in the Render Shell or locally:

    python scripts/manage_followups.py preview              # leads that would get a follow-up in the next 24 hours
    python scripts/manage_followups.py preview --hours 72   # look further ahead

Each line gives the lead id, the masked phone, which message (the WhatsApp template, or free text while the 24-hour window is still
open), when, and why the lead qualifies. Leads that are paused, opted out, closed, marked contacted, existing clients, partner
enquiries or handled by a person never appear. Message text and names are never printed. Nothing is sent or changed."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import create_app, service  # noqa: E402


def cmd_preview(args):
    from flask import current_app
    cfg = current_app.config
    print(f"Follow-ups: {'ON' if cfg['FOLLOWUPS_ENABLED'] else 'OFF (FOLLOWUPS_ENABLED=0): nothing will be sent'} | days "
          f"{', '.join(map(str, service.followup_stages())) or 'none'} | templates today "
          f"{service.template_followups_today()} of {cfg['FOLLOWUP_DAILY_CAP']} (FOLLOWUP_DAILY_CAP)")
    rows = service.upcoming_followups(args.hours)
    if not rows:
        print(f"No follow-up would be sent in the next {args.hours} hours.")
        return
    print(f"{'id':>4}  {'phone':<16} {'sends (UTC)':<17} {'message':<34} why")
    for r in rows:
        what = f"day{r['stage']} template {r['template']}" if r["kind"] == "template" else f"day{r['stage']} free text"
        print(f"{r['lead'].id:>4}  {service.mask_phone_detail(r['lead'].phone):<16} {r['send_at']:%Y-%m-%d %H:%M}  {what:<34} {r['why']}")
    print(f"{len(rows)} follow-up(s). Nothing was sent. Day 3 and Day 7 are Marketing templates, which cost more than Day 1.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    pv = sub.add_parser("preview", help="list the follow-ups that would be sent (sends nothing)")
    pv.add_argument("--hours", type=int, default=24, help="how far ahead to look (default 24)")
    pv.set_defaults(fn=cmd_preview)
    args = ap.parse_args(argv)
    app = create_app({"SCHEDULER_ENABLED": False})  # never start the scheduler from a shell
    with app.app_context():
        args.fn(args)


if __name__ == "__main__":
    main()
