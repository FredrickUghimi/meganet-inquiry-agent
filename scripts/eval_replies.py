"""Try the bot on awkward, off-script customers with the REAL Claude, in dry-run mode.

    ANTHROPIC_API_KEY=... python scripts/eval_replies.py            # run all scenarios
    python scripts/eval_replies.py --only price --verbose           # scenarios whose name contains "price"
    python scripts/eval_replies.py --list                           # show the scenarios (no API key needed)

Each scenario builds a throwaway lead in a TEMPORARY database (never your real one), then runs the real pipeline with
dry_run=True: Claude is called for real, but nothing is sent to WhatsApp and nothing is written to your database. It
prints each reply with automatic checks and a PASS or FAIL summary. Exit code 1 if anything fails.

Automatic checks: no repeated bot text (similarity above 0.8), no currency amounts, no promises, under 90 words, the AI
disclosure exactly once in the conversation, no phone number or website, and the latest question is addressed (a keyword check per scenario, so a human should still skim the replies)."""
import argparse
import ast
import os
import re
import sys
import tempfile
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentinel import ai, business_facts, create_app, knowledge, service, whatsapp  # noqa: E402
from sentinel.models import Lead, Message, db, utcnow  # noqa: E402

PROMISE_RE = re.compile(r"\b(guarantee[sd]?|i promise|we promise|100%|definitely will|works today|is working today|"
                        r"will be (live|ready|available) (by|on|next|in)|free of charge|no cost)\b", re.I)
MAX_WORDS = 90
FIRST_REPLY_WORDS = knowledge.FIRST_REPLY_MAX_WORDS
# Claims the code review found NOT BUILT, UNKNOWN or only PARTLY true: never in a first product reply.
FIRST_REPLY_FORBID = re.compile(r"\b(branch(es)?|tasks?|time tracking|android|iphone|AI|artificial intelligence|pipeline|"
                                r"reminders?|SMS|24/7|offline|portal|comprehensive|entire operations|all-in-one|all in one|"
                                r"project tracking|every facility)\b", re.I)

# history: (direction, text, hours_ago); the LAST entry is the customer's latest message (hours_ago 0).
# expect: the reply must match at least one of these patterns, which shows the latest message was addressed.
OFFER = "Would you also like a personalised demonstration with our team?"
VIDEO_SENT = ("out", f"Happy to show you. This video introduces Mega-Net EHR Cloud. Mega-Net EHR Cloud demo video: "
                     f"{business_facts.PRODUCT_DEMO_VIDEOS['Mega-Net EHR Cloud']}\n\n{OFFER}", 3)
