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

    skill_md = textwrap.dedent(f"""\
---
name: {skill_name}
description: >-
  Enrich {country} retailers from {input_file} with web data.
  Automatically loops through ALL companies — WebSearch for each one,
  WebFetch their website, extract phone/email/products/description/business type.
  No manual intervention needed.
user-invocable: true
argument-hint: "[--limit N]"
allowed-tools: Read, Bash, WebSearch, WebFetch, Grep, Glob
---

# Web Enrichment Agent — {country} Retailers

You are an AUTOMATED web research agent. When invoked, AUTOMATICALLY loop
through every company in the queue — do NOT stop, do NOT ask for confirmation.

## MANDATORY: Real web search only — NEVER use training data

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

## Execution — BATCH MODE (10 companies per cycle)

**WHY BATCHES:** Processing hundreds of companies in one go causes context
overflow, leading to fabricated results. Process exactly 10 at a time, then
re-prepare to get the next 10. The `prepare` command auto-skips companies
already in the results file, so each cycle picks up where the last left off.

### BATCH LOOP — repeat until done:

#### Step 1 — Prepare a batch of 10

```bash
python retailer_enrich.py prepare --limit 10
```

Reads `{input_file}`, skips already-done companies, outputs next 10 to queue.

**If the queue is empty (0 companies to search), go to Step 4 — you are done.**

#### Step 2 — Read the queue

```bash
cat web_enrich_queue.json
```

Each entry has: `id`, `company_name`, `trade_name`, `city`, `channel`, `search_query`.

#### Step 3 — Search each company in this batch

Process all 10 (or fewer) companies. Run 3-5 parallel WebSearch calls.

For each company:

### 3a. WebSearch (MANDATORY — you MUST call this tool)

Call **WebSearch** with query: the `search_query` field from the queue.

Set `blocked_domains`: {blocked}

**From the WebSearch result snippets ONLY**, extract:
- **website**: company's own URL (skip social media, directories)
- **phone**: customer service number if mentioned in snippets
- **web_description**: what the company does (1-2 sentences from snippets)
- **web_products**: product categories sold (comma-separated, from snippets)
- **web_business_type**: "Chain (N stores)" / "Independent" / "Buying group"

**If a field is not visible in the search results, set it to `""`.**

### 3b. WebFetch (optional — only if WebSearch found a website)

If a website URL was found in step 3a, try **WebFetch** with that URL and prompt:
"{webfetch_prompt}"

If 403 — normal, use search results instead. Move on.

### 3c. Fallback search

If no results and `trade_name` differs from `company_name`:
WebSearch for: "{{trade_name}} {fallback_suffix}"

### 3d. Detect channel from web content

Score keywords against **only the text from WebSearch/WebFetch results**:
{channel_block}

Set `web_channel_guess` to top-scoring channel.
Set `web_channel_detail` to all scores, e.g. "CE (5), MDA (3)".

### 3e. Save immediately

```bash
python retailer_enrich.py save --id {{ID}} --json '{{"website":"...","phone":"...","email":"...","web_description":"...","web_products":"...","web_business_type":"...","web_channel_guess":"...","web_channel_detail":"...","facebook":"...","instagram":"...","linkedin":"...","twitter":"..."}}'
```

Use `""` for any field not found. **Never invent data.**
Print `[N/TOTAL] Company Name — done` after each save. Continue immediately.

#### Step 3f — After finishing this batch, loop back

```bash
python retailer_enrich.py status
```

Print progress. Then **GO BACK TO STEP 1** — run `prepare --limit 10` again.
It auto-skips done companies and gives the next 10.

**Keep looping until `prepare` says "0 companies to search".**

#### Step 4 — Compile (only when ALL batches are done)

```bash
python retailer_enrich.py compile
```

#### Step 5 — Report summary

Show: total searched, websites found, phones found, emails found, channel matches.

## Rules

1. **AUTOMATIC**: Process all batches without stopping. Never ask "should I continue?"
2. **REAL SEARCH ONLY**: You MUST call WebSearch for every company. NEVER use training knowledge.
3. **BATCH OF 10**: Always `prepare --limit 10`. Never load more than 10 at once.
4. **PARALLEL**: 3-5 WebSearch calls in parallel within each batch.
5. **LOOP**: After each batch, run `prepare --limit 10` again for the next batch.
6. **NEVER INVENT**: If WebSearch returned nothing, save empty strings. Do NOT guess.
7. **SAVE OFTEN**: After each company.
8. **403 IS OK**: Use search snippets instead.
9. **SOURCE**: Every data point must come from a WebSearch snippet or WebFetch page.
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
