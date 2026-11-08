# Deploying Mega-Net Lead Sentinel to Render (phone-friendly)

The first deploy works with every WhatsApp value **blank**. The dashboard will show a red
"WhatsApp not connected" banner until you add them. Leads are still saved; nothing is sent.

## 1. Environment variables

Render asks for the `sync: false` ones when you create the Blueprint. Blank is allowed for all of them
except `DASHBOARD_PASSWORD`.

| Name | What it is | Where to get it | Needed |
|---|---|---|---|
| `DASHBOARD_PASSWORD` | Password to log in to the dashboard | You invent it. Long, unique. | **Now** |
| `SECRET_KEY` | Signs login cookies | Render generates it. Don't fill it in. | Automatic |
| `DATABASE_URL` | Postgres connection string | Render fills it from the database. Don't fill it in. | Automatic |
| `ANTHROPIC_API_KEY` | Lets Claude write replies and classify leads | console.anthropic.com > API keys (same key as Mega-Net) | Can stay blank. Without it replies use fixed fallback text. Add it before real leads arrive. |
| `WHATSAPP_VERIFY_TOKEN` | Password Meta sends when verifying the webhook | You invent any string (e.g. `meganet-verify-2026`) | Set **before** Meta's Configure Webhooks step |
| `WHATSAPP_TOKEN` | Permanent access token | Meta Business Settings > System users > Generate token (whatsapp_business_messaging + whatsapp_business_management) | Blank until number is live |
| `WHATSAPP_PHONE_NUMBER_ID` | ID of the business number (not the phone number) | Meta app > WhatsApp > API Setup | Blank until number is live |
| `WHATSAPP_APP_SECRET` | Verifies webhooks really come from Meta | Meta app > App settings > Basic > App secret | Blank until number is live (inbound webhooks are rejected while blank) |
| `OWNER_WHATSAPP_NUMBER` | Your own WhatsApp for "lead is ready" alerts, digits only with country code, e.g. `2348012345678` | Your number | Blank until number is live |
| `ANTHROPIC_MODEL` | Claude model used for classification and every drafted message. Default `claude-haiku-5-5`. If you set it in Render's Environment tab it overrides this default, so make sure it is a model your key can use | Preset | `claude-haiku-5-5` |
| `OWNER_TAKEOVER_HOURS` | Hours after an owner or staff message during which the lead counts as human-handled (the bot only sends holding replies) | Preset | `48` |
| `BOT_ANSWERS_AFTER_READY` | `1`: the bot keeps answering ready leads unless a person has taken over. `0`: old behaviour, ready means silent | Preset | `1` |
| `HOLD_REPLY_COOLDOWN_HOURS` | Hours between automatic "Fredrick has your message" replies to a lead who is ready, paused or marked contacted | Preset | `4` |
| `OWNER_REPLY_REMINDER_MINUTES` | Minutes before you are reminded about a waiting lead you have not answered (then every 3 hours, max 3) | Preset | `60` |
| `EXISTING_CLIENT_NUMBERS` | Optional. Comma separated phone numbers (digits, with country code) of existing clients. They get the support route, not sales | Your client list | Blank |
| `STAFF_WHATSAPP_NUMBERS` | Optional. Comma separated team numbers: alerted about demo requests, may send `confirm` / `decline` | Your team | Blank |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM` | Optional: email a copy of every alert to you. All six must be set, otherwise email is off (silently). Port 587 uses STARTTLS, 465 uses SSL. Gmail needs an app password | Your email provider | Blank |
| `OWNER_AS_LEAD` | Testing only. Messages from `OWNER_WHATSAPP_NUMBER` are ignored by design (logged as "ignored (sender is OWNER_WHATSAPP_NUMBER...)"). Set to `1` to test the bot from your own phone; set back to `0` afterwards | You | `0` (preset) |
| `DISPLAY_TZ` | Timezone for dates (preset `Africa/Lagos`) | Preset | Already set |
| `WHATSAPP_TEMPLATE_ACK`, `WHATSAPP_TEMPLATE_ESCALATION` | Approved template names (preset) | Preset | Already set |

## 2. Deploy on Render

1. Open the Render dashboard > **New** > **Blueprint**.
2. Connect GitHub and select the **meganet-inquiry-agent** repo. Choose the branch you want deployed.
3. Render reads `render.yaml` and shows a web service plus a database.
4. Fill in `DASHBOARD_PASSWORD`. Type a `WHATSAPP_VERIFY_TOKEN` now if you want. Leave the rest blank.
5. Tap **Apply** / **Deploy Blueprint**. First build takes a few minutes.
6. To fill values later: service > **Environment** > edit > save. Render redeploys.

## 3. Check it is alive

1. Open `https://<service-name>.onrender.com/healthz`. You should see `ok`. (No login, touches no data.)
2. Open `https://<service-name>.onrender.com/`, log in with `DASHBOARD_PASSWORD`.
3. Red "WhatsApp not connected" banner = expected for now. It disappears once `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` are set.