FIRST_REPLY_SCENARIOS = [
    dict(name="first ERP reply, bare inquiry", first_reply="erp_cloud", first_strict=True, lead=dict(product_interest="erp_cloud", status="new"),
         history=[("in", "Hi, I am interested in ERP Cloud", 0)], expect=[r"ERP Cloud", r"\?\s*$"]),
    dict(name="first EHR reply, bare inquiry", first_reply="ehr_cloud", first_strict=True, lead=dict(product_interest="ehr_cloud", status="new"),
         history=[("in", "Good morning, please tell me about your EHR Cloud", 0)], expect=[r"EHR Cloud", r"\?\s*$"]),
    dict(name="first EHR reply, demo request as the first message", first_reply="ehr_cloud", demo_first=True,
         lead=dict(product_interest="ehr_cloud", status="new"),
         history=[("in", "Hello, can I get a demo of EHR Cloud?", 0)], expect=[r"EHR Cloud"]),
    dict(name="first ERP reply, business and problem already given", first_reply="erp_cloud",
         lead=dict(product_interest="erp_cloud", status="new"),
         history=[("in", "Hello, I run a pharmacy in Lagos and my stock keeps getting lost. Interested in ERP Cloud", 0)],
         expect=[r"stock|inventory", r"ERP Cloud"]),
]
SCENARIOS = FIRST_REPLY_SCENARIOS + [
    dict(name="thanks plus price (the real transcript)", lead=dict(product_interest="ehr_cloud", status="engaged", demo_state="asked"),
         history=[("in", "Can I get a demo of EHR Cloud?", 3.1), VIDEO_SENT,
                  ("in", "Thank you for the demo video. How much does it cost?", 0)],
         expect=[r"welcome|glad|pleasure", r"tailored|Fredrick|team|personally"], forbid=[r"personalised demonstration"]),
    dict(name="topic switch ehr to erp", lead=dict(product_interest="ehr_cloud", status="engaged"),
         history=[("in", "Tell me about EHR Cloud", 2), ("out", "EHR Cloud keeps patient records, billing, lab and pharmacy in one cloud system for clinics.", 1.9),
                  ("in", "Actually we also run a shop. What does ERP Cloud do for stock?", 0)],
         expect=[r"stock|inventory|ERP"]),
    dict(name="vague ok after the offer", lead=dict(product_interest="ehr_cloud", status="engaged", demo_state="asked"),
         history=[("in", "Can I get a demo of EHR Cloud?", 2), VIDEO_SENT, ("in", "ok", 0)], expect=[r"."], forbid=[r"personalised demonstration"]),
    dict(name="thumbs up", lead=dict(product_interest="ehr_cloud", status="engaged", demo_state="asked"),
         history=[("in", "Can I get a demo of EHR Cloud?", 2), VIDEO_SENT, ("in", "👍", 0)], expect=[r"."], forbid=[r"personalised demonstration"]),
    dict(name="pidgin demo request", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Abeg how far, I wan see demo for ERP o", 0)], expect=[r"youtu\.be|video|demo"]),
    dict(name="pidgin price", lead=dict(product_interest="erp_cloud", status="engaged"),
         history=[("in", "How much una dey collect for the ERP?", 0)], expect=[r"Fredrick|team|tailored|personally"]),
    dict(name="two questions at once", lead=dict(product_interest="ehr_cloud", status="engaged"),
         history=[("in", "Is it cloud based and how long does the training take?", 0)], expect=[r"cloud", r"train"]),
    dict(name="demo on a Tuesday", lead=dict(product_interest="ehr_cloud", status="engaged", demo_state="accepted"),
         history=[("in", "Can we do the personalised demo on Tuesday at 10am?", 0)], expect=[r"Wednesday"]),
    dict(name="complaint", lead=dict(product_interest="ehr_cloud", status="engaged", is_existing_client=False),
         history=[("in", "Your last message took two days and nobody called me back. This is very poor service.", 0)],
         expect=[r"sorry|apolog", r"team|Fredrick|look into|respond"]),
    dict(name="voice note", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "[voice note]", 0)], expect=[r"type|write|text|message|voice"]),
    dict(name="asks for a human", lead=dict(product_interest="ehr_cloud", status="engaged"),
         history=[("in", "Let me talk to someone please", 0)], expect=[r"Fredrick|team|personally"]),
    dict(name="repeated question", lead=dict(product_interest="erp_cloud", status="engaged"),
         history=[("in", "What is the exact price of ERP Cloud for ten users?", 3),
                  ("out", f"{business_facts.PRICING_POLICY} A short demo is a good next step.", 2.9),
                  ("in", "What is the exact price of ERP Cloud for ten users?", 0)], expect=[r"Fredrick|team|tailored|personally"]),
    dict(name="long silence", lead=dict(product_interest="ehr_cloud", status="cold"),
         history=[("in", "Tell me about EHR Cloud", 24 * 6), ("out", "EHR Cloud keeps patient records, billing, lab and pharmacy in one cloud system.", 24 * 6 - 0.1),
                  ("in", "Hi, any news?", 0)], expect=[r"welcome back|good to hear|nice to hear|hello again|again"]),
    dict(name="asks for a discount", lead=dict(product_interest="ehr_cloud", status="ready"),
         history=[("in", "Can you give us 30% off if we sign this week?", 0)], expect=[r"Fredrick|team|personally|tailored"]),
    dict(name="how does it work after ready", lead=dict(product_interest="ehr_cloud", status="ready"),
         history=[("in", "How much is it?", 4), ("out", f"{business_facts.PRICING_POLICY}", 3.9), ("in", "ok and how does it work?", 0)],
         expect=[r"record|patient|module|billing|lab|pharmacy|role|clinic"]),
    dict(name="sms reminders question", lead=dict(product_interest="ehr_cloud", status="engaged"),
         history=[("in", "Do you send SMS reminders to patients?", 0)], expect=[r"gateway", r"team|confirm"]),
    dict(name="typos and shouting", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "helo i need sofware for my clinik pls wat do u hav???", 0)], expect=[r"EHR|clinic"]),
    # --- Master Knowledge Base topics (company, other products, Academy, development, partnership, retired names) ---
    dict(name="what is Mega-Net Software", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "What is Mega-Net Software?", 0)], expect=[r"software", r"Nigeria|healthcare|business"],
         forbid=[r"https?://|\.com\.ng|\bwww\."]),
    dict(name="HospitalPro", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "What is HospitalPro and how is it different from your EHR Cloud?", 0)],
         expect=[r"HospitalPro", r"local|install|on-premise|premises", r"cloud"], forbid=[r"https?://"]),
    dict(name="PharmacyPro", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Do you have software just for my pharmacy? I heard of PharmacyPro", 0)],
         expect=[r"PharmacyPro", r"stock|inventory|dispens"], forbid=[r"₦|price is"]),
    dict(name="Academy fees and dates", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Tell me about Mega-Net Academy. How much is the Python course and when does it start?", 0)],
         expect=[r"Academy", r"team"], forbid=[r"₦|\$|\bnaira\b", r"starts? on|begins? on|enrol(?:l)?ment is open"]),
    dict(name="BuildBuddy", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "What is BuildBuddy?", 0)], expect=[r"BuildBuddy", r"coding|guidance|architecture"],
         forbid=[r"https?://"]),
    dict(name="custom software", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Can you build me a custom delivery app for my logistics company? How long will it take?", 0)],
         expect=[r"team|assess|technical"], forbid=[r"we will build|we can build it in|weeks?\b|months?\b|free"]),
    dict(name="BizLens", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Do you sell BizLens separately?", 0)], expect=[r"Business Sentinel", r"ERP Cloud", r"(?:not|n't) sold separately", r"clinic|hospital|business"],
         forbid=[r"the check\s+built\s+into"]),
    dict(name="Daily Sales Report retired", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "I used the Daily Sales Report before. Can I still get it?", 0)],
         expect=[r"no longer", r"ERP Cloud"]),
    dict(name="are you a human", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "Am I talking to a real person or a bot?", 0)], expect=[r"\bAI\b", r"team"],
         forbid=[r"I(?:'m| am) (?:a )?(?:real )?(?:human|person)"]),
    dict(name="partnership programme", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "I know many hospital owners. Do you pay people who refer customers to you?", 0)],
         expect=[r"Referral Partnership Programme", r"20%", r"team|Fredrick", r"\?\s*$"],
         forbid=[r"₦|\$|\bnaira\b", r"you will earn|you'll earn|you are (?:now )?(?:eligible|accepted)|guarantee", r"\d+% of (?:the )?(?:price|sale|ERP|EHR)",
                 r"https?://"]),
    dict(name="partnership payout terms (not defined yet)", lead=dict(product_interest="unsure", status="engaged", partner_enquiry=True),
         history=[("in", "I would like to be a referral partner", 3), ("out", "Yes, we have the Mega-Net Referral Partnership Programme. Our team will reply personally.", 2.9),
                  ("in", "When and how is the commission paid, and what counts as the eligible payment?", 0)],
         expect=[r"team|Fredrick", r"personally|confirm"], forbid=[r"within \d+ days|monthly|bank transfer|first payment only|renewal", r"₦|\$"]),
    dict(name="referred by someone", lead=dict(product_interest="unsure", status="new"),
         history=[("in", "John Okoro sent me. I run a shop and want ERP Cloud.", 0)],
         expect=[r"ERP Cloud", r"note|team"], forbid=[r"commission", r"referral (?:is|has been) (?:valid|confirmed)"]),
    dict(name="asks for a contact number", lead=dict(product_interest="unsure", status="engaged"),
         history=[("in", "What number can I message to reach someone at Mega-Net?", 0)],
         expect=[r"707\D*600\D*0030|2347076000030"], forbid=[r"\b0[89]\d{9}\b"]),
    dict(name="changes their mind", lead=dict(product_interest="ehr_cloud", status="engaged", demo_state="accepted"),
         history=[("in", "Yes I want the team demo", 2), ("out", "Great. What is your organisation called?", 1.9),
                  ("in", "Sorry, actually no, I will think about it first", 0)], expect=[r"no problem|of course|understand|fine|sure"]),
]


