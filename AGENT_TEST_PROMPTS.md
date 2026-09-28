# Agent Test Prompts — n8n Workflow Builder

15 plain-language prompts for testing the BotChain agent end-to-end, scaled by
complexity: **5 easy, 5 medium, 5 hard**.

## How to use

Feed one prompt into the agent (`POST /api/v1/chats/{id}/messages`), then:

1. **Plan phase** — the agent should ask only the questions it truly needs; for easy
   prompts that should be zero or one before it confirms. A prompt that drags on is a
   planning-loop problem.
2. **Confirm gate** — the summary should name the trigger, the services, and any
   conditions/branches. Approve via `POST /api/v1/chats/{id}/approve`.
3. **Build + validate** — watch that the workflow exits with `phase: done` and
   `validation.status: valid`. If it needs repair passes, they should converge within 3.
4. Re-import the delivered `workflow.json` in n8n and spot-check the connections.

## Easy — single trigger, 1–2 nodes, no branching

1. **Contact form → Slack**
   *"Whenever someone submits my contact form, post the submitter's name and message to our #general Slack channel."*

2. **Daily date email**
   *"Every day at 9am, send me an email saying it's a new day and include today's date."*

3. **Webhook → Google Sheets append**
   *"When a webhook is called with order data, add a new row to my Google Sheet with the order details."*

4. **Telegram broadcast**
   *"Every morning at 7am, send my whole team a Telegram message telling them it's the start of a new workday."*

5. **Webhook → HTTP forward**
   *"When a webhook receives a lead object, look it up in the CRM and return the full customer record as the webhook's response."*

Expected (all): trigger + 1–2 action/service nodes, straight chain, no conditions.

## Medium — one branch or transform, 3–4 nodes, 2 services

6. **Conditional lead routing**
   *"Whenever a new row is added to my Google Sheet, check if the lead's company size is over 50 employees, and if so post a summary to our #sales Slack channel."*

7. **Urgent-email triage**
   *"When a new email arrives in Gmail with 'urgent' in the subject, forward it to our on-call engineer and post an alert to #incidents. Otherwise just label it 'reviewed' in Gmail."*

8. **Scheduled status report**
   *"Every Monday at 8am, pull all rows from my Google Sheet where status is 'pending', calculate the total value, and post the total to Slack."*

9. **Keyword-router bot**
   *"When a Telegram message comes in, if it mentions 'invoice' send it to our accounts email, if it mentions 'bug' post it to #bugs on Slack, otherwise just reply to the sender with a generic message."*

10. **Order-amount routing**
    *"When a webhook delivers an order, if the total is under $100 send a standard confirmation email; if it's $100 or more send a thank-you email and also post to #big-orders on Slack."*

Expected: trigger + 3–4 nodes, one IF/Switch divergence or one Code/Set transform, two
services. Watch the operator choice (numeric comparison, string-contains) — the agent
must pick valid n8n option values, not paraphrase them.

> **Easy #4 / Telegram broadcast — PASSED (chat `8862058a-e450-4ab8-a15b-4425d413ee9c`)**
> Valid ScheduleTrigger (hour 7) → Telegram sendMessage; converged in 3 attempts on
> ministral-14b.

> **Medium #10 / Order-amount routing — PASSED validator, LOGIC GAP (chat `382d845f-b930-4f23-912b-c7b652995b21`)**
> `validation.status: valid` after 3 attempts on ministral-14b. IF node used the correct
> v2 condition object (`operator: {type: number, operation: lt}`); Set used the nested
> `assignments: {assignments: [...]}` form; branches wired as `main: [[true],[false]]`.
> **Gap**: `SendThankYouEmail` and `PostToSlack` are orphaned — `SetMessages` has no
> outgoing connections, so $100+ orders run the Set node and stop. n8n's validator only
> checks structure; the build/repair loop has no spec-vs-connections re-check. Punted;
> candidate fix (not yet applied): structural coherence check in `validate_node` — every
> non-trigger node must have an incoming connection, every IF/Switch branch reachable.

## Hard — multi-branch, dependent calls, error handling, 5+ nodes, 3+ services

11. **Expense approval workflow**
    *"When someone submits an expense form, if the amount is under $100 auto-approve and notify them by email; if it's $100–$1000 post it to #finance-approvals for manual review; if it's over $1000 additionally CC the finance director's email. Log every request to a Google Sheet regardless of outcome."*

12. **Retry-aware API sync**
    *"Every hour, call our internal API to fetch new orders, and for each one create a row in Google Sheets. If the API call fails or times out, post an error alert to #alerts instead of failing silently. If it succeeds but returns zero orders, do nothing."*

13. **Cross-service customer reconciliation**
    *"Twice a day, compare new signups in my Google Sheet against customers already in our CRM. For anyone not yet in the CRM, create them via the API, send them a Slack DM welcome message, and send a personalized welcome email. Keep a running log in a separate Google Sheet of everyone processed, including failures."*

14. **Overnight order processor with guards**
    *"Every night at 2am, read the rows marked 'pending' in my orders sheet. For each one, call the fulfillment API to get its status; if it's already fulfilled, mark the row 'done'; if it's cancelled, post an alert to #ops; otherwise send the customer a status email. Never process the same order twice, and if any API call fails, post to #alerts instead of stopping."*

15. **E-commerce intake with validation + enrichment**
    *"When a webhook delivers a new order, reject it with an error response if the email or SKU is missing. For valid orders, look up the product name from the catalog API, compute the line total with the Code node, then post a receipt to #sales on Slack, email the customer, and append the enriched order to Google Sheets. If any step fails after validation, log the failure to a separate error sheet so nothing is lost."*

Expected: 5–9 nodes, a Switch or IF fan-out plus parallel logging branches, sequential
HTTP Request calls with dependant data, and an explicit error/no-result path. The agent
must NOT invent CRM/catalog fields — it should keep expressions generic or say it needs
the endpoint shape. Hard prompts are also the real test of the confirm summary: informative
but not overwhelming.