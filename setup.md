1. Create a new Python Worker project in a clean folder, then keep the generated `pyproject.toml` and tooling files:
   `uv init mailchimp-reports-worker`
   `cd mailchimp-reports-worker`
   `uv add --dev workers-py workers-runtime-sdk`
   `uv run pywrangler init`

2. Replace the generated `wrangler.toml` and `src/index.py` with the files in [mailchimp-reports-worker/wrangler.toml](C:/Users/JobSearch/Documents/Codex/mailchimp-reports-worker/wrangler.toml) and [mailchimp-reports-worker/src/index.py](C:/Users/JobSearch/Documents/Codex/mailchimp-reports-worker/src/index.py).

3. Create the KV namespace and update the IDs inside `wrangler.toml`:
   `uv run pywrangler kv namespace create CAMPAIGNS`
   Copy the production and preview IDs into `id` and `preview_id`.

4. Set the required Worker secrets. Keep the API key out of source control.
   `uv run pywrangler secret put MC_API_KEY`
   `uv run pywrangler secret put MC_SERVER`
   `uv run pywrangler secret put AUDIENCE_ID`
   `uv run pywrangler secret put MY_EMAIL`
   `uv run pywrangler secret put RESEND_API_KEY`
   `uv run pywrangler secret put RESEND_FROM_EMAIL`

5. Recommended extra secrets:
   `uv run pywrangler secret put MAILCHIMP_WEBHOOK_SECRET`

6. Use these values when prompted:
   `MC_SERVER=us4`
   `AUDIENCE_ID=4099ce1f72`
   `MY_EMAIL=adarshwork11@gmail.com`
   `RESEND_FROM_EMAIL=<a sender address on a Resend-verified domain, for example hello@notify.yourdomain.com>`

7. Deploy the worker:
   `uv run pywrangler deploy`

8. Configure the Mailchimp webhook:
   Use `https://<your-worker>.<your-subdomain>.workers.dev/webhook/<secret>` if you set `MAILCHIMP_WEBHOOK_SECRET`.
   Otherwise use `https://<your-worker>.<your-subdomain>.workers.dev/webhook`.

9. Configure the event so Mailchimp posts every newly sent campaign. The worker stores each campaign as `campaign:{id}` in KV with a `pending` status, then the daily cron at `0 9 * * *` checks campaigns whose stored `send_date` is exactly two days old and emails each report individually before marking them `reported`.

10. Manual routes:
    `GET /report/<campaign_id>` returns the HTML report in-browser.
    `POST /report/<campaign_id>` generates and emails that report immediately.
    `GET /report/<campaign_id>?email=1` previews and dispatches in one request.

11. Important deployment notes:
    Cloudflare cron expressions run in UTC, so `0 9 * * *` means `09:00 UTC` every day.
    The sender used for outbound email must be on a domain or subdomain verified inside Resend.
    The worker reads Mailchimp credentials from secrets and only uses KV for campaign/report state.

12. Resend domain setup:
    Verify a subdomain such as `notify.discover-mastersystems.com` inside Resend, then add the DNS records Resend gives you in Cloudflare DNS.
    For this project, the created Resend sender domain is `notify.discover-mastersystems.com` and the sender address can be `hello@notify.discover-mastersystems.com`.
