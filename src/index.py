from __future__ import annotations

import asyncio
import base64
import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from js import Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint, fetch


MAX_RETRIES = 4
MAX_WEBHOOK_BODY_BYTES = 128 * 1024
PROCESS_BATCH_SIZE = 4
DASHBOARD_RUN_LIMIT = 12
DASHBOARD_CAMPAIGN_LIMIT = 16
RECENT_SENT_CAMPAIGN_LIMIT = 5
METRIC_LABELS = {
    "deliveries": "Emails Delivered",
    "delivery_rate": "Delivery Rate",
    "bounce_rate": "Bounce Rate",
    "unique_open_rate": "Unique Open Rate",
    "click_rate": "Click Rate (CTR)",
    "ctor": "Click-to-Open Rate",
    "unsubscribes": "Unsubscribes",
    "unsubscribe_rate": "Unsubscribe Rate",
    "abuse_reports": "Abuse Reports",
}
STATUS_STYLE = {
    "EXCEEDED": "status-good",
    "MET": "status-good",
    "BELOW": "status-bad",
    "N/A": "status-neutral",
}
CAMPAIGN_NUMBER_RE = re.compile(r"(?:campaign\s*)?(\d+)", re.IGNORECASE)
TAGGED_SEGMENT_RE = re.compile(r"tagged\s+([A-Za-z0-9 &/_-]+)", re.IGNORECASE)
STRIP_TAGS_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


class MailchimpApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any], url: str):
        message = body.get("detail") or body.get("title") or f"Mailchimp API error {status}"
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path.rstrip("/") or "/"
        query = parse_qs(url.query, keep_blank_values=True)

        try:
            if request.method == "GET" and path == "/healthz":
                return json_response(
                    {
                        "ok": True,
                        "service": "mailchimp-reports-worker",
                        "schedule": "0 9 * * *",
                        "webhook_secret_configured": bool(
                            get_optional_env(self.env, "MAILCHIMP_WEBHOOK_SECRET")
                        ),
                    }
                )

            if request.method == "GET" and path in {"/", "/dashboard"}:
                return html_response(render_dashboard_shell())

            if request.method == "GET" and path == "/api/dashboard":
                return await self.handle_dashboard_api()

            if request.method in {"GET", "HEAD"} and is_webhook_path(
                path, get_optional_env(self.env, "MAILCHIMP_WEBHOOK_SECRET")
            ):
                if request.method == "HEAD":
                    return Response(status=200)
                return json_response({"ok": True, "webhook": "ready"})

            if request.method == "POST" and is_webhook_path(
                path, get_optional_env(self.env, "MAILCHIMP_WEBHOOK_SECRET")
            ):
                return await self.handle_webhook(request)

            if request.method == "GET" and is_test_email_path(
                path, get_optional_env(self.env, "MAILCHIMP_WEBHOOK_SECRET")
            ):
                return await self.handle_test_email()

            if path.startswith("/report/"):
                campaign_id = path.split("/", 2)[2].strip()
                if not campaign_id:
                    return json_response(
                        {"ok": False, "error": "Campaign ID is required."}, 400
                    )
                if request.method == "GET":
                    return await self.handle_manual_report(
                        campaign_id,
                        query,
                        send_email_flag=query_truthy(query, "email"),
                    )
                if request.method == "POST":
                    return await self.handle_manual_report(
                        campaign_id, query, send_email_flag=True
                    )
                return json_response({"ok": False, "error": "Method not allowed."}, 405)

            return json_response({"ok": False, "error": "Not found."}, 404)
        except MailchimpApiError as error:
            log_event(
                "mailchimp_api_error",
                {
                    "status": error.status,
                    "url": error.url,
                    "detail": error.body.get("detail"),
                },
            )
            return json_response(
                {
                    "ok": False,
                    "error": "Mailchimp API request failed.",
                    "status": error.status,
                    "detail": error.body.get("detail"),
                },
                502,
            )
        except Exception as error:
            log_event("request_failed", {"path": path, "error": str(error)})
            return json_response(
                {
                    "ok": False,
                    "error": "Internal server error.",
                    "detail": str(error),
                },
                500,
            )

    async def scheduled(self, controller):
        scheduled_time = getattr(controller, "scheduledTime", None)
        await self.process_due_campaigns(scheduled_time)

    async def handle_webhook(self, request):
        body = await request.text()
        if len(body.encode("utf-8")) > MAX_WEBHOOK_BODY_BYTES:
            return json_response({"ok": False, "error": "Webhook payload too large."}, 413)

        payload = parse_webhook_payload(body, request.headers.get("content-type") or "")
        campaign_id = first_value(
            payload,
            "data.id",
            "campaign.id",
            "campaign_id",
            "id",
            "campaign.web_id",
            "data.web_id",
        )
        explicit_status = normalize_token(
            first_value(payload, "data.status", "status", "action")
        )
        explicit_type = normalize_token(
            first_value(payload, "type", "event", "data.type", "data.event")
        )

        if explicit_status and explicit_status not in {"sent", "sending", "campaign_sent"}:
            return json_response({"ok": True, "ignored": True, "reason": explicit_status}, 202)

        if explicit_type and "campaign" not in explicit_type and "send" not in explicit_type:
            return json_response({"ok": True, "ignored": True, "reason": explicit_type}, 202)

        if not campaign_id:
            return json_response(
                {"ok": False, "error": "Webhook payload did not include a campaign ID."},
                400,
            )

        campaign = await fetch_mailchimp_json(self.env, f"/campaigns/{campaign_id}")
        audience_id = nested_get(campaign, "recipients", "list_id")
        configured_audience_id = require_env(self.env, "AUDIENCE_ID")
        if audience_id and audience_id != configured_audience_id:
            return json_response(
                {
                    "ok": True,
                    "ignored": True,
                    "reason": "different_audience",
                    "campaign_id": campaign_id,
                    "audience_id": audience_id,
                },
                202,
            )

        send_time = campaign.get("send_time") or first_value(
            payload, "fired_at", "data.send_time", "send_time"
        )
        send_date = normalize_send_date(send_time)
        record_key = campaign_kv_key(campaign_id)
        existing = await load_campaign_record(self.env, record_key)
        stored_record = build_campaign_record(
            existing, campaign, configured_audience_id, send_date
        )
        await store_campaign_record(self.env, record_key, stored_record)

        log_event(
            "campaign_webhook_stored",
            {
                "campaign_id": campaign_id,
                "send_date": send_date,
                "title": stored_record["title"],
                "status": stored_record["status"],
            },
        )
        return json_response(
            {
                "ok": True,
                "campaign_id": campaign_id,
                "send_date": send_date,
                "title": stored_record["title"],
                "status": stored_record["status"],
            }
        )

    async def handle_manual_report(
        self, campaign_id: str, query: dict[str, list[str]], send_email_flag: bool
    ):
        cached_records = await load_all_campaign_records(self.env)
        base_record = record_by_id(cached_records, campaign_id)
        bundle = await build_report_bundle(
            self.env, campaign_id, base_record, cached_records
        )
        html_body = render_report_html(bundle)

        if send_email_flag:
            record = await persist_report_result(
                self.env,
                bundle,
                html_body,
                "manual",
                prior_record=base_record,
            )
            return json_response(
                {
                    "ok": True,
                    "campaign_id": campaign_id,
                    "status": record["status"],
                    "emailed_to": require_env(self.env, "MY_EMAIL"),
                    "subject": report_subject(bundle),
                }
            )

        return html_response(html_body)

    async def handle_test_email(self):
        timestamp = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
        subject = f"Codex delivery test {timestamp}"
        html_body = (
            "<html><body style=\"font-family:Arial,sans-serif;color:#111827;\">"
            "<h1 style=\"font-size:20px;\">Mailchimp Reports Worker Test</h1>"
            f"<p>This is a live delivery test sent at <strong>{html.escape(timestamp)}</strong>.</p>"
            "<p>If you received this, Cloudflare Worker email delivery is working end to end.</p>"
            "</body></html>"
        )
        plain_text = (
            "Mailchimp Reports Worker Test\n\n"
            f"This is a live delivery test sent at {timestamp}.\n"
            "If you received this, Cloudflare Worker email delivery is working end to end.\n"
        )
        await send_report_email(self.env, subject, html_body, plain_text)
        return json_response(
            {
                "ok": True,
                "subject": subject,
                "emailed_to": require_env(self.env, "MY_EMAIL"),
            }
        )

    async def handle_dashboard_api(self):
        payload = await build_dashboard_payload(self.env)
        return json_response(payload)

    async def process_due_campaigns(self, scheduled_time):
        now_utc = (
            utc_now()
            if scheduled_time is None
            else datetime.fromtimestamp(int(scheduled_time) / 1000, tz=timezone.utc)
        )
        target_date = (now_utc.date() - timedelta(days=2)).isoformat()
        all_records = await load_all_campaign_records(self.env)
        due_records = [
            record
            for record in all_records
            if record.get("status") == "pending"
            and record.get("send_date") == target_date
        ]
        run_record = {
            "id": run_id_for_time(now_utc),
            "trigger": "scheduled",
            "status": "running",
            "started_at": now_utc.replace(microsecond=0).isoformat(),
            "target_date": target_date,
            "cron_schedule": "0 9 * * *",
            "total_cached_campaigns": len(all_records),
            "pending_campaigns": len(due_records),
            "processed_campaigns": 0,
            "successful_campaigns": 0,
            "failed_campaigns": 0,
            "campaigns": [],
        }
        await store_run_record(self.env, run_record)

        log_event(
            "scheduled_scan_completed",
            {
                "target_date": target_date,
                "pending_campaigns": len(due_records),
                "total_cached_campaigns": len(all_records),
            },
        )

        for chunk in chunked(due_records, PROCESS_BATCH_SIZE):
            results = await asyncio.gather(
                *(self.process_one_due_campaign(record, all_records) for record in chunk),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    run_record["failed_campaigns"] += 1
                    run_record["campaigns"].append(
                        {"status": "failed", "title": "Unhandled exception", "error": str(result)}
                    )
                    continue
                run_record["processed_campaigns"] += 1
                if result.get("status") == "reported":
                    run_record["successful_campaigns"] += 1
                elif result.get("status") == "failed":
                    run_record["failed_campaigns"] += 1
                run_record["campaigns"].append(result)

        if not due_records:
            run_record["status"] = "idle"
        elif run_record["failed_campaigns"] and run_record["successful_campaigns"]:
            run_record["status"] = "partial"
        elif run_record["failed_campaigns"]:
            run_record["status"] = "failed"
        else:
            run_record["status"] = "reported"
        run_record["completed_at"] = iso_now()
        await store_run_record(self.env, run_record)

    async def process_one_due_campaign(
        self, record: dict[str, Any], cached_records: list[dict[str, Any]]
    ):
        campaign_id = record["id"]
        try:
            bundle = await build_report_bundle(
                self.env, campaign_id, record, cached_records
            )
            html_body = render_report_html(bundle)
            stored_record = await persist_report_result(
                self.env,
                bundle,
                html_body,
                "scheduled",
                prior_record=record,
            )
            record.clear()
            record.update(stored_record)
            log_event(
                "campaign_report_sent",
                {
                    "campaign_id": campaign_id,
                    "title": stored_record["title"],
                    "send_date": stored_record["send_date"],
                },
            )
            return {
                "campaign_id": campaign_id,
                "title": stored_record["title"],
                "send_date": stored_record["send_date"],
                "status": "reported",
                "reported_at": stored_record.get("reported_at"),
            }
        except Exception as error:
            record["last_error"] = str(error)
            record["last_attempted_at"] = iso_now()
            await store_campaign_record(self.env, campaign_kv_key(campaign_id), record)
            log_event(
                "campaign_report_failed",
                {"campaign_id": campaign_id, "error": str(error)},
            )
            return {
                "campaign_id": campaign_id,
                "title": record.get("title") or campaign_id,
                "send_date": record.get("send_date"),
                "status": "failed",
                "error": str(error),
                "attempted_at": record.get("last_attempted_at"),
            }


def build_campaign_record(
    existing: dict[str, Any] | None,
    campaign: dict[str, Any],
    audience_id: str,
    send_date: str,
) -> dict[str, Any]:
    record = dict(existing or {})
    settings = campaign.get("settings") or {}
    recipients = campaign.get("recipients") or {}
    record.update(
        {
            "id": campaign["id"],
            "title": settings.get("title")
            or campaign.get("title")
            or record.get("title")
            or campaign["id"],
            "subject_line": settings.get("subject_line") or record.get("subject_line"),
            "from_name": settings.get("from_name") or record.get("from_name"),
            "reply_to": settings.get("reply_to") or record.get("reply_to"),
            "audience_id": audience_id,
            "audience_name": recipients.get("list_name") or record.get("audience_name"),
            "segment_text": recipients.get("segment_text") or record.get("segment_text"),
            "send_time": campaign.get("send_time") or record.get("send_time"),
            "send_date": send_date,
            "status": "reported" if record.get("status") == "reported" else "pending",
            "webhook_received_at": record.get("webhook_received_at") or iso_now(),
            "last_error": None,
        }
    )
    if existing and existing.get("metrics_snapshot"):
        record["metrics_snapshot"] = existing["metrics_snapshot"]
        record["reported_at"] = existing.get("reported_at")
        record["reported_via"] = existing.get("reported_via")
    return record


async def build_report_bundle(
    env,
    campaign_id: str,
    base_record: dict[str, Any] | None,
    cached_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    campaign_path = f"/campaigns/{campaign_id}"
    report_path = f"/reports/{campaign_id}"
    unsubscribed_path = f"/reports/{campaign_id}/unsubscribed?count=1"
    abuse_path = f"/reports/{campaign_id}/abuse-reports?count=1"

    campaign, report, unsubscribed, abuse = await asyncio.gather(
        fetch_mailchimp_json(env, campaign_path),
        fetch_mailchimp_json(env, report_path),
        fetch_mailchimp_json(env, unsubscribed_path),
        fetch_mailchimp_json(env, abuse_path),
    )

    audience_id = require_env(env, "AUDIENCE_ID")
    working_record = build_campaign_record(
        base_record,
        campaign,
        audience_id,
        normalize_send_date(campaign.get("send_time")),
    )
    metrics = compute_metrics(campaign, report, unsubscribed, abuse)
    history_source = (
        cached_records if cached_records is not None else await load_all_campaign_records(env)
    )
    comparison_records = select_comparison_records(history_source, campaign_id)
    comparison_bundle = build_comparison_bundle(metrics, comparison_records)

    send_date = parse_iso_datetime(working_record.get("send_time")) or parse_iso_datetime(
        working_record["send_date"]
    )
    report_context = {
        "campaign_id": campaign_id,
        "title": working_record["title"],
        "subject_line": working_record.get("subject_line") or "",
        "brand_name": working_record.get("from_name")
        or working_record.get("audience_name")
        or "Mailchimp",
        "audience_name": working_record.get("audience_name") or "Audience",
        "segment_name": extract_segment_name(working_record.get("segment_text")),
        "send_time_label": format_datetime_label(send_date, include_time=False),
        "last_activity_label": metrics["last_activity_label"],
        "last_activity_value": metrics["last_activity_value"],
        "current_month_year": utc_now().strftime("%B %Y"),
        "comparison_heading": comparison_bundle["heading"],
        "recommendations_heading": recommendation_heading(working_record["title"]),
    }

    return {
        "record": working_record,
        "campaign": campaign,
        "report": report,
        "metrics": metrics,
        "context": report_context,
        "executive_summary": build_executive_summary(metrics, report_context),
        "delivery_narrative": build_delivery_narrative(metrics, report_context),
        "open_narrative": build_open_narrative(metrics, report_context),
        "click_narrative": build_click_narrative(metrics, report_context),
        "trust_narrative": build_trust_narrative(metrics, report_context),
        "comparison": comparison_bundle,
        "recommendations": build_recommendations(
            metrics, report_context, comparison_bundle
        ),
        "closing_summary": build_closing_summary(
            metrics, report_context, comparison_bundle
        ),
    }


def compute_metrics(
    campaign: dict[str, Any],
    report: dict[str, Any],
    unsubscribed: dict[str, Any],
    abuse: dict[str, Any],
) -> dict[str, Any]:
    opens = report.get("opens") or {}
    clicks = report.get("clicks") or {}
    bounces = report.get("bounces") or {}
    total_sends = as_int(
        report.get("emails_sent") or nested_get(campaign, "recipients", "recipient_count")
    )
    soft_bounces = as_int(bounces.get("soft_bounces"))
    hard_bounces = as_int(bounces.get("hard_bounces"))
    syntax_errors = as_int(bounces.get("syntax_errors"))
    total_bounces = soft_bounces + hard_bounces + syntax_errors
    deliveries = max(total_sends - total_bounces, 0)
    unique_opens = as_int(opens.get("unique_opens"))
    total_opens = as_int(opens.get("opens_total"))
    unique_clicks = as_int(clicks.get("unique_clicks"))
    unique_subscriber_clicks = as_int(
        clicks.get("unique_subscriber_clicks") or unique_clicks
    )
    total_clicks = as_int(clicks.get("clicks_total"))
    unsubscribed_total = as_int(unsubscribed.get("total_items"))
    abuse_total = as_int(abuse.get("total_items"))
    open_rate = percent(opens.get("open_rate"), unique_opens, deliveries)
    click_rate = percent(clicks.get("click_rate"), unique_clicks, deliveries)
    ctor = percent(clicks.get("clicks_to_open_rate"), unique_clicks, unique_opens)
    if ctor == 0.0 and unique_opens > 0:
        ctor = round((unique_clicks / unique_opens) * 100, 1)
    bounce_rate = round((total_bounces / total_sends) * 100, 1) if total_sends else 0.0
    delivery_rate = round((deliveries / total_sends) * 100, 1) if total_sends else 0.0
    unsubscribe_rate = (
        round((unsubscribed_total / deliveries) * 100, 2) if deliveries else 0.0
    )
    last_open = parse_iso_datetime(opens.get("last_open"))
    last_click = parse_iso_datetime(clicks.get("last_click"))
    last_activity = last_click if total_clicks > 0 and last_click else last_open
    last_activity_label = "Last Click" if total_clicks > 0 and last_click else "Last Opened"

    clicks_not_applicable = infer_clicks_not_applicable(
        campaign, total_clicks, unique_clicks
    )
    scorecard_rows = [
        scorecard_row("Emails Delivered", format_integer(deliveries), "—", "MET"),
        scorecard_row(
            "Delivery Rate",
            format_percent(delivery_rate),
            "95%+ healthy",
            delivery_status(delivery_rate),
        ),
        scorecard_row(
            "Bounce Rate",
            format_percent(bounce_rate),
            "Under 5% acceptable",
            bounce_status(bounce_rate),
        ),
        scorecard_row(
            "Unique Open Rate",
            format_percent(open_rate),
            "18%–25% avg | 25%+ strong",
            open_status(open_rate),
        ),
        scorecard_row(
            "Click Rate (CTR)",
            format_percent(click_rate)
            if not clicks_not_applicable
            else "0% — No CTA included",
            "3%+ avg | 5%+ strong",
            "N/A" if clicks_not_applicable else click_status(click_rate),
        ),
        scorecard_row(
            "Click-to-Open Rate",
            format_percent(ctor) if not clicks_not_applicable else "0% — No CTA included",
            "8%–12% healthy",
            "N/A" if clicks_not_applicable else ctor_status(ctor),
        ),
        scorecard_row(
            "Unsubscribe Rate",
            format_percent(unsubscribe_rate),
            "Under 0.5%",
            unsubscribe_status(unsubscribe_rate),
        ),
        scorecard_row(
            "Abuse Reports",
            format_integer(abuse_total),
            "Effectively zero",
            abuse_status(abuse_total),
        ),
    ]

    summary_snapshot = {
        "label": comparison_label(campaign),
        "title": nested_get(campaign, "settings", "title") or campaign.get("id"),
        "send_date": normalize_send_date(campaign.get("send_time")),
        "deliveries": deliveries,
        "delivery_rate": delivery_rate,
        "bounce_rate": bounce_rate,
        "unique_open_rate": open_rate,
        "click_rate": click_rate,
        "ctor": ctor,
        "unsubscribes": unsubscribed_total,
        "unsubscribe_rate": unsubscribe_rate,
        "abuse_reports": abuse_total,
    }

    return {
        "total_sends": total_sends,
        "deliveries": deliveries,
        "delivery_rate": delivery_rate,
        "total_bounces": total_bounces,
        "soft_bounces": soft_bounces,
        "hard_bounces": hard_bounces,
        "syntax_errors": syntax_errors,
        "bounce_rate": bounce_rate,
        "total_opens": total_opens,
        "unique_opens": unique_opens,
        "open_rate": open_rate,
        "proxy_excluded_unique_opens": as_int(opens.get("proxy_excluded_unique_opens")),
        "proxy_excluded_open_rate": round(
            as_float(opens.get("proxy_excluded_open_rate")) * 100, 1
        )
        if opens.get("proxy_excluded_open_rate") not in (None, "")
        else 0.0,
        "last_open": last_open,
        "total_clicks": total_clicks,
        "unique_clicks": unique_clicks,
        "unique_subscriber_clicks": unique_subscriber_clicks,
        "click_rate": click_rate,
        "ctor": ctor,
        "last_click": last_click,
        "unsubscribes": unsubscribed_total,
        "unsubscribe_rate": unsubscribe_rate,
        "abuse_reports": abuse_total,
        "clicks_not_applicable": clicks_not_applicable,
        "last_activity_label": last_activity_label,
        "last_activity_value": format_datetime_label(last_activity, include_time=True)
        if last_activity
        else "Not yet available",
        "scorecard_rows": scorecard_rows,
        "snapshot": summary_snapshot,
    }


async def persist_report_result(
    env,
    bundle: dict[str, Any],
    html_body: str,
    source: str,
    prior_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await send_report_email(env, report_subject(bundle), html_body, render_report_plain_text(bundle))
    record = dict(prior_record or bundle["record"])
    record.update(
        {
            "status": "reported",
            "reported_at": iso_now(),
            "reported_via": source,
            "last_error": None,
            "metrics_snapshot": bundle["metrics"]["snapshot"],
            "subject_line": bundle["record"].get("subject_line"),
            "from_name": bundle["record"].get("from_name"),
            "reply_to": bundle["record"].get("reply_to"),
            "audience_name": bundle["record"].get("audience_name"),
            "segment_text": bundle["record"].get("segment_text"),
        }
    )
    await store_campaign_record(env, campaign_kv_key(record["id"]), record)
    return record


def build_executive_summary(metrics: dict[str, Any], context: dict[str, Any]) -> str:
    strongest_signal = "the clean quality profile"
    if metrics["click_rate"] >= 5:
        strongest_signal = f"the standout {format_percent(metrics['click_rate'])} click rate"
    elif metrics["open_rate"] >= 25:
        strongest_signal = f"the {format_percent(metrics['open_rate'])} unique open rate"

    primary_watchout = "no material list-health issues"
    if metrics["bounce_rate"] > 5:
        primary_watchout = f"the elevated {format_percent(metrics['bounce_rate'])} bounce rate"
    elif metrics["unsubscribe_rate"] > 0.5:
        primary_watchout = f"the {format_percent(metrics['unsubscribe_rate'])} unsubscribe rate"
    elif metrics["click_rate"] < 3 and not metrics["clicks_not_applicable"]:
        primary_watchout = f"the modest {format_percent(metrics['click_rate'])} click rate"

    audience_phrase = context["segment_name"] or context["audience_name"]
    return (
        f"{context['title']} was delivered to the {audience_phrase} audience with "
        f"{format_integer(metrics['deliveries'])} successful deliveries from {format_integer(metrics['total_sends'])} total sends "
        f"({format_percent(metrics['delivery_rate'])} delivery rate). The campaign generated a "
        f"{format_percent(metrics['open_rate'])} unique open rate and a {format_percent(metrics['click_rate'])} click rate, "
        f"with {strongest_signal} emerging as the clearest performance signal. The main watchpoint is "
        f"{primary_watchout}. With {format_integer(metrics['abuse_reports'])} abuse reports and an unsubscribe rate of "
        f"{format_percent(metrics['unsubscribe_rate'])}, the report establishes a clean benchmark for the next send."
    )


def build_delivery_narrative(metrics: dict[str, Any], context: dict[str, Any]) -> str:
    return (
        f"Emails were sent to the {context['segment_name'] or context['audience_name']} audience. "
        f"Of the {format_integer(metrics['total_sends'])} total sends, {format_integer(metrics['deliveries'])} were successfully delivered, "
        f"with {format_integer(metrics['total_bounces'])} total bounces "
        f"({format_integer(metrics['soft_bounces'])} soft, {format_integer(metrics['hard_bounces'])} hard), "
        f"resulting in a bounce rate of {format_percent(metrics['bounce_rate'])}. "
        f"{delivery_assessment(metrics)}"
    )


def build_open_narrative(metrics: dict[str, Any], context: dict[str, Any]) -> str:
    revisit_ratio = (
        round(metrics["total_opens"] / metrics["unique_opens"], 2)
        if metrics["unique_opens"]
        else 0.0
    )
    return (
        f"The campaign generated {format_integer(metrics['unique_opens'])} unique opens from "
        f"{format_integer(metrics['deliveries'])} deliveries, producing a unique open rate of "
        f"{format_percent(metrics['open_rate'])}. Total opens reached {format_integer(metrics['total_opens'])}, "
        f"which indicates an average of {revisit_ratio:.2f} opens per engaged contact. "
        f"{open_assessment(metrics)}"
    )


def build_click_narrative(metrics: dict[str, Any], context: dict[str, Any]) -> str:
    if metrics["clicks_not_applicable"]:
        return (
            "Click metrics are not treated as a performance miss for this send. The campaign recorded no clicks, "
            "which typically indicates either a deliberate brand-awareness message or a call-to-action that was not yet compelling enough "
            "to move recipients deeper into the funnel."
        )
    return (
        f"{format_integer(metrics['unique_subscriber_clicks'])} recipients clicked through, generating "
        f"{format_integer(metrics['total_clicks'])} total clicks and {format_integer(metrics['unique_clicks'])} unique clicks. "
        f"The click rate of {format_percent(metrics['click_rate'])} and click-to-open rate of {format_percent(metrics['ctor'])} "
        f"show how effectively openers translated into downstream engagement. {click_assessment(metrics)}"
    )


def build_trust_narrative(metrics: dict[str, Any], context: dict[str, Any]) -> str:
    return (
        f"The campaign recorded {format_integer(metrics['unsubscribes'])} unsubscribes "
        f"({format_percent(metrics['unsubscribe_rate'])}) and {format_integer(metrics['abuse_reports'])} abuse reports. "
        f"{trust_assessment(metrics)}"
    )


def build_closing_summary(
    metrics: dict[str, Any],
    context: dict[str, Any],
    comparison_bundle: dict[str, Any],
) -> str:
    performance_note = "a stable baseline for future campaigns"
    if metrics["click_rate"] >= 5 and metrics["ctor"] >= 12:
        performance_note = "one of the strongest click-engagement results in the current campaign set"
    elif metrics["bounce_rate"] > 5:
        performance_note = "a useful signal that list hygiene needs attention before the next send"

    comparison_note = comparison_bundle["summary"]
    return (
        f"{context['title']} closes with {performance_note}. {comparison_note} "
        f"With clear strengths in delivery and audience interest, the next send should focus on preserving list quality "
        f"while improving the weakest engagement metric identified in this report."
    )


def build_recommendations(
    metrics: dict[str, Any],
    context: dict[str, Any],
    comparison_bundle: dict[str, Any],
) -> list[str]:
    recommendations: list[str] = []

    if metrics["hard_bounces"] > 0:
        recommendations.append(
            f"Remove the {format_integer(metrics['hard_bounces'])} hard-bounce address{'es' if metrics['hard_bounces'] != 1 else ''} immediately to protect sender reputation."
        )
    if metrics["soft_bounces"] > 0:
        recommendations.append(
            f"Review the {format_integer(metrics['soft_bounces'])} soft bounces and suppress contacts that repeat across multiple campaigns."
        )
    if metrics["bounce_rate"] > 5:
        recommendations.append(
            "Run a targeted list-hygiene audit before the next send because the bounce rate is above the accepted 5% threshold."
        )
    if metrics["open_rate"] < 25:
        recommendations.append(
            "Test subject lines and preview text to improve top-of-funnel engagement and lift unique opens above the 25% benchmark."
        )
    if not metrics["clicks_not_applicable"] and (
        metrics["click_rate"] < 3 or metrics["ctor"] < 8
    ):
        recommendations.append(
            "Reduce CTA friction with a clearer next step, a more specific offer, or a lower-commitment conversion action."
        )
    if metrics["unique_opens"] > metrics["unique_subscriber_clicks"]:
        recommendations.append(
            f"Follow up with the {format_integer(metrics['unique_opens'] - metrics['unique_subscriber_clicks'])} engaged openers who did not click while the campaign is still recent."
        )
    if metrics["unsubscribe_rate"] > 0.5:
        recommendations.append(
            "Inspect unsubscribe patterns by role, company, or segment criteria to refine the next audience selection."
        )
    if metrics["click_rate"] >= 5 and metrics["ctor"] >= 12:
        recommendations.append(
            "Preserve the winning content structure and CTA placement because the click efficiency is well above healthy benchmarks."
        )
    if comparison_bundle["trend_summary"]["best_metric"] == "bounce_rate":
        recommendations.append(
            "Keep the current list-sourcing approach in place because delivery quality is improving versus earlier reported campaigns."
        )

    if len(recommendations) < 6:
        recommendations.append(
            "Keep sends close enough together to build on current recognition while this audience is still warm."
        )
    if len(recommendations) < 6:
        recommendations.append(
            "Document what changed in targeting, subject line, and CTA so the next campaign can build from this baseline with intent."
        )

    return recommendations[:6]


def build_comparison_bundle(
    current_metrics: dict[str, Any],
    comparison_records: list[dict[str, Any]],
) -> dict[str, Any]:
    previous = comparison_records[-2:]
    current_snapshot = current_metrics["snapshot"]
    headers = ["Metric"] + [item["metrics_snapshot"]["label"] for item in previous] + [
        current_snapshot["label"],
        "Trend",
    ]
    current_compare_target = previous[-1]["metrics_snapshot"] if previous else None
    rows = []

    metric_keys = [
        "deliveries",
        "delivery_rate",
        "unique_open_rate",
        "click_rate",
        "ctor",
        "unsubscribes",
        "abuse_reports",
        "bounce_rate",
    ]
    for metric_key in metric_keys:
        cells = [METRIC_LABELS.get(metric_key, metric_key.replace("_", " ").title())]
        for item in previous:
            cells.append(format_comparison_value(metric_key, item["metrics_snapshot"]))
        cells.append(format_comparison_value(metric_key, current_snapshot))
        cells.append(trend_symbol(metric_key, current_compare_target, current_snapshot))
        rows.append(cells)

    trend_parts = []
    if current_compare_target:
        open_delta = round(
            current_snapshot["unique_open_rate"]
            - current_compare_target["unique_open_rate"],
            1,
        )
        click_delta = round(
            current_snapshot["click_rate"] - current_compare_target["click_rate"], 1
        )
        bounce_delta = round(
            current_compare_target["bounce_rate"] - current_snapshot["bounce_rate"], 1
        )
        if open_delta > 0:
            trend_parts.append(f"open rate improved by {format_signed_percent(open_delta)}")
        elif open_delta < 0:
            trend_parts.append(f"open rate eased by {format_signed_percent(open_delta)}")
        if click_delta > 0:
            trend_parts.append(
                f"click rate improved by {format_signed_percent(click_delta)}"
            )
        elif click_delta < 0:
            trend_parts.append(
                f"click rate softened by {format_signed_percent(click_delta)}"
            )
        if bounce_delta > 0:
            trend_parts.append(
                f"bounce rate improved by {format_percent(abs(bounce_delta))}"
            )
        elif bounce_delta < 0:
            trend_parts.append(
                f"bounce rate worsened by {format_percent(abs(bounce_delta))}"
            )

    summary = (
        "Historical comparison is available once at least one earlier campaign has been reported."
        if not previous
        else "Compared with the most recent reported campaign, "
        + (", ".join(trend_parts[:3]) if trend_parts else "performance was broadly stable")
        + "."
    )
    heading = (
        "5. Campaign Comparison: Historical Baseline vs Current Campaign"
        if not previous
        else "5. Campaign Comparison: Previous Reported Campaigns vs Current Campaign"
    )

    return {
        "heading": heading,
        "headers": headers,
        "rows": rows,
        "summary": summary,
        "trend_summary": {
            "best_metric": best_metric_from_snapshot(current_snapshot),
        },
    }


def render_dashboard_shell() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mailchimp Reports Worker Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #f6f7fb;
      --surface: rgba(255,255,255,0.88);
      --surface-strong: #ffffff;
      --ink: #0f172a;
      --muted: #58657a;
      --line: rgba(12, 22, 41, 0.09);
      --good: #0d9488;
      --warn: #d97706;
      --bad: #dc2626;
      --neutral: #475569;
      --accent: #0f62fe;
      --accent-2: #14b8a6;
      --lavender: #eef2ff;
      --shadow: 0 28px 80px rgba(15, 23, 42, 0.10);
      --radius-xl: 28px;
      --radius-lg: 20px;
      --radius-md: 16px;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background:
      radial-gradient(circle at top left, rgba(15,98,254,0.10), transparent 32%),
      radial-gradient(circle at top right, rgba(20,184,166,0.08), transparent 24%),
      linear-gradient(180deg, #fbfcff 0%, #f3f6fb 100%);
      color: var(--ink); }
    body {
      font-family: "IBM Plex Sans", sans-serif;
      padding: 28px;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(15, 23, 42, 0.04) 1px, transparent 1px),
        linear-gradient(90deg, rgba(15, 23, 42, 0.04) 1px, transparent 1px);
      background-size: 36px 36px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.55), transparent 88%);
      pointer-events: none;
    }
    a { color: inherit; text-decoration: none; }
    .shell { max-width: 1480px; margin: 0 auto; position: relative; z-index: 1; }
    .topbar {
      display: flex; justify-content: space-between; align-items: center; gap: 18px;
      margin-bottom: 22px;
    }
    .brand {
      display: flex; align-items: center; gap: 14px;
    }
    .brand-mark {
      width: 46px; height: 46px; border-radius: 14px;
      background: linear-gradient(135deg, #0f62fe, #14b8a6);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.4), 0 18px 28px rgba(15,98,254,0.24);
      position: relative;
    }
    .brand-mark::after {
      content: "";
      position: absolute; inset: 11px;
      border-radius: 10px;
      border: 2px solid rgba(255,255,255,0.85);
      border-bottom-width: 4px;
    }
    .eyebrow, .micro {
      color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; font-size: 11px;
    }
    h1, h2, h3, h4 { font-family: "Space Grotesk", sans-serif; margin: 0; letter-spacing: -0.03em; }
    h1 { font-size: clamp(2rem, 3vw, 3.6rem); line-height: 0.98; margin-top: 14px; max-width: 10ch; }
    p { margin: 0; color: var(--muted); line-height: 1.58; }
    .toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .btn {
      border: 1px solid var(--line); background: rgba(255,255,255,0.82);
      color: var(--ink); padding: 12px 16px; border-radius: 999px;
      font-weight: 600; font-size: 14px; cursor: pointer; transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
      box-shadow: 0 8px 20px rgba(15, 23, 42, 0.05);
    }
    .btn:hover { transform: translateY(-1px); box-shadow: 0 16px 28px rgba(15,23,42,0.08); border-color: rgba(15,98,254,0.25); }
    .btn.primary { background: linear-gradient(135deg, #0f62fe, #235ef3); color: white; border-color: transparent; }
    .btn.ghost { background: transparent; box-shadow: none; }
    .hero {
      display: grid; grid-template-columns: 1.35fr 0.95fr; gap: 18px; margin-bottom: 18px;
    }
    .panel {
      background: var(--surface);
      backdrop-filter: blur(14px);
      border: 1px solid rgba(255,255,255,0.7);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
    }
    .hero-main {
      padding: 32px; position: relative; overflow: hidden;
      background:
        radial-gradient(circle at 0% 0%, rgba(15,98,254,0.22), transparent 33%),
        radial-gradient(circle at 90% 0%, rgba(20,184,166,0.18), transparent 28%),
        rgba(255,255,255,0.78);
    }
    .hero-main::after {
      content: "";
      position: absolute; right: -120px; top: -120px; width: 320px; height: 320px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(15,98,254,0.18), transparent 72%);
      pointer-events: none;
    }
    .hero-copy { max-width: 720px; }
    .hero-copy p { margin-top: 16px; max-width: 58ch; font-size: 15px; }
    .status-row { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
    .badge {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 12px; border-radius: 999px; font-size: 13px; font-weight: 600;
      border: 1px solid var(--line); background: rgba(255,255,255,0.66);
    }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--neutral); box-shadow: 0 0 0 6px rgba(71,85,105,0.12); }
    .good .dot { background: var(--good); box-shadow: 0 0 0 6px rgba(13,148,136,0.12); }
    .warn .dot { background: var(--warn); box-shadow: 0 0 0 6px rgba(217,119,6,0.12); }
    .bad .dot { background: var(--bad); box-shadow: 0 0 0 6px rgba(220,38,38,0.12); }
    .hero-aside {
      padding: 24px; display: flex; flex-direction: column; gap: 16px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.92), rgba(248,250,255,0.94));
    }
    .signal-card {
      border: 1px solid rgba(12,22,41,0.08); border-radius: var(--radius-lg); padding: 18px;
      background: var(--surface-strong);
    }
    .signal-grid, .stats-grid {
      display: grid; gap: 16px;
    }
    .signal-grid { grid-template-columns: 1fr 1fr; }
    .signal-value { font: 700 2rem/1 "Space Grotesk", sans-serif; margin-top: 10px; }
    .signal-meta { margin-top: 8px; font-size: 13px; }
    .stats-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 18px; }
    .stat-card {
      padding: 22px; min-height: 148px; position: relative; overflow: hidden;
    }
    .stat-card::after {
      content: ""; position: absolute; inset: auto -40px -40px auto; width: 110px; height: 110px;
      background: radial-gradient(circle, rgba(15,98,254,0.10), transparent 66%);
      border-radius: 50%;
    }
    .stat-card h3 { font-size: 14px; color: var(--muted); font-family: "IBM Plex Sans", sans-serif; font-weight: 600; letter-spacing: 0; }
    .stat-value { margin-top: 18px; font: 700 2.25rem/1 "Space Grotesk", sans-serif; }
    .tone-good { color: var(--good); }
    .tone-warn { color: var(--warn); }
    .tone-bad { color: var(--bad); }
    .tone-neutral { color: var(--neutral); }
    .layout {
      display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 18px; align-items: start;
    }
    .stack { display: grid; gap: 18px; }
    .section { padding: 24px; }
    .section-head {
      display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;
      margin-bottom: 18px;
    }
    .section-subtitle { margin-top: 8px; font-size: 14px; }
    .chart-card { padding: 0; overflow: hidden; }
    .chart-head { padding: 24px 24px 0; }
    .chart-wrap { padding: 12px 20px 22px; }
    svg.spark { width: 100%; height: 180px; display: block; }
    .legend { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 14px; }
    .legend-item { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--muted); }
    .legend-line { width: 18px; height: 3px; border-radius: 999px; }
    .run-list, .step-list { display: grid; gap: 14px; }
    .run-card {
      border: 1px solid rgba(12,22,41,0.08);
      border-radius: var(--radius-lg);
      background: rgba(255,255,255,0.82);
      padding: 16px 18px;
    }
    .run-top, .coverage-row, .mini-grid, .campaign-meta {
      display: flex; justify-content: space-between; gap: 12px; align-items: center; flex-wrap: wrap;
    }
    .pill {
      border-radius: 999px; padding: 7px 11px; font-size: 12px; font-weight: 700;
      background: var(--lavender); color: #31456c; border: 1px solid rgba(49,69,108,0.12);
    }
    .pill.good { background: rgba(13,148,136,0.12); color: var(--good); }
    .pill.warn { background: rgba(217,119,6,0.12); color: var(--warn); }
    .pill.bad { background: rgba(220,38,38,0.12); color: var(--bad); }
    .mini-grid { margin-top: 14px; }
    .mini-stat {
      min-width: 120px; flex: 1 1 120px; border: 1px solid rgba(12,22,41,0.06);
      border-radius: 14px; padding: 12px 14px; background: rgba(246,248,252,0.92);
    }
    .mini-stat strong { display: block; font: 700 1.2rem/1 "Space Grotesk", sans-serif; margin-bottom: 6px; }
    .activity {
      display: grid; gap: 16px;
    }
    .campaign-filter { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
    .chip {
      padding: 9px 12px; border-radius: 999px; border: 1px solid var(--line);
      background: rgba(255,255,255,0.7); cursor: pointer; font-size: 13px; font-weight: 600;
    }
    .chip.active { background: rgba(15,98,254,0.12); color: var(--accent); border-color: rgba(15,98,254,0.2); }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 14px 10px; border-bottom: 1px solid rgba(12,22,41,0.08); vertical-align: top; }
    th { color: var(--muted); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 700; }
    td { font-size: 14px; }
    .campaign-title { font-weight: 700; color: var(--ink); margin-bottom: 6px; }
    .campaign-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .action-link {
      display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line);
      padding: 9px 11px; border-radius: 12px; font-size: 12px; font-weight: 700; background: rgba(255,255,255,0.8);
    }
    .action-link:hover { border-color: rgba(15,98,254,0.24); }
    .explainer-card, .coverage-card {
      padding: 22px;
    }
    .step {
      border: 1px solid rgba(12,22,41,0.08);
      background: rgba(255,255,255,0.8);
      border-radius: var(--radius-lg);
      padding: 18px;
    }
    .step h4 { font-size: 1rem; margin-bottom: 8px; }
    .coverage-list { display: grid; gap: 12px; margin-top: 14px; }
    .coverage-row {
      border: 1px solid rgba(12,22,41,0.08);
      border-radius: 16px;
      padding: 14px 16px;
      background: rgba(255,255,255,0.82);
    }
    .coverage-row strong { display: block; margin-bottom: 4px; }
    .alert {
      margin-top: 14px; padding: 14px 16px; border-radius: 16px;
      border: 1px solid rgba(217,119,6,0.18);
      background: rgba(251, 191, 36, 0.08);
      color: #92400e;
      font-size: 14px;
    }
    .footer-note { margin-top: 18px; text-align: right; font-size: 12px; color: var(--muted); }
    .skeleton {
      position: relative; overflow: hidden; min-height: 180px;
      background: rgba(255,255,255,0.7);
      border-radius: var(--radius-xl); border: 1px solid rgba(255,255,255,0.7);
      box-shadow: var(--shadow);
    }
    .skeleton::after {
      content: ""; position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.65), transparent);
      transform: translateX(-100%); animation: shimmer 1.5s infinite;
    }
    .toast {
      position: fixed; right: 28px; bottom: 28px; z-index: 50;
      background: rgba(15,23,42,0.94); color: white; padding: 14px 16px; border-radius: 16px;
      box-shadow: 0 18px 40px rgba(15,23,42,0.28); opacity: 0; transform: translateY(8px);
      transition: all .24s ease;
      pointer-events: none;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @keyframes shimmer { to { transform: translateX(100%); } }
    @media (max-width: 1180px) {
      .hero, .layout { grid-template-columns: 1fr; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      body { padding: 16px; }
      .hero-main, .hero-aside, .section, .explainer-card, .coverage-card { padding: 20px; }
      .stats-grid { grid-template-columns: 1fr; }
      .topbar, .section-head { flex-direction: column; align-items: flex-start; }
      th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5) { display: none; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <div class="brand-mark"></div>
        <div>
          <div class="eyebrow">AI Automation Console</div>
          <h2>Mailchimp Reports Worker</h2>
        </div>
      </div>
      <div class="toolbar">
        <button class="btn ghost" id="autoRefreshLabel" type="button">Auto refresh every 60s</button>
        <button class="btn primary" id="refreshButton" type="button">Refresh live state</button>
      </div>
    </div>

    <div id="dashboardRoot">
      <div class="skeleton"></div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const state = { filter: "all", data: null, loading: false };
    const root = document.getElementById("dashboardRoot");
    const refreshButton = document.getElementById("refreshButton");
    const toast = document.getElementById("toast");

    function showToast(message, tone = "default") {
      toast.textContent = message;
      toast.style.background = tone === "bad" ? "rgba(127, 29, 29, 0.96)" : "rgba(15,23,42,0.94)";
      toast.classList.add("show");
      clearTimeout(showToast.timer);
      showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
    }

    function formatDate(value, withTime = true) {
      if (!value) return "Not available";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: withTime ? "numeric" : undefined,
        minute: withTime ? "2-digit" : undefined,
      }).format(date);
    }

    function formatPercent(value) {
      if (value === null || value === undefined || value === "") return "—";
      const numeric = Number(value);
      if (Number.isNaN(numeric)) return value;
      return `${numeric.toFixed(Math.abs(numeric - Math.round(numeric)) < 0.05 ? 0 : 1)}%`;
    }

    function toneClass(tone) {
      return tone === "good" ? "tone-good" : tone === "warn" ? "tone-warn" : tone === "bad" ? "tone-bad" : "tone-neutral";
    }

    function pillClass(status) {
      if (["reported", "healthy", "configured", "verified", "live"].includes(String(status))) return "pill good";
      if (["pending", "partial", "idle", "open", "awaiting_first_run", "stale"].includes(String(status))) return "pill warn";
      if (["failed", "missing", "error"].includes(String(status))) return "pill bad";
      return "pill";
    }

    function sparklineSvg(runs) {
      const values = runs.map((run) => run.successful_campaigns - run.failed_campaigns);
      if (!values.length) {
        return `<div class="signal-card"><div class="micro">Run telemetry</div><div class="signal-value">No runs yet</div><p class="signal-meta">Once the daily cron executes, this panel will start plotting automation quality over time.</p></div>`;
      }
      const min = Math.min(...values, 0);
      const max = Math.max(...values, 1);
      const points = values.map((value, index) => {
        const x = values.length === 1 ? 280 : (index / (values.length - 1)) * 560 + 20;
        const y = 150 - ((value - min) / ((max - min) || 1)) * 110;
        return `${x},${y}`;
      }).join(" ");
      const area = `20,170 ${points} 580,170`;
      return `
        <div class="panel chart-card">
          <div class="chart-head">
            <div class="eyebrow">Automation telemetry</div>
            <h3>Run quality over time</h3>
            <p class="section-subtitle">A quick read on whether the daily automation is completing cleanly, partially, or with failures.</p>
            <div class="legend">
              <span class="legend-item"><span class="legend-line" style="background:#0f62fe"></span> net successful campaigns per run</span>
            </div>
          </div>
          <div class="chart-wrap">
            <svg class="spark" viewBox="0 0 600 180" preserveAspectRatio="none" aria-hidden="true">
              <defs>
                <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="rgba(15,98,254,0.35)"></stop>
                  <stop offset="100%" stop-color="rgba(15,98,254,0.02)"></stop>
                </linearGradient>
              </defs>
              <path d="M ${area}" fill="url(#sparkFill)"></path>
              <polyline fill="none" stroke="#0f62fe" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" points="${points}"></polyline>
            </svg>
          </div>
        </div>`;
    }

    function renderOverviewCards(cards) {
      return cards.map((card) => `
        <div class="panel stat-card">
          <h3>${card.label}</h3>
          <div class="stat-value ${toneClass(card.tone)}">${card.value}</div>
          <p class="section-subtitle">${card.meta || ""}</p>
        </div>
      `).join("");
    }

    function renderRuns(runs) {
      if (!runs.length) {
        return `<div class="run-card"><strong>No scheduled runs yet</strong><p class="section-subtitle">The cron history will appear here after the next 09:00 UTC execution.</p></div>`;
      }
      return runs.map((run) => `
        <div class="run-card">
          <div class="run-top">
            <div>
              <div class="micro">Target window</div>
              <strong>${run.target_date || "—"}</strong>
              <p class="section-subtitle">Started ${formatDate(run.started_at)}</p>
            </div>
            <span class="${pillClass(run.status)}">${String(run.status).replaceAll("_", " ")}</span>
          </div>
          <div class="mini-grid">
            <div class="mini-stat"><strong>${run.pending_campaigns}</strong><span class="micro">pending</span></div>
            <div class="mini-stat"><strong>${run.processed_campaigns}</strong><span class="micro">processed</span></div>
            <div class="mini-stat"><strong>${run.successful_campaigns}</strong><span class="micro">sent</span></div>
            <div class="mini-stat"><strong>${run.failed_campaigns}</strong><span class="micro">failed</span></div>
          </div>
        </div>
      `).join("");
    }

    function renderCoverage(items, gapCount) {
      const rows = items.length ? items.map((item) => `
        <div class="coverage-row">
          <div>
            <strong>${item.title}</strong>
            <div class="section-subtitle">${formatDate(item.send_time)} · ${item.subject_line || "No subject line"}</div>
          </div>
          <span class="${pillClass(item.tracked ? item.tracked_status : "missing")}">${item.tracked ? item.tracked_status : "missing in KV"}</span>
        </div>
      `).join("") : `<div class="coverage-row"><div><strong>Mailchimp sync unavailable</strong><div class="section-subtitle">The worker could not pull recent Mailchimp sends for coverage validation.</div></div></div>`;
      return `
        <div class="panel coverage-card">
          <div class="eyebrow">Coverage check</div>
          <h3>Mailchimp to KV sync visibility</h3>
          <p class="section-subtitle">This panel compares recent Mailchimp sends with tracked campaign records so you can spot missing webhook captures immediately.</p>
          ${gapCount ? `<div class="alert">Attention: ${gapCount} recent Mailchimp campaign${gapCount === 1 ? "" : "s"} ${gapCount === 1 ? "is" : "are"} not yet reflected in KV tracking.</div>` : ""}
          <div class="coverage-list">${rows}</div>
        </div>`;
    }

    function renderCampaignRows(campaigns) {
      const filtered = campaigns.filter((campaign) => state.filter === "all" ? true : campaign.status === state.filter);
      if (!filtered.length) {
        return `<tr><td colspan="6"><div class="section-subtitle">No campaigns match the current filter.</div></td></tr>`;
      }
      return filtered.map((campaign) => `
        <tr>
          <td>
            <div class="campaign-title">${campaign.title}</div>
            <div class="section-subtitle">${campaign.subject_line || "No subject line"} · ${campaign.audience_name || "Audience not set"}</div>
            <div class="campaign-meta section-subtitle">
              <span>ID ${campaign.id}</span>
              <span>${campaign.segment_name || "All contacts"}</span>
            </div>
          </td>
          <td>${campaign.send_date || "—"}</td>
          <td><span class="${pillClass(campaign.status)}">${campaign.status}</span></td>
          <td>${formatPercent(campaign.metrics.open_rate)}</td>
          <td>${formatPercent(campaign.metrics.click_rate)}</td>
          <td>
            <div class="campaign-actions">
              <a class="action-link" href="/report/${campaign.id}" target="_blank" rel="noreferrer">Preview</a>
              <button class="action-link" type="button" onclick="sendReportNow('${campaign.id}')">Send now</button>
            </div>
            <div class="section-subtitle" style="margin-top:8px;">
              ${campaign.reported_at ? `Reported ${formatDate(campaign.reported_at)}` : "Awaiting scheduled send"}
              ${campaign.last_error ? ` · Error: ${campaign.last_error}` : ""}
            </div>
          </td>
        </tr>
      `).join("");
    }

    function renderSteps(steps) {
      return steps.map((step) => `
        <div class="step">
          <h4>${step.title}</h4>
          <p>${step.body}</p>
        </div>
      `).join("");
    }

    function renderDashboard(data) {
      const healthTone = data.health.cron_state === "healthy" ? "good" : "warn";
      const latestReport = data.latest_report;
      const latestWebhook = data.latest_webhook;
      root.innerHTML = `
        <div class="hero">
          <div class="panel hero-main">
            <div class="hero-copy">
              <div class="eyebrow">Operational command center</div>
              <h1>${data.service.name}</h1>
              <p>${data.service.tagline} This interface is tuned for portfolio walkthroughs, client presentations, and day-to-day automation visibility.</p>
              <div class="status-row">
                <span class="badge ${healthTone}"><span class="dot"></span>Cron ${data.health.cron_state.replaceAll("_", " ")}</span>
                <span class="badge good"><span class="dot"></span>Worker live</span>
                <span class="badge ${data.health.coverage_gap_count ? "warn" : "good"}"><span class="dot"></span>${data.health.coverage_gap_count ? `${data.health.coverage_gap_count} sync gap` : "Webhook coverage aligned"}</span>
                <span class="badge"><span class="dot"></span>${data.health.delivery_provider} delivery</span>
              </div>
            </div>
          </div>
          <div class="panel hero-aside">
            <div class="signal-card">
              <div class="micro">Last successful report</div>
              <div class="signal-value">${latestReport ? latestReport.title : "None yet"}</div>
              <p class="signal-meta">${latestReport ? `${formatDate(latestReport.reported_at)} via ${latestReport.reported_via || "automation"}` : "The worker has not delivered any reports yet."}</p>
            </div>
            <div class="signal-grid">
              <div class="signal-card">
                <div class="micro">Next scheduled cron</div>
                <div class="signal-value" style="font-size:1.55rem">${formatDate(data.health.next_scheduled_at)}</div>
                <p class="signal-meta">Runs daily at 09:00 UTC.</p>
              </div>
              <div class="signal-card">
                <div class="micro">Webhook intake</div>
                <div class="signal-value" style="font-size:1.4rem">${latestWebhook ? latestWebhook.title : "Quiet"}</div>
                <p class="signal-meta">${latestWebhook ? `Captured ${formatDate(latestWebhook.webhook_received_at)}` : "No campaign events stored yet."}</p>
              </div>
            </div>
          </div>
        </div>

        <div class="stats-grid">${renderOverviewCards(data.overview_cards)}</div>

        <div class="layout">
          <div class="stack">
            ${sparklineSvg(data.runs)}

            <div class="panel section">
              <div class="section-head">
                <div>
                  <div class="eyebrow">Run history</div>
                  <h3>What each automation cycle actually did</h3>
                  <p class="section-subtitle">Every scheduled execution stores a run summary so you can see pending counts, processed campaigns, delivery success, and any failures.</p>
                </div>
              </div>
              <div class="run-list">${renderRuns(data.runs)}</div>
            </div>

            <div class="panel section">
              <div class="section-head">
                <div>
                  <div class="eyebrow">Campaign operations</div>
                  <h3>Tracked campaign queue</h3>
                  <p class="section-subtitle">Filter the tracked campaigns, preview any report, or trigger a manual send if you want to re-deliver a report on demand.</p>
                </div>
              </div>
              <div class="campaign-filter" id="filterBar">
                ${["all", "pending", "reported"].map((value) => `<button class="chip ${state.filter === value ? "active" : ""}" data-filter="${value}" type="button">${value}</button>`).join("")}
              </div>
              <div style="overflow:auto;">
                <table>
                  <thead>
                    <tr>
                      <th>Campaign</th>
                      <th>Send date</th>
                      <th>Status</th>
                      <th>Open rate</th>
                      <th>Click rate</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>${renderCampaignRows(data.campaigns)}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="stack">
            <div class="panel explainer-card">
              <div class="eyebrow">System walkthrough</div>
              <h3>How the automation works</h3>
              <p class="section-subtitle">This gives clients a fast mental model for what the worker is doing behind the scenes and why the reporting window is intentionally delayed.</p>
              <div class="step-list" style="margin-top:16px;">${renderSteps(data.explanations)}</div>
            </div>

            ${renderCoverage(data.tracking_coverage, data.health.coverage_gap_count)}
          </div>
        </div>

        <div class="footer-note">Generated ${formatDate(data.generated_at)} · sender ${data.health.sender_identity}</div>
      `;

      root.querySelectorAll("[data-filter]").forEach((button) => {
        button.addEventListener("click", () => {
          state.filter = button.getAttribute("data-filter");
          renderDashboard(state.data);
        });
      });
    }

    async function sendReportNow(campaignId) {
      const confirmed = window.confirm(`Send the latest report for campaign ${campaignId} to the configured inbox?`);
      if (!confirmed) return;
      try {
        const response = await fetch(`/report/${campaignId}?email=1`);
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.detail || payload.error || "Could not send report");
        }
        showToast(`Report sent: ${payload.subject}`);
        await loadDashboard();
      } catch (error) {
        showToast(error.message || "Could not send report", "bad");
      }
    }
    window.sendReportNow = sendReportNow;

    async function loadDashboard() {
      if (state.loading) return;
      state.loading = true;
      refreshButton.disabled = true;
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.detail || payload.error || "Dashboard load failed");
        }
        state.data = payload;
        renderDashboard(payload);
      } catch (error) {
        root.innerHTML = `<div class="panel section"><div class="eyebrow">Dashboard error</div><h3>Could not load live automation data</h3><p style="margin-top:12px;">${String(error.message || error)}</p></div>`;
      } finally {
        state.loading = false;
        refreshButton.disabled = false;
      }
    }

    refreshButton.addEventListener("click", loadDashboard);
    loadDashboard();
    setInterval(loadDashboard, 60000);
  </script>
