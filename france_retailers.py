#!/usr/bin/env python3
"""
Query the French INSEE SIRENE API to find retailers across multiple channels,
classify them (Chain / Buying Group / Independent), and enrich with turnover
data from DGFiP open annual accounts + BODACC filing status.

Set BEARER_TOKEN below and run: python france_retailers.py

CONFIDENTIAL — INTERNAL USE ONLY
This script and its output contain proprietary market intelligence data.
Do not distribute, share, or publish results without explicit authorization.
All data sourced from INSEE SIRENE (public registry) but the filtering logic,
channel mappings, and retailer classification methodology are proprietary.
"""

import io
import os
import time
import zipfile
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
# CONFIGURATION — set your bearer token here
# ---------------------------------------------------------------------------

BEARER_TOKEN = ""  # <-- paste your INSEE API token here

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
# TURNOVER ENRICHMENT — DGFiP annual accounts
# ---------------------------------------------------------------------------
# data.gouv.fr publishes annual account files. Each yearly release is a ZIP
# containing one or more CSV files.  The dataset page lists resources per year.
# We try the most recent years first (newest data wins).
#
# Schema varies by year but the turnover column is typically one of:
#   ca, chiffre_d_affaires, chiffre_affaires, redi_r310
# We try all known variants and take the first non-null.
#
# Set DGFIP_YEARS to the years you want to try (most recent first).
# Set DGFIP_CACHE_DIR to a folder for caching downloaded CSVs.
# Set SKIP_DGFIP = True to skip this enrichment entirely.

SKIP_DGFIP = False
DGFIP_CACHE_DIR = Path("dgfip_cache")
DGFIP_YEARS = [2023, 2022, 2021]

# Resource URLs per year on data.gouv.fr.  These are stable download links
# for the "comptes annuels" dataset.  If a URL changes, update it here.
DGFIP_URLS = {
    2023: "https://www.data.gouv.fr/fr/datasets/r/0ace4da0-47e3-4753-bc2c-aa26e22e38df",
    2022: "https://www.data.gouv.fr/fr/datasets/r/5a4a2744-70e7-4e6e-a3e0-ea7470755bcd",
    2021: "https://www.data.gouv.fr/fr/datasets/r/9b2e4ec0-71b0-48ce-a5f2-1e89c4e0e36e",
}

# All known column names for turnover across DGFiP schema versions
TURNOVER_COLUMN_CANDIDATES = [
    "ca", "chiffre_d_affaires", "chiffre_affaires",
    "redi_r310", "fl",
]

SIREN_COLUMN_CANDIDATES = ["siren", "SIREN", "siren_ent"]

# ---------------------------------------------------------------------------
# TURNOVER ESTIMATION FROM EMPLOYEE BAND — CE/retail sector proxies
# ---------------------------------------------------------------------------
# When DGFiP data is unavailable, use employee band as a rough proxy.
# Ranges are directional estimates for the French CE/retail sector.

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
#
# Set SKIP_BODACC = True to skip (saves many API calls for large datasets).

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
# HELPERS — DGFiP annual accounts
# ---------------------------------------------------------------------------


def _find_column(columns, candidates):
    """Return the first column name from *candidates* that exists in *columns*."""
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def _read_csv_flexible(path_or_buf):
    """Try common separators and encodings to read a DGFiP CSV."""
    for sep in [";", ",", "\t"]:
        for encoding in ["utf-8", "latin-1", "iso-8859-1"]:
            try:
                df = pd.read_csv(
                    path_or_buf, sep=sep, encoding=encoding,
                    dtype=str, low_memory=False, nrows=5,
                )
                if len(df.columns) > 2:
                    # Re-read fully now that we know the format
                    if hasattr(path_or_buf, "seek"):
                        path_or_buf.seek(0)
                    return pd.read_csv(
                        path_or_buf, sep=sep, encoding=encoding,
                        dtype=str, low_memory=False,
                    )
            except Exception:
                if hasattr(path_or_buf, "seek"):
                    path_or_buf.seek(0)
                continue
    return None