def check_reply(reply, scenario, prior_bot_texts):
    """[(check name, ok, detail)] for one reply."""
    words = len(reply.split())
    repeated = [t for t in prior_bot_texts if ai.similarity(reply, t) > ai.REPEAT_THRESHOLD]
    first = scenario.get("first_reply")  # the approved first product reply gets its own limits and checks
    checks = [
        ("no repeated bot text", not repeated, f"similar to an earlier bot message" if repeated else ""),
        ("no currency amounts", not ai._PRICE_RE.search(reply), ""),
        ("no promises", not (PROMISE_RE.search(reply) or ai.claims_confirmed(reply)), ""),
        (f"under {FIRST_REPLY_WORDS if first else MAX_WORDS} words", words <= (FIRST_REPLY_WORDS if first else MAX_WORDS),
         f"{words} words"),
        ("latest message addressed", all(re.search(p, reply, re.I | re.S) for p in scenario.get("expect", [r"."]))
         and not any(re.search(p, reply, re.I) for p in scenario.get("forbid", [])), ""),
    ]
    intros = len(re.findall(knowledge.AI_INTRO_RE, reply))
    checks.append(("AI disclosure once, never repeated",
                   intros == (0 if any(re.search(knowledge.AI_INTRO_RE, t) for t in prior_bot_texts) else 1),
                   f"{intros} introduction(s)"))
    unofficial = reply.replace(business_facts.OFFICIAL_WHATSAPP_NUMBER, "").replace(business_facts.OFFICIAL_WHATSAPP_LINK, "")
    checks.append(("no number, e-mail or website except the official WhatsApp contact",
                   not re.search(r"meganetsoftware|\.com\.ng|\bwww\.|\b0[789]\d{9}\b|\+\d{10,}|wa\.me", unofficial, re.I), ""))
    if first:
        own = business_facts.video_for(first)[1]
        other = [u for u in business_facts.PRODUCT_DEMO_VIDEOS.values() if u != own]
        urls = re.findall(r"https?://\S+", reply)
        n_questions = re.sub(r"https?://\S+", "", reply).count("?")  # the ?si= in the link is not a question
        body = re.sub(r"https?://\S+", "", reply).replace("flow into the accounting automatically", "")
        body = body.replace(knowledge.AI_INTRO, "")  # the required AI-disclosure sentence is not a claim about the product
        forbidden = FIRST_REPLY_FORBID.search(body)
        demo_first = scenario.get("demo_first")  # the first message was itself a demo request: a shorter, direct reply is right
        exactly_one = scenario.get("first_strict") or demo_first
        checks += [
            ("uses the customer's name", bool(re.search(r"\bAmaka\b", reply)), ""),
            ("exactly one closing question" if exactly_one else "at most one question",
             n_questions == 1 if exactly_one else n_questions <= 1, f"{n_questions} questions"),
            ("own video link, exactly as stored", urls.count(own) == 1 and not [u for u in urls if u != own and u not in other],
             f"links found: {urls}"),
            ("no other product's video", not any(u in reply for u in other), ""),
            (("Wednesday 10:00 demo offer" if demo_first else "Wednesday live-demo offer"),
             ("Wednesday" in reply and "10:00" in reply) if demo_first else knowledge.demo_line() in reply, ""),
            ("only confirmed features", not forbidden, forbidden.group(0) if forbidden else ""),
        ]
        if scenario.get("first_strict"):
            feature_sentence = next(line for line in knowledge.first_reply(first, "x").split("\n\n") if line.startswith(f"Mega-Net"))
            checks.append(("approved feature sentence kept", feature_sentence.split(". It combines ", 1)[1] in reply,
                           "the 'It combines ...' sentence was changed"))
    return checks


