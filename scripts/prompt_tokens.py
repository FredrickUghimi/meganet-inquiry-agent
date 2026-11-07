"""Input tokens per message: what the system prompt costs for each kind of conversation.

    python scripts/prompt_tokens.py              # estimates (about 4 characters per token), no API key needed
    ANTHROPIC_API_KEY=... python scripts/prompt_tokens.py   # exact counts from Anthropic's token counter as well

Every message sends the whole system prompt. The first two blocks are cached (cache_control), so after the first call of a
topic they are billed at the cache-read rate; "per-message" is the part that is never cached. In production each reply's
real usage (input, cache read, cache creation, output tokens) is in the 'draft ... usage=' trace and in the logs.

BEFORE is the prompt before the tiered knowledge base (measured on the previous commit with the same method): the whole of
the lead's product notes and the voice and company notes in one uncached string."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import ai, create_app  # noqa: E402
from sentinel.models import Lead  # noqa: E402

BEFORE = {  # estimated tokens of the draft prompt / classification prompt before this change (chars / 4)
    "ehr_cloud": (5361, 4537), "erp_cloud": (5605, 4781), "unsure": (2839, 2015),
}
CASES = [  # (label, lead product, topic)
    ("EHR Cloud lead, product question", "ehr_cloud", "ehr"),
    ("ERP Cloud lead, product question", "erp_cloud", "erp"),
    ("unsure lead, general (greeting, demo, pricing)", "unsure", "general"),
    ("EHR lead asks about another product", "ehr_cloud", "other_product"),
    ("unsure lead asks about the Academy", "unsure", "academy"),
    ("unsure lead asks for custom software", "unsure", "dev_services"),
    ("unsure lead asks about a partnership", "unsure", "partnership"),
]


CACHE_READ_RATE = 0.1  # cache reads are billed at a tenth of the normal input price


def est(text):
    return (len(text) + 3) // 4


def main():
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tempfile.mkdtemp()}/t.db", "SCHEDULER_ENABLED": False})
    client = model = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client, model = anthropic.Anthropic(), app.config["ANTHROPIC_MODEL"]
    extra = ai.REPLY_RULES + "\n" + ai.SITUATIONS["continue"] + "\n" + ai.TURN_SPEC
    print(f"{'conversation':50} {'draft':>7} {'cached':>7} {'per-msg':>8} {'on cache hit':>13} {'classify':>9} {'before':>7}")
    with app.app_context():
        for label, product, topic in CASES:
            lead = Lead(phone="2348000000001", name="Amaka", product_interest=product)
            blocks = ai._system(lead, extra, topic)
            total = sum(est(b["text"]) for b in blocks)
            cached = sum(est(b["text"]) for b in blocks if "cache_control" in b)
            classify = sum(est(b["text"]) for b in ai._system(lead, ai.CLASSIFY_INSTRUCTIONS, None))
            before = BEFORE[product][0]
            hit = round(total - cached + CACHE_READ_RATE * cached)  # billed-equivalent tokens when both blocks are read from cache
            line = f"{label:50} {total:7} {cached:7} {total - cached:8} {hit:13} {classify:9} {before:7}"
            if client:
                n = client.messages.count_tokens(model=model, system=blocks,
                                                 messages=[{"role": "user", "content": "Conversation so far:\nLead: Hello"}])
                line += f"   exact draft input tokens: {n.input_tokens}"
            print(line)
    print("\ntokens are estimates (4 characters per token) unless an exact count is shown. draft = whole prompt; cached = the two "
          "cached blocks; per-msg = the never-cached rest; 'on cache hit' = per-msg + 10% of cached (billing equivalent); "
          "'before' = the draft prompt of the previous version (never cached).")


if __name__ == "__main__":
    main()
