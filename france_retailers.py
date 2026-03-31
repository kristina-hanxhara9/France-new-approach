#!/usr/bin/env python3
"""
Query the French INSEE SIRENE API to find retailers across multiple channels.
Set BEARER_TOKEN below and run: python france_retailers.py

CONFIDENTIAL — INTERNAL USE ONLY
This script and its output contain proprietary market intelligence data.
Do not distribute, share, or publish results without explicit authorization.
All data sourced from INSEE SIRENE (public registry) but the filtering logic,
channel mappings, and retailer classification methodology are proprietary.
"""

import time
from collections import Counter

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
# RETAILER TYPE CLASSIFICATION — known patterns
# ---------------------------------------------------------------------------

# Major French retail chains (centralized ownership / franchise networks).
# Each entry is a substring matched against the uppercased legal name.
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

# French buying groups / purchasing cooperatives for independent retailers.
# Members keep independent ownership but buy through the group.
KNOWN_BUYING_GROUPS = [
    "EXPERT", "EURONICS", "GITEM", "PRO & CIE", "PRO ET CIE",
    "PULSAT", "CONNEXION", "ELECTROPLANET", "ELECTRO PLANET",
    "MENAGER PLUS", "DIGITAL SHOPPING",
    "GROUPEMENT", "CENTRALE D'ACHAT", "CENTRALE D ACHAT",
    "COOPERATIVE D'ACHAT", "COOPERATIVE D ACHAT",
    "GIE ", "SYNDICAT",
]

# Minimum number of establishments (SIRETs) sharing the same SIREN
# to be classified as a chain by structure alone (when name doesn't match).
CHAIN_SIREN_THRESHOLD = 3


# ---------------------------------------------------------------------------
# HELPERS
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

    Logic (applied in priority order):
    1. Name matches a known CHAIN pattern         -> "Chain"
    2. Name matches a known BUYING GROUP pattern   -> "Buying Group"
    3. SIREN has >= CHAIN_SIREN_THRESHOLD sites    -> "Chain"
       (multi-site operator not in our name lists)
    4. Otherwise                                   -> "Independent"
    """
    # 1. Known chain by name
    for pattern in KNOWN_CHAINS:
        if pattern in name_upper:
            return "Chain"

    # 2. Known buying group by name
    for pattern in KNOWN_BUYING_GROUPS:
        if pattern in name_upper:
            return "Buying Group"

    # 3. Multi-site detection via SIREN
    if siren and siren_counts.get(siren, 0) >= CHAIN_SIREN_THRESHOLD:
        return "Chain"

    # 4. Default
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
# MAIN
# ---------------------------------------------------------------------------


def main():
    if not BEARER_TOKEN:
        print("ERROR: Set BEARER_TOKEN at the top of the script before running.")
        return

    print(f"\n{'='*60}")
    print(CONFIDENTIALITY_NOTICE)
    print(f"{'='*60}\n")

    all_rows = []  # list of dicts ready for DataFrame

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

    # ----- Retailer type classification -----
    # Count how many distinct SIRETs each SIREN has across the full dataset.
    # A SIREN with many SIRETs = multi-site operator = likely a chain.
    siren_counts = Counter(df["siren"])

    df["retailer_type"] = df.apply(
        lambda row: classify_retailer_type(
            (row["legal_name"] or "").upper(),
            row["siren"],
            siren_counts,
        ),
        axis=1,
    )

    # ----- Channel collapsing & deduplication -----
    def collapse(group):
        channels = sorted(set(group["channel"]))
        ape_codes = sorted(set(group["ape_code"]))
        # Keep the first row's data as representative
        result = group.iloc[0].copy()
        result["channel"] = " | ".join(channels)
        result["ape_code"] = " | ".join(ape_codes)
        return result

    df = (
        df.groupby("siret", group_keys=False)
        .apply(collapse)
        .reset_index(drop=True)
    )

    # Final column order
    df = df[[
        "siret", "siren", "legal_name", "channel", "retailer_type",
        "ape_code", "address", "postcode", "city", "size_band",
    ]]

    # ----- Write Excel -----
    output_file = "france_retailers.xlsx"
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Retailers")

        # Add a metadata / confidentiality sheet
        meta = pd.DataFrame({
            "Field": [
                "Notice",
                "Generated",
                "Source",
                "Size filter",
                "APE codes queried",
                "Classification method",
            ],
            "Value": [
                CONFIDENTIALITY_NOTICE,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                "INSEE SIRENE API v3",
                f"trancheEffectifs >= {SIZE_MIN}",
                ", ".join(APE_CODES.keys()),
                (
                    "Chain = known chain name OR >= "
                    f"{CHAIN_SIREN_THRESHOLD} establishments per SIREN; "
                    "Buying Group = known buying group name pattern; "
                    "Independent = all others"
                ),
            ],
        })
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

    print(f"\n  Total unique SIRETs : {len(df)}")
    print(f"  Total unique SIRENs : {df['siren'].nunique()}")


if __name__ == "__main__":
    main()
