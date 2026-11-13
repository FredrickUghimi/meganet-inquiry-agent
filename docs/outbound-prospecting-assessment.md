# Outbound prospecting copilot for Lead Sentinel: assessment and design

Status: assessment only. No production code, settings or data were changed to produce this document.
Prepared for Fredrick. Date: 2026-11-13.

## 0. Summary and ground rules

**Verdict.** The approach is sound, with one honest caveat: because WhatsApp Cloud API outbound needs a *recorded opt-in*,
and cold prospects have none, almost all first-touch messages in the first months will be **human-sent** (personal WhatsApp via
a `wa.me` link, email via `mailto:`, or a phone call). The system's real value is therefore **research, qualification, drafting
and bookkeeping**, not automated sending. That is a good thing: it is the lowest-risk part, and it is where the time saving is.

**Design principle.** Smallest design that works; no new services, cron jobs or dependencies. Everything below runs inside the
existing Flask process, the existing database, the existing 5-minute scheduler tick, the existing dashboard and the existing owner
alert path.

**How this was checked.** I read the code (files named in each section) and searched it with grep. I did **not** run the app or the
test suite, and I did not call Meta, Anthropic or any third party. Where I say "exists" I saw it in a file; where I say "appears" I read a
name or a docstring but not the whole implementation. Facts about Meta, the NDPA and the NCC come from general knowledge, not from
this repository, may have changed, and are marked **(verify)**. Nothing here is legal advice; Section 2 and Section 3.12 list what
counsel must confirm.

**Time estimates** are Fredrick's minutes, labelled estimates. Cost figures are order-of-magnitude estimates, not quotes.

---

## 1. Current state (what exists, with file names)

### 1.1 Stack and runtime
| Item | Finding | Where |
|---|---|---|
| Framework | Flask app, `gunicorn app:app`, one worker on purpose (the scheduler is in-process) | `app.py`, `render.yaml` |
| ORM / DB | Flask-SQLAlchemy; Postgres on Render, SQLite locally. **No migrations**: `db.create_all()` creates *new tables* automatically, but new columns on existing tables must be added to `NEW_COLUMNS` | `sentinel/__init__.py` |
| Scheduler | One APScheduler `BackgroundScheduler`, one interval job (`tick`, every `FOLLOWUP_CHECK_MINUTES`=5) running `run_unanswered`, `run_owner_reminders`, `run_due_followups`. `run_async()` runs slow work on its own daemon thread (deliberately not the APScheduler pool) | `sentinel/scheduler.py` |
| Config | **Environment variables only** (`load_config`). The Settings page is **read-only** and shows business facts and a few modes. Changing a setting means editing Render env vars and restarting | `sentinel/config.py`, `dashboard.settings` |
| Auth | Single-user password login, CSRF on every POST, session cookie | `sentinel/dashboard.py` |

### 1.2 Models (`sentinel/models.py`)
- `Lead`: phone (unique), email, product_interest (`ehr_cloud/erp_cloud/unsure`), status (`new/engaged/ready/cold/closed_*`),
  `source` (String(10): `whatsapp/form/email`), follow-up state, `opted_out`/`opted_out_at`, `followups_off`, existing-client and
  partner flags, demo state, owner-alert throttles, `template_error`.
- `Message`: direction, channel (`whatsapp/form/email/manual`), body, `external_id` (WhatsApp wamid, dedupes webhooks), `status`,
  `error`, `sender` (`owner`/null=bot), `template`, `delivery_status` (`sent/delivered/read/failed`).
- `DemoRequest`, `OpenQuestion`.
- **Missing:** any prospect, campaign, source, consent, suppression, audit-log or settings table.

### 1.3 WhatsApp (`sentinel/whatsapp.py`, `templates_meta.py`, `webhook.py`, `service.py`)
| Capability | Exists? | Detail |
|---|---|---|
| Send text / buttons / template | Yes | `send_text`, `send_buttons`, `send_template` (body parameters only; **no header, button or quick-reply components**) |
| Templates | Yes, 5, all category **UTILITY** | `day1/day3/day7` (follow-ups), `ack`, `escalation`. Wording says "following up on your inquiry", so **they are false and wrong-category for a cold prospect and must not be reused** |
| 24-hour window | Yes | `service.window_open` (23.5 h, inbound WhatsApp only) |
| Delivery status | Yes | `webhook.receive` reads `statuses`; `service.note_delivery_status` moves sent→delivered→read, records failures with Meta's code; a failed template pauses that lead |
| Opt-out | Yes, per lead | `ai.optout_intent` (fixed patterns), set on arrival in `service.record_inbound`; `_opt_out_and_confirm` sends one confirmation; `send_whatsapp` blocks any template/follow-up to an opted-out lead ("last line of defence"); a clear "restart" lifts it |
| Caps | Partly | `FOLLOWUP_DAILY_CAP` (30 template follow-ups per Lagos day), 20 h gap, 3 follow-ups max per lead (`followup_block_reason`). **No cap for first-touch outbound, no per-campaign cap, no total cap** |
| Quality rating / messaging limit tier | **No** | `grep -ri "quality\|messaging_limit\|tier"` across `sentinel/`, `scripts/`, README, DEPLOY finds nothing relevant. The webhook only reads `messages` and `statuses`; other change fields are ignored |
| Number-level health alerts | No | Nothing subscribes to quality or account-update webhooks |
| Consent / opt-in record | **No** | Nothing records that someone agreed to be messaged, when, or how |