</body>
</html>"""


def render_report_html(bundle: dict[str, Any]) -> str:
    context = bundle["context"]
    metrics = bundle["metrics"]
    comparison = bundle["comparison"]
    scorecard_html = render_table(
        ["Metric", "Result", "Industry Benchmark", "Status"],
        [
            [row["label"], row["result"], row["benchmark"], status_badge(row["status"])]
            for row in metrics["scorecard_rows"]
        ],
        wide=True,
    )
    delivery_table = render_table(
        [],
        [
            ["Total Sends", format_integer(metrics["total_sends"])],
            ["Successful Deliveries", format_integer(metrics["deliveries"])],
            ["Total Bounces", format_integer(metrics["total_bounces"])],
            ["Soft Bounces", format_integer(metrics["soft_bounces"])],
            ["Hard Bounces", format_integer(metrics["hard_bounces"])],
            ["Bounce Rate", format_percent(metrics["bounce_rate"])],
        ],
    )
    open_table = render_table(
        [],
        [
            ["Total Opens", format_integer(metrics["total_opens"])],
            ["Unique Opens", format_integer(metrics["unique_opens"])],
            ["Unique Open Rate", format_percent(metrics["open_rate"])],
            [
                "Last Opened",
                format_datetime_label(metrics["last_open"], include_time=True)
                if metrics["last_open"]
                else "Not yet available",
            ],
        ],
    )
    click_table = render_table(
        [],
        [
            ["Total Clicks", format_integer(metrics["total_clicks"])],
            ["Unique Clicks", format_integer(metrics["unique_clicks"])],
            [
                "Recipients Who Clicked",
                format_integer(metrics["unique_subscriber_clicks"]),
            ],
            [
                "Click Rate (CTR)",
                format_percent(metrics["click_rate"])
                if not metrics["clicks_not_applicable"]
                else "0% — No CTA included",
            ],
            [
                "Click-to-Open Rate",
                format_percent(metrics["ctor"])
                if not metrics["clicks_not_applicable"]
                else "0% — No CTA included",
            ],
        ],
    )
    trust_table = render_table(
        [],
        [
            [
                "Unsubscribes",
                f"{format_integer(metrics['unsubscribes'])} ({format_percent(metrics['unsubscribe_rate'])})",
            ],
            ["Abuse Reports", format_integer(metrics["abuse_reports"])],
            [
                "Campaign Window",
                f"{context['send_time_label']} – {context['last_activity_value']}",
            ],
        ],
    )
    comparison_html = render_table(comparison["headers"], comparison["rows"], wide=True)
    recommendations_html = "".join(
        f"<li>{html.escape(item)}</li>" for item in bundle["recommendations"]
    )

    brand = html.escape(context["brand_name"])
    title = html.escape(context["title"])
    subject_line = (
        html.escape(context["subject_line"]) if context["subject_line"] else ""
    )
    header_meta = (
        f"Send Date: {html.escape(context['send_time_label'])}  |  "
        f"{html.escape(context['last_activity_label'])}: "
        f"{html.escape(context['last_activity_value'])}"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title} Report</title>
    <style>
      body {{
        margin: 0;
        padding: 0;
        background: #ececec;
        font-family: Arial, sans-serif;
        color: #000000;
      }}
      .page {{
        width: 100%;
        background: #ececec;
        padding: 24px 0;
      }}
      .doc {{
        width: 816px;
        max-width: 816px;
        margin: 0 auto;
        background: #ffffff;
        padding: 36px;
        box-sizing: border-box;
      }}
      .hero,
      .hero-sub,
      .hero-meta {{
        background: #C0392B;
        color: #ffffff;
        text-align: center;
      }}
      .hero {{
        font-size: 18px;
        font-weight: 700;
        letter-spacing: 0.4px;
        padding: 14px 18px 8px;
      }}
      .hero-sub {{
        font-size: 16px;
        font-weight: 700;
        padding: 6px 18px;
      }}
      .hero-meta {{
        font-size: 13px;
        padding: 6px 18px 14px;
      }}
      .section-header {{
        margin-top: 18px;
        background: #1A1A2E;
        color: #ffffff;
        font-size: 14px;
        font-weight: 700;
        text-transform: uppercase;
        padding: 10px 14px;
      }}
      .section-title {{
        margin: 18px 0 8px;
        color: #1A1A2E;
        font-size: 16px;
        font-weight: 700;
      }}
      .body-copy {{
        margin: 0 0 10px;
        font-size: 13px;
        line-height: 1.55;
      }}
      .summary-box,
      .callout-box {{
        background: #F5F5F5;
        border: 1px solid #d8d8d8;
        padding: 12px 14px;
        margin-bottom: 14px;
      }}
      .callout-box {{
        font-style: italic;
      }}
      table.report-table {{
        width: 100%;
        border-collapse: collapse;
        margin: 6px 0 14px;
        table-layout: fixed;
      }}
      table.report-table th,
      table.report-table td {{
        border: 1px solid #b7b7b7;
        padding: 10px 12px;
        font-size: 13px;
        vertical-align: top;
        word-wrap: break-word;
      }}
      table.report-table th {{
        background: #1A1A2E;
        color: #ffffff;
        font-weight: 700;
        text-align: left;
      }}
      table.report-table tr:nth-child(even) td {{
        background: #F5F5F5;
      }}
      table.report-table.narrow td:first-child {{
        width: 46%;
        font-weight: 700;
      }}
      ul.recommendations {{
        margin: 8px 0 0 18px;
        padding: 0;
      }}
      ul.recommendations li {{
        margin: 0 0 8px;
        font-size: 13px;
        line-height: 1.5;
      }}
      .footer {{
        margin-top: 20px;
        text-align: center;
        font-size: 12px;
        color: #666666;
        font-style: italic;
      }}
      .status-good,
      .status-bad,
      .status-neutral {{
        font-weight: 700;
      }}
      .subject-line {{
        margin-top: 8px;
        font-size: 13px;
      }}
      @media only screen and (max-width: 860px) {{
        .doc {{
          width: 100%;
          max-width: 100%;
          padding: 20px;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page">
      <div class="doc">
        <table class="report-table" aria-hidden="true">
          <tr><td class="hero">EMAIL CAMPAIGN PERFORMANCE DEBRIEF</td></tr>
          <tr><td class="hero-sub">{brand} | {title}</td></tr>
          <tr><td class="hero-meta">{header_meta}</td></tr>
        </table>
        <div class="section-header">Executive Summary</div>
        <div class="summary-box">
          <p class="body-copy">{html.escape(bundle['executive_summary'])}</p>
          {f'<p class="body-copy subject-line"><strong>Subject Line:</strong> {subject_line}</p>' if subject_line else ''}
        </div>

        <p class="section-title">Performance Scorecard</p>
        {scorecard_html}

        <p class="section-title">1. Delivery Performance</p>
        <p class="body-copy">{html.escape(bundle['delivery_narrative'])}</p>
        {delivery_table}
        <div class="callout-box">{html.escape(delivery_assessment(metrics))}</div>

        <p class="section-title">2. Open Performance</p>
        <p class="body-copy">{html.escape(bundle['open_narrative'])}</p>
        {open_table}
        <div class="callout-box">{html.escape(open_assessment(metrics))}</div>

        <p class="section-title">3. Click Performance</p>
        <p class="body-copy">{html.escape(bundle['click_narrative'])}</p>
        {click_table}
        <div class="callout-box">{html.escape(click_assessment(metrics))}</div>

        <p class="section-title">4. Quality &amp; Trust Signals</p>
        <p class="body-copy">{html.escape(bundle['trust_narrative'])}</p>
        {trust_table}
        <div class="callout-box">{html.escape(trust_assessment(metrics))}</div>

        <p class="section-title">{html.escape(context['comparison_heading'])}</p>
        <p class="body-copy">{html.escape(comparison['summary'])}</p>
        {comparison_html}

        <p class="section-title">{html.escape(context['recommendations_heading'])}</p>
        <ul class="recommendations">
          {recommendations_html}
        </ul>

        <div class="summary-box">
          <p class="body-copy">{html.escape(bundle['closing_summary'])}</p>
        </div>

        <p class="footer">Report prepared automatically | Mailchimp Campaign Reporting | {html.escape(context['current_month_year'])}</p>
      </div>
    </div>
  </body>
</html>"""


