#!/usr/bin/env python3
"""
Query the French INSEE SIRENE API to find retailers across multiple channels,
classify them (Chain / Buying Group / Independent), and enrich with turnover
data from INPI / Pappers annual accounts + BODACC filing status.

Set BEARER_TOKEN below and run: python france_retailers.py

CONFIDENTIAL — INTERNAL USE ONLY
This script and its output contain proprietary market intelligence data.
Do not distribute, share, or publish results without explicit authorization.
All data sourced from public registries (INSEE, INPI, BODACC) but the
filtering logic, channel mappings, and classification methodology are
proprietary.
"""

import time
from collections import Counter
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIDENTIALITY
# ---------------------------------------------------------------------------

CONFIDENTIALITY_NOTICE = (
    "CONFIDENTIAL — INTERNAL USE ONLY. "
    "This file contains proprietary market intelligence. "
    "Do not distribute without authorization."
)

# ---------------------------------------------------------------------------
# CONFIGURATION — set your tokens / keys here
# ---------------------------------------------------------------------------

BEARER_TOKEN = ""      # INSEE SIRENE API token (required)
PAPPERS_API_KEY = ""   # Pappers API key (optional — for turnover enrichment)
INPI_USERNAME = ""     # INPI data.inpi.fr username (optional — alternative)
INPI_PASSWORD = ""     # INPI data.inpi.fr password (optional — alternative)

APE_CODES = {
    "47.78C": "Photo",
    "47.43Z": "CE",
    "47.41Z": "CE",
    "47.54Z": "MDA/SDA",
    "47.42Z": "Mobile",
    "47.59B": "Accessories",
    "95.11Z": "Refurb",
    "95.12Z": "Refurb",
}

KEYWORDS = [
    "TELEPHON", "MOBILE", "PHOTO", "MULTIMEDIA", "ELECTROMENAGER",
    "RECONDITIONN", "NUMERIQUE", "ACCESSOIRE", "HI-FI", "SMARTPHONE",
    "DARTY", "FNAC", "BOULANGER", "ELECTRODEPOT", "BUT",
]

SIZE_MIN = "11"  # trancheEffectifs code — means 10+ employees

FIELDS = [
    "siret",
    "denominationUniteLegale",
    "adresseEtablissement",
    "codePostalEtablissement",
    "libelleCommuneEtablissement",
    "activitePrincipaleEtablissement",
    "trancheEffectifsEtablissement",
    "etatAdministratifEtablissement",
]

BASE_URL = "https://api.insee.fr/entreprises/sirene/V3/siret"
PAGE_SIZE = 1000

# Ordered size bands for comparison
SIZE_BANDS = [
    "NN", "00", "01", "02", "03",
    "11", "12", "21", "22", "31", "32",
    "41", "42", "51", "52", "53",
]

# ---------------------------------------------------------------------------
# TURNOVER ENRICHMENT CONFIGURATION
# ---------------------------------------------------------------------------
#
# Three sources, tried in priority order:
#
# SOURCE 1 — INPI API (free, requires registration at data.inpi.fr)
#   Provides annual accounts directly from the RNE (Registre National des
#   Entreprises).  Updated daily.  Data available from 2017 to present
#   (currently up to fiscal year 2024/2025).  Requires INPI_USERNAME and
#   INPI_PASSWORD.  Quota: 10,000 requests/day.
#
# SOURCE 2 — Pappers API (100 free requests/month, then paid)
#   Aggregates INPI data into a clean REST API.  Returns chiffre_d_affaires
#   directly.  Requires PAPPERS_API_KEY.
#   Docs: https://www.pappers.fr/api/documentation
#
# SOURCE 3 — Employee band estimate (always available, no auth needed)
#   Maps SIRENE trancheEffectifs to rough CE-sector turnover ranges.
#   Used as fallback when neither INPI nor Pappers returns a figure.
#
# Set SKIP_TURNOVER_API = True to skip all API-based enrichment and use
# only the employee band estimate.

SKIP_TURNOVER_API = False