The default service name is `meganet-lead-sentinel`, so the URL is `https://meganet-lead-sentinel.onrender.com`.
If Render adds a suffix, use the exact URL shown at the top of the service page.

## 4. Connect WhatsApp (when the number is live)

1. In Render, set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `OWNER_WHATSAPP_NUMBER`, `WHATSAPP_VERIFY_TOKEN`. Save and wait for the redeploy.
2. In Meta: your app > WhatsApp > **Configuration** > **Configure Webhooks** > Edit.
3. **Callback URL:** `https://<service-name>.onrender.com/webhook`
4. **Verify token:** exactly the value you put in Render's `WHATSAPP_VERIFY_TOKEN`.
5. Tap **Verify and save**, then subscribe to the **messages** field.
6. Submit the message templates (see README) so follow-ups and first replies can send.

## First-contact menu (new message flow)

When a **new** lead's first WhatsApp message is only a greeting or generic opener (hello, hi, good morning,
"are you there", "anyone", "I need help"; short, no product name, no real question), the bot does **not** draft a
reply. It sends one free-form interactive message (inside the 24h window, no template) with three buttons:
**EHR Cloud**, **ERP Cloud**, **Something else**. A message that names EHR/ERP or asks a real question is handled
exactly as before. A short greeting-like message the fixed check cannot decide is judged by Claude; if Claude is
unavailable it is treated as a normal message.

After a tap (or a typed `1`/`2`/`3`, `EHR`, `ERP`):

- **EHR Cloud / ERP Cloud:** the lead's product is set, status becomes engaged, and the usual short qualifying
  question (size and main problem) is sent, written by Claude with that product's facts.
- **Something else:** the bot asks them to describe what they need in one message, flags the lead **Needs your
  reply** (cleared by "Mark contacted", `done <id>` or a dashboard reply), and sends you one WhatsApp alert.

Rules and fallbacks:

- If Meta rejects the interactive message, the bot sends plain text: "Reply 1 for EHR Cloud, 2 for ERP Cloud, 3 for
  something else." While a lead is awaiting a choice, `1`, `2`, `3`, `EHR` or `ERP` count as a tap. Any other
  message ignores the menu and is processed normally.
- The menu is never sent twice to the same lead within 6 hours, and never to ready, paused, contacted or closed leads.
- The lead page shows a tap as "Chose: <title>". Taps and menu messages are stored like any other message.
- Logs carry one INFO line per menu sent, tap received and fallback used (`menu lead=...`), never message text.
- Wording and the 6-hour limit are constants in `sentinel/menu.py`. No new environment variables.
- The app adds the new database columns by itself on start-up; nothing to run.

## Ready leads, business facts and demos

