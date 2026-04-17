#!/usr/bin/env python3
"""
Customizable Web Enrichment Agent
===================================
Generic, config-driven script for enriching retailer data with web search.
Works for any country and any set of channels. Edit the CONFIG dict below.

Commands:
    python retailer_enrich.py prepare [--limit N]   Read Excel → search queue
    python retailer_enrich.py save --id X --json '{...}'   Save one result
    python retailer_enrich.py compile                Merge results → enriched Excel
    python retailer_enrich.py status                 Show progress
    python retailer_enrich.py skill                  Generate SKILL.md for the agent

The agent (Copilot / Claude Code) uses WebSearch + WebFetch to search each
company, then calls 'save' for each result. No external search libraries.
"""

import argparse
import json
import os
import re
import sys
import textwrap
from urllib.parse import urlparse

import pandas as pd

# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  CONFIG — Edit this section for your country / channels / language   ║
# ╚═══════════════════════════════════════════════════════════════════════╝

CONFIG = {
    # ── Country & language ────────────────────────────────────────────
    "country": "France",
    "language": "fr",
    # Appended to every WebSearch query: "{company} {city} {search_suffix}"
    "search_suffix": "france magasin site officiel",
    # Fallback search if primary gives no results: "{trade_name} {fallback_suffix}"
    "fallback_suffix": "magasin france",

    # ── Input / output files ──────────────────────────────────────────
    "input_file": "france_retailers-with-keywords.xlsx",
    "input_sheet": "All Retailers",
    "output_file": "france_retailers_enriched.xlsx",
    "queue_file": "web_enrich_queue.json",
    "results_file": "web_enrich_results.json",

    # ── Column mapping (must match your Excel) ────────────────────────
    "company_id_field": "siren",       # unique company identifier
    "company_name_field": "legal_name",
    "trade_name_field": "trade_name",  # set "" if not available
    "city_field": "city",              # set "" if not available
    "channel_field": "channel",        # set "" if not available

    # ── Domains to skip when picking the company's own website ────────
    # Social media (facebook, linkedin, etc.) are always skipped.
    # Add your country's business registries and directories here.
    "skip_domains": [
        "societe.com", "verif.com", "pappers.fr", "infogreffe.fr",
        "pagesjaunes.fr", "google.fr",
        "annuaire-entreprises.data.gouv.fr",
    ],

    # ── Domains to block in WebSearch (passed to the agent) ───────────
    "blocked_domains": [
        "societe.com", "verif.com", "pappers.fr",
        "wikipedia.org", "indeed.fr", "glassdoor.fr",
    ],

    # ── Channel definitions ───────────────────────────────────────────
    # Each channel has web_keywords used to detect what a company sells
    # from its website content. Add/remove/rename channels freely.
    "channels": {
        "Photo": {
            "web_keywords": [
                "photo", "photographie", "camera", "appareil photo",
                "objectif", "reflex", "hybride", "optique",
                "tirage photo", "labo photo", "studio photo",
            ],
        },
        "CE": {
            "web_keywords": [
                "informatique", "ordinateur", "pc portable", "tablette",
                "multimedia", "audio", "video", "television", "tv",
                "hi-fi", "hifi", "home cinema", "gaming", "console",
            ],
        },
        "MDA": {
            "web_keywords": [
                "electromenager", "gros electromenager",
                "lave-linge", "lave linge", "machine a laver",
                "refrigerateur", "frigo", "congelateur",
                "lave-vaisselle", "four", "cuisiniere", "hotte",
            ],
        },
        "SDA": {
            "web_keywords": [
                "petit electromenager", "cafetiere", "machine a cafe",
                "aspirateur", "robot cuisine", "mixeur", "blender",
                "bouilloire", "grille-pain", "fer a repasser",
            ],
        },
        "Mobile": {
            "web_keywords": [
                "telephone", "mobile", "smartphone", "forfait",
                "operateur", "telecom", "abonnement mobile",
                "iphone", "samsung galaxy",
            ],
        },
        "Phone Accessories": {
            "web_keywords": [
                "coque", "accessoire telephone", "accessoire mobile",
                "protection ecran", "verre trempe", "chargeur",
                "ecouteur", "casque audio", "enceinte bluetooth",
            ],
        },
        "Refurb": {
            "web_keywords": [
                "reconditionne", "refurbished", "remis a neuf",
                "reparation", "depannage", "seconde main",
                "occasion", "reconditionnement",
            ],
        },
    },

    # ── SKILL.md settings ─────────────────────────────────────────────
    "skill_name": "web-enrich-retailers",

    # ── WebFetch prompt (sent to every company website) ───────────────
    "webfetch_prompt": (
        "Extract: 1) phone number, 2) email, 3) social media links "
        "(facebook, instagram, linkedin, twitter), 4) products sold, "
        "5) business description, 6) number of stores. Return structured data."
    ),
}

