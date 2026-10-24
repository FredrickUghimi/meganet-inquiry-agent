# Mega-Net Lead Sentinel

Logs every inquiry, replies instantly on WhatsApp, chases quiet leads on Day 1, Day 3 and Day 7, and pings Fredrick on WhatsApp the moment a lead is ready to talk pricing or book a demo.

One Flask app, one database, one in-process scheduler, one Render web service. No queues, no workers.

## How it works

1. **Intake.** A WhatsApp message hits `/webhook` (Meta Cloud API). Or you log an email inquiry by hand at `/new` in the dashboard. Each one creates or updates a **Lead** and stores a **Message**.
2. **Auto-reply.** Claude drafts a reply. On first contact it confirms receipt, asks EHR Cloud vs ERP Cloud if unclear, and asks one or two qualifying questions (size, pain point). Replies after that answer from the product notes and ask at most one question.
3. **Classification.** Claude classifies the conversation after every inbound message: *exploring*, *ready for demo*, *ready for pricing*, or *not a fit*.
4. **Escalation.** The first time a lead turns "ready", Fredrick gets a WhatsApp message: one-line summary plus a link to the lead. Nothing is auto-booked.
5. **Follow-ups.** If a lead goes quiet, they get a WhatsApp message 1, 3 and 7 days after **their last message**. Any reply cancels the sequence and restarts the clock. After the Day 7 message the lead is marked **cold** and is never messaged automatically again. A reply from a cold lead revives them.

### Lead statuses