# Rough CE-sector turnover ranges by employee band
TURNOVER_ESTIMATE_BY_BAND = {
    "11": "€1M – €5M",
    "12": "€3M – €15M",
    "21": "€10M – €40M",
    "22": "€25M – €100M",
    "31": "€50M – €150M",
    "32": "€80M – €200M",
    "41": "€100M – €500M",
    "42": "€200M – €1B",
    "51": "€500M – €2B",
    "52": "€1B – €5B",
    "53": "€5B+",
}

# ---------------------------------------------------------------------------
# BODACC — filing activity check
# ---------------------------------------------------------------------------
# BODACC is the official gazette for commercial announcements.  Free, no auth.
# We query it per SIREN to check for recent "depot des comptes" filings.
# This is a health/activity signal, not a turnover number.

SKIP_BODACC = False
BODACC_URL = "https://bodacc-datadila.opendatasoft.com/api/records/1.0/search/"
BODACC_MAX_LOOKUPS = 300  # cap to keep runtime reasonable

# ---------------------------------------------------------------------------
# RETAILER TYPE CLASSIFICATION — known patterns
# ---------------------------------------------------------------------------

KNOWN_CHAINS = [
    # CE / MDA / SDA national chains
    "FNAC", "DARTY", "BOULANGER", "ELECTRO DEPOT", "ELECTRODEPOT",
    "BUT ", "BUT-", "CONFORAMA", "LDLC", "MATERIEL.NET", "MATERIEL NET",
    "CULTURA", "LICK", "HUBSIDE",
    # Telecom operator retail
    "ORANGE", "SFR", "BOUYGUES TELECOM", "FREE MOBILE", "FREE SAS",
    # Mobile / tech chains
    "MICROMANIA", "SAMSUNG ELECTRONICS", "APPLE RETAIL", "APPLE FRANCE",
    "XIAOMI", "HUAWEI",
    # Photo chains
    "PHOX", "CAMARA",
    # Refurb / second-hand chains
    "CASH CONVERTERS", "CASH CONVERTER", "EASY CASH", "HAPPY CASH",
    "CASH EXPRESS", "BACKMARKET", "BACK MARKET", "RECOMMERCE",
    "SMAAART", "CERTIDEAL", "REMADE", "REBORN",
    # Mobile repair / accessory franchise chains
    "POINT SERVICE MOBILES", "PSM ", "WEFIX", "WE FIX", "SAVE ",
    "IRIPARO", "MOBILAX",
    # Hypermarket groups (when they show up under CE APE codes)
    "AUCHAN", "LECLERC", "CARREFOUR", "CORA ", "CASINO",
    "INTERMARCHE", "HYPER U", "SUPER U", "SYSTEME U",
    "LEROY MERLIN", "IKEA", "ACTION",
    # International
    "MEDIAMARKT", "MEDIA MARKT", "CURRYS", "COOLBLUE",
]

KNOWN_BUYING_GROUPS = [
    "EXPERT", "EURONICS", "GITEM", "PRO & CIE", "PRO ET CIE",
    "PULSAT", "CONNEXION", "ELECTROPLANET", "ELECTRO PLANET",
    "MENAGER PLUS", "DIGITAL SHOPPING",
    "GROUPEMENT", "CENTRALE D'ACHAT", "CENTRALE D ACHAT",
    "COOPERATIVE D'ACHAT", "COOPERATIVE D ACHAT",
    "GIE ", "SYNDICAT",
]

CHAIN_SIREN_THRESHOLD = 3


# ---------------------------------------------------------------------------
# HELPERS — SIRENE
# ---------------------------------------------------------------------------


def size_band_gte(value, minimum):
    """Return True if *value* is at or above *minimum* in the SIZE_BANDS order."""
    try:
        return SIZE_BANDS.index(value) >= SIZE_BANDS.index(minimum)
    except ValueError:
        return False


def matches_keyword(name):
    """Return True if *name* contains at least one KEYWORD (case-insensitive)."""
    if not name:
        return False
    upper = name.upper()
    return any(kw in upper for kw in KEYWORDS)


