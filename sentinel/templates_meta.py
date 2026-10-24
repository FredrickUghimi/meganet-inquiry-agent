"""WhatsApp message templates to submit to Meta (category UTILITY).

Body variables are positional. Meta rejects bodies that start or end with a variable, so the
fixed text wraps them. {{3}} is the one line Claude writes fresh on each send.
"""
import re

TEMPLATES = {
    "day1": {
        "env": "WHATSAPP_TEMPLATE_DAY1",
        "name": "meganet_followup_day1",
        "body": "Hi {{1}}, Mega-Net here, following up on your inquiry about {{2}}. {{3}} "
                "Reply to this message and we'll pick up where we left off.",
        "example": ["Amaka", "EHR Cloud", "Still deciding between a phased rollout and going live in one go?"],
    },
    "day3": {
        "env": "WHATSAPP_TEMPLATE_DAY3",
        "name": "meganet_followup_day3",
        "body": "Hi {{1}}, checking in once more on {{2}}. {{3}} "
                "A quick reply here is all it takes to continue.",
        "example": ["Amaka", "EHR Cloud", "Tell us how many staff would use it and we can scope it properly."],
    },
    "day7": {
        "env": "WHATSAPP_TEMPLATE_DAY7",
        "name": "meganet_followup_day7",
        "body": "Hi {{1}}, this is our last message about {{2}} unless you reply. {{3}} "
                "If the timing is wrong, no problem. Message us any time and we'll pick it up.",
        "example": ["Amaka", "EHR Cloud", "The offer to walk you through it still stands."],
    },
    # Optional. Needed only so form/email leads (who have not messaged on WhatsApp) can be contacted first.
    "ack": {
        "env": "WHATSAPP_TEMPLATE_ACK",
        "name": "meganet_inquiry_ack",
        "body": "Hi {{1}}, thanks for your inquiry about {{2}}. This is Mega-Net. "
                "Reply here and we'll continue the conversation on WhatsApp.",
        "example": ["Amaka", "EHR Cloud"],
        "optional": True,
    },
    # Optional. Needed only if Fredrick has not messaged the business number in the last 24h when an alert fires.
    "escalation": {
        "env": "WHATSAPP_TEMPLATE_ESCALATION",
        "name": "meganet_lead_alert",
        "body": "Lead Sentinel alert: {{1}} Open the record at {{2}} to follow up.",
        "example": ["Amaka (EHR Cloud) wants a demo.", "https://example.com/leads/1"],
        "optional": True,
    },
}


def clean_param(value, limit=300):
    """Template variables may not contain newlines, tabs or runs of 4+ spaces."""
    value = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    value = re.sub(r" {2,}", " ", value).strip()
    return value[:limit]


def render(key, params):
    body = TEMPLATES[key]["body"]
    for i, p in enumerate(params, start=1):
        body = body.replace("{{%d}}" % i, p)
    return body
