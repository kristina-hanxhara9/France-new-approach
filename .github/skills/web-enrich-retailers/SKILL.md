---
name: web-enrich-retailers
description: >-
  Enrich French retailers from france_retailers-with-keywords.xlsx with web data.
  Use when asked to enrich retailers, find retailer websites, look up phone numbers,
  verify retailer channels, or add web data to the retailer database.
  Automatically loops through ALL companies — no manual intervention needed.
user-invocable: true
argument-hint: "[--limit N]"
allowed-tools: Read, Bash, WebSearch, WebFetch, Grep, Glob
---

# Web Enrichment Agent — Orchestrator

You are the ORCHESTRATOR. You do NOT search the web yourself. You spawn
subagents — each one handles exactly 10 companies with a fresh context.

## How it works

1. You run `prepare --limit 10` to get the next batch
2. You spawn a **subagent** with the batch — it does all the WebSearch calls
3. When the subagent finishes, you run `prepare --limit 10` again for the next batch
4. Repeat until 0 companies remain
5. Run `compile` at the end

Each subagent gets a clean context with only 10 companies, so it ALWAYS calls
WebSearch properly and never fabricates data.

## Step 1 — LOOP: Spawn subagents for batches of 10

Repeat this loop until done:

### 1a. Prepare the next batch

```bash
python retailer_enrich.py prepare --limit 10
```

**If output says "0 companies to search" → go to Step 2 (compile).**

### 1b. Read the queue

```bash
cat web_enrich_queue.json
```

### 1c. Spawn a subagent for this batch

Use the **Agent** tool to spawn a subagent. Pass the FULL queue JSON and the
instructions below as the subagent's prompt. The subagent does all the work.

**Subagent prompt — copy this exactly, replacing {QUEUE_JSON} with the actual
queue contents:**

---BEGIN SUBAGENT PROMPT---

You are a web research agent. You MUST call WebSearch for every company below.
NEVER use training data. NEVER fabricate. If WebSearch returns nothing, save "".

## Companies to search:

{QUEUE_JSON}

## For EACH company, do these steps:

### A. WebSearch (MANDATORY)
Call WebSearch with the `search_query` field.
Set blocked_domains: ["societe.com", "verif.com", "pappers.fr", "wikipedia.org", "indeed.fr", "glassdoor.fr"]

From the search result snippets ONLY, extract:
- website: the company's own URL (skip facebook, linkedin, societe.com, etc.)
- phone: customer service number if in snippets
- web_description: what the company does (1-2 sentences from snippets)
- web_products: product categories sold (comma-separated, from snippets)
- web_business_type: "Chain (N stores)" / "Independent" / "Buying group"

If a field is NOT in the search results, set it to "".

### B. WebFetch (optional)
If a website was found, try WebFetch with prompt:
"Extract: 1) phone number, 2) email, 3) social media links (facebook, instagram, linkedin, twitter), 4) products sold, 5) business description, 6) number of stores."

If 403 or error — use search snippets only. Move on.

### C. Fallback
If no results and trade_name differs from company_name, try one more WebSearch:
"{trade_name} magasin france"

### D. Detect channel
Score these keywords against ONLY the WebSearch/WebFetch text:
- Photo: photo, camera, objectif, optique, reflex, hybride
- CE: informatique, ordinateur, multimedia, audio, video, tv, gaming
- MDA: electromenager, lave-linge, refrigerateur, four, cuisiniere
- SDA: petit electromenager, cafetiere, aspirateur, robot cuisine
- Mobile: telephone, mobile, smartphone, forfait, operateur, telecom
- Phone Accessories: coque, accessoire telephone, chargeur, protection
- Refurb: reconditionne, reparation, occasion, seconde main

Set web_channel_guess to top-scoring. Set web_channel_detail to all scores.

### E. Save IMMEDIATELY after each company

```bash
python retailer_enrich.py save --id {ID} --json '{"website":"...","phone":"...","email":"...","web_description":"...","web_products":"...","web_business_type":"...","web_channel_guess":"...","web_channel_detail":"...","facebook":"...","instagram":"...","linkedin":"...","twitter":"..."}'
```

Print [N/TOTAL] Company Name — done.

## Rules
- You MUST call WebSearch for EVERY company. No exceptions.
- NEVER use training knowledge. You know NOTHING about these companies.
- If WebSearch returned nothing → save empty strings. Do NOT guess.
- Save after each company so progress is never lost.
- 403 from WebFetch is normal. Use search snippets instead.

---END SUBAGENT PROMPT---

### 1d. After the subagent returns

Print the subagent's summary. Then check progress:

```bash
python retailer_enrich.py status
```

**Go back to step 1a** — run `prepare --limit 10` again for the next batch.

## Step 2 — Compile (when all batches are done)

```bash
python retailer_enrich.py compile
```

## Step 3 — Report

Show a summary table:
- Total companies searched
- Websites found
- Phone numbers found
- Emails found
- Channel matches vs mismatches
- Companies with no web results

## Rules for the orchestrator

1. **NEVER search yourself.** Always spawn a subagent.
2. **10 per subagent.** Never pass more than 10 companies to a subagent.
3. **LOOP until done.** Keep spawning subagents until prepare says 0 remaining.
4. **COMPILE once** at the very end after all batches.
5. **NEVER INVENT data.** Neither you nor the subagent should fabricate anything.
