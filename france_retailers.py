#!/usr/bin/env python3
"""
Query the French INSEE SIRENE API to find retailers across multiple channels.
Set BEARER_TOKEN below and run: python france_retailers.py
"""

import time
import requests
import pandas as pd

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


def fetch_all_for_code(ape_code):
    """Paginate through SIRENE API for a single APE code. Returns (records, total)."""
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}", "Accept": "application/json"}
    params_base = {
        "q": f"activitePrincipaleEtablissement:{ape_code} AND etatAdministratifEtablissement:A",
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
        active = [r for r in records if r.get("etatAdministratifEtablissement") == "A"
                  or (r.get("periodesEtablissement") or [{}])[0].get("etatAdministratifEtablissement") == "A"]

        # Filter 2 — size band >= SIZE_MIN
        sized = [r for r in active if size_band_gte(
            r.get("trancheEffectifsEtablissement", "NN"), SIZE_MIN
        )]

        # Filter 3 — keyword filter (only if > 500 results for this code)
        if total > 500:
            filtered = [r for r in sized if matches_keyword(
                r.get("denominationUniteLegale") or
                (r.get("uniteLegale") or {}).get("denominationUniteLegale", "")
            )]
            print(f"  Keyword filter applied ({len(sized)} → {len(filtered)})")
        else:
            filtered = sized

        print(f"  Kept {len(filtered)} records after filtering.")

        for rec in filtered:
            ul = rec.get("uniteLegale") or {}
            row = {
                "siret": rec.get("siret", ""),
                "legal_name": rec.get("denominationUniteLegale")
                              or ul.get("denominationUniteLegale", ""),
                "channel": channel,
                "ape_code": ape_code,
                "address": build_address(rec),
                "postcode": rec.get("codePostalEtablissement")
                            or (rec.get("adresseEtablissement") or {}).get("codePostalEtablissement", ""),
                "city": rec.get("libelleCommuneEtablissement")
                        or (rec.get("adresseEtablissement") or {}).get("libelleCommuneEtablissement", ""),
                "size_band": rec.get("trancheEffectifsEtablissement", ""),
            }
            all_rows.append(row)

    if not all_rows:
        print("\nNo records collected. Check your BEARER_TOKEN and network.")
        return

    df = pd.DataFrame(all_rows)

    # Channel collapsing & deduplication — group by SIRET
    def collapse(group):
        channels = sorted(set(group["channel"]))
        best = group.loc[group["channel"].str.count(r"\|").idxmax()] if len(group) > 1 else group.iloc[0]
        result = best.copy()
        result["channel"] = " | ".join(channels)
        return result

    df = df.groupby("siret", group_keys=False).apply(collapse).reset_index(drop=True)

    # Ensure column order
    df = df[["siret", "legal_name", "channel", "ape_code", "address", "postcode", "city", "size_band"]]

    # Write Excel
    output_file = "france_retailers.xlsx"
    df.to_excel(output_file, index=False, engine="openpyxl")
    print(f"\nWrote {len(df)} rows to {output_file}")

    # Summary
    print("\n--- Summary ---")
    for ch in sorted(APE_CODES.values()):
        count = df["channel"].str.contains(ch, regex=False).sum()
        print(f"  {ch}: {count}")
    print(f"  Total unique SIRETs: {len(df)}")


if __name__ == "__main__":
    main()
