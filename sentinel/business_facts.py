"""Business facts the bot may state. EDIT THIS FILE with the real answers.

A value that is still "TODO" is unknown: the bot says the team will confirm it and never guesses.
Facts are put in Claude's system prompt (see ai._system) and listed on the dashboard Settings page.
"""
TODO = "TODO"

# Structured so demo slots can be validated. days: 0=Monday ... 6=Sunday; times are local clock times in `tz`.
BUSINESS_HOURS = {"days": (0, 1, 2, 3, 4), "start": "09:00", "end": "17:00", "tz": "Africa/Lagos"}

# Days and hours Fredrick or the team can run demos, in words (e.g. "Tuesday to Thursday, 10:00 to 16:00").
# Slot validation uses BUSINESS_HOURS; this wording is what the bot may tell customers.
DEMO_DAYS_AND_HOURS = TODO

# How long a demo lasts, in minutes (a number as text, e.g. "30"). Unknown: stored as 30, never stated to customers.
DEMO_DURATION_MINUTES = TODO

# How a demo is held (e.g. "video call or WhatsApp call").
DEMO_CHANNELS = TODO

# Minimum notice in hours before a demo can start (a number as text, e.g. "4"). Unknown: 2 hours is enforced.
DEMO_NOTICE_HOURS = TODO

# What the bot may promise about confirming a pencilled-in demo (e.g. "within one working day").
DEMO_CONFIRMATION_PROMISE = TODO

# What the bot may promise when a customer complains (e.g. "Fredrick will respond within one working day").
COMPLAINT_RESPONSE_PROMISE = TODO

# When customer support is available.
SUPPORT_HOURS = TODO

# Where existing clients should go for help (e.g. "email support@... or message this number").
SUPPORT_ROUTE_FOR_EXISTING_CLIENTS = TODO

# The ONE approved sentence about pricing. Used whenever a customer asks about price.
PRICING_POLICY = ("Pricing is shared by Fredrick or a member of our team after learning about your needs.")

# How a customer gets started, in order (e.g. "Discovery call, setup, data import, training, go-live").
IMPLEMENTATION_STEPS = TODO

# Whether and how existing data can be migrated into the product.
DATA_MIGRATION = TODO

# What training is offered.
TRAINING = TODO

# Things Mega-Net does not offer or do (so the bot can say no honestly).
NOT_OFFERED = TODO

FACTS = (  # (constant name, label shown to Claude and on the Settings page)
    ("BUSINESS_HOURS", "Business hours"),
    ("DEMO_DAYS_AND_HOURS", "Demo days and hours"),
    ("DEMO_DURATION_MINUTES", "Demo duration (minutes)"),
    ("DEMO_CHANNELS", "Demo channels"),
    ("DEMO_NOTICE_HOURS", "Demo notice (hours)"),
    ("DEMO_CONFIRMATION_PROMISE", "Demo confirmation promise"),
    ("COMPLAINT_RESPONSE_PROMISE", "Complaint response promise"),
    ("SUPPORT_HOURS", "Support hours"),
    ("SUPPORT_ROUTE_FOR_EXISTING_CLIENTS", "Support route for existing clients"),
    ("PRICING_POLICY", "Pricing policy"),
    ("IMPLEMENTATION_STEPS", "Implementation steps"),
    ("DATA_MIGRATION", "Data migration"),
    ("TRAINING", "Training"),
    ("NOT_OFFERED", "Not offered"),
)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def value(name):
    return globals()[name]


def is_todo(name):
    return str(value(name)).strip().upper() == TODO


def display(name):
    v = value(name)
    if name == "BUSINESS_HOURS":
        d = sorted(v["days"])
        days = f"{_DAYS[d[0]]}-{_DAYS[d[-1]]}" if d == list(range(d[0], d[-1] + 1)) and len(d) > 1 else ", ".join(_DAYS[i] for i in d)
        return f"{days} {v['start']}-{v['end']} {v['tz']}"
    return str(v)


def todo_names():
    return [n for n, _ in FACTS if is_todo(n)]


def number(name, default):
    """A numeric fact, or `default` while it is still TODO / not a number."""
    try:
        return int(str(value(name)).strip())
    except ValueError:
        return default


def prompt_block():
    lines = []
    for name, label in FACTS:
        lines.append(f"- {label}: " + ("UNKNOWN (say the team will confirm; never guess)" if is_todo(name) else display(name)))
    return "Business facts:\n" + "\n".join(lines)