def build_lead(scenario, n):
    lead = Lead(phone=f"23480000{n:05d}", name="Amaka", source="whatsapp", **scenario["lead"])
    db.session.add(lead)
    db.session.flush()
    now = utcnow()
    for i, (direction, text, hours_ago) in enumerate(scenario["history"]):
        last = i == len(scenario["history"]) - 1
        db.session.add(Message(lead_id=lead.id, direction="inbound" if direction == "in" else "outbound", channel="whatsapp",
                               body=text, sent_at=now - timedelta(hours=hours_ago), handled=not last,
                               status="received" if direction == "in" else "sent", external_id=f"eval.{n}.{i}"))
    if lead.demo_state == "asked":
        lead.demo_offer_asked_at, lead.demo_offer_open = now - timedelta(hours=3), True
    db.session.commit()
    return lead


def _n(x):
    return x or 0


def collect_stats(seconds, reply):
    """Timing and token usage of the scenario that just ran (classify + every draft attempt), from the thread's last
    classify and draft info. A hidden-reasoning estimate is the output tokens that the visible reply cannot account for."""
    cinfo, dinfo = ai.last_classify_info() or {}, ai.last_draft_info() or {}
    attempts = dinfo.get("attempts") or ([dinfo["usage"]] if dinfo.get("usage") else [])
    d_in = sum(_n(u.get("input_tokens")) for u in attempts)
    d_out = sum(_n(u.get("output_tokens")) for u in attempts)
    d_read = sum(_n(u.get("cache_read_input_tokens")) for u in attempts)
    d_write = sum(_n(u.get("cache_creation_input_tokens")) for u in attempts)
    visible = round(len(reply or "") / 3.5)
    return {"seconds": seconds,
            "classify_in": _n(cinfo.get("input_tokens")), "classify_out": _n(cinfo.get("output_tokens")),
            "classify_read": _n(cinfo.get("cache_read_input_tokens")), "classify_write": _n(cinfo.get("cache_creation_input_tokens")),
            "draft_in": d_in, "draft_out": d_out, "draft_read": d_read, "draft_write": d_write, "draft_calls": len(attempts),
            "max_tokens_hits": _n(dinfo.get("max_tokens_hits")) + (1 if cinfo.get("retried") else 0),
            "reply_chars": len(reply or ""), "hidden_out_est": max(0, d_out - visible)}


