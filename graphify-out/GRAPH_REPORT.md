# Graph Report - .  (2026-04-16)

## Corpus Check
- 4 files · ~10,193 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 102 nodes · 286 edges · 6 communities detected
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]

## God Nodes (most connected - your core abstractions)
1. `build_report_bundle()` - 23 edges
2. `compute_metrics()` - 21 edges
3. `build_dashboard_payload()` - 15 edges
4. `format_percent()` - 14 edges
5. `render_report_html()` - 12 edges
6. `format_integer()` - 12 edges
7. `Default` - 10 edges
8. `persist_report_result()` - 9 edges
9. `fetch_mailchimp_json()` - 9 edges
10. `load_all_campaign_records()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `build_report_bundle()` --calls--> `build_campaign_record()`  [EXTRACTED]
  src/index.py → src/index.py  _Bridges community 3 → community 2_
- `build_report_bundle()` --calls--> `fetch_mailchimp_json()`  [EXTRACTED]
  src/index.py → src/index.py  _Bridges community 2 → community 5_
- `build_report_bundle()` --calls--> `require_env()`  [EXTRACTED]
  src/index.py → src/index.py  _Bridges community 2 → community 4_
- `build_report_bundle()` --calls--> `compute_metrics()`  [EXTRACTED]
  src/index.py → src/index.py  _Bridges community 2 → community 0_
- `build_report_bundle()` --calls--> `build_comparison_bundle()`  [EXTRACTED]
  src/index.py → src/index.py  _Bridges community 2 → community 1_

## Communities

### Community 0 - "Community 0"
Cohesion: 0.16
Nodes (24): abuse_status(), as_float(), as_int(), bounce_status(), click_status(), comparison_label(), compute_metrics(), ctor_status() (+16 more)

### Community 1 - "Community 1"
Cohesion: 0.19
Nodes (19): best_metric_from_snapshot(), build_click_narrative(), build_comparison_bundle(), build_delivery_narrative(), build_executive_summary(), build_open_narrative(), build_trust_narrative(), click_assessment() (+11 more)

### Community 2 - "Community 2"
Cohesion: 0.15
Nodes (19): build_closing_summary(), build_dashboard_payload(), build_recommendations(), build_report_bundle(), cron_state_for_run(), due_date_for_send_date(), extract_segment_name(), first_sorted_record() (+11 more)

### Community 3 - "Community 3"
Cohesion: 0.21
Nodes (13): build_campaign_record(), campaign_kv_key(), chunked(), Default, first_value(), iso_now(), log_event(), normalize_token() (+5 more)

### Community 4 - "Community 4"
Cohesion: 0.22
Nodes (11): get_optional_env(), html_response(), is_test_email_path(), is_webhook_path(), json_response(), query_truthy(), record_by_id(), render_dashboard_shell() (+3 more)

### Community 5 - "Community 5"
Cohesion: 0.29
Nodes (6): Exception, fetch_mailchimp_json(), fetch_recent_sent_campaigns(), MailchimpApiError, retry_delay_seconds(), safe_error_body()

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Default` connect `Community 3` to `Community 0`, `Community 4`?**
  _High betweenness centrality (0.051) - this node is a cross-community bridge._
- **Why does `MailchimpApiError` connect `Community 5` to `Community 0`?**
  _High betweenness centrality (0.039) - this node is a cross-community bridge._
- **Why does `build_report_bundle()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._