**Who answers.** A lead is *human-handled* if it is paused, marked contacted, or has an owner or staff message
stored within `OWNER_TAKEOVER_HOURS` (default `48`). Resume / Reopen hands it back to the bot. "Ready" alone no longer
silences the bot: a ready lead that is not human-handled runs the normal pipeline on every message (classify, draft
from the product notes, business facts and history), the status stays ready, and no alert goes out unless one is
needed. Human-handled leads keep the old behaviour: one throttled holding reply, an owner alert per waiting message,
the Needs reply flag and reminders. Set `BOT_ANSWERS_AFTER_READY=0` to restore the old behaviour where ready means silent.
Replying from the dashboard, `done`, `reply`, Mark contacted and Pause all switch a lead to human-handled.

**Business facts.** Edit `sentinel/business_facts.py` (hours, demo days, duration, channels, notice, promises, support,
implementation, migration, training, what is not offered). Anything left as `TODO` is unknown: the bot says the team
will confirm it and never guesses. Startup logs one WARNING listing the TODO facts, and the dashboard **Settings** page
shows them. `PRICING_POLICY` is the one approved pricing sentence.

**Alerts only when needed.** Claude flags a complaint, a custom requirement or a question the notes do not cover. The bot
answers safely (the team will confirm), the question is saved on the lead page under Open questions, and you get one alert
(throttled to one per 10 minutes per lead).

**How replies work now.** Claude writes every reply and answers the customer's latest message first (thanks, every
question in it, a price question with the pricing policy, small talk, Pidgin, one-word replies read in context, a voice
note it cannot hear). It returns structured output with a `demo_signal`, any details the customer gave and a
`needs_human` reason, and the code applies the signal only after checking it. There is no scripted question-and-answer
flow, so no stored state can make the bot repeat a canned line. Guardrails in code:

- **Demo rules.** Live demos run only on **Wednesdays at 10:00 AM WAT**, 30 minutes, with at least 2 hours' notice, and
  `DEMO_SLOTS_PER_SESSION` (default 1) **confirmed** demos per Wednesday (change the constants in
  `sentinel/business_facts.py`). Tentative requests never use up a slot, so several can be pencilled in for the same
  Wednesday, and the bot never tells a prospect a Wednesday is full because of someone else's unconfirmed request. Any
  other day or time is redirected to the next two Wednesdays with a free slot. The Demos page lists tentative requests
  per Wednesday.
- **Video and offer.** A demo request gets the video for the lead's product (both if unknown); the links are added by
  code exactly as stored and never sent twice to the same lead within 24 hours. A prospect who prefers to speak to the
  team gets no video first. The personalised-demo question is asked at most once per 24 hours; if two asks were ignored
  it stops until the customer asks for a demo again. Thanks, a question, an emoji or silence never repeats it.
- **Collecting and pencilling.** After a yes, Claude asks only for what is missing (name, organisation, phone, WhatsApp
  video call or Google Meet) and proposes the next free Wednesday. When the customer agrees, a *tentative* demo is created
  (the columns are added automatically at start-up) and Fredrick and the numbers in `STAFF_WHATSAPP_NUMBERS` get an alert
  with `confirm <id>` and `decline <id> [new time]`.
- **Never confirmed by the bot.** A draft that says a demo is confirmed or booked is rejected and regenerated. Only
  `confirm <id>` confirms (it then tells the prospect). The demo state only moves forward.
- **No repeats on any path.** A bot reply identical or near-identical (similarity above 0.8) to any bot message in the
  last 24 hours (holding replies, support replies, demo texts, menu text, fallbacks included) is regenerated by Claude
  with the customer's latest message in focus. If Claude fails, a short different acknowledgement mentioning their latest
  message is sent, the lead is flagged **Needs your reply**, and a WARNING is logged.
- **Fixed texts are fallbacks only.** When Claude fails (or a guard rejects its draft twice) a fixed text is used, a
  WARNING is logged and the lead page shows "Fixed fallback text used". If you see that note often, check
  `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`.
