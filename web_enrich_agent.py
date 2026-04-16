#!/usr/bin/env python3
"""
Web Enrichment Agent — Helper for French Retailers
====================================================
This script is a DATA PROCESSOR that works WITH an AI agent's built-in
WebSearch and WebFetch tools.  It does NOT search the web itself.

Architecture:
  1. Agent runs:  python web_enrich_agent.py prepare
     → reads france_retailers-with-keywords.xlsx
     → outputs web_enrich_queue.json  (list of companies to search)

  2. Agent uses its WebSearch / WebFetch tools for each company,
     then saves each result:
       python web_enrich_agent.py save --siren 123456789 --json '{...}'

  3. Agent runs:  python web_enrich_agent.py compile
     → reads saved results + original Excel
     → outputs france_retailers_enriched.xlsx

Works with any agent that has web search: Claude Code, GitHub Copilot, etc.
No DuckDuckGo, no paid APIs — just the agent's own tools.

Quick start (inside an agent session):
    python web_enrich_agent.py prepare --limit 10
    # then the agent searches each company and calls 'save' per result
    python web_enrich_agent.py compile
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

INPUT_FILE = "france_retailers-with-keywords.xlsx"
OUTPUT_FILE = "france_retailers_enriched.xlsx"
QUEUE_FILE = "web_enrich_queue.json"
RESULTS_FILE = "web_enrich_results.json"

# ═══════════════════════════════════════════════════════════════════════
# CHANNEL DETECTION — keywords to look for on a retailer's website
# ═══════════════════════════════════════════════════════════════════════

WEB_CHANNEL_SIGNALS = {
    "Photo": [
        "photo", "photographie", "camera", "appareil photo",
        "objectif", "reflex", "hybride", "optique", "tirage photo",
        "labo photo", "studio photo",
    ],
    "CE": [
        "informatique", "ordinateur", "pc portable", "tablette",
        "multimedia", "audio", "video", "television", "tv",
        "hi-fi", "hifi", "home cinema", "gaming", "console",
    ],
    "MDA": [
        "electromenager", "gros electromenager",
        "lave-linge", "lave linge", "machine a laver",
        "refrigerateur", "frigo", "congelateur",
        "lave-vaisselle", "four", "cuisiniere", "hotte",
    ],
    "SDA": [
        "petit electromenager", "cafetiere", "machine a cafe",
        "aspirateur", "robot cuisine", "mixeur", "blender",
        "bouilloire", "grille-pain", "fer a repasser",
    ],
    "Mobile": [
        "telephone", "mobile", "smartphone", "forfait",
        "operateur", "telecom", "abonnement mobile",
        "iphone", "samsung galaxy",
    ],
    "Phone Accessories": [
        "coque", "accessoire telephone", "accessoire mobile",
        "protection ecran", "verre trempe", "chargeur",
        "ecouteur", "casque audio", "enceinte bluetooth",
    ],
    "Refurb": [
        "reconditionne", "refurbished", "remis a neuf",
        "reparation", "depannage", "seconde main",
        "occasion", "reconditionnement",
    ],
}

# Domains to skip when picking the company's own website
SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "societe.com", "verif.com", "pappers.fr", "infogreffe.fr",
    "pagesjaunes.fr", "google.com", "google.fr", "bing.com",
    "wikipedia.org", "annuaire-entreprises.data.gouv.fr",
    "indeed.fr", "glassdoor.fr",
}


# ═══════════════════════════════════════════════════════════════════════
# HELPERS — used by the agent to parse fetched web content
# ═══════════════════════════════════════════════════════════════════════

def detect_channel(text):
    """Score each channel based on keywords in the text.
    Returns (best_channel, detail_string)."""
    text_lower = text.lower()
    scores = {}
    for channel, keywords in WEB_CHANNEL_SIGNALS.items():
        hits = sum(1 for kw in keywords if kw in text_lower)
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
    from urllib.parse import urlparse
    for url in urls:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        return url
    return ""


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: prepare — read Excel, output queue of companies to search
# ═══════════════════════════════════════════════════════════════════════

def cmd_prepare(args):
    """Read the Excel and produce a JSON queue of companies to search."""
    input_file = args.input or INPUT_FILE
    limit = args.limit

    try:
        df = pd.read_excel(input_file, sheet_name="All Retailers")
    except Exception as exc:
        print(f"ERROR: {exc}")
        return

    print(f"Loaded {len(df)} rows from '{input_file}' → 'All Retailers'")

    # Group by siren — search once per company
    companies = (
        df.groupby("siren")
        .agg({
            "legal_name": "first",
            "trade_name": "first",
            "city": "first",
            "channel": "first",
        })
        .reset_index()
    )

    # Load existing results to skip already-searched companies
    existing = set()
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                for siren in json.load(f):
                    existing.add(siren)
        except Exception:
            pass

    queue = []
    for _, row in companies.iterrows():
        siren = str(row["siren"])
        if siren in existing:
            continue
        name = row["legal_name"] or row["trade_name"] or ""
        if not name:
            continue
        queue.append({
            "siren": siren,
            "legal_name": name,
            "trade_name": row["trade_name"] or "",
            "city": row["city"] or "",
            "channel": row["channel"] or "",
            "search_query": f"{name} {row['city'] or ''} france".strip(),
        })

    if limit:
        queue = queue[:limit]

    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    print(f"Queue: {len(queue)} companies to search ({len(existing)} already done)")
    print(f"Saved to {QUEUE_FILE}")
    print()
    print("Next: the agent should read this file, and for each company:")
    print("  1. WebSearch for the search_query")
    print("  2. WebFetch the company's website for phone/email/description")
    print("  3. Save result: python web_enrich_agent.py save --siren <X> --json '<data>'")
    print("  4. When done: python web_enrich_agent.py compile")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: save — save one company's enrichment result
# ═══════════════════════════════════════════════════════════════════════

def cmd_save(args):
    """Save a single company's web enrichment result to the results file."""
    siren = args.siren
    data = json.loads(args.json)

    # Load existing results
    results = {}
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                results = json.load(f)
        except Exception:
            results = {}

    results[siren] = data

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    name = data.get("legal_name", "")
    website = data.get("website", "")
    phone = data.get("phone", "")
    print(f"Saved: {siren} ({name}) — website={website}, phone={phone}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: compile — merge results into enriched Excel
# ═══════════════════════════════════════════════════════════════════════

def cmd_compile(args):
    """Merge web enrichment results into the retailer Excel."""
    input_file = args.input or INPUT_FILE
    output_file = args.output or OUTPUT_FILE

    # Load results
    if not os.path.exists(RESULTS_FILE):
        print(f"ERROR: No results file ({RESULTS_FILE}). Run searches first.")
        return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results = json.load(f)
    print(f"Loaded {len(results)} enrichment results")

    # Load Excel
    try:
        df = pd.read_excel(input_file, sheet_name="All Retailers")
    except Exception as exc:
        print(f"ERROR reading {input_file}: {exc}")
        return
    print(f"Loaded {len(df)} rows from '{input_file}'")

    # Map results to rows (by siren)
    enrich_cols = [
        "website", "phone", "email",
        "web_description", "web_products", "web_business_type",
        "web_channel_guess", "web_channel_detail",
        "facebook", "instagram", "linkedin", "twitter",
    ]
    for col in enrich_cols:
        df[col] = df["siren"].apply(
            lambda s: results.get(str(s), {}).get(col, "")
        )

    # Channel match check
    def _channel_match(row):
        guess = (row.get("web_channel_guess") or "").strip()
        assigned = (row.get("channel") or "")
        if not guess:
            return ""
        if guess in assigned:
            return "Match"
        return f"Web suggests: {guess}"

    df["web_vs_assigned"] = df.apply(_channel_match, axis=1)

    # Write enriched Excel
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        # Per-channel sheets
        all_channels = set()
        for ch_str in df["channel"].dropna().unique():
            for ch in ch_str.split(" | "):
                ch = ch.strip()
                if ch:
                    all_channels.add(ch)
        for ch in sorted(all_channels):
            mask = df["channel"].str.contains(ch, regex=False)
            ch_df = df[mask].copy()
            if not ch_df.empty:
                sheet = ch.replace("/", "-")[:31]
                ch_df.to_excel(writer, index=False, sheet_name=sheet)

        # All Retailers
        df.to_excel(writer, index=False, sheet_name="All Retailers")

        # Enrichment log
        log_rows = []
        for siren, info in results.items():
            log_rows.append({"siren": siren, **info})
        if log_rows:
            pd.DataFrame(log_rows).to_excel(
                writer, index=False, sheet_name="Web Enrichment Log"
            )

    # Stats
    has_website = df["website"].astype(bool).sum()
    has_phone = df["phone"].astype(bool).sum()
    has_email = df["email"].astype(bool).sum()
    has_guess = df["web_channel_guess"].astype(bool).sum()
    matches = (df["web_vs_assigned"] == "Match").sum()

    print(f"\nSaved: {output_file}")
    print(f"  Rows:             {len(df)}")
    print(f"  Website found:    {has_website}")
    print(f"  Phone found:      {has_phone}")
    print(f"  Email found:      {has_email}")
    print(f"  Channel detected: {has_guess}")
    print(f"  Channel matches:  {matches}")


# ═══════════════════════════════════════════════════════════════════════
# COMMAND: status — show progress
# ═══════════════════════════════════════════════════════════════════════

def cmd_status(args):
    """Show how many companies have been searched vs remaining."""
    done = 0
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            done = len(json.load(f))

    remaining = 0
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            remaining = len(json.load(f))

    total = done + remaining
    pct = (done / total * 100) if total else 0
    print(f"Progress: {done}/{total} companies enriched ({pct:.0f}%)")
    print(f"  Done:      {done}")
    print(f"  Remaining: {remaining}")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Web Enrichment Agent — helper for AI-agent-driven "
                    "web search enrichment of French retailers."
    )
    sub = parser.add_subparsers(dest="command")

    # prepare
    p_prep = sub.add_parser("prepare", help="Read Excel → output search queue JSON")
    p_prep.add_argument("--input", "-i", default=None)
    p_prep.add_argument("--limit", "-l", type=int, default=None,
                        help="Only queue first N companies")

    # save
    p_save = sub.add_parser("save", help="Save one company's enrichment result")
    p_save.add_argument("--siren", required=True)
    p_save.add_argument("--json", required=True,
                        help='JSON string: {"website":"...","phone":"...",...}')

    # compile
    p_comp = sub.add_parser("compile", help="Merge results → enriched Excel")
    p_comp.add_argument("--input", "-i", default=None)
    p_comp.add_argument("--output", "-o", default=None)

    # status
    sub.add_parser("status", help="Show enrichment progress")

    args = parser.parse_args()
    if args.command == "prepare":
        cmd_prepare(args)
    elif args.command == "save":
        cmd_save(args)
    elif args.command == "compile":
        cmd_compile(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
