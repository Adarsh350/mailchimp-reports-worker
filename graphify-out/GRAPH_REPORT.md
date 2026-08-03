# Graph Report - .  (2026-08-03)

## Corpus Check
- 6 files · ~10,111 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 118 nodes · 359 edges · 8 communities (7 shown, 1 thin omitted)
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
  src/index.py →   _Bridges community 1 → community 0_
- `build_campaign_record()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 1 → community 2_
- `build_dashboard_payload()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 1 → community 4_
- `fetch_mailchimp_json()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 1 → community 6_
- `json_response()` --references--> `Any`  [EXTRACTED]
  src/index.py →   _Bridges community 1 → community 3_

## Import Cycles
- None detected.

## Communities (8 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.16
Nodes (25): abuse_status(), as_float(), as_int(), bounce_status(), click_status(), comparison_label(), compute_metrics(), ctor_status() (+17 more)

### Community 1 - "Community 1"
Cohesion: 0.23
Nodes (24): Any, best_metric_from_snapshot(), build_click_narrative(), build_closing_summary(), build_comparison_bundle(), build_delivery_narrative(), build_executive_summary(), build_open_narrative() (+16 more)

### Community 2 - "Community 2"
Cohesion: 0.21
Nodes (13): build_campaign_record(), campaign_kv_key(), chunked(), Default, first_value(), iso_now(), load_campaign_record(), log_event() (+5 more)

### Community 3 - "Community 3"
Cohesion: 0.21
Nodes (12): Response, get_optional_env(), html_response(), is_test_email_path(), is_webhook_path(), json_response(), query_truthy(), record_by_id() (+4 more)

### Community 4 - "Community 4"
Cohesion: 0.19
Nodes (15): datetime, build_dashboard_payload(), cron_state_for_run(), due_date_for_send_date(), extract_segment_name(), first_sorted_record(), jsify(), load_all_campaign_records() (+7 more)

### Community 5 - "Community 5"
Cohesion: 0.17
Nodes (11): devDependencies, wrangler, license, name, private, scripts, deploy, dev (+3 more)

### Community 6 - "Community 6"
Cohesion: 0.29
Nodes (6): Exception, fetch_mailchimp_json(), fetch_recent_sent_campaigns(), MailchimpApiError, retry_delay_seconds(), safe_error_body()

## Knowledge Gaps
- **9 isolated node(s):** `name`, `version`, `license`, `private`, `deploy` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Default` connect `Community 2` to `Community 0`, `Community 3`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Why does `build_report_bundle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.019) - this node is a cross-community bridge._
- **Why does `MailchimpApiError` connect `Community 6` to `Community 0`?**
  _High betweenness centrality (0.019) - this node is a cross-community bridge._
- **What connects `name`, `version`, `license` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._