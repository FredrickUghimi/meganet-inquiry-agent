"""First-contact tap-to-choose menu: wording constants, greeting detection, and reply parsing.
Wording lives here as constants so it can be changed in one place."""
import re
from datetime import timedelta

MENU_BODY = ("Hello! I'm Mega-Net Lead Sentinel, Mega-Net's AI assistant. What can we help you with? "
             "EHR Cloud is for clinics and hospitals. ERP Cloud is for business accounts, sales and stock.")
MENU_BUTTONS = (  # (id, title); WhatsApp allows 3 buttons, titles up to 20 characters
    ("ehr_cloud", "EHR Cloud"),
    ("erp_cloud", "ERP Cloud"),
    ("other", "Something else"),
)
MENU_FALLBACK_TEXT = "Reply 1 for EHR Cloud, 2 for ERP Cloud, 3 for something else."
OTHER_REPLY = ("No problem. Please describe what you need in one message and Fredrick will reply to you "
               "personally.")
OTHER_ALERT = "{name} chose 'Something else' on the welcome menu and needs your reply."
MENU_COOLDOWN = timedelta(hours=6)

MAX_BODY, MAX_TITLE = 1024, 20
assert len(MENU_BODY) <= MAX_BODY and all(len(t) <= MAX_TITLE for _, t in MENU_BUTTONS)

BUTTON_IDS = tuple(i for i, _ in MENU_BUTTONS)

# typed replies that count as a tap while a lead is awaiting a choice
TYPED_CHOICES = {"1": "ehr_cloud", "ehr": "ehr_cloud", "ehr cloud": "ehr_cloud",
                 "2": "erp_cloud", "erp": "erp_cloud", "erp cloud": "erp_cloud",
                 "3": "other", "other": "other", "something else": "other"}

_GREETING = (r"(?:hello|hi|hey|hiya|howdy|yo|greetings|good\s+(?:morning|afternoon|evening|day)|"
             r"morning|afternoon|evening|salam|hello\s+hello|hi\s+hi|hey\s+hey)")
_TAIL = r"(?:\s+(?:there|team|sir|ma'?am|madam|please|everyone|all|mega-?net(?:\s+software)?|good\s+people))*"
_OPENERS = (
    rf"{_GREETING}{_TAIL}",
    r"(?:are\s+you|is\s+anyone|is\s+anybody)\s+(?:there|here|available|online)",
    r"(?:any\s*one|any\s*body|someone|somebody)(?:\s+(?:there|here|available))?",
    r"(?:i\s+)?(?:need|want)\s+(?:some\s+)?help(?:\s+please)?",
    r"(?:can|could)\s+(?:you|someone|anyone)\s+help(?:\s+me)?(?:\s+please)?",
    r"help(?:\s+me)?(?:\s+please)?",
    rf"{_GREETING}{_TAIL}\s+(?:i\s+)?(?:need|want)\s+(?:some\s+)?help(?:\s+please)?",
    rf"{_GREETING}{_TAIL}\s+(?:is\s+)?(?:anyone|anybody)\s+(?:there|here)",
    rf"{_GREETING}{_TAIL}\s+are\s+you\s+(?:there|here)",
)
_OPENER_RE = re.compile(r"^(?:%s)$" % "|".join(_OPENERS), re.I)
_PRODUCT_RE = re.compile(r"\b(ehr|erp|emr|mega-?net\s+(?:ehr|erp)|price|pricing|cost|how\s+much|demo|quote)\b", re.I)
_START_RE = re.compile(rf"^{_GREETING}\b", re.I)
MAX_WORDS = 6


def normalize(text):
    """Lowercase, drop emoji/punctuation (keeping apostrophes and hyphens), collapse spaces."""
    text = re.sub(r"[^\w\s'\-]", " ", (text or "").lower().replace("_", " "))
    return " ".join(text.split())


def classify_opener(text):
    """Deterministic check. Returns "yes" (bare greeting / generic opener), "no" (clearly something else) or
    "unsure" (short greeting-like message the patterns do not recognise: ask Claude)."""
    raw = text or ""
    norm = normalize(raw)
    if not norm or _PRODUCT_RE.search(norm) or len(norm.split()) > MAX_WORDS or re.search(r"\d", norm):
        return "no"
    if _OPENER_RE.match(norm):
        return "yes"
    if "?" in raw:
        return "no"
    return "unsure" if _START_RE.match(norm) else "no"


def typed_choice(text):
    """The menu id a typed reply stands for ('1', 'ERP', ...), or None."""
    return TYPED_CHOICES.get(normalize(text).replace("-", " "))