# ═══════════════════════════════════════════════════════════════════════
# UNIVERSAL SKIP DOMAINS — always filtered, regardless of country
# ═══════════════════════════════════════════════════════════════════════

UNIVERSAL_SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "google.com", "bing.com", "wikipedia.org", "wikidata.org",
    "indeed.com", "glassdoor.com",
}

# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _all_skip_domains():
    """Merge universal + country-specific skip domains."""
    return UNIVERSAL_SKIP_DOMAINS | set(CONFIG.get("skip_domains", []))


def detect_channel(text):
    """Score each channel based on keywords found in the text.
    Returns (best_channel, detail_string)."""
    text_lower = text.lower()
    scores = {}
    for channel, ch_cfg in CONFIG["channels"].items():
        hits = sum(1 for kw in ch_cfg["web_keywords"] if kw in text_lower)
        if hits:
            scores[channel] = hits
    if not scores:
        return "", ""
    best = max(scores, key=scores.get)
    parts = sorted(scores.items(), key=lambda x: -x[1])
    detail = ", ".join(f"{ch} ({n})" for ch, n in parts)
    return best, detail


def pick_website(urls):
    """From a list of URLs, pick the most likely company website."""
    skip = _all_skip_domains()
    for url in urls:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        if any(s in domain for s in skip):
            continue
        return url
    return ""


