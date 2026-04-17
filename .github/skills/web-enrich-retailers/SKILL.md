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
2. You spawn a **subagent** with the batch — it does all the web searching
3. When the subagent finishes, you run `prepare --limit 10` again for the next batch
4. Repeat until 0 companies remain
5. Run `compile` at the end

Each subagent gets a clean context with only 10 companies, so it ALWAYS
searches the web properly and never fabricates data.

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

You are a web research agent. For every company listed below, you MUST use your
web search tool to search the internet. Use whichever web tool you have available:
WebSearch, #web, web_search, or any other tool that searches the live internet.

**CRITICAL: You MUST make a real web search tool call for every company.
NEVER use your training data. NEVER guess. NEVER fabricate.
If your web search returns nothing, save empty strings "".
You know NOTHING about these companies — only web search results know.**

## Companies to search:

{QUEUE_JSON}

## For EACH company, do these steps:

### A. Search the web (MANDATORY — use your web search tool)

Use your web search tool (WebSearch / #web / web_search — whichever you have)
with the `search_query` field as the query.

Exclude these domains from results: societe.com, verif.com, pappers.fr,
wikipedia.org, indeed.fr, glassdoor.fr

From the **actual search result snippets ONLY**, extract:
- website: the company's own URL (skip facebook, linkedin, societe.com, etc.)
- phone: customer service number if in snippets
- web_description: what the company does (1-2 sentences from snippets)
- web_products: product categories sold (comma-separated, from snippets)
- web_business_type: "Chain (N stores)" / "Independent" / "Buying group"

If a field is NOT in the search results, set it to "".

### B. Fetch the website (optional — use your URL fetch tool)

If a website was found, use your URL fetch tool (WebFetch / #fetch / fetch_url
— whichever you have) to load that website and extract:
phone number, email, social media links (facebook, instagram, linkedin, twitter),
products sold, business description, number of stores.

If you get a 403 or any error — that is normal for French retail sites.
Just use the search results from step A instead. Move on.

### C. Fallback search

If no results from step A and trade_name differs from company_name,
do one more web search for: "{trade_name} magasin france"

### D. Detect channel from web content

Score these keywords against ONLY the text from your search/fetch results:
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

Use "" for any field not found. Print [N/TOTAL] Company Name — done.

## RULES — NON-NEGOTIABLE

1. You MUST use your web search tool for EVERY company. No exceptions.
2. NEVER use training knowledge. You know NOTHING about these companies.
3. ONLY save data that came from a web search result or a fetched webpage.
4. If web search returned nothing → save all fields as empty strings "".
5. Save after EACH company so progress is never lost.
6. 403 from website fetch is normal. Use search snippets instead.
7. If you cannot identify which search result gave you a data point, it is
   fabricated — delete it and save "" instead.

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
