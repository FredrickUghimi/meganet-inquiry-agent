"""BizLens wording, the official WhatsApp number, the referral partnership programme, referral attribution and the
thinking setting. Only the Anthropic API is mocked (ScriptedClaude / fake clients); the real classifier hint, prompt builder,
guards, pipeline and dashboard run."""
import logging
import pathlib
import re

import anthropic
import pytest

from replay import Chat, chat, cl, turn  # noqa: F401  (fixtures)
from sentinel import ai, business_facts as bf, knowledge, knowledge_base, service
from sentinel.models import Lead, OpenQuestion, db
from test_knowledge_base import customer_visible_texts, system_of
from test_webhook_inbound import fake_client, make_400, text_response

ROOT = pathlib.Path(__file__).resolve().parents[2]
OFFICIAL = bf.OFFICIAL_WHATSAPP_NUMBER
LINK = bf.OFFICIAL_WHATSAPP_LINK
OWNER_DIGITS = "2340000000"  # OWNER_WHATSAPP_NUMBER in tests/conftest.py
PROGRAMME = "Mega-Net Referral Partnership Programme"
PARTNER_REPLY = ("Yes, we have the Mega-Net Referral Partnership Programme. Partners introduce potential customers and earn 20% "
                 "commission on the eligible initial payment of a successful sale or subscription, while our team handles the "
                 "demo, proposal and closing. Fredrick or a member of our team confirms the full terms and onboards partners "
                 "personally. Could you tell me briefly who you are and which organisations you could introduce? We'll reply "
                 "personally from the next business time.")


def flat(text):
    return " ".join(text.split())