def build_address(rec):
    """Concatenate address sub-fields into a single string."""
    addr = rec.get("adresseEtablissement") or {}
    if isinstance(addr, str):
        return addr
    parts = [
        addr.get("numeroVoieEtablissement", ""),
        addr.get("typeVoieEtablissement", ""),
        addr.get("libelleVoieEtablissement", ""),
    ]
    return " ".join(p for p in parts if p).strip()


def classify_retailer_type(name_upper, siren, siren_counts):
    """
    Classify a retailer as Chain, Buying Group, or Independent.

    Priority:
    1. Name matches a known CHAIN pattern         -> "Chain"
    2. Name matches a known BUYING GROUP pattern   -> "Buying Group"
    3. SIREN has >= CHAIN_SIREN_THRESHOLD sites    -> "Chain"
    4. Otherwise                                   -> "Independent"
    """
    for pattern in KNOWN_CHAINS:
        if pattern in name_upper:
            return "Chain"
    for pattern in KNOWN_BUYING_GROUPS:
        if pattern in name_upper:
            return "Buying Group"
    if siren and siren_counts.get(siren, 0) >= CHAIN_SIREN_THRESHOLD:
        return "Chain"
    return "Independent"


def fetch_all_for_code(ape_code):
    """Paginate through SIRENE API for a single APE code. Returns (records, total)."""
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}", "Accept": "application/json"}
    params_base = {
        "q": (
            f"activitePrincipaleEtablissement:{ape_code} "
            f"AND etatAdministratifEtablissement:A"
        ),
        "nombre": PAGE_SIZE,
        "champs": ",".join(FIELDS),
    }

    records = []
    debut = 0
    total = None

    while True:
        params = {**params_base, "debut": debut}
        time.sleep(1)
        resp = requests.get(BASE_URL, headers=headers, params=params, timeout=30)

        if resp.status_code != 200:
            print(f"  [ERROR] APE {ape_code} — HTTP {resp.status_code}: {resp.text[:200]}")
            return records, 0

        data = resp.json()
        header = data.get("header", {})
        if total is None:
            total = header.get("total", 0)
            print(f"  APE {ape_code}: {total} total records found")

        batch = data.get("etablissements", [])
        if not batch:
            break

        records.extend(batch)
        debut += PAGE_SIZE
        if debut >= total:
            break

    return records, total


# ---------------------------------------------------------------------------
# HELPERS — Turnover enrichment: INPI API
# ---------------------------------------------------------------------------


