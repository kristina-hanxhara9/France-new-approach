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

## ⚠️ MANDATORY: Real web search only — NEVER use training data

**YOU MUST CALL THE WebSearch TOOL FOR EVERY SINGLE COMPANY.**

- You MUST invoke the `WebSearch` tool to get data. Do NOT skip this step.
- NEVER fill in website, phone, email, description, or products from your own
  knowledge or training data. You do NOT know these companies.
- If WebSearch returns no useful results, save ALL fields as empty strings `""`.
- Every piece of data you save MUST come from a WebSearch result snippet or a
  WebFetch page. If you cannot point to which search result gave you the data,
  you are fabricating — stop and save empty strings instead.
- Do NOT say "I know this company is..." or "Based on my knowledge..." — you
  know NOTHING. Only WebSearch and WebFetch know.

**TEST: If you have not made a WebSearch tool call for a company, you CANNOT
save any data for that company. Period.**

## Automatic execution — full pipeline

Run these steps in order, automatically, without pausing:

### Step 1 — Prepare the queue

```bash
python retailer_enrich.py prepare $ARGUMENTS
```

This reads `france_retailers-with-keywords.xlsx` ("All Retailers" tab) and
outputs `web_enrich_queue.json`.

### Step 2 — Read the queue

```bash
cat web_enrich_queue.json
```

Parse the JSON array. Each entry has: `id`, `company_name`, `trade_name`,
`city`, `channel`, `search_query`.

### Step 3 — AUTO-LOOP: search every company

**CRITICAL: Do this automatically for EVERY company in the queue. Do NOT stop
between companies. Process them in batches of 3-5 parallel WebSearch calls.**

For each company in the queue:

#### 3a. WebSearch (MANDATORY — you MUST call this tool)

Call **WebSearch** with query: the `search_query` field from the queue entry.

Set `blocked_domains`: `["societe.com", "verif.com", "pappers.fr", "wikipedia.org", "indeed.fr", "glassdoor.fr"]`

**From the WebSearch result snippets ONLY**, extract:
- **website**: the company's own URL (skip social media, directories, job sites)
- **phone**: customer service number if mentioned in snippets
- **web_description**: what the company does (1-2 sentences from snippets)
- **web_products**: product categories they sell (comma-separated, from snippets)
- **web_business_type**: "Chain (N stores)" or "Independent" or "Buying group (N members)"

**If a field is not visible in the search results, set it to `""`.**

#### 3b. WebFetch (optional — only if WebSearch found a website)

If a website URL was found in step 3a, try **WebFetch** with that URL and prompt:
"Extract: 1) phone number, 2) email, 3) social media links (facebook, instagram,
linkedin, twitter), 4) products sold, 5) business description, 6) number of stores.
Return structured data."

If WebFetch returns 403 or fails — that is normal for French retail sites. Use
the WebSearch results instead. Do NOT retry, just move on.

#### 3c. Fallback: if no results and trade_name differs from company_name

Try one more **WebSearch**: `"{trade_name} magasin france"`

#### 3d. Detect channel from web content

Score these keywords against **only the text from WebSearch/WebFetch results**:
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
python retailer_enrich.py save --id {ID} --json '{"website":"...","phone":"...","email":"...","web_description":"...","web_products":"...","web_business_type":"...","web_channel_guess":"...","web_channel_detail":"...","facebook":"...","instagram":"...","linkedin":"...","twitter":"..."}'
```

Use `""` for any field not found. **Never invent data.**

Then immediately continue to the next company. Do NOT pause.

### Step 4 — Compile

After ALL companies are done:

```bash
python retailer_enrich.py compile
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
2. **REAL SEARCH ONLY**: You MUST call WebSearch for every company. NEVER use training knowledge.
3. **PARALLEL**: Run 3-5 WebSearch calls in parallel when possible to go faster.
4. **SKIP DONE**: If a company is already in `web_enrich_results.json`, skip it.
5. **NEVER INVENT**: If WebSearch returned nothing, save empty strings. Do NOT guess.
6. **SAVE OFTEN**: Save after each company. If interrupted, progress is kept.
7. **403 IS OK**: Many French sites block bots. Use search snippets instead.
8. **PROGRESS**: Print `[N/TOTAL] Company Name — done` after each save.
9. **SOURCE**: Every data point must come from a WebSearch snippet or WebFetch page.