| Status | Meaning | Follow-ups run? |
|---|---|---|
| `new` | Just arrived, hasn't replied to our first message | yes |
| `engaged` | Conversation under way | yes |
| `ready` | Wants demo/pricing. Fredrick alerted | no (Fredrick's turn) |
| `cold` | Silent through Day 7 | no |
| `closed_won` / `closed_lost` | Set by Fredrick, or `closed_lost` when Claude judges "not a fit" | no |

Dashboard buttons: **Mark contacted** (logs it, pauses the bot), **Closed won/lost**, **Pause/Resume follow-ups**, **Reopen**. While a lead is paused, the bot sends no follow-ups *and* no auto-replies, so it won't talk over you. Claude still classifies their messages and will still alert you if they turn "ready".

### Things worth knowing

- **The 24-hour rule.** After a lead's last WhatsApp message you have 24 hours to send free-form text. After that, WhatsApp only allows pre-approved **templates**. Day 1/3/7 follow-ups go out as templates. Each template has fixed wording plus a slot, `{{3}}`, that Claude fills with a fresh line each time (it is told what it said before, so it varies). If a lead is somehow still inside the window, the follow-up goes as plain text instead.
- **Form/email leads and the first message.** Someone who emailed you has never messaged your WhatsApp number, so there is no open window and WhatsApp won't deliver a free-form first message. The `ack` template (`meganet_inquiry_ack`, enabled by default) is sent as the instant first message, so they get a reply right away. It only delivers once Meta has approved it (step 8); until then the send fails and is logged, and the Day 1/3/7 templates still follow.
- **Escalation alerts have the same rule.** Alerts to Fredrick are tried as free-form messages first, which only work if Fredrick has messaged the business number within the last 24h. If that fails, the `escalation` template (`meganet_lead_alert`, enabled by default) is sent instead, once approved. The app logs loudly when an alert couldn't be delivered, and the lead still shows as **ready** (sorted to the top) in the dashboard.
- **Messages from Fredrick's own number are ignored** by intake, so texting the business number never creates a lead.
- **Product facts.** Claude may only state what's in `sentinel/knowledge.py`. **Edit that file** with accurate product details before going live. It is told never to quote prices, discounts or dates.
- **Failures are contained.** A failed WhatsApp send is stored on the message (shown red in the dashboard) and logged. The scheduler carries on with other leads. A failed follow-up is retried after an hour; after 3 failures that lead's sequence is paused. If Claude is down, built-in fallback copy is used, so leads still get a reply.

## Local setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in what you have; see below
set -a; . ./.env; set +a
python app.py                 # http://localhost:5000, SQLite in ./instance/
```

Minimum to see the dashboard: `DASHBOARD_PASSWORD` (and `SESSION_COOKIE_SECURE=0` on plain http). To exercise the whole loop you need the Anthropic key and WhatsApp credentials.

Receiving real WhatsApp webhooks locally needs a public URL. Use a tunnel such as `cloudflared tunnel --url http://localhost:5000` or ngrok, and give Meta `https://<tunnel>/webhook`.

Run the tests (no network or API keys needed; Claude and WhatsApp are stubbed):

```bash
pip install pytest && python -m pytest
```

Tables are created automatically on startup. There are no migrations. If you change the models later, add the column by hand or recreate the database.

## Deploy to Render

1. Push this repo to GitHub.
2. Render dashboard → **New → Blueprint** → select the repo. Render reads `render.yaml` and creates the web service and a Postgres database.
3. When prompted, fill the `sync: false` variables (see `.env.example` for what each is): `DASHBOARD_PASSWORD`, `ANTHROPIC_API_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `OWNER_WHATSAPP_NUMBER`. `WHATSAPP_TEMPLATE_ACK` and `WHATSAPP_TEMPLATE_ESCALATION` are prefilled with the default template names; leave them unless you renamed the templates in Meta.
4. After the first deploy, open `https://<your-service>.onrender.com/healthz` (should say `ok`) and then `/` to log in.
5. Complete the Meta setup below, using `https://<your-service>.onrender.com/webhook` as the callback URL.

**Cost and sizing notes.** The web service must be an always-on paid instance (`starter` in `render.yaml`). A free instance sleeps after inactivity, and a sleeping service cannot run the follow-up scheduler. The database uses Render's paid `basic-256mb`, since the free Postgres is deleted after 30 days. `render.yaml` runs gunicorn with **one worker** on purpose: the scheduler lives inside the process, and extra workers would send every follow-up twice.

---

## Meta WhatsApp Business setup (first-time walkthrough)

You need: a Facebook account, a phone number that is **not currently registered on regular WhatsApp or WhatsApp Business app** (or one you're willing to move; a fresh SIM or a virtual number that can receive an SMS/call works), and about an hour. Meta's UI moves around, so button names below may differ slightly. Official docs: <https://developers.facebook.com/docs/whatsapp/cloud-api/get-started>.

### 1. Create a Meta Business portfolio
1. Go to <https://business.facebook.com> → create a business portfolio for Mega-Net Software.
2. Settings → **Business info** → start **Business verification** (company documents). You can build and test without it, but sending to the public at volume, and a stable display name, need it. Start it early; it can take days.

### 2. Create the developer app
1. <https://developers.facebook.com> → **My Apps → Create App**. Use case: **Other** → type **Business** → attach your business portfolio.
2. In the app dashboard, **Add product → WhatsApp → Set up**. This creates a WhatsApp Business Account (WABA) with a free **test number**.

### 3. Find your credentials (WhatsApp → API Setup)
- **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID` (this is *not* the phone number).
- **WhatsApp Business Account ID** → `WHATSAPP_BUSINESS_ACCOUNT_ID` (only for the template script).
- **App secret**: App settings → Basic → *App secret* → `WHATSAPP_APP_SECRET`. The app uses it to reject webhook calls that don't really come from Meta.
- Invent a **verify token** (any random string) → `WHATSAPP_VERIFY_TOKEN`.

The temporary token on the API Setup page expires in 24 hours. Use it to test, then create a permanent one (step 5).

### 4. Add your real number
WhatsApp → API Setup → **Add phone number**. Enter the business name, category and number, verify by SMS/voice code. The display name goes through a quick Meta review. Until your number is added, only the test number works, and it can message only recipients you list (up to 5) as test recipients.

### 5. Create a permanent access token
1. Business Settings → Users → **System users** → Add → role **Admin**.
2. **Add assets** → select your app → give **Full control**. Also add the WhatsApp account asset.
3. **Generate token** → pick your app → tick `whatsapp_business_messaging` and `whatsapp_business_management` → token expiry **Never**.
4. Copy it into `WHATSAPP_TOKEN`. You only see it once. Treat it like a password.

### 6. Point the webhook at Lead Sentinel
1. App dashboard → WhatsApp → **Configuration → Webhook → Edit**.
2. **Callback URL**: `https://<your-service>.onrender.com/webhook`. **Verify token**: your `WHATSAPP_VERIFY_TOKEN`. Click **Verify and save**. (The service must already be deployed with those env vars set, or verification fails.)
3. Under **Webhook fields**, **subscribe to `messages`**.

### 7. Test it
Message your business number from another phone. Within seconds you should see a new lead in the dashboard and get a reply. If nothing arrives, check Render's logs, then the checklist below.

### 8. Get the follow-up templates approved

Free-form messages are only allowed in the 24h window, so the Day 1/3/7 follow-ups need approved templates.

**Option A: script** (needs `WHATSAPP_TOKEN` and `WHATSAPP_BUSINESS_ACCOUNT_ID` in your environment):

```bash
python scripts/submit_templates.py --dry-run   # look at what will be sent
python scripts/submit_templates.py --all       # submit all 5 templates (3 follow-ups + ack + owner alert)
```

**Option B: by hand.** WhatsApp Manager → **Message templates → Create template**. Category **Utility**, language **English**, and paste each body exactly, including the `{{1}}`-style variables. Add the sample values when asked.

| Name | Body | Sample values for {{1}}, {{2}}, {{3}} |
|---|---|---|
| `meganet_followup_day1` | `Hi {{1}}, Mega-Net here, following up on your inquiry about {{2}}. {{3}} Reply to this message and we'll pick up where we left off.` | Amaka · EHR Cloud · Still deciding between a phased rollout and going live in one go? |
| `meganet_followup_day3` | `Hi {{1}}, checking in once more on {{2}}. {{3}} A quick reply here is all it takes to continue.` | Amaka · EHR Cloud · Tell us how many staff would use it and we can scope it properly. |
| `meganet_followup_day7` | `Hi {{1}}, this is our last message about {{2}} unless you reply. {{3}} If the timing is wrong, no problem. Message us any time and we'll pick it up.` | Amaka · EHR Cloud · The offer to walk you through it still stands. |

Approval usually takes minutes to a day. Status shows in WhatsApp Manager. If you use different names, set `WHATSAPP_TEMPLATE_DAY1/3/7` (and `WHATSAPP_TEMPLATE_LANG`) to match exactly.

**Honest warning about categories.** Meta decides the category, not you. Follow-ups that read as promotion can be re-categorised as **Marketing** (which is billed differently, and needs opt-in). The wording above is deliberately tied to the person's own inquiry and contains no offers or sales language, which is what gets "utility" approved. If Meta rejects or re-categorises one, edit the wording (in `sentinel/templates_meta.py` and in Meta) to be more plainly about their request, and resubmit.

**If a template is pending, rejected or paused**, sends for that stage fail. That shows up as a red "NOT DELIVERED" message in the lead's history and in the logs, and the lead is retried hourly, then paused after 3 failures.

### First-message and alert templates

Both are **on by default** and submitted by `--all` above (or by hand from `sentinel/templates_meta.py`). Their env vars only need changing if you use different names in Meta.

- `meganet_inquiry_ack` → `WHATSAPP_TEMPLATE_ACK`. Instant first message to form/email leads on WhatsApp.
- `meganet_lead_alert` → `WHATSAPP_TEMPLATE_ESCALATION`. Gets alerts to Fredrick even when his 24h window has lapsed.

### Moving off the test number (go-live checklist)
- [ ] Real number added and verified (step 4), display name approved
- [ ] Permanent system-user token in `WHATSAPP_TOKEN` (step 5)
- [ ] Webhook verified and subscribed to `messages` (step 6)
- [ ] All 5 templates **Approved** (step 8): Day 1/3/7, `meganet_inquiry_ack`, `meganet_lead_alert`
- [ ] `OWNER_WHATSAPP_NUMBER` set
- [ ] `sentinel/knowledge.py` edited with accurate product facts
- [ ] Business verification started

### Troubleshooting
- **Webhook "couldn't be validated":** verify token mismatch, or the service isn't deployed / env vars not set.
- **Messages arrive in Meta but nothing in the dashboard:** you didn't subscribe to `messages`; or `WHATSAPP_APP_SECRET` is wrong (logs show `Invalid signature`/403s).
- **Replies fail with code 131047 / "re-engagement":** you're outside the 24h window. Use a template.
- **Errors like "(#131030) recipient not in allowed list":** you're on the test number. Add the recipient as a test recipient or move to your real number.
- **Template send fails with code 132001:** template name/language doesn't match an *approved* template.
- **Alerts to Fredrick never arrive:** see the 24-hour note above.

## Configuration reference

Every setting is an environment variable. `.env.example` lists them all with comments. Required in production: `DASHBOARD_PASSWORD`, `SECRET_KEY`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `OWNER_WHATSAPP_NUMBER`. Webhook calls without a valid signature are rejected.

## Project layout

```
app.py                  entry point (gunicorn app:app)
sentinel/
  __init__.py           app factory
  config.py             env var loading
  models.py             Lead, Message
  service.py            intake, reply handling, escalation, follow-up sequence
  ai.py                 Claude: classify + draft (with fallbacks)
  whatsapp.py           Cloud API client
  webhook.py            /webhook (verify + inbound)
  dashboard.py          login, lead list/detail, actions, manual intake
  scheduler.py          APScheduler (follow-up tick every 5 min)
  templates_meta.py     the WhatsApp templates
  knowledge.py          product facts + brand voice given to Claude
scripts/submit_templates.py
tests/
```