def summarise(stats):
    """Totals, averages and cache hit rate over a list of collect_stats() dicts."""
    n = max(len(stats), 1)
    tot = {k: sum(s[k] for s in stats) for k in stats[0]} if stats else {}
    if not stats:
        return {"scenarios": 0}
    reads = tot["classify_read"] + tot["draft_read"]
    fresh = tot["classify_in"] + tot["draft_in"]
    writes = tot["classify_write"] + tot["draft_write"]
    return {"scenarios": len(stats), "total_seconds": tot["seconds"], "avg_seconds": tot["seconds"] / n,
            "max_seconds": max(s["seconds"] for s in stats), "input_tokens": fresh, "output_tokens": tot["classify_out"] + tot["draft_out"],
            "cache_read_tokens": reads, "cache_write_tokens": writes,
            "cache_hit_rate": reads / (reads + fresh + writes) if (reads + fresh + writes) else 0.0,
            "max_tokens_retries": tot["max_tokens_hits"], "hidden_out_est": tot["hidden_out_est"],
            "draft_out": tot["draft_out"], "avg_reply_chars": tot["reply_chars"] / n}


def run_scenario(scenario, n):
    """Run one scenario through the real pipeline in dry-run mode. Returns the drafted reply text (or None); the timing
    and token stats of the run are left in run_scenario.stats."""
    lead = build_lead(scenario, n)
    lines = []
    started = time.perf_counter()
    outcome, reason = service.handle_lead_reply(lead.id, retry=True, dry_run=True, trace=lines.append)
    seconds = time.perf_counter() - started
    reply = None
    for line in lines:
        if line.startswith("drafted reply text: "):
            reply = ast.literal_eval(line[len("drafted reply text: "):])
    run_scenario.stats = collect_stats(seconds, reply)
    return reply, outcome, reason