### 1.4 Claude integration (`sentinel/ai.py`, `knowledge*.py`, `business_facts.py`)
Plain `messages.create` calls (no tools, no web search) through `_create()`; default model `claude-haiku-5-5` (`ANTHROPIC_MODEL`).
Per-message classification (`classify`, intents `exploring/ready_demo/ready_pricing/not_fit`), structured reply drafting
(`draft_turn`), a follow-up line writer (`draft_followup_line`, fills template slot `{{3}}`), and a library of deterministic guards
that appear reusable for openers: `BANNED_PHRASES`, `unsupported_terms`, `_PRICE_RE`, `is_generic_opener`, `repeats`/`similarity`,
`phone_numbers`/`foreign_numbers`, `claims_human`, `trim_to_sentence`. Product facts for EHR Cloud and ERP Cloud live in
`knowledge.py`; the **official WhatsApp number and link** are `OFFICIAL_WHATSAPP_NUMBER/LINK` in `business_facts.py`. The
bot discloses it is an AI in its first message. **Missing:** a web search / research capability, structured prospect scoring,
reply-theme tagging.

### 1.5 Owner commands, alerts, dashboard
- Owner commands over WhatsApp (`service.COMMANDS`): `list`, `done <id>`, `reply <id> <text>`, `help`, `confirm <id>`, `decline <id>`.
  Staff numbers may send `confirm/decline/help`. Only the exact owner number is honoured, after the Meta signature check.
- Owner alerts: `send_owner_alert` (free text if Fredrick's 24 h window is open, else the `escalation` template; optional SMTP email
  copy via `emailer.py`; 10-minute throttle). It is **lead-centric** (appends `(lead N) Reply "done N"...`), so a weekly summary needs
  a small lead-less variant.
- Dashboard pages: Leads (`/leads`), lead detail (chat view, reply, actions), Demos, Follow-ups (preview of what will be sent),
  Settings (read-only), Log inquiry (`/new`), delete-leads confirm flow, `/healthz`. Mobile-friendly templates.
- Scripts: `submit_templates.py`, `manage_followups.py preview` (a dry-run pattern worth copying), `eval_replies.py`, `diagnose.py`.

### 1.6 Tests
`tests/` uses pytest with fixtures that stub Claude and WhatsApp (`conftest.py`: `sent`, `claude`, `app`), plus scripted transcript
replays in `tests/transcripts/`. New work can follow the same pattern (no network, no API keys).

### 1.7 Reuse / change / missing
| Reuse as is | Needs changing | Missing (new) |
|---|---|---|
| Scheduler tick and `run_async`; dashboard shell, login, CSRF, base template; `send_owner_alert` path and `escalation` template; `emailer` SMTP plumbing (for alerts); delivery-status webhook; `optout_intent` patterns; Claude client + guard functions; `business_facts` official contact; Lagos-day cap helper `_lagos_day_start_utc`; `Message` for conversation history; Settings page layout; `manage_followups.py` preview pattern; test fixtures | `send_whatsapp` needs a consent/suppression/kill-switch check for *outbound-origin* sends; `send_template` needs button components (Stop/quick-reply) and category-aware templates; `record_inbound`/`webhook` must match an inbound number to a Prospect; `followup_block_reason`/`next_followup_time` must **never** chase outbound-origin leads with the "your inquiry" Day 1/3/7 sequence; `send_owner_alert` needs a lead-less variant; config needs DB-backed (not env-only) settings so an emergency stop is instant; `delete_leads` must keep an opt-out stub (see below) | Prospect/Campaign/Source/Draft models; consent and global suppression register; discovery + research + scoring job; opener drafter and checker; work-queue pages; review report; audit log; retention purge; quality-rating/limit monitor; marketing-category templates; public unsubscribe/privacy endpoints (needed only for automated email) |

**Gaps found while reading that matter regardless of this project**
1. `service.delete_leads` removes the lead row, including `opted_out`. After deletion, a new inbound recreates the lead *not*
   opted out, and any outbound list could contact that number again. Deletion must leave a minimal suppression entry.
2. The opt-out is stored on `Lead` only, and `send_whatsapp` enforces it only for templates/follow-ups. A suppression check keyed
   by phone/email, independent of any lead, does not exist.
3. No data-retention policy exists for leads or messages today. (Applies to inbound data too.)
4. Settings cannot be changed without a redeploy.

---

## 2. Risk register

Ratings assume the design in Section 3 with defaults. "Residual" is after mitigation.

