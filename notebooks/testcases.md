Here are 9 test prompts you can feed straight into `chat()`, scaled by complexity — good for stress-testing both the validation retry loop and the approval gate across different node combos.

## Easy (1 trigger, 1-2 nodes, no branching)

1. **Form → Slack notification**
   *"Whenever someone submits my contact form, post the submitter's name and message to our #general Slack channel."*
   → Form Trigger + Slack. Tests the simplest possible pipeline, one credential, no conditions.

2. **Schedule → Email digest**
   *"Every day at 9am, send me an email summarizing that it's a new day with today's date."*
   → Schedule Trigger + Gmail/Send Email. Tests time-based triggers and a static/expression-only payload.

3. **Webhook → Google Sheets append**
   *"When a webhook is called with order data, add a new row to my Google Sheet with the order details."*
   → Webhook + Google Sheets. Tests basic data mapping from incoming JSON to sheet columns.

## Medium (conditional branching, 3-4 nodes, 2 services)

4. **Conditional lead routing** *(the one you already tested)*
   *"Whenever a new row is added to my Google Sheet, check if the lead's company size is over 50, and if so post a summary to our #sales Slack channel."*
   → Trigger + IF + Slack. Tests numeric comparison operators (exactly where the earlier bug lived).

5. **Ticket triage with priority tagging**
   *"When a new email arrives in Gmail with 'urgent' in the subject, forward it to our on-call engineer's email and also post an alert to #incidents. Otherwise just label it as 'reviewed' in Gmail."**
   → Gmail Trigger + IF (string contains) + two branches (Gmail send + Slack) vs (Gmail label). Tests true/false branch divergence and string-matching operators.

6. **Scheduled report with data transform**
   *"Every Monday at 8am, pull all rows from my Google Sheet where status is 'pending', calculate the total value, and post the total to Slack."*
   → Schedule Trigger + Google Sheets + Code/Set (aggregation) + Slack. Tests the Code/Set node doing actual computation, not just field mapping.

## Hard (multiple conditions, error handling, 5+ nodes, 3+ services)

7. **Multi-stage approval workflow**
   *"When someone submits an expense form, if the amount is under $100 auto-approve and notify them by email; if it's $100-$1000 post it to #finance-approvals for manual review; if it's over $1000, additionally CC the finance director's email. Log every request to a Google Sheet regardless of outcome."*
   → Form Trigger + Switch (3-way branch, not just IF) + multiple Slack/Gmail nodes + Google Sheets (parallel logging branch). Tests Switch node logic and fan-out (one trigger feeding multiple parallel paths).

8. **Retry-aware API sync with error handling**
   *"Every hour, call our internal API to fetch new orders, and for each one create a row in Google Sheets. If the API call fails or times out, post an error alert to #alerts instead of failing silently. If it succeeds but returns zero orders, do nothing."*
   → Schedule Trigger + HTTP Request (with error output branch) + IF (empty check) + Google Sheets + Slack (error path). Tests the HTTP Request node's error/timeout output path specifically — the same pattern flagged as a "designed to fail" lesson in the n8n template your agent pulled earlier.

9. **Cross-service reconciliation pipeline**
   *"Twice a day, compare new signups in my Google Sheet against customers already in our CRM (via HTTP Request to our CRM API). For anyone not yet in the CRM, create them via the API, send them a Slack DM welcome message, and send them a personalized welcome email. Keep a running log in a separate Google Sheet of everyone processed, including failures."*
   → Schedule Trigger + Google Sheets (read) + HTTP Request (lookup) + IF (not-found branch) + HTTP Request (create) + Slack + Gmail + Google Sheets (write, logging both success and failure paths). Tests multi-node data flow with dependent sequential API calls and a logging branch that must capture both success and failure — good stress test for whether the agent invents plausible-but-wrong CRM API fields versus admitting it doesn't know your CRM's actual endpoint shape.

A good order to run them in: 1→3 first to confirm the pipeline still works end-to-end after your two changes, then 4 again to confirm the self-repair loop silently fixes the operator bug without you seeing errors, then 7-9 to see whether the approval gate summary is actually useful (informative but not overwhelming) once workflows get busier — that's the real signal for whether the human-in-the-loop gate is pulling its weight or just adding friction.