run_scenario.stats = None


def evaluate(app, scenarios, out=print, verbose=False):
    """Run the scenarios and print results. Returns (passed, total). Needs an app whose database is a throwaway."""
    passed = 0
    stats = []
    with app.app_context():
        for n, sc in enumerate(scenarios, 1):
            prior = [t for d, t, _ in sc["history"] if d == "out"]
            reply, outcome, reason = run_scenario(sc, n)
            st = run_scenario.stats
            stats.append(st)
            out(f"\n[{n:02d}] {sc['name']}")
            out(f"  customer: {sc['history'][-1][1]}")
            if reply is None:
                out(f"  FAIL no reply drafted ({outcome}: {reason})")
                continue
            checks = check_reply(reply, sc, prior)
            ok = all(c[1] for c in checks)
            passed += ok
            out(f"  bot:      {reply}")
            out(f"  speed:    {st['seconds']:.1f}s | classify in/out/cache-read {st['classify_in']}/{st['classify_out']}/"
                f"{st['classify_read']} | draft in/out/cache-read {st['draft_in']}/{st['draft_out']}/{st['draft_read']} "
                f"({st['draft_calls']} call(s)) | max_tokens hits {st['max_tokens_hits']} | reply {st['reply_chars']} chars, "
                f"~{st['hidden_out_est']} output tokens beyond the visible text")
            for name, good, detail in checks:
                if verbose or not good:
                    out(f"    {'ok  ' if good else 'FAIL'} {name} {detail}".rstrip())
            out(f"  => {'PASS' if ok else 'FAIL'}")
    if stats:
        t = summarise(stats)
        out(f"\nSPEED: {t['total_seconds']:.1f}s total, {t['avg_seconds']:.1f}s average, {t['max_seconds']:.1f}s slowest | "
            f"tokens: {t['input_tokens']} input, {t['output_tokens']} output, {t['cache_read_tokens']} cache-read, "
            f"{t['cache_write_tokens']} cache-write | cache hit rate {t['cache_hit_rate']:.0%} | "
            f"max_tokens retries {t['max_tokens_retries']} | draft output beyond visible text ~{t['hidden_out_est']} of {t['draft_out']}")
        out("          (change the pipeline only if the average exceeds 6 seconds or a first attempt hits max_tokens)")
    out(f"\n{'PASS' if passed == len(scenarios) else 'FAIL'}: {passed}/{len(scenarios)} scenarios passed")
    return passed, len(scenarios)


def make_eval_app():
    tmp = tempfile.mkdtemp(prefix="eval_replies_")
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp}/eval.db", "SCHEDULER_ENABLED": False,
                      "WHATSAPP_TOKEN": "eval", "WHATSAPP_PHONE_NUMBER_ID": "eval", "OWNER_WHATSAPP_NUMBER": None,
                      "SECRET_KEY": "eval"})

    def refuse(*a, **k):
        raise RuntimeError("eval_replies must never send to WhatsApp")
    whatsapp._post = refuse  # belt and braces: dry-run never reaches this
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="run scenarios whose name contains this text")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--verbose", action="store_true", help="show passing checks too")
    args = ap.parse_args(argv)
    chosen = [s for s in SCENARIOS if not args.only or args.only.lower() in s["name"].lower()]
    if args.list:
        for i, s in enumerate(chosen, 1):
            print(f"{i:02d} {s['name']}: {s['history'][-1][1]}")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set, so there is nothing to evaluate (the eval uses the real Claude). "
              "Set it and run again; use --list to see the scenarios.")
        return 0
    passed, total = evaluate(make_eval_app(), chosen, verbose=args.verbose)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