def _load_results():
    """Load existing results from the results file."""
    path = CONFIG["results_file"]
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_results(results):
    """Write results dict to the results file."""
    with open(CONFIG["results_file"], "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: prepare
# ═══════════════════════════════════════════════════════════════════════


def cmd_prepare(args):
    """Read the Excel and produce a JSON queue of companies to search."""
    input_file = args.input or CONFIG["input_file"]
    sheet = CONFIG["input_sheet"]
    id_field = CONFIG["company_id_field"]
    name_field = CONFIG["company_name_field"]
    trade_field = CONFIG.get("trade_name_field", "")
    city_field = CONFIG.get("city_field", "")
    channel_field = CONFIG.get("channel_field", "")
    limit = args.limit

    try:
        df = pd.read_excel(input_file, sheet_name=sheet)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return

    print(f"Loaded {len(df)} rows from '{input_file}' → '{sheet}'")

    # Validate required column exists
    if id_field not in df.columns:
        print(f"ERROR: Column '{id_field}' not found. Available: {list(df.columns)}")
        return

    # Build aggregation dict
    agg_dict = {name_field: "first"} if name_field in df.columns else {}
    for f in [trade_field, city_field, channel_field]:
        if f and f in df.columns:
            agg_dict[f] = "first"

    companies = df.groupby(id_field).agg(agg_dict).reset_index()

    # Skip already-searched companies
    existing = set(_load_results().keys())

    queue = []
    for _, row in companies.iterrows():
        cid = str(row[id_field])
        if cid in existing:
            continue
        name = str(row.get(name_field, "") or "")
        trade = str(row.get(trade_field, "") or "") if trade_field else ""
        city = str(row.get(city_field, "") or "") if city_field else ""
        channel = str(row.get(channel_field, "") or "") if channel_field else ""
        if not name:
            continue
        search_q = f"{name} {city} {CONFIG['search_suffix']}".strip()
        queue.append({
            "id": cid,
            "company_name": name,
            "trade_name": trade,
            "city": city,
            "channel": channel,
            "search_query": search_q,
        })

    if limit:
        queue = queue[:limit]

    with open(CONFIG["queue_file"], "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    print(f"Queue: {len(queue)} companies to search ({len(existing)} already done)")
    print(f"Saved to {CONFIG['queue_file']}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: save
# ═══════════════════════════════════════════════════════════════════════


def cmd_save(args):
    """Save a single company's web enrichment result."""
    cid = args.id
    data = json.loads(args.json)
    results = _load_results()
    results[cid] = data
    _save_results(results)
    name = data.get("company_name", data.get("legal_name", ""))
    website = data.get("website", "")
    phone = data.get("phone", "")
    print(f"Saved: {cid} ({name}) — website={website}, phone={phone}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: compile
# ═══════════════════════════════════════════════════════════════════════


def cmd_compile(args):
    """Merge web enrichment results into the retailer Excel."""
    input_file = args.input or CONFIG["input_file"]
    output_file = args.output or CONFIG["output_file"]
    sheet = CONFIG["input_sheet"]
    id_field = CONFIG["company_id_field"]
    channel_field = CONFIG.get("channel_field", "")

    results = _load_results()
    if not results:
        print(f"ERROR: No results in {CONFIG['results_file']}. Run searches first.")
        return
    print(f"Loaded {len(results)} enrichment results")

    try:
        df = pd.read_excel(input_file, sheet_name=sheet)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return
    print(f"Loaded {len(df)} rows from '{input_file}'")

    # Map results onto rows
    enrich_cols = [
        "website", "phone", "email",
        "web_description", "web_products", "web_business_type",
        "web_channel_guess", "web_channel_detail",
        "facebook", "instagram", "linkedin", "twitter",
    ]
    for col in enrich_cols:
        df[col] = df[id_field].apply(
            lambda s: results.get(str(s), {}).get(col, "")
        )

    # Channel match check
    if channel_field and channel_field in df.columns:
        def _match(row):
            guess = (row.get("web_channel_guess") or "").strip()
            assigned = str(row.get(channel_field) or "")
            if not guess:
                return ""
            if guess in assigned:
                return "Match"
            return f"Web suggests: {guess}"
        df["web_vs_assigned"] = df.apply(_match, axis=1)

    # Write enriched Excel
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        # Per-channel sheets
        if channel_field and channel_field in df.columns:
            all_channels = set()
            for ch_str in df[channel_field].dropna().unique():
                for ch in str(ch_str).split(" | "):
                    ch = ch.strip()
                    if ch:
                        all_channels.add(ch)
            for ch in sorted(all_channels):
                mask = df[channel_field].str.contains(ch, regex=False)
                ch_df = df[mask].copy()
                if not ch_df.empty:
                    sheet_name = ch.replace("/", "-")[:31]
                    ch_df.to_excel(writer, index=False, sheet_name=sheet_name)

        df.to_excel(writer, index=False, sheet_name="All Retailers")

        # Enrichment log
        log_rows = [{"id": cid, **info} for cid, info in results.items()]
        if log_rows:
            pd.DataFrame(log_rows).to_excel(
                writer, index=False, sheet_name="Web Enrichment Log"
            )

    has_website = df["website"].astype(bool).sum()
    has_phone = df["phone"].astype(bool).sum()
    has_email = df["email"].astype(bool).sum()
    has_guess = df["web_channel_guess"].astype(bool).sum()
    matches = (df.get("web_vs_assigned") == "Match").sum() if "web_vs_assigned" in df.columns else 0

    print(f"\nSaved: {output_file}")
    print(f"  Rows:             {len(df)}")
    print(f"  Website found:    {has_website}")
    print(f"  Phone found:      {has_phone}")
    print(f"  Email found:      {has_email}")
    print(f"  Channel detected: {has_guess}")
    print(f"  Channel matches:  {matches}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: status
# ═══════════════════════════════════════════════════════════════════════


def cmd_status(args):
    """Show enrichment progress."""
    done = len(_load_results())
    remaining = 0
    if os.path.exists(CONFIG["queue_file"]):
        with open(CONFIG["queue_file"], "r", encoding="utf-8") as f:
            remaining = len(json.load(f))
    total = done + remaining
    pct = (done / total * 100) if total else 0
    print(f"Progress: {done}/{total} ({pct:.0f}%)")
    print(f"  Done:      {done}")
    print(f"  Remaining: {remaining}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: skill — generate SKILL.md from CONFIG
# ═══════════════════════════════════════════════════════════════════════


def cmd_skill(args):
    """Generate a SKILL.md file for the agent from the current CONFIG."""

    # Build channel keywords block
    ch_lines = []
    for name, ch_cfg in CONFIG["channels"].items():
        kws = ", ".join(ch_cfg["web_keywords"][:8])
        ch_lines.append(f"   - **{name}**: {kws}")
    channel_block = "\n".join(ch_lines)

    # Build blocked domains JSON
    blocked = json.dumps(CONFIG["blocked_domains"])

    country = CONFIG["country"]
    search_suffix = CONFIG["search_suffix"]
    fallback_suffix = CONFIG.get("fallback_suffix", f"store {country.lower()}")
    input_file = CONFIG["input_file"]
    id_field = CONFIG["company_id_field"]
    name_field = CONFIG["company_name_field"]
    trade_field = CONFIG.get("trade_name_field", "trade_name")
    city_field = CONFIG.get("city_field", "city")
    webfetch_prompt = CONFIG.get("webfetch_prompt", "Extract phone, email, products, description, social media links.")
    skill_name = CONFIG.get("skill_name", "web-enrich-retailers")

    # Build channel keywords block for subagent prompt
    ch_lines_sub = []
    for name, ch_cfg in CONFIG["channels"].items():
        kws = ", ".join(ch_cfg["web_keywords"][:8])
        ch_lines_sub.append(f"- {name}: {kws}")
    channel_block_sub = "\n".join(ch_lines_sub)

    skill_md = textwrap.dedent(f"""\
---
name: {skill_name}
description: >-
  Enrich {country} retailers from {input_file} with web data.
  Spawns subagents (10 companies each) that call WebSearch for every company.
  Fully automatic — no manual intervention needed.
user-invocable: true
argument-hint: "[--limit N]"
allowed-tools: Read, Bash, WebSearch, WebFetch, Grep, Glob
---

# Web Enrichment Agent — Orchestrator

You are the ORCHESTRATOR. You do NOT search the web yourself. You spawn
subagents — each one handles exactly 10 companies with a fresh context.

## How it works

1. Run `prepare --limit 10` to get the next batch
2. Spawn a **subagent** with the batch — it does all the WebSearch calls
3. When the subagent finishes, run `prepare --limit 10` again for the next batch
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
instructions below as the subagent's prompt.

**Subagent prompt — copy this exactly, replacing {{QUEUE_JSON}} with the actual
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

{{QUEUE_JSON}}

## For EACH company, do these steps:

### A. Search the web (MANDATORY — use your web search tool)

Use your web search tool (WebSearch / #web / web_search — whichever you have)
with the `search_query` field as the query.

Exclude these domains from results: {", ".join(CONFIG["blocked_domains"])}

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
{webfetch_prompt}

If you get a 403 or any error — use search snippets from step A instead. Move on.

### C. Fallback search

If no results from step A and trade_name differs from company_name,
do one more web search for: "{{trade_name}} {fallback_suffix}"

### D. Detect channel from web content

Score these keywords against ONLY the text from your search/fetch results:
{channel_block_sub}

Set web_channel_guess to top-scoring. Set web_channel_detail to all scores.

### E. Save IMMEDIATELY after each company

```bash
python retailer_enrich.py save --id {{ID}} --json '{{"website":"...","phone":"...","email":"...","web_description":"...","web_products":"...","web_business_type":"...","web_channel_guess":"...","web_channel_detail":"...","facebook":"...","instagram":"...","linkedin":"...","twitter":"..."}}'
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
""")

    # Output
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(skill_md)
        print(f"Generated: {args.output}")
    else:
        print(skill_md)


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Customizable Web Enrichment Agent — works for any "
                    "country and channels. Edit CONFIG at top of script."
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("prepare", help="Read Excel → output search queue")
    p.add_argument("--input", "-i", default=None)
    p.add_argument("--limit", "-l", type=int, default=None)

    p = sub.add_parser("save", help="Save one company's enrichment result")
    p.add_argument("--id", required=True, help="Company ID (siren, org number, etc.)")
    p.add_argument("--json", required=True, help='JSON: {"website":"...","phone":"...",...}')

    p = sub.add_parser("compile", help="Merge results → enriched Excel")
    p.add_argument("--input", "-i", default=None)
    p.add_argument("--output", "-o", default=None)

    sub.add_parser("status", help="Show enrichment progress")

    p = sub.add_parser("skill", help="Generate SKILL.md from CONFIG")
    p.add_argument("--output", "-o", default=None,
                    help="Write to file instead of stdout")

    args = parser.parse_args()
    cmds = {
        "prepare": cmd_prepare,
        "save": cmd_save,
        "compile": cmd_compile,
        "status": cmd_status,
        "skill": cmd_skill,
    }
    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
