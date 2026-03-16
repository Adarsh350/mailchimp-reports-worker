# Mailchimp Reports Worker

Cloudflare Worker for event-driven Mailchimp campaign reporting.

## What It Does

- Accepts Mailchimp webhook events for newly sent campaigns
- Stores campaign metadata in Cloudflare KV
- Runs a daily cron to find campaigns sent exactly two days earlier
- Builds branded HTML performance reports from Mailchimp reporting data
- Emails each report individually through Resend
- Supports manual preview and manual send routes per campaign

## Main Files

- `src/index.py` - Worker logic, reporting, webhook handling, cron processing
- `wrangler.toml` - Cloudflare Worker configuration
- `setup.md` - deployment and secret setup steps

## Routes

- `POST /webhook/<secret>` - Mailchimp webhook endpoint
- `GET /report/<campaign_id>` - preview a report in the browser
- `GET /report/<campaign_id>?email=1` - preview and send a report
- `POST /report/<campaign_id>` - send a report immediately

## Notes

- Mailchimp credentials and Resend credentials are stored as Worker secrets
- Campaign state is stored in Cloudflare KV
- The cron schedule is `0 9 * * *` in UTC

See [setup.md](./setup.md) for setup details.