def render_report_plain_text(bundle: dict[str, Any]) -> str:
    metrics = bundle["metrics"]
    lines = [
        "EMAIL CAMPAIGN PERFORMANCE DEBRIEF",
        f"{bundle['context']['brand_name']} | {bundle['context']['title']}",
        f"Send Date: {bundle['context']['send_time_label']}",
        "",
        "EXECUTIVE SUMMARY",
        bundle["executive_summary"],
        "",
        "PERFORMANCE SCORECARD",
    ]
    for row in metrics["scorecard_rows"]:
        lines.append(
            f"- {row['label']}: {row['result']} ({row['status']}; benchmark {row['benchmark']})"
        )
    lines.extend(
        [
            "",
            "DELIVERY PERFORMANCE",
            bundle["delivery_narrative"],
            "",
            "OPEN PERFORMANCE",
            bundle["open_narrative"],
            "",
            "CLICK PERFORMANCE",
            bundle["click_narrative"],
            "",
            "QUALITY & TRUST SIGNALS",
            bundle["trust_narrative"],
            "",
            "CAMPAIGN COMPARISON",
            bundle["comparison"]["summary"],
            "",
            "RECOMMENDATIONS",
        ]
    )
    lines.extend(f"- {item}" for item in bundle["recommendations"])
    lines.extend(["", "CLOSING SUMMARY", bundle["closing_summary"]])
    return "\n".join(lines)