- **People.** "Let me talk to someone" hands the lead over (the bot pauses, you get an alert and the friendly line goes
  out). A customer repeating the same question is escalated with the reason "loop: customer repeated the same question".
  Complaints and questions the notes do not cover alert you as before.

**First product reply.** The first reply when a lead first shows interest in a product (named in their message, or chosen
with the EHR Cloud / ERP Cloud button) is the approved text in `sentinel/knowledge.py` (`ERP_FIRST_REPLY`,
`EHR_FIRST_REPLY`): the first name ("Hello!" if unknown), a short description with the confirmed modules, the product
demonstration video, the Wednesday live-demo line (built from `business_facts.py`) and one closing question. The video link
is never in a template or a prompt: the text has a `{video_url}` placeholder and the code inserts the exact link from
`PRODUCT_DEMO_VIDEOS`, skipping the video if the same one went out in the last 24 hours and removing any other link. Claude
may adapt the reply to what the customer already wrote (for example skip the closing question), but it may not add features,
use the words "comprehensive", "entire operations" or "all-in-one", go over 165 words, or ask two questions; a draft that
does is regenerated, and if it still does, the approved text is sent as it is. When the reply goes out the code records that
the video was sent and that the live-demo offer was made, so there is no second video or offer within 24 hours, and a
reply such as "yes, Wednesday works" is treated as accepting the offer. To change what the bot claims, edit the text there
and the matching "Customer-safe module summary" in the same file. `python scripts/eval_replies.py` checks the first replies
too (name, one question, within 165 words, the product's own link exactly as stored, the Wednesday line, confirmed features
only).

**Checking replies before you rely on them.** In the Render Shell run `python scripts/eval_replies.py` (needs
`ANTHROPIC_API_KEY`; `--list` shows the scenarios without it, `--only price --verbose` narrows it by name, `--only 28,31` or `--only 28-31` re-runs scenarios by number, `--out run.txt` also saves the output; failed scenarios are listed again at the very end under FAILED SCENARIOS). It runs 56 awkward
customers (thanks plus price, a topic switch, a vague "ok", Pidgin, two questions at once, a Tuesday demo request, a
complaint, a voice note, a request for a human, repeated questions, a long silence and more) through the real pipeline
with the real Claude in dry-run mode, using a temporary database, so nothing is sent and nothing is stored. Each reply
is checked for repeated text, currency amounts, promises, length under 90 words and whether the latest message was
addressed, then a PASS or FAIL summary is printed. The checks are automatic, so skim the replies too.

Commands from the owner number, and from staff numbers (staff may send only `confirm`, `decline` and `help`):

- `confirm <demo id>`: confirms the demo and tells the lead. Refused, with a message to whoever sent it, if that
  Wednesday is already full of **confirmed** demos (use `decline <id> [new time]` instead) or if the start time has
  already passed. If the demo starts in under the notice period (2 hours) it is still confirmed, with a warning in the reply.
  Same rules for the owner and staff numbers.
- `decline <demo id>`: declines it and offers the next two Wednesdays.
- `decline <demo id> <new time>` (e.g. `decline 3 next Wednesday`): proposes the new time (it must be a free Wednesday at
  10:00) and creates a new tentative demo. Send `confirm <new id>` once they agree.

Demos are listed on the dashboard **Demos** page and on each lead page. Times are Africa/Lagos. Outside business hours
(Monday to Friday, 9:00 AM to 5:00 PM WAT) the bot still answers and says when the team will reply.

**Existing clients.** If a lead says they already use a Mega-Net product, is listed in `EXISTING_CLIENT_NUMBERS`
(comma separated, digits only), or you tick **Existing client** on the lead page, every message from them is classified
as support or sales:

- **Support** (problems, how do I, my account, reports, errors): the bot sends the support route and support hours,
  alerts you once, and runs no sales flow or menu.
