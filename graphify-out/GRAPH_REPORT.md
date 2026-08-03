# Graph Report - .  (2026-08-03)

## Corpus Check
- 6 files · ~10,109 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 117 nodes · 358 edges · 10 communities (9 shown, 1 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9

## God Nodes (most connected - your core abstractions)
1. `build_report_bundle()` - 24 edges
2. `compute_metrics()` - 22 edges
3. `build_dashboard_payload()` - 16 edges
4. `format_percent()` - 15 edges
5. `render_report_html()` - 13 edges
6. `format_integer()` - 13 edges
7. `Default` - 10 edges
8. `persist_report_result()` - 10 edges
9. `fetch_mailchimp_json()` - 10 edges
10. `load_all_campaign_records()` - 9 edges

## Surprising Connections (you probably didn't know these)
- `as_float()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 0 → community 1_
- `build_campaign_record()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 0 → community 5_
- `build_dashboard_payload()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 0 → community 3_
- `chunked()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 0 → community 7_
- `fetch_mailchimp_json()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 0 → community 6_

## Import Cycles
- None detected.

## Communities (10 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.18
Nodes (29): Any, best_metric_from_snapshot(), build_click_narrative(), build_closing_summary(), build_comparison_bundle(), build_delivery_narrative(), build_executive_summary(), build_open_narrative() (+21 more)

### Community 1 - "Community 1"
Cohesion: 0.16
Nodes (25): abuse_status(), as_float(), as_int(), bounce_status(), click_status(), comparison_label(), compute_metrics(), ctor_status() (+17 more)

### Community 2 - "Community 2"
Cohesion: 0.23
Nodes (9): Response, html_response(), is_test_email_path(), is_webhook_path(), json_response(), query_truthy(), record_by_id(), render_dashboard_shell() (+1 more)

### Community 3 - "Community 3"
Cohesion: 0.25
Nodes (11): datetime, build_dashboard_payload(), cron_state_for_run(), due_date_for_send_date(), extract_segment_name(), first_sorted_record(), next_daily_utc_occurrence(), parse_iso_datetime() (+3 more)

### Community 4 - "Community 4"
Cohesion: 0.18
Nodes (10): devDependencies, wrangler, name, private, scripts, deploy, dev, start (+2 more)

### Community 5 - "Community 5"
Cohesion: 0.31
Nodes (9): build_campaign_record(), campaign_kv_key(), first_value(), iso_now(), log_event(), normalize_token(), persist_report_result(), render_report_plain_text() (+1 more)

### Community 6 - "Community 6"
Cohesion: 0.29
Nodes (6): Exception, fetch_mailchimp_json(), fetch_recent_sent_campaigns(), MailchimpApiError, retry_delay_seconds(), safe_error_body()

### Community 7 - "Community 7"
Cohesion: 0.50
Nodes (3): chunked(), Default, WorkerEntrypoint

### Community 8 - "Community 8"
Cohesion: 0.83
Nodes (3): get_optional_env(), require_env(), send_report_email()

## Knowledge Gaps
- **8 isolated node(s):** `name`, `version`, `private`, `deploy`, `dev` (+3 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Default` connect `Community 7` to `Community 8`, `Community 1`, `Community 2`, `Community 5`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Why does `build_report_bundle()` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.020) - this node is a cross-community bridge._
- **Why does `MailchimpApiError` connect `Community 6` to `Community 1`?**
  _High betweenness centrality (0.019) - this node is a cross-community bridge._
- **What connects `name`, `version`, `private` to the rest of the system?**
  _8 weakly-connected nodes found - possible documentation gaps or missing edges._