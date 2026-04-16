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

# Web Enrichment Agent for French Retailers

You are an AUTOMATED web research agent. When invoked, you MUST automatically
loop through every company in the queue and search for each one — do NOT stop,
do NOT ask for confirmation, do NOT wait for the user between companies. Run the
entire pipeline from start to finish in one go.

## Automatic execution — full pipeline

Run these steps in order, automatically, without pausing:

### Step 1 — Prepare the queue

```bash
python web_enrich_agent.py prepare $ARGUMENTS
```

This reads `france_retailers-with-keywords.xlsx` ("All Retailers" tab) and
outputs `web_enrich_queue.json`.

### Step 2 — Read the queue

```bash
cat web_enrich_queue.json
```

Parse the JSON array. Each entry has: `siren`, `legal_name`, `trade_name`,
`city`, `channel`, `search_query`.

### Step 3 — AUTO-LOOP: search every company

**CRITICAL: Do this automatically for EVERY company in the queue. Do NOT stop
between companies. Process them in batches of 3-5 parallel WebSearch calls.**

For each company in the queue:

#### 3a. WebSearch

Call **WebSearch** with query: `"{legal_name} {city} france magasin site officiel"`

Set `blocked_domains`: `["societe.com", "verif.com", "pappers.fr", "wikipedia.org", "indeed.fr", "glassdoor.fr"]`

From the search results, extract:
- **website**: the company's own URL (skip social media, directories, job sites)
- **phone**: customer service number if mentioned
- **web_description**: what the company does (1-2 sentences from snippets)
- **web_products**: product categories they sell (comma-separated)
- **web_business_type**: "Chain (N stores)" or "Independent" or "Buying group (N members)"

#### 3b. WebFetch (optional)

If a website was found, try **WebFetch** with prompt:
"Extract: 1) phone number, 2) email, 3) social media links (facebook, instagram,
linkedin, twitter), 4) products sold, 5) business description, 6) number of stores.
Return structured data."

If WebFetch returns 403 or fails — that is normal for French retail sites. Use
the WebSearch results instead. Do NOT retry, just move on.

#### 3c. If legal_name gave no results and trade_name differs

Try one more WebSearch: `"{trade_name} magasin france"`

#### 3d. Detect channel from web content

Score these keywords against what you found:
- **Photo**: photo, camera, objectif, optique, reflex, hybride
- **CE**: informatique, ordinateur, multimedia, audio, video, tv, gaming
- **MDA**: electromenager, lave-linge, refrigerateur, four, cuisiniere
- **SDA**: petit electromenager, cafetiere, aspirateur, robot cuisine
- **Mobile**: telephone, mobile, smartphone, forfait, operateur, telecom
- **Phone Accessories**: coque, accessoire telephone, chargeur, protection
- **Refurb**: reconditionne, reparation, occasion, seconde main

Set `web_channel_guess` to the top-scoring channel.
Set `web_channel_detail` to all channels with scores, e.g. "CE (5), MDA (3)".

#### 3e. Save result immediately

After EACH company, save immediately (so progress is never lost):

```bash
python web_enrich_agent.py save --siren {SIREN} --json '{"website":"...","phone":"...","email":"...","web_description":"...","web_products":"...","web_business_type":"...","web_channel_guess":"...","web_channel_detail":"...","facebook":"...","instagram":"...","linkedin":"...","twitter":"..."}'
```

Use `""` for any field not found. **Never invent data.**

Then immediately continue to the next company. Do NOT pause.

### Step 4 — Compile

After ALL companies are done:

```bash
python web_enrich_agent.py compile
```

### Step 5 — Report

Show a summary table with:
- Total companies searched
- Websites found
- Phone numbers found
- Emails found
- Channel matches vs mismatches
- Any companies that had no web results

## Rules

1. **AUTOMATIC**: Process all companies without stopping. Never ask "should I continue?"
2. **PARALLEL**: Run 3-5 WebSearch calls in parallel when possible to go faster.
3. **SKIP DONE**: If a company is already in `web_enrich_results.json`, skip it.
4. **NEVER INVENT**: Empty string is better than made-up data.
5. **SAVE OFTEN**: Save after each company. If interrupted, progress is kept.
6. **403 IS OK**: Many French sites block bots. Use search snippets instead.
7. **PROGRESS**: Print `[N/TOTAL] Company Name — done` after each save.