- **Sales** (they ask about EHR Cloud or ERP Cloud as something to buy, upgrading, migrating, moving to the cloud,
  pricing or a demo): treated as a normal sales lead. The existing-client flag stays, the lead page shows
  **Existing client, upgrade enquiry**, and you are alerted.

Existing clients get no automatic follow-ups unless their latest message is a sales enquiry. Logged as
`existing client lead=...`.

**Settings page.** Shows every business fact and a red list of the constants still `TODO`. No secrets are shown.

## Deleting leads (clearing test data)

Deleting is permanent, so keep a copy first. In the Render Shell (or locally), from the project folder:

```
python scripts/manage_leads.py list                                # id, name, masked phone, status, created, messages, demos
python scripts/manage_leads.py export --out /tmp/leads_export.csv  # copy of all leads, messages and demo requests
python scripts/manage_leads.py delete --ids 2,3                    # DRY RUN: shows what would go, changes nothing
python scripts/manage_leads.py delete --all-except 4,5             # dry run for everything except leads 4 and 5
python scripts/manage_leads.py delete --ids 2,3 --yes              # really delete (one database transaction)
python scripts/manage_leads.py delete --all --yes                  # delete every lead
```

`delete` always prints the lead ids and counts first. It removes each lead with its messages, demo requests and open
questions (reminder state, menu state and alert throttles live on the lead and go with it), and nothing else. Message text
is never printed or logged; each deletion logs one line (`lead deleted id=... messages=... demo_requests=... open_questions=...`).
The export file does contain message text and phone numbers, so keep it private. Render's shell is not persistent: download or
copy the export before the shell closes.

**From the dashboard:** open a lead and tap **Delete lead**, or tap **Select** on the leads list, tick leads and tap
**Delete selected**. Both open a confirmation page with the names, masked phone numbers and counts, and delete only after you
tick **I understand this cannot be undone**. If a deleted person messages again, they start as a brand-new lead and get the
first-contact menu.

## 5. Render plans

- **Web service: Starter or higher (always on).** Free instances sleep after idle. A sleeping service misses Meta webhooks and its in-process follow-up scheduler stops. The service must also run exactly **one** worker (already set), or follow-ups would be sent twice.
- **Postgres: paid (`basic-256mb`).** Render's free database expires after about 30 days and is deleted. Leads must not disappear.

## Check which version is running

If the bot behaves like an old release (for example it repeats a scripted line that was removed), the first thing to check is
that the latest commit is actually deployed. Open the dashboard **Settings** page: **Running commit** shows the first 7
characters of the commit Render built (from `RENDER_GIT_COMMIT`); it should match the latest commit on `main` on GitHub. The
same value is logged at start-up as `Lead Sentinel starting, commit=...`. In Render, the service's **Events** tab also lists
each deploy and its commit. If the commit is old, tap **Manual Deploy > Deploy latest commit** (or turn on Auto-Deploy for
the `main` branch). Messages already stored in the database are never re-sent, and retired scripted lines are blocked even if
an old conversation contains them.

## Troubleshooting: "messages arrive but nothing happens"

Every `POST /webhook` event writes one INFO log line: `webhook event kind=... sender=234*******78 phone_number_id=... outcome=...`, and the auto-reply step writes a `reply lead=... outcome=...` line. Search the Render logs for `webhook event` and read the `outcome`. Common reasons:

