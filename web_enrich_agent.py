#!/usr/bin/env python3
"""
Web Enrichment Agent for French Retailers
==========================================
Reads france_retailers.xlsx, searches the web for each unique company,
and enriches with: website, phone, email, social media links, a short
description, and a web-based channel verification.

No paid API needed — uses DuckDuckGo (free) + basic web scraping.

Usage:
    pip install duckduckgo-search beautifulsoup4 requests pandas openpyxl lxml
    python web_enrich_agent.py
    python web_enrich_agent.py --limit 10          # test: first 10 companies
    python web_enrich_agent.py --input my_file.xlsx --output enriched.xlsx

Designed to run standalone, via GitHub Actions, or as a Copilot agent task.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
except ImportError:
    print("ERROR: duckduckgo-search not installed.")
    print("  pip install duckduckgo-search")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

SEARCH_DELAY = 3          # seconds between DuckDuckGo searches
SCRAPE_TIMEOUT = 12       # seconds per page fetch
MAX_SEARCH_RESULTS = 5    # DuckDuckGo results per query
CACHE_FILE = "web_enrich_cache.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Domains to skip when looking for a company's own website
SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "societe.com", "verif.com", "pappers.fr", "infogreffe.fr",
    "pagesjaunes.fr", "google.com", "google.fr", "bing.com",
    "wikipedia.org", "wikidata.org",
    "sirene.fr", "annuaire-entreprises.data.gouv.fr",
    "indeed.fr", "glassdoor.fr", "indeed.com",
}

# Social media patterns — we DO want to capture these separately
SOCIAL_PATTERNS = {
    "facebook":  re.compile(r"https?://(?:www\.)?facebook\.com/[^\s\"'<>]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>]+", re.I),
    "linkedin":  re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[^\s\"'<>]+", re.I),
    "twitter":   re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s\"'<>]+", re.I),
}

# ═══════════════════════════════════════════════════════════════════════
# CHANNEL DETECTION — keywords found on a retailer's own website
# ═══════════════════════════════════════════════════════════════════════

WEB_CHANNEL_SIGNALS = {
    "Photo": [
        "photo", "photographie", "photographe", "camera", "appareil photo",
        "objectif", "reflex", "hybride", "optique", "tirage photo",
        "labo photo", "studio photo", "impression photo",
    ],
    "CE": [
        "informatique", "ordinateur", "pc portable", "tablette",
        "multimedia", "audio", "video", "television", "tv ", "hi-fi",
        "hifi", "son ", "home cinema", "gaming", "console", "jeux video",
    ],
    "MDA": [
        "electromenager", "electro-menager", "gros electromenager",
        "lave-linge", "lave linge", "machine a laver",
        "refrigerateur", "frigo", "congelateur",
        "lave-vaisselle", "four", "cuisiniere", "plaque de cuisson",
        "hotte", "seche-linge",
    ],
    "SDA": [
        "petit electromenager", "petit electro-menager",
        "cafetiere", "machine a cafe", "expresso", "nespresso",
        "aspirateur", "robot cuisine", "robot patissier",
        "mixeur", "blender", "bouilloire", "grille-pain",
        "fer a repasser", "centrale vapeur",
    ],
    "Mobile": [
        "telephone", "mobile", "smartphone", "forfait",
        "operateur", "telecom", "abonnement mobile",
        "iphone", "samsung galaxy", "portable",
    ],
    "Phone Accessories": [
        "coque", "accessoire telephone", "accessoire mobile",
        "protection ecran", "verre trempe", "chargeur",
        "cable usb", "ecouteur", "casque audio", "enceinte bluetooth",
        "batterie externe", "powerbank",
    ],
    "Refurb": [
        "reconditionne", "reconditionn", "refurbished", "remis a neuf",
        "reparation", "depannage", "seconde main", "second hand",
        "occasion", "reprise", "rachat", "reconditionnement",
    ],
}

# ═══════════════════════════════════════════════════════════════════════
# PHONE / EMAIL EXTRACTION PATTERNS (French)
# ═══════════════════════════════════════════════════════════════════════

# French phone: +33, 0X XX XX XX XX with optional separators
PHONE_RE = re.compile(
    r"(?:\+33\s*\(?\d\)?|0\s*[1-9])"   # +33 X or 0X
    r"(?:[\s.\-/]*\d{2}){4}",           # then 4 groups of 2 digits
)

# International-looking phone (catch-all)
PHONE_INTL_RE = re.compile(r"\+\d[\d\s.\-]{8,15}\d")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# ═══════════════════════════════════════════════════════════════════════
# CACHE — avoid re-searching companies across runs
# ═══════════════════════════════════════════════════════════════════════

_cache = {}


def _load_cache():
    global _cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            print(f"  Loaded {len(_cache)} cached results from {CACHE_FILE}")
        except Exception:
            _cache = {}


def _save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"  [WARN] Could not save cache: {exc}")


# ═══════════════════════════════════════════════════════════════════════
# WEB SEARCH
# ═══════════════════════════════════════════════════════════════════════

def search_business(name, city=""):
    """Search DuckDuckGo for a French business. Returns list of result dicts."""
    query = f"{name} {city} france".strip()
    try:
        results = DDGS().text(query, region="fr-fr", max_results=MAX_SEARCH_RESULTS)
        return results or []
    except Exception as exc:
        print(f"    [WARN] Search failed for '{name}': {exc}")
        return []


def _pick_website(search_results):
    """From search results, pick the most likely company website URL."""
    for r in search_results:
        url = r.get("href") or r.get("link") or ""
        if not url:
            continue
        domain = urlparse(url).netloc.lower().lstrip("www.")
        # Skip known directories / social / aggregators
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        return url
    return ""


def _pick_pagesjaunes(search_results):
    """Check if any result is a Pages Jaunes listing (often has phone)."""
    for r in search_results:
        url = r.get("href") or r.get("link") or ""
        if "pagesjaunes.fr" in url:
            return url
    return ""


# ═══════════════════════════════════════════════════════════════════════
# WEB SCRAPING
# ═══════════════════════════════════════════════════════════════════════

def _fetch_page(url):
    """Fetch a page and return (soup, raw_text). Returns (None, '') on failure."""
    try:
        resp = requests.get(
            url,
            timeout=SCRAPE_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        resp.raise_for_status()
        # Only parse HTML
        ct = resp.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return None, ""
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)
        return soup, text
    except Exception:
        return None, ""


def extract_contact_info(url):
    """Scrape a web page for phone, email, description, and social links."""
    result = {
        "phone": "",
        "email": "",
        "description": "",
        "facebook": "",
        "instagram": "",
        "linkedin": "",
        "twitter": "",
    }
    soup, text = _fetch_page(url)
    if not soup:
        return result, ""

    full_html = str(soup)

    # Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        result["description"] = meta["content"][:300].strip()
    if not result["description"]:
        og = soup.find("meta", attrs={"property": "og:description"})
        if og and og.get("content"):
            result["description"] = og["content"][:300].strip()

    # Phone — search text first, then HTML attributes
    phones = PHONE_RE.findall(text)
    if not phones:
        phones = PHONE_INTL_RE.findall(text)
    if not phones:
        # Check tel: links
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("tel:"):
                phones.append(a["href"][4:].strip())
    if phones:
        # Clean up the first phone found
        phone = re.sub(r"[^\d+]", "", phones[0])
        result["phone"] = phone

    # Email — search text, then mailto links
    emails = EMAIL_RE.findall(text)
    if not emails:
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("mailto:"):
                emails.append(a["href"][7:].split("?")[0].strip())
    # Filter out generic/noreply
    filtered = [e for e in emails if not re.match(r"(noreply|no-reply|info@example)", e, re.I)]
    if filtered:
        result["email"] = filtered[0]
    elif emails:
        result["email"] = emails[0]

    # Social media links
    for platform, pattern in SOCIAL_PATTERNS.items():
        matches = pattern.findall(full_html)
        if matches:
            result[platform] = matches[0].rstrip('/"')

    return result, text[:5000]


def extract_from_pagesjaunes(url):
    """Try to get phone from a Pages Jaunes listing."""
    soup, text = _fetch_page(url)
    if not soup:
        return ""
    phones = PHONE_RE.findall(text)
    return re.sub(r"[^\d+]", "", phones[0]) if phones else ""


# ═══════════════════════════════════════════════════════════════════════
# CHANNEL DETECTION FROM WEB CONTENT
# ═══════════════════════════════════════════════════════════════════════

def detect_channels_from_web(page_text, description=""):
    """Score each channel based on keywords found in the page text.

    Returns a dict like {"CE": 5, "Photo": 2} — only channels with hits.
    Also returns a single best-guess string and a combined label.
    """
    combined = f"{description} {page_text}".lower()
    scores = {}
    for channel, keywords in WEB_CHANNEL_SIGNALS.items():
        hits = sum(1 for kw in keywords if kw in combined)
        if hits:
            scores[channel] = hits

    if not scores:
        return {}, "", ""

    best = max(scores, key=scores.get)
    # Build label: "CE (5 hits), Photo (2 hits)"
    parts = sorted(scores.items(), key=lambda x: -x[1])
    label = ", ".join(f"{ch} ({n})" for ch, n in parts)
    return scores, best, label


# ═══════════════════════════════════════════════════════════════════════
# ENRICHMENT — one company at a time
# ═══════════════════════════════════════════════════════════════════════

def enrich_one(name, city=""):
    """Search the web for one business and return enrichment dict."""
    cache_key = f"{name}||{city}".upper()
    if cache_key in _cache:
        return _cache[cache_key]

    result = {
        "website": "",
        "phone": "",
        "email": "",
        "web_description": "",
        "web_channel_guess": "",
        "web_channel_detail": "",
        "facebook": "",
        "instagram": "",
        "linkedin": "",
        "twitter": "",
    }

    # 1. Search DuckDuckGo
    time.sleep(SEARCH_DELAY)
    search_results = search_business(name, city)
    if not search_results:
        _cache[cache_key] = result
        return result

    # 2. Collect snippets from search results for channel detection
    snippets = " ".join(r.get("body", "") for r in search_results)

    # 3. Find the company's own website
    website = _pick_website(search_results)
    result["website"] = website

    # 4. Scrape the website for contact info
    page_text = ""
    if website:
        contact, page_text = extract_contact_info(website)
        result["phone"] = contact["phone"]
        result["email"] = contact["email"]
        result["web_description"] = contact["description"]
        result["facebook"] = contact["facebook"]
        result["instagram"] = contact["instagram"]
        result["linkedin"] = contact["linkedin"]
        result["twitter"] = contact["twitter"]

    # 5. If no phone yet, try Pages Jaunes
    if not result["phone"]:
        pj_url = _pick_pagesjaunes(search_results)
        if pj_url:
            time.sleep(SEARCH_DELAY)
            result["phone"] = extract_from_pagesjaunes(pj_url)

    # 6. Channel detection from web content
    all_text = f"{snippets} {page_text}"
    _, best_ch, detail = detect_channels_from_web(all_text, result["web_description"])
    result["web_channel_guess"] = best_ch
    result["web_channel_detail"] = detail

    _cache[cache_key] = result
    _save_cache()
    return result


# ═══════════════════════════════════════════════════════════════════════
# BATCH PROCESSING
# ═══════════════════════════════════════════════════════════════════════

def run(input_file, output_file, limit=None):
    """Read the retailer Excel, enrich each company, save enriched output."""
    print(f"\n{'='*60}")
    print("  Web Enrichment Agent — French Retailers")
    print(f"{'='*60}")
    print(f"  Input:  {input_file}")
    print(f"  Output: {output_file}")
    print(f"  Search: DuckDuckGo (free, no API key)")
    if limit:
        print(f"  Limit:  {limit} companies (test mode)")
    print(f"{'='*60}\n")

    _load_cache()

    # Read the All Retailers sheet
    try:
        df = pd.read_excel(input_file, sheet_name="All Retailers")
    except Exception as exc:
        print(f"ERROR reading {input_file}: {exc}")
        print("  Make sure france_retailers.py has been run first.")
        return

    print(f"  Loaded {len(df)} rows from 'All Retailers' sheet")

    # Group by SIREN — search once per company, not per establishment
    if "siren" not in df.columns or "legal_name" not in df.columns:
        print("ERROR: Expected 'siren' and 'legal_name' columns.")
        return

    unique_companies = (
        df.groupby("siren")
        .agg({
            "legal_name": "first",
            "city": "first",
            "trade_name": "first",
        })
        .reset_index()
    )
    print(f"  {len(unique_companies)} unique companies (by SIREN)")

    if limit:
        unique_companies = unique_companies.head(limit)
        print(f"  Limited to first {limit}")

    # Enrich each company
    enrichment_map = {}  # siren -> enrichment dict
    total = len(unique_companies)
    already_cached = 0

    for idx, row in unique_companies.iterrows():
        siren = row["siren"]
        name = row["legal_name"] or row["trade_name"] or ""
        city = row["city"] or ""

        if not name:
            continue

        cache_key = f"{name}||{city}".upper()
        is_cached = cache_key in _cache
        if is_cached:
            already_cached += 1

        pos = idx + 1
        status = "cached" if is_cached else "searching"
        print(f"  [{pos}/{total}] {name[:40]:<40} ({city[:20]})  [{status}]")

        enrichment = enrich_one(name, city)
        enrichment_map[siren] = enrichment

        # Also try trade_name if legal_name gave no website
        if not enrichment.get("website") and row["trade_name"] and row["trade_name"] != name:
            trade = row["trade_name"]
            print(f"         -> retrying with trade name: {trade[:40]}")
            alt = enrich_one(trade, city)
            # Merge: prefer non-empty values from the alt search
            for k, v in alt.items():
                if v and not enrichment.get(k):
                    enrichment[k] = v
            enrichment_map[siren] = enrichment

    print(f"\n  Done: {total} companies searched ({already_cached} from cache)")
    _save_cache()

    # Map enrichment back to the full DataFrame (by siren)
    enrich_cols = [
        "website", "phone", "email", "web_description",
        "web_channel_guess", "web_channel_detail",
        "facebook", "instagram", "linkedin", "twitter",
    ]
    for col in enrich_cols:
        df[col] = df["siren"].apply(
            lambda s: enrichment_map.get(s, {}).get(col, "")
        )

    # Channel match flag: does web_channel_guess match the assigned channel?
    def _channel_match(row):
        guess = (row.get("web_channel_guess") or "").strip()
        assigned = (row.get("channel") or "")
        if not guess:
            return ""
        if guess in assigned:
            return "Match"
        return f"Web suggests: {guess}"

    df["web_vs_assigned"] = df.apply(_channel_match, axis=1)

    # ─── Write enriched Excel ──────────────────────────────────────
    print(f"\n  Writing {output_file} …")
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        # Per-channel sheets
        channels = sorted(df["channel"].str.split(" | ").explode().unique())
        for ch in channels:
            if not ch:
                continue
            mask = df["channel"].str.contains(ch, regex=False)
            ch_df = df[mask].copy()
            if not ch_df.empty:
                sheet = ch.replace("/", "-")[:31]
                ch_df.to_excel(writer, index=False, sheet_name=sheet)

        # All Retailers (enriched)
        df.to_excel(writer, index=False, sheet_name="All Retailers")

        # Enrichment summary
        summary_rows = []
        for siren, info in enrichment_map.items():
            name_row = unique_companies[unique_companies["siren"] == siren]
            name = name_row["legal_name"].values[0] if len(name_row) else ""
            summary_rows.append({
                "siren": siren,
                "legal_name": name,
                **info,
            })
        if summary_rows:
            pd.DataFrame(summary_rows).to_excel(
                writer, index=False, sheet_name="Web Enrichment Log"
            )

    print(f"  Saved: {output_file}")
    print(f"  Sheets: {', '.join(channels)}, All Retailers, Web Enrichment Log")

    # Stats
    has_website = df["website"].astype(bool).sum()
    has_phone = df["phone"].astype(bool).sum()
    has_email = df["email"].astype(bool).sum()
    has_guess = df["web_channel_guess"].astype(bool).sum()
    matches = (df["web_vs_assigned"] == "Match").sum()

    print(f"\n  {'='*50}")
    print(f"  Enrichment Results")
    print(f"  {'='*50}")
    print(f"  Rows:              {len(df)}")
    print(f"  Website found:     {has_website}")
    print(f"  Phone found:       {has_phone}")
    print(f"  Email found:       {has_email}")
    print(f"  Channel detected:  {has_guess}")
    print(f"  Channel matches:   {matches}")
    print(f"  {'='*50}\n")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Web Enrichment Agent — enrich French retailer data with "
                    "web search results (website, phone, email, channel verification)."
    )
    parser.add_argument(
        "--input", "-i",
        default="france_retailers.xlsx",
        help="Input Excel file (default: france_retailers.xlsx)",
    )
    parser.add_argument(
        "--output", "-o",
        default="france_retailers_enriched.xlsx",
        help="Output Excel file (default: france_retailers_enriched.xlsx)",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int, default=None,
        help="Only process first N companies (test mode)",
    )
    args = parser.parse_args()
    run(args.input, args.output, args.limit)


if __name__ == "__main__":
    main()
