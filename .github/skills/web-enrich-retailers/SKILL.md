---
name: web-enrich-retailers
description: >-
  Enrich French retailers from france_retailers-with-keywords.xlsx with web data.
  Use when asked to enrich retailers, find retailer websites, look up phone numbers,
  verify retailer channels, or add web data to the retailer database.
  Searches the web for each company, scrapes their website, and saves
  website URL, phone, email, products sold, business description,
  business type (chain/independent/buying group), channel verification,
  and social media links.
user-invocable: true
argument-hint: "[--limit N]"
allowed-tools: Read, Bash, WebSearch, WebFetch, Grep, Glob
---

# Web Enrichment Agent for French Retailers

You are a web research agent that enriches French retailer data with information
found on the web. You use **WebSearch** to find companies and **WebFetch** to
scrape their websites for contact details, products, and business descriptions.

## Setup

The helper script `web_enrich_agent.py` handles all data I/O (reading Excel,
saving results to JSON, compiling the enriched output). You drive the web
searching.

## Workflow

### Step 1 — Prepare the queue

```bash
python web_enrich_agent.py prepare $ARGUMENTS
```

This reads `france_retailers-with-keywords.xlsx` (the "All Retailers" tab) and
outputs `web_enrich_queue.json` — a list of companies to search. Each entry has:
- `siren` — unique company ID
- `legal_name` — registered company name
- `trade_name` — brand name (may differ from legal name)
- `city` — city where the company is located
- `channel` — assigned channel (CE, Photo, MDA, etc.)
- `search_query` — pre-built search string

### Step 2 — Search each company

Read `web_enrich_queue.json` and for each company:

1. **WebSearch** for: `"{legal_name} {city} france magasin site officiel"`
   - Block these domains: societe.com, verif.com, pappers.fr, wikipedia.org, indeed.fr
   - From the results, identify:
     - The company's **own website URL** (skip directories, social media, job sites)
     - **Phone number** if mentioned in search snippets
     - **Business description** from snippets
     - **Products** they sell
     - Whether it's a **chain** (multiple stores), **independent**, or **buying group**

2. **WebFetch** the company's website (if found and not blocked):
   - Prompt: "Extract: 1) phone number, 2) email, 3) social media links
     (facebook, instagram, linkedin, twitter), 4) what products they sell,
     5) business description, 6) number of stores"
   - Many French retail sites return 403 — that's fine, use search results instead

3. If the legal_name search gave poor results and `trade_name` differs, retry
   with: `"{trade_name} magasin france"`

4. **Detect the channel** from web content. Look for these keywords:
   - **Photo**: photo, camera, objectif, optique, reflex, hybride
   - **CE**: informatique, ordinateur, multimedia, audio, video, tv, gaming
   - **MDA**: electromenager, lave-linge, refrigerateur, four, cuisiniere
   - **SDA**: petit electromenager, cafetiere, aspirateur, robot cuisine
   - **Mobile**: telephone, mobile, smartphone, forfait, operateur, telecom
   - **Phone Accessories**: coque, accessoire telephone, chargeur, protection
   - **Refurb**: reconditionne, reparation, occasion, seconde main

### Step 3 — Save each result

After searching each company, save the result:

```bash
python web_enrich_agent.py save --siren {SIREN} --json '{
  "website": "https://...",
  "phone": "...",
  "email": "...",
  "web_description": "What the company does (1-2 sentences)",
  "web_products": "Product categories they sell (comma-separated)",
  "web_business_type": "Chain (N stores) / Independent / Buying group (N members)",
  "web_channel_guess": "CE",
  "web_channel_detail": "CE (5), MDA (3)",
  "facebook": "https://facebook.com/...",
  "instagram": "https://instagram.com/...",
  "linkedin": "https://linkedin.com/company/...",
  "twitter": "https://twitter.com/..."
}'
```

Fields explanation:
- `web_description`: What the business does, from web content
- `web_products`: Comma-separated list of product categories sold
- `web_business_type`: Chain (with store count), Independent, or Buying group
- `web_channel_guess`: Best-fit channel based on web content keywords
- `web_channel_detail`: All channel scores, e.g. "CE (5), MDA (3), SDA (1)"

Use empty string `""` for any field not found. Never invent data.

### Step 4 — Compile the enriched Excel

```bash
python web_enrich_agent.py compile
```

This merges all saved results into `france_retailers_enriched.xlsx` with the
new columns added alongside the original data.

### Step 5 — Report results

Show a summary table:
- Total companies searched
- Websites found
- Phone numbers found
- Emails found
- Channel matches vs mismatches

## Important rules

- **Never invent data.** If you can't find a phone number, leave it empty.
- **Batch WebSearch calls** — run 3-5 in parallel when possible.
- **Skip already-done companies** — check `web_enrich_results.json` first.
- **Use `python web_enrich_agent.py status`** to check progress.
- If WebFetch returns 403, that's normal for French retail sites. Use search results.
- Always save results after each company so progress isn't lost.
