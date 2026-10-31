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

## 5. Render plans

- **Web service: Starter or higher (always on).** Free instances sleep after idle. A sleeping service misses Meta webhooks and its in-process follow-up scheduler stops. The service must also run exactly **one** worker (already set), or follow-ups would be sent twice.
- **Postgres: paid (`basic-256mb`).** Render's free database expires after about 30 days and is deleted. Leads must not disappear.