def render_table(headers: list[str], rows: list[list[str]], wide: bool = False) -> str:
    table_class = "report-table wide" if wide else "report-table narrow"
    head_html = ""
    if headers:
        head_html = (
            "<thead><tr>"
            + "".join(f"<th>{html.escape(item)}</th>" for item in headers)
            + "</tr></thead>"
        )
    body_rows = []
    for row in rows:
        cells = []
        for cell in row:
            if cell.startswith("<span"):
                cells.append(f"<td>{cell}</td>")
            else:
                cells.append(f"<td>{html.escape(cell)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class=\"{table_class}\">{head_html}<tbody>{''.join(body_rows)}</tbody></table>"


def scorecard_row(label: str, result: str, benchmark: str, status: str) -> dict[str, str]:
    return {"label": label, "result": result, "benchmark": benchmark, "status": status}


def status_badge(status: str) -> str:
    css_class = STATUS_STYLE.get(status, "status-neutral")
    symbol = {
        "EXCEEDED": "✓ Exceeded",
        "MET": "✓ Met",
        "BELOW": "▼ Below",
        "N/A": "— N/A",
    }.get(status, status)
    return f"<span class=\"{css_class}\">{html.escape(symbol)}</span>"


async def send_report_email(env, subject: str, html_body: str, plain_text: str):
    destination = require_env(env, "MY_EMAIL")
    sender = get_optional_env(env, "RESEND_FROM_EMAIL") or require_env(
        env, "REPORT_FROM_EMAIL"
    )
    api_key = require_env(env, "RESEND_API_KEY")
    payload = {
        "from": sender,
        "to": [destination],
        "subject": subject,
        "html": html_body,
        "text": plain_text,
    }
    response = await fetch(
        "https://api.resend.com/emails",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body=json.dumps(payload),
    )
    body_text = await response.text()
    parsed_body = {}
    if body_text:
        try:
            parsed_body = json.loads(body_text)
        except json.JSONDecodeError:
            parsed_body = {"raw": body_text}
    if response.status >= 400:
        detail = parsed_body.get("message") or parsed_body.get("name") or body_text
        raise RuntimeError(f"Resend API request failed ({response.status}): {detail}")
    log_event(
        "email_sent",
        {
            "provider": "resend",
            "to": destination,
            "from": sender,
            "subject": subject,
            "email_id": parsed_body.get("id"),
        },
    )


async def fetch_mailchimp_json(
    env, path: str, init: dict[str, Any] | None = None
) -> dict[str, Any]:
    api_key = require_env(env, "MC_API_KEY")
    server = require_env(env, "MC_SERVER")
    base_url = f"https://{server}.api.mailchimp.com/3.0"
    url = f"{base_url}{path if path.startswith('/') else '/' + path}"
    authorization = base64.b64encode(f"codex:{api_key}".encode("utf-8")).decode(
        "utf-8"
    )
    request_init = {
        "method": "GET",
        "headers": {
            "Authorization": f"Basic {authorization}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    }
    if init:
        request_init.update(init)
        if init.get("headers"):
            request_init["headers"].update(init["headers"])

    for attempt in range(MAX_RETRIES + 1):
        response = await fetch(url, **request_init)
        if response.ok:
            text = await response.text()
            return json.loads(text or "{}")

        error_body = await safe_error_body(response)
        if attempt >= MAX_RETRIES or response.status not in (429, 500, 502, 503, 504):
            raise MailchimpApiError(int(response.status), error_body, url)

        await asyncio.sleep(retry_delay_seconds(response, attempt))

    raise RuntimeError(f"Retries exhausted for {url}")


async def safe_error_body(response) -> dict[str, Any]:
    try:
        return json.loads(await response.text())
    except Exception:
        return {
            "status": int(response.status),
            "title": "Mailchimp error",
            "detail": "Could not parse error response.",
        }


def retry_delay_seconds(response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(2**attempt, 8)


async def load_all_campaign_records(env) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = None
    while True:
        options = {"prefix": "campaign:"}
        if cursor:
            options["cursor"] = cursor
        page = await env.CAMPAIGNS.list(jsify(options))
        page_data = to_python(page)
        keys = page_data.get("keys") or []
        for item in keys:
            key_name = item.get("name")
            if not key_name:
                continue
            record = await load_campaign_record(env, key_name)
            if record:
                records.append(record)
        cursor = page_data.get("cursor")
        if not page_data.get("list_complete") and cursor:
            continue
        break
    records.sort(key=lambda item: (item.get("send_date") or "", item.get("id") or ""))
    return records


async def load_campaign_record(env, key_name: str) -> dict[str, Any] | None:
    raw = await env.CAMPAIGNS.get(key_name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def store_campaign_record(env, key_name: str, record: dict[str, Any]):
    await env.CAMPAIGNS.put(key_name, json.dumps(record, separators=(",", ":")))


async def load_all_run_records(env, limit: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = None
    while True:
        options = {"prefix": "run:"}
        if cursor:
            options["cursor"] = cursor
        page = await env.CAMPAIGNS.list(jsify(options))
        page_data = to_python(page)
        keys = page_data.get("keys") or []
        for item in keys:
            key_name = item.get("name")
            if not key_name:
                continue
            raw = await env.CAMPAIGNS.get(key_name)
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        cursor = page_data.get("cursor")
        if not page_data.get("list_complete") and cursor:
            continue
        break
    records.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    if limit is not None:
        return records[:limit]
    return records


async def store_run_record(env, record: dict[str, Any]):
    await env.CAMPAIGNS.put(run_kv_key(record["id"]), json.dumps(record, separators=(",", ":")))


async def fetch_recent_sent_campaigns(env, count: int = RECENT_SENT_CAMPAIGN_LIMIT) -> list[dict[str, Any]]:
    payload = await fetch_mailchimp_json(
        env,
        f"/campaigns?status=sent&sort_field=send_time&sort_dir=DESC&count={count}",
    )
    return payload.get("campaigns") or []


async def build_dashboard_payload(env) -> dict[str, Any]:
    now = utc_now()
    campaigns = await load_all_campaign_records(env)
    runs = await load_all_run_records(env, DASHBOARD_RUN_LIMIT)
    campaigns.sort(
        key=lambda item: (
            item.get("send_time") or "",
            item.get("reported_at") or "",
            item.get("webhook_received_at") or "",
        ),
        reverse=True,
    )

    pending_campaigns = [item for item in campaigns if item.get("status") == "pending"]
    reported_campaigns = [item for item in campaigns if item.get("status") == "reported"]
    failed_campaigns = [
        item for item in campaigns if item.get("status") != "reported" and item.get("last_error")
    ]
    two_days_ago = (now.date() - timedelta(days=2)).isoformat()
    overdue_campaigns = [
        item for item in pending_campaigns if (item.get("send_date") or "") <= two_days_ago
    ]
    latest_report = first_sorted_record(reported_campaigns, "reported_at")
    latest_webhook = first_sorted_record(campaigns, "webhook_received_at")
    latest_run = runs[0] if runs else None

    recent_sent_campaigns: list[dict[str, Any]] = []
    coverage_error = None
    try:
        recent_sent_campaigns = await fetch_recent_sent_campaigns(env)
    except Exception as error:
        coverage_error = str(error)

    tracking_coverage = []
    for campaign in recent_sent_campaigns:
        tracked = record_by_id(campaigns, campaign["id"])
        tracking_coverage.append(
            {
                "id": campaign["id"],
                "title": nested_get(campaign, "settings", "title") or campaign["id"],
                "subject_line": nested_get(campaign, "settings", "subject_line") or "",
                "send_time": campaign.get("send_time"),
                "tracked": bool(tracked),
                "tracked_status": tracked.get("status") if tracked else "missing",
                "reported_at": tracked.get("reported_at") if tracked else None,
            }
        )

    pending_next_due = sorted(
        (
            {
                "id": item["id"],
                "title": item.get("title") or item["id"],
                "due_at": due_date_for_send_date(item.get("send_date")),
            }
            for item in pending_campaigns
            if due_date_for_send_date(item.get("send_date"))
        ),
        key=lambda item: item["due_at"],
    )

    health = {
        "worker_live": True,
        "cron_schedule": "0 9 * * *",
        "cron_state": cron_state_for_run(latest_run, now),
        "webhook_state": "configured"
        if get_optional_env(env, "MAILCHIMP_WEBHOOK_SECRET")
        else "open",
        "delivery_provider": "Resend",
        "sender_identity": get_optional_env(env, "RESEND_FROM_EMAIL") or "Not configured",
        "latest_run_started_at": latest_run.get("started_at") if latest_run else None,
        "latest_run_status": latest_run.get("status") if latest_run else "unknown",
        "next_scheduled_at": next_daily_utc_occurrence(now, hour=9).isoformat(),
        "coverage_gap_count": sum(1 for item in tracking_coverage if not item["tracked"]),
    }

    overview_cards = [
        {
            "label": "Worker health",
            "value": "Live",
            "meta": health["cron_state"].replace("_", " ").title(),
            "tone": "good" if health["cron_state"] == "healthy" else "warn",
        },
        {
            "label": "Pending campaigns",
            "value": str(len(pending_campaigns)),
            "meta": f"{len(overdue_campaigns)} due or overdue",
            "tone": "warn" if overdue_campaigns else "neutral",
        },
        {
            "label": "Reports delivered",
            "value": str(len(reported_campaigns)),
            "meta": latest_report.get("title") if latest_report else "No reports yet",
            "tone": "good" if reported_campaigns else "neutral",
        },
        {
            "label": "Run issues",
            "value": str(len(failed_campaigns)),
            "meta": coverage_error or "No active delivery errors",
            "tone": "bad" if failed_campaigns or coverage_error else "good",
        },
    ]

    return {
        "ok": True,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "service": {
            "name": "Mailchimp Reports Worker",
            "tagline": "Event-driven campaign intelligence for AI-powered reporting operations.",
            "version": "dashboard-2026.03",
        },
        "health": health,
        "overview_cards": overview_cards,
        "latest_report": public_campaign_record(latest_report) if latest_report else None,
        "latest_webhook": public_campaign_record(latest_webhook) if latest_webhook else None,
        "next_due": pending_next_due[:3],
        "runs": [public_run_record(item) for item in runs],
        "campaigns": [public_campaign_record(item) for item in campaigns[:DASHBOARD_CAMPAIGN_LIMIT]],
        "tracking_coverage": tracking_coverage,
        "explanations": [
            {
                "title": "1. Capture the send",
                "body": "Mailchimp posts a campaign event to the worker webhook the moment a campaign is sent. The worker fetches campaign metadata and stores it in KV as a pending report job.",
            },
            {
                "title": "2. Hold for two days",
                "body": "The automation waits exactly two days so opens, clicks, and delivery behavior can mature before a report is generated.",
            },
            {
                "title": "3. Run the daily automation",
                "body": "At 09:00 UTC each day, the cron checks KV for campaigns whose send date is exactly two days old, then generates one report per qualifying campaign.",
            },
            {
                "title": "4. Deliver and archive",
                "body": "Each report is emailed through Resend, the campaign record is marked reported, and the run history is persisted for auditability and client-facing visibility.",
            },
        ],
    }


def public_campaign_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    snapshot = record.get("metrics_snapshot") or {}
    return {
        "id": record.get("id"),
        "title": record.get("title") or record.get("id"),
        "subject_line": record.get("subject_line") or "",
        "send_date": record.get("send_date"),
        "send_time": record.get("send_time"),
        "status": record.get("status") or "pending",
        "reported_at": record.get("reported_at"),
        "reported_via": record.get("reported_via"),
        "webhook_received_at": record.get("webhook_received_at"),
        "last_error": record.get("last_error"),
        "last_attempted_at": record.get("last_attempted_at"),
        "audience_name": record.get("audience_name") or "",
        "segment_name": extract_segment_name(record.get("segment_text")),
        "metrics": {
            "delivery_rate": snapshot.get("delivery_rate"),
            "open_rate": snapshot.get("unique_open_rate"),
            "click_rate": snapshot.get("click_rate"),
            "bounce_rate": snapshot.get("bounce_rate"),
            "unsubscribe_rate": snapshot.get("unsubscribe_rate"),
        },
    }


def public_run_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "trigger": record.get("trigger") or "scheduled",
        "status": record.get("status") or "unknown",
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "target_date": record.get("target_date"),
        "pending_campaigns": as_int(record.get("pending_campaigns")),
        "processed_campaigns": as_int(record.get("processed_campaigns")),
        "successful_campaigns": as_int(record.get("successful_campaigns")),
        "failed_campaigns": as_int(record.get("failed_campaigns")),
        "campaigns": record.get("campaigns") or [],
    }


def first_sorted_record(records: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not records:
        return None
    ordered = sorted(records, key=lambda item: item.get(key) or "", reverse=True)
    return ordered[0] if ordered else None


def run_id_for_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def due_date_for_send_date(send_date: str | None) -> str | None:
    parsed = parse_iso_datetime(send_date)
    if not parsed:
        return None
    return (parsed.date() + timedelta(days=2)).isoformat()


def cron_state_for_run(run: dict[str, Any] | None, now: datetime) -> str:
    if not run:
        return "awaiting_first_run"
    started_at = parse_iso_datetime(run.get("started_at"))
    if not started_at:
        return "unknown"
    if now - started_at <= timedelta(hours=30):
        return "healthy"
    return "stale"


def next_daily_utc_occurrence(now: datetime, hour: int) -> datetime:
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def select_comparison_records(
    records: list[dict[str, Any]], current_campaign_id: str
) -> list[dict[str, Any]]:
    eligible = [
        record
        for record in records
        if record.get("id") != current_campaign_id
        and record.get("status") == "reported"
        and record.get("metrics_snapshot")
    ]
    eligible.sort(key=lambda item: (item.get("send_date") or "", item.get("reported_at") or ""))
    return eligible


def record_by_id(records: list[dict[str, Any]], campaign_id: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("id") == campaign_id:
            return record
    return None


def recommendation_heading(title: str) -> str:
    number = extract_campaign_number(title)
    if number is not None:
        return f"6. Recommendations for Campaign {number + 1}"
    return "6. Recommendations for the Next Campaign"


def report_subject(bundle: dict[str, Any]) -> str:
    return f"Report: {bundle['context']['title']} ({bundle['record']['send_date']})"


def is_webhook_path(path: str, secret: str | None) -> bool:
    if path == "/webhook":
        return True
    return bool(secret and path == f"/webhook/{secret}")


def is_test_email_path(path: str, secret: str | None) -> bool:
    return bool(secret and path == f"/email-test/{secret}")


def parse_webhook_payload(body: str, content_type: str) -> dict[str, str]:
    body = body or ""
    if "application/json" in content_type:
        parsed = json.loads(body or "{}")
        return flatten_payload(parsed)
    flat = {}
    for key, values in parse_qs(body, keep_blank_values=True).items():
        value = values[-1] if values else ""
        flat[key] = value
        flat[normalize_form_key(key)] = value
    return flat


def flatten_payload(
    value: Any, prefix: str = "", output: dict[str, str] | None = None
) -> dict[str, str]:
    output = output or {}
    if isinstance(value, dict):
        for key, nested in value.items():
            new_prefix = f"{prefix}.{key}" if prefix else str(key)
            flatten_payload(nested, new_prefix, output)
    elif isinstance(value, list):
        if value:
            flatten_payload(value[0], prefix, output)
    elif prefix:
        output[prefix] = "" if value is None else str(value)
    return output


def normalize_form_key(key: str) -> str:
    return key.replace("[", ".").replace("]", "").strip(".")


def first_value(payload: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def nested_get(data: dict[str, Any] | None, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def infer_clicks_not_applicable(
    campaign: dict[str, Any], total_clicks: int, unique_clicks: int
) -> bool:
    if total_clicks or unique_clicks:
        return False
    text = " ".join(
        filter(
            None,
            [
                nested_get(campaign, "settings", "title"),
                nested_get(campaign, "settings", "subject_line"),
                nested_get(campaign, "settings", "preview_text"),
            ],
        )
    ).lower()
    return any(
        token in text for token in ("intro", "introduction", "awareness", "launch", "debut")
    )


def comparison_label(campaign: dict[str, Any]) -> str:
    title = (
        nested_get(campaign, "settings", "title")
        or campaign.get("title")
        or campaign.get("id")
        or "Campaign"
    )
    number = extract_campaign_number(title)
    if number is not None:
        return f"Campaign {number}"
    send_time = normalize_send_date(campaign.get("send_time"))
    short_title = title if len(title) <= 26 else f"{title[:23]}..."
    return f"{short_title} ({send_time})" if send_time else short_title


def extract_campaign_number(title: str | None) -> int | None:
    if not title:
        return None
    match = CAMPAIGN_NUMBER_RE.search(title)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def extract_segment_name(segment_text: str | None) -> str:
    if not segment_text:
        return "campaign audience"
    plain = WHITESPACE_RE.sub(
        " ", html.unescape(STRIP_TAGS_RE.sub(" ", segment_text))
    ).strip()
    tag_match = TAGGED_SEGMENT_RE.search(plain)
    if tag_match:
        return tag_match.group(1).strip() + " segment"
    if "for a total of" in plain:
        plain = plain.split("for a total of", 1)[0].strip()
    return plain or "campaign audience"


def delivery_assessment(metrics: dict[str, Any]) -> str:
    if metrics["bounce_rate"] > 5:
        return (
            f"A bounce rate of {format_percent(metrics['bounce_rate'])} is above the accepted 5% threshold and should be addressed before the next send. "
            "Hard bounces need immediate suppression, while repeated soft bounces should be monitored closely."
        )
    if metrics["hard_bounces"] > 0:
        return (
            f"The overall bounce rate is within range, but the {format_integer(metrics['hard_bounces'])} hard bounce{'es' if metrics['hard_bounces'] != 1 else ''} "
            "should still be removed immediately to preserve deliverability."
        )
    return (
        f"A bounce rate of {format_percent(metrics['bounce_rate'])} is comfortably within acceptable limits and reflects a healthy list foundation for the next campaign."
    )


def open_assessment(metrics: dict[str, Any]) -> str:
    if metrics["open_rate"] >= 30:
        return (
            f"The {format_percent(metrics['open_rate'])} unique open rate is a strong result and suggests the subject line and audience targeting were well aligned."
        )
    if metrics["open_rate"] >= 25:
        return (
            f"The {format_percent(metrics['open_rate'])} unique open rate meets a healthy benchmark and provides a stable base to build stronger click performance."
        )
    return (
        f"The {format_percent(metrics['open_rate'])} unique open rate sits below the stronger B2B benchmark range, which makes subject line testing and tighter audience framing the clearest next levers."
    )


def click_assessment(metrics: dict[str, Any]) -> str:
    if metrics["clicks_not_applicable"]:
        return (
            "That outcome is acceptable for a pure awareness send, but future campaigns should introduce a deliberate CTA once the audience is ready to convert."
        )
    if metrics["click_rate"] >= 6 or metrics["ctor"] >= 20:
        return (
            f"The campaign is translating attention into action exceptionally well, with a {format_percent(metrics['click_rate'])} CTR and {format_percent(metrics['ctor'])} CTOR."
        )
    if metrics["click_rate"] >= 3 and metrics["ctor"] >= 8:
        return (
            "Click efficiency is healthy, but there is still room to increase conversion volume through stronger CTA placement or a more direct offer."
        )
    return (
        "Click performance is lagging behind the healthier benchmark range, which usually points to CTA friction, offer clarity, or mismatch between the message and the next step."
    )


def trust_assessment(metrics: dict[str, Any]) -> str:
    if metrics["abuse_reports"] == 0 and metrics["unsubscribe_rate"] <= 0.5:
        return (
            "These trust signals are clean and support continued deliverability, indicating recipients did not experience the message as irrelevant or abusive."
        )
    if metrics["abuse_reports"] > 0:
        return (
            "Abuse complaints need immediate review because they are one of the fastest ways to damage inbox placement and sender trust."
        )
    return (
        "The trust profile is still workable, but unsubscribe behavior should be reviewed so the next send is more tightly aligned to recipient expectations."
    )


def delivery_status(rate: float) -> str:
    if rate >= 98:
        return "EXCEEDED"
    if rate >= 95:
        return "MET"
    return "BELOW"


def bounce_status(rate: float) -> str:
    if rate <= 2.5:
        return "EXCEEDED"
    if rate <= 5:
        return "MET"
    return "BELOW"


def open_status(rate: float) -> str:
    if rate >= 35:
        return "EXCEEDED"
    if rate >= 25:
        return "MET"
    return "BELOW"


def click_status(rate: float) -> str:
    if rate >= 5:
        return "EXCEEDED"
    if rate >= 3:
        return "MET"
    return "BELOW"


def ctor_status(rate: float) -> str:
    if rate >= 12:
        return "EXCEEDED"
    if rate >= 8:
        return "MET"
    return "BELOW"


def unsubscribe_status(rate: float) -> str:
    if rate == 0:
        return "EXCEEDED"
    if rate <= 0.5:
        return "MET"
    return "BELOW"


def abuse_status(total: int) -> str:
    return "EXCEEDED" if total == 0 else "BELOW"


def trend_symbol(
    metric_key: str, previous: dict[str, Any] | None, current: dict[str, Any]
) -> str:
    if not previous:
        return "—"
    previous_value = previous.get(metric_key)
    current_value = current.get(metric_key)
    if previous_value == current_value:
        return "—"
    higher_is_better = metric_key not in {
        "bounce_rate",
        "unsubscribes",
        "unsubscribe_rate",
        "abuse_reports",
    }
    improved = current_value > previous_value if higher_is_better else current_value < previous_value
    return "▲" if improved else "▼"


def format_comparison_value(metric_key: str, snapshot: dict[str, Any]) -> str:
    value = snapshot.get(metric_key)
    if metric_key in {"deliveries", "unsubscribes", "abuse_reports"}:
        return format_integer(value)
    return format_percent(value)


def best_metric_from_snapshot(snapshot: dict[str, Any]) -> str:
    best_metric = "open_rate"
    best_score = snapshot.get("unique_open_rate", 0)
    if snapshot.get("click_rate", 0) > best_score:
        best_metric = "click_rate"
        best_score = snapshot.get("click_rate", 0)
    if snapshot.get("bounce_rate", 100) < 3:
        best_metric = "bounce_rate"
    return best_metric


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text + "T00:00:00+00:00")
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None


def normalize_send_date(value: str | None) -> str:
    parsed = parse_iso_datetime(value)
    if parsed:
        return parsed.date().isoformat()
    if value and re.match(r"^\d{4}-\d{2}-\d{2}$", str(value).strip()):
        return str(value).strip()
    return utc_now().date().isoformat()


def format_datetime_label(value: datetime | None, include_time: bool) -> str:
    if not value:
        return "Not yet available"
    if include_time:
        hour = value.hour % 12 or 12
        meridiem = "AM" if value.hour < 12 else "PM"
        return f"{value.strftime('%B %d, %Y')}, {hour}:{value.minute:02d} {meridiem} UTC"
    return value.strftime("%B %d, %Y")


def json_response(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        status=status,
    )


def html_response(body: str, status: int = 200) -> Response:
    return Response(
        body,
        headers={"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        status=status,
    )


def require_env(env, key: str) -> str:
    value = get_optional_env(env, key)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Missing required environment variable: {key}")
    return str(value).strip()


def get_optional_env(env, key: str) -> str | None:
    value = getattr(env, key, None)
    if value is None:
        return None
    return str(value)


def percent(explicit_value: Any, numerator: int, denominator: int) -> float:
    if explicit_value not in (None, ""):
        raw = as_float(explicit_value)
        if raw <= 1:
            return round(raw * 100, 1)
        return round(raw, 1)
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def as_int(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_percent(value: Any) -> str:
    number = as_float(value)
    if abs(number - round(number)) < 0.05:
        return f"{round(number):.0f}%"
    return f"{number:.1f}%"


def format_signed_percent(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def format_integer(value: Any) -> str:
    return f"{as_int(value):,}"


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def campaign_kv_key(campaign_id: str) -> str:
    return f"campaign:{campaign_id}"


def run_kv_key(run_id: str) -> str:
    return f"run:{run_id}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def normalize_token(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def query_truthy(query: dict[str, list[str]], key: str) -> bool:
    return any(item.lower() in {"1", "true", "yes", "send"} for item in query.get(key, []))


def to_python(value: Any) -> Any:
    if hasattr(value, "to_py"):
        return value.to_py()
    return value


def jsify(value: Any) -> Any:
    return to_js(value, depth=-1, dict_converter=Object.fromEntries)


def log_event(event: str, payload: dict[str, Any]):
    print(json.dumps({"timestamp": iso_now(), "event": event, **payload}, ensure_ascii=False))