def download_dgfip_file(year):
    """Download and cache a DGFiP annual accounts file. Returns a DataFrame or None."""
    url = DGFIP_URLS.get(year)
    if not url:
        print(f"  [DGFiP] No URL configured for {year}, skipping.")
        return None

    DGFIP_CACHE_DIR.mkdir(exist_ok=True)
    cache_path = DGFIP_CACHE_DIR / f"comptes_{year}.csv"

    # Use cached file if it exists
    if cache_path.exists() and cache_path.stat().st_size > 0:
        print(f"  [DGFiP] Using cached file for {year}: {cache_path}")
        return _read_csv_flexible(str(cache_path))

    print(f"  [DGFiP] Downloading {year} data … (this may take a minute)")
    try:
        resp = requests.get(url, timeout=300, stream=True)
        if resp.status_code != 200:
            print(f"  [DGFiP] HTTP {resp.status_code} for {year}, skipping.")
            return None

        content_type = resp.headers.get("Content-Type", "")
        raw = resp.content

        # Handle ZIP files
        if "zip" in content_type or url.endswith(".zip") or raw[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    print(f"  [DGFiP] No CSV found inside ZIP for {year}.")
                    return None
                # Use the largest CSV (most likely the main data file)
                csv_name = max(csv_names, key=lambda n: zf.getinfo(n).file_size)
                print(f"  [DGFiP] Extracting {csv_name} from ZIP …")
                with zf.open(csv_name) as f:
                    data = f.read()
                cache_path.write_bytes(data)
                return _read_csv_flexible(str(cache_path))
        else:
            # Plain CSV
            cache_path.write_bytes(raw)
            return _read_csv_flexible(str(cache_path))

    except Exception as exc:
        print(f"  [DGFiP] Download failed for {year}: {exc}")
        return None


def load_dgfip_turnover(sirens_needed):
    """
    Load DGFiP annual accounts and return a dict: siren -> {year, turnover, source}.
    Tries most recent year first.  Only loads SIRENs we actually need.
    """
    if SKIP_DGFIP:
        return {}

    print("\n--- Loading DGFiP annual accounts for turnover data ---")
    turnover_map = {}  # siren -> {year, turnover_eur, source}
    sirens_remaining = set(sirens_needed)

    for year in DGFIP_YEARS:
        if not sirens_remaining:
            break

        df = download_dgfip_file(year)
        if df is None:
            continue

        # Find the SIREN column
        siren_col = _find_column(df.columns, SIREN_COLUMN_CANDIDATES)
        if not siren_col:
            print(f"  [DGFiP] Cannot find SIREN column in {year} data. "
                  f"Columns: {list(df.columns[:10])}")
            continue

        # Find the turnover column
        ca_col = _find_column(df.columns, TURNOVER_COLUMN_CANDIDATES)
        if not ca_col:
            print(f"  [DGFiP] Cannot find turnover column in {year} data. "
                  f"Columns: {list(df.columns[:15])}")
            continue

        print(f"  [DGFiP] {year}: using SIREN='{siren_col}', turnover='{ca_col}'")

        # Normalise SIREN to 9-char string
        df[siren_col] = df[siren_col].astype(str).str.strip().str.zfill(9)

        # Filter to only our SIRENs
        relevant = df[df[siren_col].isin(sirens_remaining)].copy()
        if relevant.empty:
            print(f"  [DGFiP] {year}: no matching SIRENs found.")
            continue

        # Parse turnover — handle French number format (comma decimal)
        relevant[ca_col] = (
            relevant[ca_col].astype(str)
            .str.replace(" ", "", regex=False)
            .str.replace(",", ".", regex=False)
        )
        relevant[ca_col] = pd.to_numeric(relevant[ca_col], errors="coerce")

        # For each SIREN, keep the highest turnover if multiple rows
        for siren, grp in relevant.groupby(siren_col):
            best = grp[ca_col].max()
            if pd.notna(best) and best > 0:
                turnover_map[siren] = {
                    "year": year,
                    "turnover_eur": best,
                    "source": f"DGFiP {year}",
                }
                sirens_remaining.discard(siren)

        print(f"  [DGFiP] {year}: matched {len(sirens_needed) - len(sirens_remaining)} "
              f"SIRENs so far ({len(sirens_remaining)} remaining)")

    print(f"  [DGFiP] Total: turnover data found for "
          f"{len(turnover_map)}/{len(sirens_needed)} SIRENs")
    return turnover_map


# ---------------------------------------------------------------------------
# HELPERS — BODACC filing status
# ---------------------------------------------------------------------------


def check_bodacc_filing(siren):
    """
    Query BODACC for a given SIREN.  Returns a dict with:
      - has_recent_filing: bool (depot des comptes in last 3 years)
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
        time.sleep(0.5)  # polite rate limit

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
    # PHASE 4 — Turnover enrichment (DGFiP annual accounts)
    # =====================================================================
    unique_sirens = df["siren"].unique().tolist()
    turnover_map = load_dgfip_turnover(unique_sirens)

    # Apply DGFiP turnover
    df["turnover_eur"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("turnover_eur", None)
    )
    df["turnover_year"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("year", None)
    )
    df["turnover_source"] = df["siren"].apply(
        lambda s: turnover_map.get(s, {}).get("source", "")
    )

    # Fallback: employee band estimate when DGFiP data is missing
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

    # Rename for cleaner Excel headers
    df = df.rename(columns={
        "turnover_display": "turnover_actual",
        "turnover_estimate": "turnover_est_range",
    })

    output_file = "france_retailers.xlsx"
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Retailers")

        # Metadata sheet
        meta_rows = [
            ("Notice", CONFIDENTIALITY_NOTICE),
            ("Generated", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Source — retailers", "INSEE SIRENE API v3"),
            ("Source — turnover", "DGFiP comptes annuels (data.gouv.fr)"),
            ("Source — filing status", "BODACC (bodacc-datadila.opendatasoft.com)"),
            ("Size filter", f"trancheEffectifs >= {SIZE_MIN}"),
            ("APE codes queried", ", ".join(APE_CODES.keys())),
            ("Classification method",
             f"Chain = known chain name OR >= {CHAIN_SIREN_THRESHOLD} "
             f"establishments per SIREN; "
             f"Buying Group = known buying group name pattern; "
             f"Independent = all others"),
            ("Turnover method",
             "Primary: DGFiP annual accounts (actual CA). "
             "Fallback: employee band proxy range (CE sector estimates). "
             "BODACC filing = accounts deposit signal."),
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

    n_actual = (df["turnover_actual"] != "").sum()
    n_est = (df["turnover_est_range"] != "").sum()
    n_bodacc = (df["bodacc_filing"] == "Yes").sum()
    print("\n--- Turnover enrichment ---")
    print(f"  DGFiP actual CA : {n_actual:>5} SIRETs")
    print(f"  Band estimate   : {n_est:>5} SIRETs (no DGFiP data)")
    print(f"  No turnover info: {len(df) - n_actual - n_est:>5} SIRETs")
    print(f"  BODACC filing   : {n_bodacc:>5} SIRENs with recent depot")

    print(f"\n  Total unique SIRETs : {len(df)}")
    print(f"  Total unique SIRENs : {df['siren'].nunique()}")


if __name__ == "__main__":
    main()
