"""Diagnose why a lead is not getting a reply. Run in the Render Shell:

    python scripts/diagnose.py --lead 2          # dry run: real Claude calls, nothing written, nothing sent
    python scripts/diagnose.py --lead 2 --send   # really runs the pipeline and sends the WhatsApp reply

Secrets are never printed (only "set (N chars)" / "MISSING")."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import ai, create_app, service  # noqa: E402
from sentinel.models import Lead, db  # noqa: E402

SECRETS = ("ANTHROPIC_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
           "DASHBOARD_PASSWORD", "SECRET_KEY")
PLAIN = ("WHATSAPP_PHONE_NUMBER_ID", "ANTHROPIC_MODEL", "WHATSAPP_API_VERSION", "WHATSAPP_TEMPLATE_ACK",
         "SCHEDULER_ENABLED", "OWNER_AS_LEAD")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, required=True)
    ap.add_argument("--send", action="store_true", help="really send to WhatsApp (default: dry run)")
    args = ap.parse_args()

    app = create_app({"SCHEDULER_ENABLED": False})  # never start the scheduler in a shell
    with app.app_context():
        cfg = app.config
        print("== Settings ==")
        for k in SECRETS:
            v = cfg.get(k)
            print(f"  {k}: {'set (%d chars)' % len(v) if v else 'MISSING'}")
        for k in PLAIN:
            print(f"  {k}: {cfg.get(k)!r}")
        owner = cfg.get("OWNER_WHATSAPP_NUMBER")
        print(f"  OWNER_WHATSAPP_NUMBER: {service.mask_phone(owner) if owner else 'MISSING'}")

        print("== Anthropic tiny call ==")
        try:
            r = ai._get_client().messages.create(model=cfg["ANTHROPIC_MODEL"], max_tokens=5,
                                                 messages=[{"role": "user", "content": "Say ok"}])
            print("  ok:", "".join(b.text for b in r.content if b.type == "text").strip()[:20])
        except Exception as exc:
            print("  FAILED:", ai.describe_error(exc))

        lead = db.session.get(Lead, args.lead)
        if lead is None:
            print(f"== Lead {args.lead} not found ==")
            return 1
        print(f"== Lead {lead.id} ==")
        print(f"  sender={service.mask_phone(lead.phone)} status={lead.status} paused={lead.followup_paused} "
              f"stage={lead.followup_stage} product={lead.product_interest}")
        for m in lead.messages:
            print(f"  {m.sent_at:%d %b %H:%M} {m.direction:8} {m.channel:8} status={m.status} handled={m.handled}"
                  f"{' error=' + m.error if m.error else ''}")

        print(f"== Reply pipeline ({'REAL SEND' if args.send else 'dry run, no send'}) ==")
        outcome, reason = service.handle_lead_reply(lead.id, retry=True, dry_run=not args.send,
                                                    trace=lambda line: print("  ·", line))
        print(f"RESULT: {outcome}: {reason}")
        return 0 if outcome != "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