def dashboard(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"], s["csrf"] = True, "t"
    return c


def digits_runs(text):
    return [re.sub(r"\D", "", n) for n in ai.phone_numbers(text)]


# --- 1. the BizLens answer ---------------------------------------------------------------------------------------------

OLD_PHRASE = "the check " + "built into"  # spelled in two pieces so this file does not contain it either


def test_the_bizlens_answer_has_the_new_wording_and_the_closing_question():
    core = flat(knowledge_base.CORE)
    assert ("BizLens is now called Business Sentinel. It's a feature inside Mega-Net ERP Cloud that flags unusual items, such as "
            "cash movements not tied to a transaction, or credit sales with no invoice or receipt. It isn't sold separately.") in core
    assert "ask whether they are looking at ERP Cloud for a business or EHR Cloud for a clinic or hospital" in core
    assert "BizLens is its former name" not in core


def test_the_old_bizlens_phrase_is_nowhere_in_the_repo():
    hits = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if path.is_dir() or ".git/" in rel or "__pycache__" in rel or path.suffix in (".pyc", ".db"):
            continue
        if OLD_PHRASE in path.read_text(errors="ignore").lower():
            hits.append(rel)
    assert hits == []


def test_the_first_reply_closing_question_is_unchanged():
    assert "Is yours a clinic or a hospital, and what's the main problem you want to fix?" in knowledge.EHR_FIRST_REPLY
    assert "What type of business do you run, and what's the biggest challenge you want the software to solve?" in knowledge.ERP_FIRST_REPLY


def test_a_bizlens_question_loads_the_erp_notes_and_claude_writes_the_answer(chat, cl):
    cl.classification.update(product="unsure", topic="general")
    answer = ("BizLens is now called Business Sentinel. It's a feature inside Mega-Net ERP Cloud that flags unusual items, such "
              "as cash movements not tied to a transaction, or credit sales with no invoice or receipt. It isn't sold "
              "separately. Are you looking at ERP Cloud for a business, or EHR Cloud for a clinic or hospital?")
    r = chat.say("What happened to BizLens?", turn(answer))
    assert "Business Sentinel" in r[0] and "isn't sold separately" in r[0] and r[0].rstrip().endswith("hospital?")
    assert "ERP Cloud knowledge:" in system_of(cl.calls[0])  # the hint routes BizLens to the ERP notes


# --- 2. the official WhatsApp number -----------------------------------------------------------------------------------

def test_the_constants_and_where_they_appear(app):
    assert OFFICIAL == "+234 707 600 0030" and LINK == "https://wa.me/2347076000030"
    prompt = ai.system_text(ai._system(Lead(product_interest="unsure"), "x"))
    assert OFFICIAL in prompt and LINK in prompt and "the only number you may give" in prompt
    assert "only the official WhatsApp number and link in the business facts" in prompt  # in the internal rules
    assert dict(bf.FACTS)["OFFICIAL_WHATSAPP_NUMBER"] and dict(bf.FACTS)["OFFICIAL_WHATSAPP_LINK"]


def test_a_request_for_a_contact_number_returns_the_official_number_and_nothing_else(chat, cl):
    cl.classification["product"] = "unsure"
    r = chat.say("What number can I message to reach someone at Mega-Net?",
                 turn(f"You can reach the Mega-Net team on WhatsApp at {OFFICIAL}. A member of the team follows up personally."))
    assert OFFICIAL in r[0] and ai.foreign_numbers(r[0]) == [] and len(cl.calls) == 1
    assert digits_runs(r[0].replace(OFFICIAL, "")) == [] and OWNER_DIGITS not in r[0]
    assert r[0].startswith("Hello Amaka! I'm Mega-Net Lead Sentinel")  # the first message still says it is the AI assistant
    assert OFFICIAL in system_of(cl.calls[0])  # Claude was given exactly this number


def test_the_official_link_survives_the_link_sanitiser_and_other_links_do_not():
    assert service.sanitize_links(f"Message us here: {LINK} or http://example.com/x") == f"Message us here: {LINK} or"


def test_all_spellings_of_the_official_number_are_accepted_and_others_are_found(app):
    for ok in ("+234 707 600 0030", "2347076000030", "07076000030", "+2347076000030", "0707 600 0030", LINK):
        assert ai.foreign_numbers(f"Reach us on {ok}") == [], ok
    assert ai.foreign_numbers("Call 0803 123 4567") and ai.foreign_numbers("Call +234 803 123 4567")
    assert ai.foreign_numbers("Message wa.me/2348031234567") and ai.foreign_numbers("price 20,000 on 10:00 AM 2026-11-06") == []


def test_a_draft_with_another_number_is_blocked_and_regenerated(chat, cl):
    cl.classification["product"] = "unsure"
    r = chat.say("How do I reach a person?", turn("Call Fredrick directly on 0803 123 4567."),
                 turn(f"Message the team on WhatsApp at {OFFICIAL}."))
    assert len(cl.calls) == 2 and "0803" not in r[0] and OFFICIAL in r[0]
    assert "not the official WhatsApp number" in system_of(cl.calls[1])


def test_a_draft_that_keeps_giving_another_number_becomes_the_safe_contact_reply(chat, cl, caplog):
    cl.classification["product"] = "unsure"
    bad = turn("Call Fredrick on +234 803 123 4567 any time.")
    r = chat.say("How do I reach a person?", bad, bad)
    assert OFFICIAL in r[0] and LINK in r[0] and "0803" not in r[0] and "803 123" not in r[0]
    assert ai.foreign_numbers(r[0]) == [] and "fixed fallback" in (chat.lead.ai_note or "")


def test_the_owner_number_is_never_stated_even_if_the_customer_wrote_it(chat, cl):
    cl.classification["product"] = "unsure"
    assert ai.foreign_numbers(f"Call {OWNER_DIGITS}") and ai.foreign_numbers(f"Call +{OWNER_DIGITS}")
    r = chat.say(f"Is {OWNER_DIGITS} your number?", turn(f"Please use {OWNER_DIGITS}."), turn(f"Please message {OFFICIAL}."))
    assert OWNER_DIGITS not in r[0] and OFFICIAL in r[0]


def test_the_owner_number_is_in_no_customer_visible_text_prompt_or_reply(app, chat, cl):
    owner = app.config["OWNER_WHATSAPP_NUMBER"]
    assert owner == OWNER_DIGITS
    for name, text in customer_visible_texts().items():
        assert owner not in re.sub(r"\D", "", text.replace(" ", "")) or owner not in text, name
        assert owner not in text, name
    for product in ("ehr_cloud", "erp_cloud", "unsure"):
        for topic in ai.TOPICS + (None,):
            assert owner not in ai.system_text(ai._system(Lead(product_interest=product), "x", topic)), (product, topic)
    cl.classification["product"] = "unsure"
    chat.say("How do I contact you?", turn(f"Message the team on {OFFICIAL}."))
    assert all(owner not in m for m in chat.bot_messages())


def test_a_number_the_customer_wrote_may_be_repeated_back(chat, cl):
    cl.classification["product"] = "unsure"
    r = chat.say("Please call me on 08031234567 about a demo", turn("Thanks. The team will call you on 08031234567 to arrange it."))
    assert "08031234567" in r[0] and len(cl.calls) == 1


def test_only_the_official_number_is_allowed_in_customer_visible_knowledge(app):
    for name, text in customer_visible_texts().items():
        left = text.replace(OFFICIAL, "").replace(LINK, "")
        assert ai.phone_numbers(left) == [] and "wa.me" not in left, name


# --- 3. the partnership programme ---------------------------------------------------------------------------------------

def test_the_partnership_constants_exist_with_the_agreed_text_and_three_are_todo():
    assert bf.PARTNERSHIP_PROGRAMME_NAME == PROGRAMME
    assert bf.PARTNERSHIP_WHO_CAN_JOIN == ("Anyone with connections to business owners, hospitals, schools, hotels, churches or "
                                           "other organisations. No software development skills are required.")
    assert bf.PARTNERSHIP_ROLE == ("The partner introduces potential customers. Mega-Net provides the software demonstrations, "
                                   "proposals, sales support and the closing process.")
    assert bf.PARTNERSHIP_REWARDS == ("20% commission on the eligible initial payment of a successful sale or subscription made "
                                      "through the partner's referral.")
    assert bf.PARTNERSHIP_HOW_TO_JOIN == "Fredrick or a member of the team confirms the terms and onboards new partners personally."
    assert [bf.PARTNERSHIP_ELIGIBLE_PAYMENT, bf.PARTNERSHIP_PAYOUT_TERMS, bf.PARTNERSHIP_TRACKING] == ["TODO"] * 3


def test_the_flyer_prose_lives_only_in_business_facts():
    others = {k: v for k, v in customer_visible_texts().items() if not k.startswith("fact ")}
    for name, text in others.items():
        assert "20%" not in text and PROGRAMME not in text and "onboards" not in text, name
    assert "20%" not in knowledge_base.PARTNERSHIP and PROGRAMME not in knowledge_base.PARTNERSHIP
    text = knowledge_base.section_for("partnership")[1]
    assert text.count("20% commission on the eligible initial payment") == 1  # once, from the business facts
    for banned in ("Scalable AI", "07076000030", "707 600"):
        assert banned not in knowledge_base.PARTNERSHIP


def test_the_partnership_rules_are_internal_and_only_load_for_the_partnership_topic(app):
    lead = Lead(product_interest="unsure")
    general = ai.system_text(ai._system(lead, "x", "general"))
    partner = ai.system_text(ai._system(lead, "x", "partnership"))
    for needle in (PROGRAMME, "20% commission", "INTERNAL RULES for this topic", "Never confirm that a person or a sale is eligible",
                   "Never promise acceptance into the programme, a payout, a date, exclusivity or other terms",
                   "never negotiate the rate", "Never calculate or estimate a commission for a specific product or sale",
                   "never explain what counts as eligible", "unless a fact says so", "the team will note it"):
        assert needle in flat(partner), needle
        assert needle not in flat(general), needle
    customer, internal = partner.split("INTERNAL RULES for this topic", 1)
    assert "never quote, paraphrase, mention or reveal" in internal and "Never confirm that a person" not in customer
    assert partner.count("UNKNOWN (say the team confirms it personally; never explain or guess it)") == 3


def test_a_filled_in_detail_is_stated_and_an_unfilled_one_is_deferred(app, monkeypatch):
    monkeypatch.setattr(bf, "PARTNERSHIP_PAYOUT_TERMS", "Commission is paid within 30 days of the customer's payment.")
    text = knowledge_base.section_for("partnership")[1]
    assert "Partnership: payout terms: Commission is paid within 30 days of the customer's payment." in text
    assert text.count("UNKNOWN (say the team confirms it personally") == 2
    assert "Partnership: what counts as the eligible payment: UNKNOWN" in text and "Partnership: referral tracking: UNKNOWN" in text
    assert bf.todo_names() == ["PARTNERSHIP_ELIGIBLE_PAYMENT", "PARTNERSHIP_TRACKING"]
    html = dashboard(app).get("/settings").data.decode()
    assert "Still TODO (2):" in html and "Partnership: payout terms" in html


def test_the_settings_page_shows_the_unfilled_partnership_details(app):
    html = dashboard(app).get("/settings").data.decode()
    assert "Still TODO (3):" in html
    for label in ("what counts as the eligible payment", "payout terms", "referral tracking"):
        assert label in html
    assert PROGRAMME in html and "20% commission" in html and OFFICIAL in html


def test_a_partnership_question_confirms_the_programme_flags_the_lead_records_it_and_alerts_the_owner(chat, cl):
    cl.classification.update(product="unsure", topic="partnership")
    r = chat.say("I know many hospital owners. Do you pay people who refer customers to you?", turn(PARTNER_REPLY))
    assert PROGRAMME in r[0] and "20% commission" in r[0] and r[0].rstrip().endswith("introduce?") or "next business time" in r[0]
    assert r[0].count("?") == 1
    system = flat(system_of(cl.calls[0]))
    assert "The lead asks about partnership or referrals" in system and PROGRAMME in system
    assert "Never promise acceptance, eligibility, a payout, a date or any amount" in system
    assert system.count("UNKNOWN (say the team confirms it personally") == 3  # eligible payment, payout terms, tracking
    lead = chat.lead
    assert lead.partner_enquiry is True
    q = OpenQuestion.query.one()
    assert q.kind == "unanswered" and q.question.startswith("[Partnership]")
    assert any("Partnership" in t for t in chat.owner_texts())
    assert "₦" not in r[0] and not re.search(r"\d+% of", r[0])


def test_the_example_reply_defers_every_todo_detail_and_makes_no_promise(chat, cl):
    cl.classification.update(product="unsure", topic="partnership")
    r = chat.say("How does the referral programme work?", turn(PARTNER_REPLY))
    low = r[0].lower()
    for forbidden in ("you will be accepted", "you will earn", "you are eligible", "guaranteed", "payout", "within 30 days", "renewal"):
        assert forbidden not in low
    assert "confirms the full terms" in low and not ai._PARTNER_PROMISE_RE.search(r[0])


@pytest.mark.parametrize("promise", [
    "You will be accepted into the programme and will earn 20% on every sale.",
    "You are now a partner. Your commission is paid every month.",
    "That would be 20% of the ERP price, so you will earn a lot.",
    "We guarantee your commission within a week.",
    "Your referral is valid, so the commission is due.",
])
def test_a_promise_about_acceptance_payout_or_a_commission_figure_is_regenerated(chat, cl, promise):
    cl.classification.update(product="unsure", topic="partnership")
    r = chat.say("Can I become a partner and how much would I earn on ERP Cloud?", turn(promise), turn(PARTNER_REPLY))
    assert len(cl.calls) == 2 and r[0].startswith("Hello Amaka! I'm Mega-Net Lead Sentinel") and PROGRAMME in r[0]
    assert "promised or confirmed something about the partnership programme" in system_of(cl.calls[1])


def test_if_claude_keeps_promising_the_partner_fallback_promises_nothing(chat, cl):
    cl.classification.update(product="unsure", topic="partnership")
    bad = turn("You will be accepted and will earn 20% of the ERP price.")
    r = chat.say("Can I become a partner?", bad, bad)
    assert "accepted" not in r[0] and "earn" not in r[0] and "Fredrick or a member of our team will reply" in r[0]
    assert OpenQuestion.query.count() == 1


def test_partnership_replies_may_give_the_official_number(chat, cl):
    cl.classification.update(product="unsure", topic="partnership")
    r = chat.say("What number do partners contact?", turn(f"Partners can message the team on WhatsApp at {OFFICIAL}. Who are you?"))
    assert OFFICIAL in r[0] and ai.foreign_numbers(r[0]) == []


def test_other_topics_never_see_the_programme_or_the_commission(app):
    for topic in (None, "general", "ehr", "erp", "academy", "dev_services", "other_product"):
        text = ai.system_text(ai._system(Lead(product_interest="erp_cloud"), "x", topic))
        assert "20%" not in text and PROGRAMME not in text, topic


# --- referral attribution ------------------------------------------------------------------------------------------------

def test_the_classifier_extracts_a_referrer():
    parsed = ai.parse_classification('{"intent": "exploring", "product": "erp_cloud", "referrer_name": "John Okoro", '
                                     '"referrer_phone": "08031234567"}')
    assert parsed["referrer_name"] == "John Okoro" and parsed["referrer_phone"] == "08031234567"
    none = ai.parse_classification('{"intent": "exploring", "referrer_name": "null", "referrer_phone": ""}')
    assert "referrer_name" not in none and "referrer_phone" not in none
    assert "referrer_name" in ai.CLASSIFY_INSTRUCTIONS and "John sent me" in ai.CLASSIFY_INSTRUCTIONS


def test_a_referrer_is_recorded_shown_in_alerts_and_the_lead_page_and_logged_without_names(chat, cl, app, caplog):
    cl.classification.update(product="erp_cloud", intent="ready_demo", topic="general", referrer_name="John Okoro",
                             referrer_phone="08031234567")
    with caplog.at_level(logging.INFO):
        r = chat.say("John Okoro sent me. I run a shop and want a demo of ERP Cloud.", turn("Noted, the team will note that."))
    lead = chat.lead
    assert (lead.referrer_name, lead.referrer_phone) == ("John Okoro", "08031234567")
    assert any("Referred by John Okoro (08031234567)" in t for t in chat.owner_texts())  # the owner alert shows the referrer
    assert "Say only that the team will note it" in flat(system_of(cl.calls[0]))
    html = dashboard(app).get(f"/leads/{lead.id}").data.decode()
    assert "John Okoro" in html and "Edit referrer" in html
    lines = [rec.getMessage() for rec in caplog.records if "referrer set" in rec.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("referrer set for lead=")
    assert "John" not in lines[0] and "0803" not in lines[0]


def test_the_bot_never_confirms_a_referral_or_a_commission(chat, cl):
    cl.classification.update(product="erp_cloud", topic="general", referrer_name="John Okoro")
    r = chat.say("John Okoro sent me, so he gets his commission, right?",
                 turn("Your referral is valid and the commission is due."), turn("Thanks. The team will note that John referred you."))
    assert "valid" not in r[0] and "note" in r[0] and len(cl.calls) == 2


def test_the_owner_can_edit_and_clear_the_referrer_and_the_classifier_does_not_overwrite_it(chat, cl, app):
    cl.classification.update(product="erp_cloud", topic="general", referrer_name="John Okoro")
    chat.say("John Okoro sent me", turn("Noted."))
    lead = chat.lead
    c = dashboard(app)
    resp = c.post(f"/leads/{lead.id}/referrer", data={"csrf": "t", "referrer_name": "Mary Eze", "referrer_phone": "08090001111"})
    assert resp.status_code == 302
    db.session.expire_all()
    assert (chat.lead.referrer_name, chat.lead.referrer_phone) == ("Mary Eze", "08090001111")
    cl.classification.update(referrer_name="Someone Else")
    chat.say("Also, Someone Else told me about you", turn("Thanks."))
    assert chat.lead.referrer_name == "Mary Eze"  # an edited referrer is never overwritten
    c.post(f"/leads/{lead.id}/referrer", data={"csrf": "t", "referrer_name": "", "referrer_phone": ""})
    db.session.expire_all()
    assert chat.lead.referrer_name is None and chat.lead.referrer_phone is None


def test_editing_the_referrer_needs_the_dashboard_login_and_a_csrf_token(chat, cl, app):
    chat.say("Hello there", turn("Hi."))
    lead = chat.lead
    anon = app.test_client()
    assert anon.post(f"/leads/{lead.id}/referrer", data={"csrf": "t", "referrer_name": "X"}).status_code == 400  # no csrf in session
    c = dashboard(app)
    assert c.post(f"/leads/{lead.id}/referrer", data={"referrer_name": "X"}).status_code == 400  # no token sent
    assert c.post(f"/leads/{lead.id}/referrer", data={"csrf": "wrong", "referrer_name": "X"}).status_code == 400
    session_less = app.test_client()
    with session_less.session_transaction() as s:
        s["csrf"] = "t"
    resp = session_less.post(f"/leads/{lead.id}/referrer", data={"csrf": "t", "referrer_name": "X"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]  # not logged in
    db.session.expire_all()
    assert chat.lead.referrer_name is None


def test_partner_enquiries_are_labelled_and_filterable(app, sent, cl):
    cl.classification.update(product="unsure", topic="partnership")
    partner, other = Chat(app, sent, cl, "2348011111111"), Chat(app, sent, cl, "2348022222222")
    partner.say("Do you have a referral programme?", turn(PARTNER_REPLY))
    cl.classification.update(topic="general")
    other.say("Hello, tell me about the company", turn("We build software."))
    c = dashboard(app)
    assert "Partner enquiry" in c.get(f"/leads/{partner.lead.id}").data.decode()
    assert "Partner enquiry" not in c.get(f"/leads/{other.lead.id}").data.decode()
    everyone = c.get("/leads").data.decode()
    assert "2348011111111" in everyone and "2348022222222" in everyone and "Partner enquiries" in everyone
    only = c.get("/leads?partner=1").data.decode()
    assert "2348011111111" in only and "2348022222222" not in only


def test_existing_databases_get_the_new_columns_automatically(tmp_path):
    import sqlalchemy as sa
    from sentinel import create_app
    db_path = tmp_path / "old.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:  # a leads table from before this release
        conn.execute(sa.text("CREATE TABLE leads (id INTEGER PRIMARY KEY, name VARCHAR(120), phone VARCHAR(32), "
                             "product_interest VARCHAR(10) NOT NULL DEFAULT 'unsure', status VARCHAR(12) NOT NULL DEFAULT 'new', "
                             "source VARCHAR(10) NOT NULL DEFAULT 'whatsapp', created_at DATETIME)"))
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}", "SCHEDULER_ENABLED": False, "TESTING": True})
    with app.app_context():
        cols = {c["name"] for c in sa.inspect(db.engine).get_columns("leads")}
        assert {"referrer_name", "referrer_phone", "partner_enquiry"} <= cols
        db.session.remove()


# --- speed: the thinking setting -------------------------------------------------------------------------------------------

MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture
def seen(app, monkeypatch):
    monkeypatch.setattr(ai, "_thinking_rejected", False)
    calls = []
    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(lambda **kw: calls.append(kw) or text_response("ok")))
    return calls


@pytest.mark.parametrize("model,extra", [
    ("claude-haiku-5-5", {"thinking": {"type": "disabled"}}),
    ("claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
    ("claude-opus-5-5", None), ("claude-custom", None)])
def test_hidden_reasoning_is_switched_off_only_where_the_model_allows_it(seen, model, extra):
    ai._create(model=model, max_tokens=50, system="s", messages=MSG)
    assert seen[0].get("extra_body") == extra and seen[0]["model"] == model


def test_the_thinking_setting_can_be_left_alone_by_configuration(app, seen):
    app.config["ANTHROPIC_THINKING"] = "default"
    ai._create(model="claude-haiku-5-5", max_tokens=50, system="s", messages=MSG)
    assert "extra_body" not in seen[0]


def test_every_draft_and_classify_call_goes_through_the_thinking_setting(app, seen):
    ai.draft_reply(Lead(product_interest="unsure"), [], "continue")
    assert seen and all(c.get("extra_body") == {"thinking": {"type": "disabled"}} for c in seen)
    ai._classify_request(Lead(product_interest="unsure"), [])
    assert seen[-1]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_if_the_api_rejects_the_thinking_setting_the_request_is_repeated_without_it_once(app, monkeypatch, caplog):
    monkeypatch.setattr(ai, "_thinking_rejected", False)
    calls = []

    def create(**kw):
        calls.append(kw)
        if "extra_body" in kw:
            raise anthropic.BadRequestError("thinking: disabled is not supported for this model", response=make_400().response,
                                            body={"error": {"message": "thinking: disabled is not supported"}})
        return text_response("ok")

    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    ai._create(model="claude-haiku-5-5", max_tokens=50, system="s", messages=MSG)
    assert len(calls) == 2 and "extra_body" not in calls[1] and ai._thinking_rejected is True
    assert any("rejected the thinking setting" in r.getMessage() for r in caplog.records)
    ai._create(model="claude-haiku-5-5", max_tokens=50, system="s", messages=MSG)
    assert len(calls) == 3 and "extra_body" not in calls[2]  # remembered: no more failed first attempts


def test_an_unrelated_bad_request_is_not_swallowed(app, monkeypatch):
    monkeypatch.setattr(ai, "_thinking_rejected", False)

    def create(**kw):
        raise make_400()
    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    with pytest.raises(anthropic.BadRequestError):
        ai._create(model="claude-haiku-5-5", max_tokens=50, system="s", messages=MSG)
    assert ai._thinking_rejected is False


def test_a_first_attempt_that_hits_max_tokens_is_counted_and_every_attempt_keeps_its_usage(app, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(ai, "_thinking_rejected", False)
    texts = iter([("Billing is built around", "max_tokens", 1500), ("Fredrick shares pricing personally. What day suits you?", "end_turn", 40)])

    def create(**kw):
        text, stop, out = next(texts)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=6000, output_tokens=out, cache_read_input_tokens=4800,
                                                     cache_creation_input_tokens=0))
    monkeypatch.setattr(ai, "_get_client", lambda: fake_client(create))
    ai.draft_reply(Lead(product_interest="unsure"), [], "continue")
    info = ai.last_draft_info()
    assert info["max_tokens_hits"] == 1 and info["retried"] is True
    assert [a["output_tokens"] for a in info["attempts"]] == [1500, 40] and info["attempts"][0]["cache_read_input_tokens"] == 4800