def _inpi_authenticate():
    """Authenticate with the INPI API and return a bearer token, or None."""
    if not INPI_USERNAME or not INPI_PASSWORD:
        return None
    try:
        resp = requests.post(
            "https://registre-national-entreprises.inpi.fr/api/sso/login",
            json={"username": INPI_USERNAME, "password": INPI_PASSWORD},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("token")
        print(f"  [INPI] Auth failed: HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  [INPI] Auth error: {exc}")
    return None


def _inpi_get_turnover(siren, token):
    """
    Fetch the most recent annual accounts for a SIREN from the INPI API.
    Returns (turnover_eur, fiscal_year) or (None, None).
    """
    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"https://registre-national-entreprises.inpi.fr/api/companies/{siren}/attachments",
            headers=headers,
            params={"type": "bilan"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None, None

        attachments = resp.json()
        if not attachments:
            return None, None

        # Get the most recent bilan
        latest = None
        for att in attachments if isinstance(attachments, list) else []:
            year = att.get("dateCloture", "")[:4]
            if not latest or year > latest.get("dateCloture", "")[:4]:
                latest = att

        if not latest:
            return None, None

        # Fetch the actual account data
        att_id = latest.get("id")
        if not att_id:
            return None, None

        detail_resp = requests.get(
            f"https://registre-national-entreprises.inpi.fr/api/companies/{siren}/attachments/{att_id}",
            headers=headers,
            timeout=15,
        )
        if detail_resp.status_code != 200:
            return None, None

        detail = detail_resp.json()

        # Extract turnover from the compte de résultat
        # Field names vary: look for common patterns
        ca = None
        for key in ["chiffreAffairesNet", "fl", "ca", "chiffre_affaires"]:
            val = detail.get(key)
            if val is not None:
                try:
                    ca = float(str(val).replace(" ", "").replace(",", "."))
                    if ca > 0:
                        break
                except (ValueError, TypeError):
                    continue

        # Also check nested structures
        if ca is None:
            for section in ["compteResultat", "compte_resultat", "resultats"]:
                sub = detail.get(section) or {}
                for key in ["chiffreAffairesNet", "fl", "ca"]:
                    val = sub.get(key)
                    if val is not None:
                        try:
                            ca = float(str(val).replace(" ", "").replace(",", "."))
                            if ca > 0:
                                break
                        except (ValueError, TypeError):
                            continue
                if ca and ca > 0:
                    break

        year = latest.get("dateCloture", "")[:4]
        return (ca, year) if ca and ca > 0 else (None, None)

    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# HELPERS — Turnover enrichment: Pappers API
# ---------------------------------------------------------------------------


def _pappers_get_turnover(siren):
    """
    Fetch turnover for a SIREN via the Pappers API.
    Returns (turnover_eur, fiscal_year) or (None, None).
    Docs: https://www.pappers.fr/api/documentation
    """
    if not PAPPERS_API_KEY:
        return None, None

    try:
        resp = requests.get(
            "https://api.pappers.fr/v2/entreprise",
            params={"api_token": PAPPERS_API_KEY, "siren": siren},
            timeout=15,
        )
        if resp.status_code != 200:
            return None, None

        data = resp.json()

        # Pappers returns finances as a list of yearly records
        finances = data.get("finances", [])
        if not finances:
            # Try the direct chiffre_d_affaires field
            ca = data.get("chiffre_d_affaires")
            year = data.get("annee_finances")
            if ca:
                return float(ca), str(year) if year else ""
            return None, None

        # Get the most recent year
        latest = max(finances, key=lambda f: f.get("annee", 0))
        ca = latest.get("chiffre_d_affaires")
        year = latest.get("annee")

        if ca is not None:
            return float(ca), str(year)
        return None, None

    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# HELPERS — Turnover enrichment: orchestrator
# ---------------------------------------------------------------------------


def load_turnover_data(sirens_needed):
    """
    Try each turnover source in priority order for the given SIRENs.
    Returns dict: siren -> {turnover_eur, year, source}.
    """
    if SKIP_TURNOVER_API:
        return {}

    turnover_map = {}
    sirens_remaining = set(sirens_needed)

    # --- Source 1: INPI API ---
    if INPI_USERNAME and INPI_PASSWORD:
        print("\n--- Turnover enrichment: INPI API ---")
        token = _inpi_authenticate()
        if token:
            print(f"  [INPI] Authenticated. Querying {len(sirens_remaining)} SIRENs …")
            done = 0
            for siren in list(sirens_remaining):
                ca, year = _inpi_get_turnover(siren, token)
                if ca and ca > 0:
                    turnover_map[siren] = {
                        "turnover_eur": ca,
                        "year": year,
                        "source": f"INPI {year}",
                    }
                    sirens_remaining.discard(siren)
                done += 1
                if done % 50 == 0:
                    print(f"  [INPI] {done}/{len(sirens_needed)} queried, "
                          f"{len(turnover_map)} with turnover …")
                time.sleep(0.3)  # respect 10K/day quota
            print(f"  [INPI] Done: {len(turnover_map)}/{len(sirens_needed)} "
                  f"SIRENs with turnover data")
        else:
            print("  [INPI] Authentication failed, skipping.")
    else:
        print("\n--- Turnover enrichment: INPI credentials not set, skipping ---")

    # --- Source 2: Pappers API (for remaining SIRENs) ---
    if PAPPERS_API_KEY and sirens_remaining:
        print(f"\n--- Turnover enrichment: Pappers API ({len(sirens_remaining)} remaining) ---")
        done = 0
        for siren in list(sirens_remaining):
            ca, year = _pappers_get_turnover(siren)
            if ca and ca > 0:
                turnover_map[siren] = {
                    "turnover_eur": ca,
                    "year": year,
                    "source": f"Pappers {year}",
                }
                sirens_remaining.discard(siren)
            done += 1
            if done % 25 == 0:
                print(f"  [Pappers] {done} queried, "
                      f"{len(turnover_map)} total with turnover …")
            time.sleep(0.5)
        print(f"  [Pappers] Done: {len(turnover_map)}/{len(sirens_needed)} "
              f"SIRENs with turnover data")
    elif not PAPPERS_API_KEY:
        print("\n--- Turnover enrichment: Pappers API key not set, skipping ---")

    print(f"\n  Turnover API total: {len(turnover_map)}/{len(sirens_needed)} SIRENs "
          f"({len(sirens_remaining)} will use employee band estimate)")
    return turnover_map


# ---------------------------------------------------------------------------
# HELPERS — BODACC filing status
# ---------------------------------------------------------------------------


def check_bodacc_filing(siren):
    """
    Query BODACC for a given SIREN.  Returns a dict with:
      - has_recent_filing: bool (depot des comptes found)
      - last_filing_date: str or None
      - total_announcements: int
    """
    try:
        params = {
            "dataset": "annonces-commerciales",
            "q": siren,
            "rows": 20,
            "sort": "-dateparution",
            "facet": "typeavis_lib",
        }
        resp = requests.get(BODACC_URL, params=params, timeout=15)
        if resp.status_code != 200:
            return None

        data = resp.json()
        total = data.get("nhits", 0)
        records = data.get("records", [])

        has_depot = False
        last_depot_date = None

        for rec in records:
            fields = rec.get("fields", {})
            typeavis = (fields.get("typeavis_lib") or "").upper()
            if "COMPTE" in typeavis or "DEPOT" in typeavis:
                has_depot = True
                if not last_depot_date:
                    last_depot_date = fields.get("dateparution")
                break

        return {
            "has_recent_filing": has_depot,
            "last_filing_date": last_depot_date,
            "total_announcements": total,
        }
    except Exception:
        return None


def enrich_bodacc(df):
    """Add BODACC filing status columns to the DataFrame."""
    if SKIP_BODACC:
        df["bodacc_filing"] = ""
        df["bodacc_last_date"] = ""
        return df

    unique_sirens = df["siren"].unique()
    n = min(len(unique_sirens), BODACC_MAX_LOOKUPS)
    print(f"\n--- Checking BODACC filing status for {n} SIRENs ---")

    bodacc_cache = {}
    for i, siren in enumerate(unique_sirens[:BODACC_MAX_LOOKUPS]):
        if i > 0 and i % 50 == 0:
            print(f"  [BODACC] {i}/{n} checked …")
        result = check_bodacc_filing(siren)
        if result:
            bodacc_cache[siren] = result
        time.sleep(0.5)

    print(f"  [BODACC] Done. Got responses for {len(bodacc_cache)}/{n} SIRENs")

    df["bodacc_filing"] = df["siren"].apply(
        lambda s: "Yes" if bodacc_cache.get(s, {}).get("has_recent_filing") else
                  ("No" if s in bodacc_cache else "")
    )
    df["bodacc_last_date"] = df["siren"].apply(
        lambda s: bodacc_cache.get(s, {}).get("last_filing_date") or ""
    )
    return df


# ---------------------------------------------------------------------------
# HELPERS — turnover formatting
# ---------------------------------------------------------------------------


def format_turnover(value):
    """Format a numeric turnover value into a readable string with € symbol."""
    if pd.isna(value) or value <= 0:
        return ""
    if value >= 1_000_000_000:
        return f"€{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"€{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"€{value / 1_000:.0f}K"
    return f"€{value:.0f}"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    if not BEARER_TOKEN:
        print("ERROR: Set BEARER_TOKEN at the top of the script before running.")
        return

    print(f"\n{'='*60}")
    print(CONFIDENTIALITY_NOTICE)
    print(f"{'='*60}\n")

    # =====================================================================
    # PHASE 1 — Fetch & filter from SIRENE
    # =====================================================================
    all_rows = []

    for ape_code, channel in APE_CODES.items():
        print(f"\nFetching APE {ape_code} ({channel}) …")
        try:
            records, total = fetch_all_for_code(ape_code)
        except Exception as exc:
            print(f"  [ERROR] APE {ape_code}: {exc}")
            continue

        if not records:
            print(f"  No records returned for {ape_code}.")
            continue

        # Filter 1 — active establishments
        active = [
            r for r in records
            if r.get("etatAdministratifEtablissement") == "A"
            or (r.get("periodesEtablissement") or [{}])[0]
               .get("etatAdministratifEtablissement") == "A"
        ]

        # Filter 2 — size band >= SIZE_MIN
        sized = [
            r for r in active
            if size_band_gte(r.get("trancheEffectifsEtablissement", "NN"), SIZE_MIN)
        ]

        # Filter 3 — keyword filter (only if > 500 results for this code)
        if total > 500:
            filtered = [
                r for r in sized
                if matches_keyword(
                    r.get("denominationUniteLegale")
                    or (r.get("uniteLegale") or {}).get("denominationUniteLegale", "")
                )
            ]
            print(f"  Keyword filter applied ({len(sized)} -> {len(filtered)})")
        else:
            filtered = sized

        print(f"  Kept {len(filtered)} records after filtering.")

        for rec in filtered:
            ul = rec.get("uniteLegale") or {}
            siret = rec.get("siret", "")
            row = {
                "siret": siret,
                "siren": siret[:9] if len(siret) >= 9 else "",
                "legal_name": (
                    rec.get("denominationUniteLegale")
                    or ul.get("denominationUniteLegale", "")
                ),
                "channel": channel,
                "ape_code": ape_code,
                "address": build_address(rec),
                "postcode": (
                    rec.get("codePostalEtablissement")
                    or (rec.get("adresseEtablissement") or {})
                       .get("codePostalEtablissement", "")
                ),
                "city": (
                    rec.get("libelleCommuneEtablissement")
                    or (rec.get("adresseEtablissement") or {})
                       .get("libelleCommuneEtablissement", "")
                ),
                "size_band": rec.get("trancheEffectifsEtablissement", ""),
            }
            all_rows.append(row)

    if not all_rows:
        print("\nNo records collected. Check your BEARER_TOKEN and network.")
        return

    df = pd.DataFrame(all_rows)

    # =====================================================================
    # PHASE 2 — Retailer type classification
    # =====================================================================
    siren_counts = Counter(df["siren"])

    df["retailer_type"] = df.apply(
        lambda row: classify_retailer_type(
            (row["legal_name"] or "").upper(),
            row["siren"],
            siren_counts,
        ),
        axis=1,
    )

    # =====================================================================
    # PHASE 3 — Channel collapsing & deduplication
    # =====================================================================
    def collapse(group):
        channels = sorted(set(group["channel"]))
        ape_codes = sorted(set(group["ape_code"]))
        result = group.iloc[0].copy()
        result["channel"] = " | ".join(channels)
        result["ape_code"] = " | ".join(ape_codes)
        return result

    df = (
        df.groupby("siret", group_keys=False)
        .apply(collapse)
        .reset_index(drop=True)
    )

    # =====================================================================
    # PHASE 4 — Turnover enrichment (INPI -> Pappers -> band estimate)
    # =====================================================================
    unique_sirens = df["siren"].unique().tolist()
    turnover_map = load_turnover_data(unique_sirens)

    # Apply API-sourced turnover
    df["turnover_eur"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("turnover_eur", None)
    )
    df["turnover_year"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("year", None)
    )
    df["turnover_source"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("source", "")
    )

    # Format actual turnover; fill in band estimate as fallback
    df["turnover_display"] = df.apply(
        lambda row: format_turnover(row["turnover_eur"])
                    if pd.notna(row["turnover_eur"]) and row["turnover_eur"] > 0
                    else "",
        axis=1,
    )
    df["turnover_estimate"] = df.apply(
        lambda row: TURNOVER_ESTIMATE_BY_BAND.get(row["size_band"], "")
                    if not row["turnover_display"]
                    else "",
        axis=1,
    )
    df["turnover_source"] = df.apply(
        lambda row: row["turnover_source"]
                    if row["turnover_source"]
                    else ("Employee band estimate" if row["turnover_estimate"] else ""),
        axis=1,
    )

    # =====================================================================
    # PHASE 5 — BODACC filing status
    # =====================================================================
    df = enrich_bodacc(df)

    # =====================================================================
    # PHASE 6 — Output
    # =====================================================================
    df = df[[
        "siret", "siren", "legal_name", "channel", "retailer_type",
        "ape_code", "address", "postcode", "city", "size_band",
        "turnover_display", "turnover_estimate", "turnover_year",
        "turnover_source",
        "bodacc_filing", "bodacc_last_date",
    ]]

    df = df.rename(columns={
        "turnover_display": "turnover_actual",
        "turnover_est_range": "turnover_est_range",
    })

    output_file = "france_retailers.xlsx"
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Retailers")

        meta_rows = [
            ("Notice", CONFIDENTIALITY_NOTICE),
            ("Generated", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Source — retailers", "INSEE SIRENE API v3"),
            ("Source — turnover (primary)",
             "INPI RNE API (data.inpi.fr) — actual chiffre d'affaires "
             "from filed annual accounts, updated daily, covers FY 2017-present"),
            ("Source — turnover (secondary)",
             "Pappers API (pappers.fr) — aggregated INPI data, "
             "100 free requests/month"),
            ("Source — turnover (fallback)",
             "Employee band estimate — SIRENE trancheEffectifs mapped "
             "to CE-sector turnover ranges"),
            ("Source — filing status",
             "BODACC (bodacc-datadila.opendatasoft.com) — "
             "official gazette, depot des comptes signal"),
            ("Size filter", f"trancheEffectifs >= {SIZE_MIN}"),
            ("APE codes queried", ", ".join(APE_CODES.keys())),
            ("Classification",
             f"Chain = known chain name OR >= {CHAIN_SIREN_THRESHOLD} "
             f"establishments per SIREN; "
             f"Buying Group = known buying group name pattern; "
             f"Independent = all others"),
        ]
        meta = pd.DataFrame(meta_rows, columns=["Field", "Value"])
        meta.to_excel(writer, index=False, sheet_name="Metadata")

    print(f"\nWrote {len(df)} rows to {output_file}")

    # ----- Summary -----
    print(f"\n{'='*60}")
    print(CONFIDENTIALITY_NOTICE)
    print(f"{'='*60}")

    print("\n--- Channel breakdown ---")
    for ch in sorted(set(APE_CODES.values())):
        count = df["channel"].str.contains(ch, regex=False).sum()
        print(f"  {ch:15s}: {count:>5}")

    print("\n--- Retailer type breakdown ---")
    for rtype, count in df["retailer_type"].value_counts().items():
        print(f"  {rtype:15s}: {count:>5}")

    n_actual = (df["turnover_display"] != "").sum()
    n_est = (df["turnover_estimate"] != "").sum()
    n_bodacc = (df["bodacc_filing"] == "Yes").sum()
    print("\n--- Turnover enrichment ---")
    print(f"  Actual CA (INPI/Pappers): {n_actual:>5} SIRETs")
    print(f"  Band estimate (fallback): {n_est:>5} SIRETs")
    print(f"  No turnover info        : {len(df) - n_actual - n_est:>5} SIRETs")
    print(f"  BODACC filing detected  : {n_bodacc:>5} SIRENs")

    print(f"\n  Total unique SIRETs : {len(df)}")
    print(f"  Total unique SIRENs : {df['siren'].nunique()}")


if __name__ == "__main__":
    main()