| # | Risk | Inherent | Mitigation | Residual |
|---|---|---|---|---|
| R1 | **WhatsApp Business Messaging policy**: business-initiated messages need opt-in and a compliant template; spam reports lower quality (verify current policy). Also verify Meta's current terms on AI/chatbot use on the platform (ours is a business-specific assistant, but check) | High | Cloud API only for contacts with a recorded opt-in; consent register checked at send; no cold messages from the official number; Stop button/keyword on every template | Low-Med |
| R2 | **Quality rating drops** (users block/report) and messaging limit shrinks or number is restricted | High | Quality monitor (3.9); auto-pause outbound when rating is not green; tiny volumes; opt-in only; kill switch; weekly report line | Medium |
| R3 | **Restriction of the official inbound number** (same number serves customers, demo alerts, owner alerts): a ban would take down the whole inbound business | High | Official number never used for cold outreach; outbound Cloud API only to opted-in contacts; consider a second WABA/number *only if* opted-in volume ever justifies it (not now); health alerts to Fredrick | Medium |
| R4 | **Messaging-limit tiers**: new/unverified portfolios have low unique-contact limits (verify) | Medium | Business Verification first (prerequisite); app cap far below the tier; read the tier from the API | Low |
| R5 | **NDPA 2023 direct marketing / right to object**: need lawful basis, notice, an easy way to object, honour objections (verify with counsel, incl. NDPC directives) | High | Basis recorded per prospect; objection in every opener ("reply STOP"); global suppression checked before every send and at queue time; DSR page | Medium (until counsel signs off) |
| R6 | **NCC unsolicited-message rules** (SMS/voice; applicability to WhatsApp/email to be confirmed) | Medium | No SMS or auto-dial built; calls human-led, within set hours; counsel to confirm DND treatment | Low |
| R7 | **Source terms of service** (scraping directories, LinkedIn, social media, Maps) | High | Section 5: no scraping tool built; permitted sources only; each source has a recorded ToS review | Low |
| R8 | **Data minimisation / over-collection**, healthcare context | Medium | Business-level data only; no patient data ever; no personal social profiles; Claude prompts carry no phone/email; 90-day purge for never-engaged prospects | Low |
| R9 | **Cross-border processing** (Anthropic, Meta, hosting region) under NDPA transfer rules | Medium | Record processors and regions in the notice; minimise what leaves; counsel to confirm transfer mechanism | Medium until advised |
| R10 | **Reputation**: pitchy or wrong messages to hospitals/pharmacies; hallucinated facts | Medium | Pain-question openers, not pitches; deterministic checks (no price, no banned terms, no invented claims); human approval of the first 40, then still on; research notes shown beside each draft | Low |
| R11 | **Personal WhatsApp account risk** (manual cold messages from Fredrick's own number) | Medium | Manual only, ≤4/day, one at a time, never bulk or scripted; Stop honoured; stop immediately on any report | Low-Med |
| R12 | **Cost creep** (Claude research, web search, Meta marketing templates) | Low-Med | Weekly target and back-pressure; per-run token budget; monthly cost line in weekly summary; section 6 estimates | Low |
| R13 | **Operational overload** (queue grows, Fredrick falls behind) | Medium | Back-pressure pauses discovery; drafts expire silently; one weekly message; no reminders | Low |
| R14 | **Automation bug sends too much** (loop, duplicate tick, double worker) | High | Gate evaluated at send time against DB counters; per-day caps; kill switch; automated sending off by default; one gunicorn worker already enforced; idempotent state transitions | Low |
| R15 | **Existing follow-up machinery misfires on prospects** ("following up on your inquiry") | High | Outbound-origin leads created with follow-ups off; explicit test (Section 6) | Low |
| R16 | **Suppression lost** (lead deletion, restore from backup) | High | Separate suppression table that survives lead and prospect deletion | Low |

---

## 3. Architecture: a modular extension

New package `sentinel/outbound/` (plain modules, a Blueprint registered like `dashboard`), **no new service, cron job or dependency**.
It adds new tables (created by `db.create_all()`) and a few columns on `leads` (via `NEW_COLUMNS`).

```
sentinel/outbound/
  models.py      Campaign, Prospect, ProspectSource, Draft, ConsentRecord, Suppression, OutboundSetting, AuditEvent, ReviewReport
  settings.py    get/set with env defaults; kill switch
  discovery.py   Claude research -> prospects (cap + back-pressure)
  scoring.py     rule-based score + reasons (Claude supplies facts, code scores)
  drafting.py    opener generation + deterministic checks
  gate.py        can_send(): consent, suppression, caps, channel, approval, kill switch
  sender.py      Cloud API / email send via existing helpers; manual "mark sent"
  queue_views.py dashboard Blueprint: Today, Prospect card, Call sheet, Settings, Report
  report.py      review report + weekly summary
  retention.py   expiry, purge, DSR export/delete
```

### 3.1 Data model sketch
```text
Campaign        id, key ('erp'|'ehr'), name, product, active, weekly_target, max_pending, segments(JSON list),
                scoring(JSON weights/thresholds), opener_rules(JSON: pain themes, tone, banned terms),
                daily_send_cap, allowed_channels(JSON), template_key (Meta template for opted-in sends)

ProspectSource  id, key, name, kind ('registry'|'directory'|'web'|'referral'|'manual_list'|'inbound'),
                tos_note, tos_reviewed_on, allowed_use, enabled

Prospect        id, campaign_id, source_id, source_url, found_at,
                org_name, segment, city, state, website,
                contact_role, contact_name(optional), business_phone(optional), business_email(optional),   # business contact details only
                basis ('b2b_legitimate_interest'|'consent'|'inbound'|'referral'|'existing_relationship'),  # basis for contacting
                score, score_reasons(JSON), pain_hypothesis, research_notes(short),
                status: discovered|researched|qualified|drafted|awaiting_review|approved|sent|replied|
                        demo|won|lost|suppressed|expired|deleted_stub
                review_expires_at, lead_id (FK, set when they reply or a Cloud API conversation starts),
                retention_until, created_at, last_action_at

Draft           id, prospect_id, channel ('whatsapp_cloud'|'email'|'wa_manual'|'email_manual'|'call'),
                kind ('first_touch'|'followup'), subject, body, template_params(JSON), version,
                status: draft|approved|rejected|expired|sent|failed|blocked,
                approved_at, approved_by, edited_by_human, counts_toward_gate,
                sent_at, sent_mode ('auto'|'manual'), external_id, delivery_status, error, block_reason

ConsentRecord   id, contact_key (normalised phone/email), channel, kind ('opt_in'|'objection'|'withdrawal'),
                basis_text, evidence (e.g. inbound wamid / form entry id / note), recorded_at, recorded_by, prospect_id?, lead_id?

Suppression     id, contact_key (unique), kind ('phone'|'email'|'domain'), reason, added_at, source
                # global do-not-contact; NEVER deleted by lead/prospect deletion; holds the minimum needed to honour it

OutboundSetting key, value, updated_at, updated_by          # DB-backed, env var supplies the default
AuditEvent      id, at, actor ('system'|'owner'), action, entity, entity_id, detail(JSON, no message bodies)
ReviewReport    id, generated_at, window_start/end, sent_count, metrics(JSON), acknowledged_at
Lead (+cols)    origin ('inbound'|'outbound'), prospect_id          # origin='outbound' => follow-ups off, see 3.6
```
Reuse of `Message`: when a prospect replies or a Cloud API send happens, a `Lead` exists and the opener is stored as a
`Message` (outbound) so the existing pipeline sees the conversation.

### 3.2 Settings (all new; DB-backed with env defaults, editable on a new "Outbound settings" page)
| Setting | Default | Purpose |
|---|---|---|
| `OUTBOUND_ENABLED` | off | Master switch for anything outbound-related (discovery included) |
| `OUTBOUND_KILL` | off | **Emergency stop**: blocks every send and pauses discovery; one tap on the page and an owner command `stop outbound`; env var `OUTBOUND_KILL=1` also forces it |
| `OUTBOUND_WEEKLY_TARGET` (per campaign) | ERP 10, EHR 10 (see open question) | Qualified prospects to produce per week |
| `OUTBOUND_MAX_PENDING` | 12 | Back-pressure: discovery pauses while prospects awaiting review exceed this |
| `OUTBOUND_DRAFT_EXPIRY_DAYS` | 3 | Un-actioned drafts expire; no reminders |
| `OUTBOUND_APPROVAL_REQUIRED` | on | Stays on after the review; only Fredrick changes it |
| `OUTBOUND_GATE_COUNT` | 40 | Number of sent messages that triggers the review report |
| `OUTBOUND_GATE_COUNTS` | `first_touch` | What counts: `first_touch` or `all` (also follow-ups). Configurable |
| `OUTBOUND_FOLLOWUP_REUSE_APPROVED` | on | Follow-ups to an approved prospect reuse approved wording (no new Claude copy) under the same caps |
| `OUTBOUND_DAILY_CAP_TOTAL` / per campaign / per channel | 6 / 4 / 4 (manual), 5 (Cloud) | Hard daily ceilings (Lagos day) |
| `OUTBOUND_SEND_HOURS` | Mon–Fri 09:00–17:00 Lagos | Automated sends only inside this window |
| `OUTBOUND_CHANNEL_WA_CLOUD` | off | Cloud API outbound (opt-in contacts only) |
| `OUTBOUND_CHANNEL_EMAIL_AUTO` | **off** | Automated email, off until Fredrick enables it after legal advice |
| `OUTBOUND_CHANNEL_MANUAL` | on | Queue items for personal WhatsApp, manual email and calls |
| `OUTBOUND_MIN_QUALITY` | green | Cloud sends pause if the WhatsApp quality rating is below this |
| `OUTBOUND_RETENTION_DAYS` | 90 | Never-engaged prospects are purged after this |
| `OUTBOUND_RESEARCH_BUDGET_USD_WEEK` | 5 | Hard stop for Claude spend on discovery per week (estimate-based counter) |
| `OUTBOUND_REPORT_PAUSES_AUTO` | on | After the 40th send, automated channels stay paused until the report is acknowledged |

### 3.3 Prospect and source tracking
Every prospect row carries `source_id`, `source_url`, `found_at`, `basis`, `segment`, `campaign_id`, `score` and `score_reasons`
(short bullets, e.g. "private clinic, 3 doctors listed", "no patient portal visible", "publishes a business WhatsApp number").
A prospect cannot reach `qualified` without a source, a basis and a business contact route. Duplicates are matched on normalised
phone, email, domain and org name+city, and against `Lead` and `Suppression`.

### 3.4 Discovery, research, qualification and scoring (copilot)
1. **Discovery job** runs from the existing tick, but only starts when: `OUTBOUND_ENABLED`, not killed, the weekly target for a campaign is
   unmet, pending reviews ≤ `OUTBOUND_MAX_PENDING`, within the weekly Claude budget, and no run happened in the last N hours
   (`last_run_at` in `OutboundSetting`, so a restart cannot double-run). It runs on `run_async` (own thread) so it never delays the
   follow-up tick.
2. Claude is given the campaign segments and *permitted source material* (Section 5) and returns structured candidate facts about
   organisations. **Claude never receives phone numbers or emails**; code attaches business contact details from the source record.
3. **Scoring is rule-based and explainable**: Claude extracts facts (size signals, current-system signals, location, whether a decision maker is
   named); code applies per-campaign weights and thresholds from `Campaign.scoring`, so Fredrick can see why a score is what it is. Scores below the
   threshold are discarded (and not stored beyond a hash of org+domain to avoid rediscovery).
4. Qualified prospects (score ≥ threshold, basis set, not suppressed, not an existing client, not already a lead) proceed to drafting.

### 3.5 Opener drafting
One Claude call per qualified prospect, using the campaign `opener_rules`: **start with a relevant pain question** (e.g. for a
pharmacy: "How are you tracking expiry dates and stock-outs across your shelves today?"), one short context line on why we are
writing and where we found them, no product pitch, no price, no feature list, a clear way out ("Reply STOP and we won't contact you
again"). Then deterministic checks reuse existing guards (`unsupported_terms`, `_PRICE_RE`, banned phrases, `is_generic_opener`,
word limit, exactly one question, no links except the official one). A draft failing twice is dropped, not sent to Fredrick. Each draft
stores its research note beside it so he approves with evidence in view. Three variants of wording per campaign are fine; do not
generate unbounded variety.

For the Cloud API channel the opener must fit a **Marketing-category template** with the pain question in a variable slot (same
`{{3}}`-style pattern as `templates_meta.py`; bodies cannot start or end with a variable). That is a real constraint: Cloud-API
openers are short and templated; personalisation lives in the slot.

### 3.6 Approval queue, channel gate, consent and suppression
**`gate.can_send(draft)`** is one function, called (a) when a draft is queued, (b) when approved, and (c) **again immediately before
any automated send and before showing a manual "send" link**. It returns `(ok, reason)`, logs an `AuditEvent` on refusal, and checks in
this order:

1. Kill switch and `OUTBOUND_ENABLED`.
2. Campaign active; channel enabled for that channel and campaign.
3. **Suppression**: contact_key (phone, email, domain) in `Suppression`, or a matching `Lead.opted_out`.
4. **Consent** for `whatsapp_cloud` (a `ConsentRecord` of kind `opt_in` for that phone, not withdrawn). Email-auto requires the basis recorded
   plus counsel-approved mode flag. Manual channels require a recorded basis but not an opt-in.
5. Draft is `approved` (while `OUTBOUND_APPROVAL_REQUIRED`) and not expired.
6. Review checkpoint: if sent count ≥ gate and report unacknowledged, automated channels are blocked (manual still allowed).
7. Daily caps (total / campaign / channel) and send hours; per-prospect rule: one first touch, then follow-ups only per the reuse rule.
8. WhatsApp quality rating at or above `OUTBOUND_MIN_QUALITY` and Meta template approved (Cloud only).

**Integration safeguard.** A prospect that starts a Cloud API conversation, or replies, becomes a `Lead` with `origin='outbound'`.
`followup_block_reason` / `next_followup_time` get one early return for `origin='outbound'` so the Day 1/3/7 "your inquiry"
sequence can never fire on them. (Today these functions would otherwise treat a new lead as an inquirer; see §1.3.)

### 3.7 The 40-message approval gate and the review report
- **Counter:** `count(Draft where sent_at is not null and status='sent' and counts_toward_gate)`. Drafts, rejections and prospects do not
  count. Manual sends count when Fredrick taps "Mark as sent" (we cannot see his phone; this is the only evidence available, and the report
  says "marked sent" for manual channels vs "delivered" for Cloud API).
- **What counts** is `OUTBOUND_GATE_COUNTS`: default `first_touch` (assumption: the 40 are openers). Follow-ups to approved prospects reuse the
  approved wording under the same caps (`OUTBOUND_FOLLOWUP_REUSE_APPROVED`), so they need no new approval; set `all` to count them.
- **Approval stays on after 40.** `OUTBOUND_APPROVAL_REQUIRED` is never changed by code. At the 40th counted send the system:
  generates the report; alerts Fredrick once; and (if `OUTBOUND_REPORT_PAUSES_AUTO`) pauses *automated* channels until he taps
  "Reviewed" (manual queue continues with approval). Nothing increases automatically.
- **Review report contents** (one dashboard page + the same figures in the weekly message):
  messages sent (by channel/campaign); delivered and read (Cloud API) / marked sent (manual); reply rate; positive vs negative replies;
  objections by theme (price, timing, already have a system, not the decision maker, data/trust, other; tagged by a small separate Claude call on
  logged replies, *not* by changing the production classifier); lead quality (score vs outcome); demos requested; conversions (won);
  opt-outs and complaints; WhatsApp quality rating and messaging tier where available; compliance checks (every send had basis + suppression
  check + approval; zero sends to suppressed contacts; consent present for all Cloud sends; counts of expired drafts; any gate refusals).
  Caveat printed in the report: manual-channel replies are only as complete as Fredrick's logging.

### 3.8 Scheduling and daily caps
No new scheduler. `scheduler.tick` gains calls to `outbound.tick()` (expire drafts, purge, send approved Cloud/email drafts that are
due, kick discovery on its own thread, weekly summary check). All state is in the database so a restart cannot double-send: a send
claims the draft with an atomic status update (`approved → sending`) before calling out. Caps use the Lagos-day helper that already exists.

### 3.9 Delivery tracking, quality and failure handling
Reuse the status webhook: look up `Message.external_id` as today, and also match `Draft.external_id`. A failed outbound send is stored with
Meta's code; after 2 failures of one draft it is `failed` and not retried; **3 failures or any template rejection in a day pauses
that channel** and alerts Fredrick once (reuse the existing throttle idea). Quality monitor: add webhook handling for the number's quality/limit
update events and a once-daily read of the phone-number node (fields such as quality rating and messaging-limit tier; **verify field names in
Meta's current docs**); a non-green rating blocks Cloud outbound and appears on the report and Settings. Meta configuration step: subscribe the
app to those fields (Fredrick, one-off).

### 3.10 Emergency stop
`OUTBOUND_KILL` stored in the DB and read at every `can_send`, so it takes effect immediately without a redeploy. Triggers: a button on every
outbound page, the owner command `stop outbound` (added to `service.COMMANDS`, owner only), the env var, and automatic triggers
(quality drop, ≥2 complaint/“spam” signals in a day, failure burst). Resuming is manual only. Each use is an `AuditEvent`.

### 3.11 Audit logging
`AuditEvent` rows for: prospect created/qualified/suppressed, draft created/edited/approved/rejected/expired, send attempted/blocked/
succeeded, setting changed, kill-switch used, consent recorded, DSR actions, report generated/acknowledged. Record who, when, why; **never
message bodies or personal data** in the detail field (ids and reasons only). Kept 12 months.

### 3.12 Compliance: what the software must support so legal advice can be followed
Prerequisites before any send beyond Fredrick's own manual messages: **(a)** Meta Business Verification complete; **(b)** written Nigerian
data-protection and direct-marketing advice (NDPA 2023 and NDPC directives, NCC rules, email/WhatsApp specifics) **(verify)**. The software
will support the likely requirements:

| Need | Support |
|---|---|
| Record of lawful basis per contact | `Prospect.basis` + `ConsentRecord` with evidence text, timestamp, who recorded |
| Record of processing (ROPA) | Export of sources, purposes, processors, retention, from `ProspectSource` + settings (one "Records" page) |
| Privacy notice / notice at first contact | Opener includes who we are and where we found them; a public `/privacy` page (static text Fredrick supplies after advice) linked from emails; wording stored versioned |
| Objection / withdrawal | Keyword Stop (existing `optout_intent`), button on Cloud templates, "They said stop" one-tap on manual prospects, unsubscribe link + `List-Unsubscribe` for any automated email (public tokenised route, **only built in the email phase**) → writes `Suppression` immediately |
| Global do-not-contact | `Suppression` table, checked at queue, approval and send; also checked by the **inbound** side so a re-contact is flagged; survives deletions; import/export |
| Access / correction / deletion requests | DSR page: search by phone/email → show everything held → export or delete, keeping only a suppression stub; audit event; target response time shown |
| Retention | `OUTBOUND_RETENTION_DAYS` purge for never-engaged prospects; replied prospects follow a lead retention period Fredrick sets after advice; suppression kept as long as advised |
| Data minimisation | Business contact data only; no patient or personal-social data; Claude prompts exclude phone/email |
| Cross-border processing | Processors and regions listed in the Records page and notice (Anthropic, Meta, hosting region **to be confirmed from Render settings**); data sent to Claude minimised; counsel decides on transfer mechanism and whether a DPIA/registration with NDPC applies |
| Evidence for audit | `AuditEvent` + report compliance checks |

### 3.13 Hand-off of replies to the inbound pipeline
In `webhook.receive`, after `find_or_create_lead`, look up `Prospect` by phone: if found, set `prospect.status='replied'`, link `lead_id`,
set `lead.origin='outbound'` (if it was created there), write an `opt_in` `ConsentRecord` (they messaged us: user-initiated), and let the existing
`handle_lead_reply` run unchanged. The opener is written into the lead's history as an outbound `Message` (channel `manual`) so the
bot sees the context. The existing opt-out path then also writes a `Suppression` row. Risk to test: the first reply from the bot to a stranger
is the welcome menu (`menu.py`); acceptable, but confirm it reads well after an outbound opener.

---

## 4. The human work queue (sized for about 4 prospects a day)

One dashboard page, **Today** (`/outbound`), phone-first, built from the existing base template.

1. **Ready drafts.** Up to ~6 prospect cards: org, segment, score + reasons, one-line research note, source link, the opener and the
   suggested channel. Each card already contains the final text; nothing needs writing.
2. **Batch approve.** Tick or "select all", edit inline only if wanted, **Approve selected**. Reject takes one tap plus an optional reason
   (reasons feed the report). Approval is a status change only; it sends nothing.
3. **One-tap send links** (manual channels): `https://wa.me/<number>?text=<opener>` for personal WhatsApp, `mailto:<email>?subject=...&body=...` for
   email. Both open Fredrick's own app with the text filled in. The link is generated only when `gate.can_send` passes at that moment.
4. **Mark as sent** on each card (also "Sent all selected"). This writes `sent_at`, counts toward the gate, and starts the reply clock.
5. **Call sheet.** Prospects whose best channel is phone, or who replied "call me": name, role, number, 3 talking points from the research
   note, the pain question, outcome buttons (spoke / no answer / call back / not interested / **do not contact**).
6. **Expiry.** A draft not acted on in `OUTBOUND_DRAFT_EXPIRY_DAYS` expires silently; its prospect returns to `expired` (can be revived with one
   tap). No reminders, no nagging alerts.
7. **Weekly summary.** One WhatsApp (and email copy, if SMTP is set) per week: prospects found by campaign, sent, replies, demos, expired,
   opt-outs, WhatsApp health line, estimated Claude cost, and a link to the report. Delivered through the existing owner-alert path
   (via the existing `escalation` template when his 24 h window is closed); needs the small lead-less variant of `send_owner_alert`.
8. **Logging replies to a personal WhatsApp or manual email.** The system cannot see those chats. On each sent card there is **"They
   replied"** with four buttons: *Interested* · *Not now* · *Not interested* · *Stop/Do not contact*. One tap sets the status; "Stop" writes a
   `Suppression` row. Pasting the reply text is optional (used for objection themes). Email replies can also be pasted through the existing
   **Log inquiry** page, which is extended with a prospect id. No mailbox integration (no IMAP) is built.
9. **Inviting a replying prospect onto the official line.** The card offers a pre-written reply containing `OFFICIAL_WHATSAPP_LINK`
   from `business_facts.py` ("Happy to share more: message us on our official line here <link>"). When the prospect messages that line,
   Section 3.13 links them and records the opt-in, which also makes later Cloud API messages legitimate.

**Fredrick's time (estimates).**
| Activity | Minutes per day |
|---|---|
| Review and batch-approve ~4 cards | 2–3 |
| Tap-to-send and mark-as-sent, 4 messages | 3 |
| One-tap reply logging | 1 |
| **Steady state total** | **about 6–8 per day, about 35–40 per week** (+ about 3 for the weekly summary) |
| During the first 40 sends (read each draft carefully) | about 10–12 per day |
| Calls and real conversations with interested prospects | Sales work, not overhead; not included |

---

## 5. Sources

**Principle:** no scraping tool is built. Discovery reads only what a person or a licensed/permitted access route may read, and every
source is a `ProspectSource` row with a recorded terms-of-service review and allowed use. Prefer warm sources.

**Permitted (subject to a per-source terms check, marked verify):**
1. **Fredrick's own lists and existing relationships**: contacts he already knows, past inquiries, event attendees who agreed to be contacted
   (CSV paste/import with basis recorded). Warmest and safest.
2. **Referrals** from the Mega-Net referral partnership programme (`business_facts.py` has the programme facts): record the referrer and basis.
3. **Public registers of licensed or registered organisations** whose terms allow reference use, e.g. company registry search (CAC), state
   health-facility licensing lists, pharmacy premises registers, health-insurance accredited facility lists (**verify each site's terms and which
   lists actually exist and are public**).
4. **Organisations' own public websites and published business contact pages** (read a few pages through Claude's web search/fetch
   tool if the account offers one **(verify availability, pricing and whether the current model supports it)**; business contact details only; respect robots.txt and
   site terms; a handful of pages per prospect, no crawling).
5. **Official/licensed APIs** (e.g. a mapping-data API under its licence; check the storage and caching limits before keeping results).
6. **Inbound** (website forms, WhatsApp inquiries): not prospecting, but the same register.
7. **Manual LinkedIn**: Fredrick may look at profiles by hand to understand a role and may send a connection request or message by hand.
   The system stores at most a note "LinkedIn: identified role X" typed by him.

**Must not be used**
- Scraping or automating LinkedIn, Facebook, Instagram, X, WhatsApp groups/communities, Google Maps pages, or directory sites whose terms forbid it.
- Buying or renting contact lists of unknown provenance, or harvested "WhatsApp number" databases.
- Personal (non-business) phone numbers, personal email addresses, or any patient/health data about individuals.
- Any tool that mass-messages from personal WhatsApp, WhatsApp automation hacks, unofficial WhatsApp libraries.
- Contacting anyone who is in `Suppression`, an existing client (`Lead.is_existing_client`/`EXISTING_CLIENT_NUMBERS`), or a current partner.
- Feeding contact details into Claude prompts when business-level facts suffice.

---

## 6. Phases (safest first)

Sizes: S ≈ ½–1 day of build, M ≈ 2–4 days, L ≈ 1–2 weeks. Fredrick's time is split into one-off (setup, testing) and then steady-state minutes.
Costs are **estimates**; check current rate cards before relying on them.

Cost basis: Claude Haiku-class input at about US$1 per million tokens and output at about US$5 per million (**estimate, verify**). A researched,
scored and drafted prospect is assumed to use roughly 20–40k input and 3–5k output tokens (≈ US$0.04–0.10), plus any web-search fee if that tool is used
(**verify**). Allow US$0.15–0.30 per prospect all-in. WhatsApp marketing template: assume US$0.03–0.06 per delivered message in Nigeria (**estimate, verify
on Meta's rate card**).

| Phase | Content | Size | Fredrick's time (one-off / steady) | New outbound sending? |
|---|---|---|---|---|
| 0 | Prerequisites: Meta Business Verification; legal advice; decisions in §7 | n/a | 2–4 h of his time, plus counsel | None |
| 1 | Foundations: tables, DB settings page, kill switch, audit, suppression import, lead-deletion fix, origin guard | M | 30 min test / 0 | None |
| 2 | Discovery, research, scoring, back-pressure (per campaign) | L | 1 h tuning / 0 | None |
| 3 | Opener drafting, work queue, manual "mark sent", call sheet, expiry | M | 1 h tuning / 6–8 min/day | **Only human-sent** |
| 4 | Gate counter, review report, weekly summary, reply logging, official-line hand-off | M | 30 min / +3 min/week; report review ~20 min once at 40 | Human-sent only |
| 5 | Consent register + WhatsApp Cloud outbound (opt-in contacts only), marketing templates, quality monitor | L | 1 h Meta/template setup / ~+1–2 min/day | **Yes, off by default** |
| 6 | Automated email (built, off) with unsubscribe, SPF/DKIM check | M | 1 h / 0 until enabled | Yes, off, after legal advice |

### Phase 1: Foundations (M)
- *Acceptance:* all new tables exist; settings editable on a page and default **off**; kill switch stops everything within one request; suppression
  import and lookup work; `delete_leads` leaves an opt-out stub; `origin='outbound'` leads never get Day 1/3/7; every change writes an audit row.
- *Tests:* unit tests for settings precedence (DB over env); kill-switch blocks `can_send`; deletion keeps suppression; follow-up guard test using existing
  `followup_block_reason` fixtures; migration test that `NEW_COLUMNS` adds columns to an old SQLite DB.
- *Cost:* none (no Claude, no WhatsApp).

### Phase 2: Discovery, research, scoring (L)
- *Acceptance:* with the feature on and a stubbed Claude, 20 prospects/week are produced per configured split; stops at the target; pauses when pending > max;
  every prospect has source, basis, segment, campaign, score and reasons; duplicates, existing leads, clients and suppressed contacts are never created; weekly Claude budget enforced.
- *Tests:* stub the Claude client (as `conftest.claude` does); property tests for dedupe; back-pressure toggles; budget stop; restart idempotence (job state in DB); a test that no phone/email appears in the prompt.
- *Estimated cost:* about 20 prospects × US$0.15–0.30 ≈ **US$3–6/week**, **US$13–26/month** (Claude only).

### Phase 3: Drafting and work queue (M)
- *Acceptance:* each qualified prospect has a draft that starts with a pain question, has no price/pitch, one question, and an opt-out line; batch approve works on a phone; wa.me and mailto links open with correct, URL-encoded text and are produced only when the gate passes; expired drafts disappear; nothing is sent by the server.
- *Tests:* an eval script like `scripts/eval_replies.py` over ~40 synthetic prospects checking openers against the deterministic guards; template-render and link-encoding tests; a test that **monkeypatches `whatsapp._post` and the SMTP call to fail the test if invoked**; expiry tests.
- *Estimated cost:* drafting ≈ US$0.01–0.03 per prospect, folded into Phase 2 figure. WhatsApp: **US$0**.

### Phase 4: Gate, report, weekly summary, reply logging (M)
- *Acceptance:* counter counts only sent messages (not drafts/rejections/prospects) per `OUTBOUND_GATE_COUNTS`; at the 40th a report is created once and Fredrick alerted once; approval remains required afterwards; automated channels (when later built) are blocked until "Reviewed"; weekly summary arrives once; "They replied" and "Stop" update status and suppression; replying to the official line links the prospect and logs an opt-in.
- *Tests:* counter edge cases (39/40/41, failed sends, mode `all`); report metrics from seeded data; summary sent exactly once per week across ticks and restarts; inbound hand-off test through `webhook.receive` with a signed payload; Stop → no future `can_send`.
- *Estimated cost:* objection-theme tagging ≈ US$0.005 per reply; negligible. WhatsApp: summary uses owner-alert path ≈ **US$0.01–0.06/week** if the template is needed.

### Phase 5: Consent register and Cloud API outbound (L). Start only after Phase 4 report, Business Verification and legal advice.
- *Acceptance:* with the channel off nothing sends; with it on, only contacts with an `opt_in` record are messaged, using an approved Marketing template that includes a Stop control; quality below threshold blocks sends; daily caps respected; failures are recorded and a failure burst pauses the channel; opt-out replies suppress immediately.
- *Tests:* gate tests for each check in 3.6 individually and in combination; the template payload (with button component) built correctly against a stubbed `_post`; delivery-status events update `Draft`; quality-event webhook blocks sends; a soak test of 200 queued drafts against caps; no sends outside hours.
- *Estimated cost:* ≈ 40 messages × US$0.03–0.06 ≈ **US$1–3** for the first 40; then ≈ US$1–5/month at ≤5 a day on opted-in contacts. Meta template approval takes time but no fee.

### Phase 6: Automated email, off by default (M). Only after written legal advice.
- *Acceptance:* default off; when on, each email carries identification, the source, an unsubscribe link and `List-Unsubscribe`; unsubscribe is public, one-click and writes `Suppression`; hard bounces suppress; sender domain authentication confirmed by Fredrick; same caps and approval.
- *Tests:* unsubscribe token tests; bounce handling; gate tests; no email when `OUTBOUND_CHANNEL_EMAIL_AUTO` is off.
- *Estimated cost:* SMTP provider cost only (existing SMTP plumbing is alert-grade; a transactional provider may be needed) **(verify)**.

**Fredrick's weekly time by stage (estimates):** Phases 1–2 ≈ 0 min/week once set up; Phase 3 ≈ 35–40 min/week (first 40 sends: about 55–60); Phase 4 adds ≈ 5 min/week and one 20-minute report review;
Phase 5 adds ≈ 5–10 min/week only if opted-in volume exists.

---

## 7. What not to build, and open questions

**Not to build**
- No scraping or crawling tool, no browser automation, no LinkedIn/Facebook/Maps data extraction.
- No automatic outreach from the official number to contacts without a recorded opt-in; no bulk messaging from personal WhatsApp.
- No auto-dialer, SMS blast, or voice-bot calling.
- No automatic increase of automation after the review; no code path that changes `OUTBOUND_APPROVAL_REQUIRED`.
- No new service, queue, cron, vector store or CRM; no mailbox/IMAP integration; no A/B-testing framework; no complex multi-step sequences (existing 3-follow-up cap stays).
- No AI-written follow-ups beyond reuse of approved wording until reviewed.
- No second WhatsApp number or WABA for now.

**Open questions for Fredrick**
1. **Split of the 20/week** between ERP Cloud and EHR Cloud (assumed 10/10).
2. **After the 40th send:** should automated channels pause until you tap "Reviewed" (assumed yes), and should manual sending continue meanwhile (assumed yes)?
3. **What counts toward the 40:** first-touch openers only (assumed), or also follow-ups?
4. Are **manual "marked sent"** messages acceptable as counted sends (we cannot verify them)?
5. Which **counsel** and by when? Who is the Nigerian data-protection contact, and is registration with NDPC applicable to Mega-Net?
6. **Meta Business Verification** status? Is the 40-message review something you want before *any* Cloud API outbound (assumed yes)?
7. Is a **web-search/research tool** available on your Anthropic account and are you comfortable with its per-search fee? If not, discovery relies on pasted lists and permitted registers.
8. Which **registers/directories** do you already use and trust (we will check their terms)?
9. Do you want a **separate WhatsApp number** for outbound if opted-in volume grows (decision deferred)?
10. **Retention periods** (never-engaged prospects 90 days assumed; replied leads: your choice after advice).
11. **Working hours/days** for sends and calls (assumed Mon–Fri 09:00–17:00 Lagos) and who makes calls.
12. Who may approve (assumed only you); should staff numbers be able to approve?
13. Monthly **budget ceiling** for Claude/WhatsApp (assumed ≤ US$30 combined).

**Items not verified in the code** (flag for the implementer): behaviour of `ai._system`/prompt assembly for a lead with outbound history; the content of
`lead.html`/`followups.html` (not read); how the welcome menu reads after an outbound opener; Render region for the database/web service; whether the
account's Claude model supports server-side web search.
