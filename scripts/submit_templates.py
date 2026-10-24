"""Submit the Lead Sentinel message templates to Meta for approval.

    python scripts/submit_templates.py            # the 3 follow-up templates (Day 1/3/7)
    python scripts/submit_templates.py --all      # also the optional ack + owner-alert templates
    python scripts/submit_templates.py --dry-run  # print what would be sent

Needs WHATSAPP_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID (the WABA id, not the phone number id).
Approval is asynchronous: check status in WhatsApp Manager > Message templates.
"""
import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sentinel.templates_meta import TEMPLATES  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include the optional templates")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("WHATSAPP_TOKEN")
    waba = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID")
    version = os.environ.get("WHATSAPP_API_VERSION", "v23.0")
    lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")
    if not args.dry_run and not (token and waba):
        sys.exit("Set WHATSAPP_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID first.")

    for key, t in TEMPLATES.items():
        if t.get("optional") and not args.all:
            continue
        name = os.environ.get(t["env"]) or t["name"]
        payload = {
            "name": name, "language": lang, "category": "UTILITY",
            "components": [{"type": "BODY", "text": t["body"], "example": {"body_text": [t["example"]]}}],
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            continue
        r = requests.post(f"https://graph.facebook.com/{version}/{waba}/message_templates", json=payload,
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        print(f"{name}: HTTP {r.status_code} {r.text[:300]}")


if __name__ == "__main__":
    main()