- `ignored (sender is OWNER_WHATSAPP_NUMBER...)`: you are texting from your own owner number. Use another phone, or temporarily set `OWNER_AS_LEAD=1`.
- `ignored (phone_number_id is not ours)`: the message was for a different number than `WHATSAPP_PHONE_NUMBER_ID` (also what Meta's dashboard "Test" button sends).
- `ignored (duplicate delivery ...)`: Meta retried a message already stored.
- `reply ... ignored (lead is closed_lost ...)` / `(follow-ups paused ...)` / `(lead already ready ...)`: the lead exists but the bot is deliberately quiet.

## Troubleshooting: "lead is stored but no reply"

Each reply attempt logs `reply lead=N started ...` and `reply lead=N finished outcome=<replied|skipped|failed> reason=...`, plus `claude classify ok/failed`, `claude draft_reply ok` and `whatsapp send ok: HTTP 200` / `whatsapp send failed: HTTP 4xx <Meta message> (code N)`. Search the Render logs for `reply lead`.

In the Render Shell:

```
python scripts/diagnose.py --lead 2          # safe: checks settings, tests Claude, walks the pipeline, sends nothing
python scripts/diagnose.py --lead 2 --send   # same, but really sends the reply
```

Or open the lead on the dashboard and tap **Retry reply**. Replies run on their own thread, and a safety-net sweep (every follow-up tick) answers any inbound message left unanswered for 2+ minutes.

## Waiting prospects (ready / paused / contacted)

When a lead you are handling writes again, the bot sends one short holding reply ("Thank you, <name>. Fredrick has your message and will contact you personally soon."), at most once per `HOLD_REPLY_COOLDOWN_HOURS`, and alerts you on WhatsApp (and email, if configured) at most once per 10 minutes per lead. If you have not answered after `OWNER_REPLY_REMINDER_MINUTES`, you get up to 3 reminders, 3 hours apart.

The dashboard shows a red **Needs your reply** badge with the waiting time, and the **Needs reply** filter lists those leads. The app cannot see replies you type on your own phone, so **tap Mark contacted on the lead page after you reply**. That clears the flag and stops the reminders.

## Replying and owner commands

**From the dashboard:** open the lead and use the **Reply to <name>** box (max 1000 characters). It works only while the 24-hour window is open ("Reply window open until HH:MM"); otherwise it is disabled and says "Window closed. Message the lead from your own number." The lead page works like a chat: the thread runs oldest to newest, the composer sits under the newest message (sticky at the bottom on a phone) and the page opens scrolled to the newest message. Inbound messages nobody has answered yet carry a red **Waiting** label. Tap **Reply** on a message to quote it: the customer sees your answer attached to their message (if WhatsApp refuses the quote, or the message has no WhatsApp id, it is sent without the quote and you are told). A successful send is stored as a message from **You**, marks the lead contacted, pauses the bot for that lead, clears the red **Needs your reply** flag and stops the reminders. A failed send shows Meta's error code and message and changes nothing else.

**From WhatsApp:** text the business number from your owner number (`OWNER_WHATSAPP_NUMBER`). These are commands, not leads, and each one gets a short plain-text answer back:

| Command | What it does |
|---|---|
| `list` | Up to 5 leads waiting for a reply: id, name, minutes waiting, product |
| `done <id>` | Mark that lead contacted. Clears the flag, pauses the bot for them, stops reminders. Use it after you answered from your own phone |
| `reply <id> <text>` | Send `<text>` to that lead through the business number (window must be open; otherwise you are told to message them from your own number) |
| `help` | List the commands |

Only the exact owner number can run commands, and only after the webhook signature has been verified. Any other text from the owner is ignored (or treated as a lead when `OWNER_AS_LEAD=1`). Every alert and reminder includes the lead id and the line `Reply "done <id>" after you have contacted them.`


## Before template follow-ups go live

Set these on Render (all optional): `FOLLOWUPS_ENABLED` (1 = on, 0 = no automatic follow-ups), `FOLLOWUP_DAILY_CAP` (default 30 follow-up
templates a day), `FOLLOWUP_STAGES` (default `1,3,7`; `1,3` skips the Day 7 template; `1` sends only Day 1). **Day 3 and Day 7 are Marketing templates,
which cost more per message than Day 1 (Utility).** Run `python scripts/manage_followups.py preview` in the Shell to see who would be messaged
in the next 24 hours (nothing is sent), or open **Follow-ups** on the dashboard and pause any lead. A lead that writes "stop" (or "abeg stop") is
opted out at once and never receives a template again.
