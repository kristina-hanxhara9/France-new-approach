#!/usr/bin/env python3
"""
Query the French INSEE SIRENE API to find retailers across multiple channels,
classify them (Chain / Buying Group / Independent), and enrich with turnover
data from INPI / Pappers annual accounts + BODACC filing status.

Set up your .env file and run: python france_retailers.py

Note: All source data comes from public French government registries
(INSEE SIRENE, INPI, BODACC). The channel mappings, retailer classification
logic, and curated lists in this script are original work — please credit
or check before sharing externally.
"""

import os
import time
import urllib3
from collections import Counter
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# HTTP SESSION — handles SSL certificate issues automatically
# ---------------------------------------------------------------------------
# Some machines (especially corporate networks / Windows) fail SSL verification
# when calling api.insee.fr.  We try with verification first; if it fails, we
# retry without verification and suppress the noisy warnings.

http = requests.Session()

def _test_ssl():
    """Check if SSL works for the INSEE API. Disable verify if it doesn't."""
    try:
        requests.head("https://api.insee.fr", timeout=10)
    except requests.exceptions.SSLError:
        print("  [SSL] Certificate verification failed — disabling for this run.")
        print("  [SSL] This is common on corporate networks. Data is still encrypted.")
        http.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass  # other errors will surface later in the actual API calls

# Load .env file if present (no dependency on python-dotenv)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

# ---------------------------------------------------------------------------
# NOTICE
# ---------------------------------------------------------------------------

DATA_NOTICE = (
    "Source data: public French registries (INSEE, INPI, BODACC). "
    "Channel mappings and classification logic are original work."
)

# ---------------------------------------------------------------------------
# CONFIGURATION — reads from .env file or environment variables
# ---------------------------------------------------------------------------

# INSEE SIRENE — get your API key from https://portail-api.insee.fr
#
# Option A (recommended): Create a "Simple" application
#   → gives you one API key. Paste it as SIRENE_API_KEY below.
#
# Option B: Create a "Machine-to-machine" application
#   → gives you client_id + client_secret. Paste both below.
#   The script will auto-generate a Bearer token from them.
#
# Steps:
#   1. Create an account on portail-api.insee.fr
#   2. Create an application (Simple is easiest)
#   3. Subscribe to the API Sirene
#   4. Copy the key(s) into your .env file
SIRENE_API_KEY = os.environ.get("SIRENE_API_KEY", "")
SIRENE_CLIENT_ID = os.environ.get("SIRENE_CLIENT_ID", "")
SIRENE_CLIENT_SECRET = os.environ.get("SIRENE_CLIENT_SECRET", "")

# INPI — optional, for actual turnover data
INPI_USERNAME = os.environ.get("INPI_USERNAME", "")
INPI_PASSWORD = os.environ.get("INPI_PASSWORD", "")

# Note: APE_CODES maps one code to one primary channel. 47.54Z covers both
# MDA and SDA — we assign it to MDA here and add SDA via the known retailers
# name search + an extra query below.
APE_CODES = {
    "47.78C": "Photo",
    "47.43Z": "CE",
    "47.41Z": "CE",
    "47.54Z": "MDA",
    "47.42Z": "Mobile",
    "47.59B": "Phone Accessories",
    "95.11Z": "Refurb",
    "95.12Z": "Refurb",
}
# Extra: same APE code mapped to additional channels
APE_CODES_EXTRA = {
    "47.54Z": "SDA",  # same code as MDA — appliance stores sell both
}

# ---------------------------------------------------------------------------
# PER-CHANNEL KEYWORD FILTERS
# ---------------------------------------------------------------------------
# When an APE code returns many results (broad code), we filter by these
# keywords to keep only relevant retailers.  If a code is "strict" we ALWAYS
# apply the keyword filter regardless of result count.  If "loose" we only
# apply when > 500 results.

CHANNEL_KEYWORDS = {
    "Photo": {
        "keywords": [
            "PHOTO", "PHOX", "CAMARA", "OPTIQUE", "CAMERA", "CANON",
            "NIKON", "SONY", "FUJI", "LEICA", "OBJECTIF", "LABO PHOTO",
            "STUDIO PHOTO", "IMAGE", "FNAC", "DARTY",
        ],
        "mode": "strict",  # 47.78C is very broad — always filter
    },
    "CE": {
        "keywords": [
            "FNAC", "DARTY", "BOULANGER", "ELECTRO", "ELECTROMENAGER",
            "MULTIMEDIA", "HI-FI", "HIFI", "AUDIO", "VIDEO", "SONO",
            "INFORMATIQUE", "ORDINATEUR", "COMPUTER", "NUMERIQUE",
            "DIGITAL", "MICROMANIA", "CULTURA", "LDLC", "MATERIEL",
            "SAMSUNG", "LG ", "HUBSIDE", "LICK", "TV ", "TELE",
        ],
        "mode": "loose",
    },
    "MDA": {
        "keywords": [
            "ELECTROMENAGER", "ELECTRO MENAGER", "MENAGER", "DARTY",
            "BOULANGER", "BUT ", "BUT-", "CONFORAMA",
            "LAVE", "FRIGO", "REFRIGER", "CONGELAT", "FOUR",
            "CUISSON", "HOTTE", "SECHE", "LAVE-VAISSELLE",
            "WHIRLPOOL", "ELECTROLUX", "MIELE", "SIEMENS", "BOSCH",
            "BRANDT", "VEDETTE", "CANDY", "HAIER", "HISENSE", "BEKO",
            "SAMSUNG", "LG ", "INDESIT",
            "EXPERT", "GITEM", "PULSAT", "EURONICS", "PRO & CIE",
        ],
        "mode": "loose",
    },
    "SDA": {
        "keywords": [
            "ELECTROMENAGER", "ELECTRO MENAGER", "MENAGER", "DARTY",
            "BOULANGER", "FNAC",
            "ASPIRAT", "CAFETIERE", "ROBOT", "MIXEUR", "BLENDER",
            "BOUILLOIRE", "GRILLE-PAIN", "FER A REPASSER",
            "KENWOOD", "MOULINEX", "SEB ", "DELONGHI", "KRUPS",
            "KITCHENAID", "DYSON", "SMEG", "PHILIPS", "BRAUN",
            "ROWENTA", "TEFAL", "MAGIMIX", "NESPRESSO",
            "EXPERT", "GITEM", "PULSAT", "EURONICS", "PRO & CIE",
        ],
        "mode": "loose",
    },
    "Mobile": {
        "keywords": [
            "TELEPHON", "MOBILE", "SMARTPHONE", "ORANGE", "SFR",
            "BOUYGUES", "FREE ", "APPLE", "SAMSUNG", "XIAOMI",
            "HUAWEI", "OPPO", "WIKO", "POINT SERVICE", "PSM",
            "WEFIX", "WE FIX", "SAVE ", "IRIPARO", "MOBILAX",
            "HUBSIDE", "TELECOM", "PHONE",
        ],
        "mode": "loose",
    },
    "Phone Accessories": {
        "keywords": [
            "ACCESSOIRE", "COQUE", "PROTECTION", "CHARGEUR", "CABLE",
            "ECOUTEUR", "CASQUE", "ENCEINTE", "BATTERIE", "ETUI",
            "TELEPHON", "MOBILE", "SMARTPHONE", "PHONE",
            "VERRE TREMPE", "FILM PROTEC",
            "RHINOSHIELD", "BELKIN", "ANKER", "LICK", "MOBILIZE",
            "FNAC", "DARTY", "BOULANGER",
        ],
        "mode": "strict",  # 47.59B is very broad — must match phone keywords
    },
    "Refurb": {
        "keywords": [
            "RECONDITION", "RECONDITIO", "REFURB", "REPAIR", "REPARATION",
            "CASH CONVERTER", "CASH EXPRESS", "EASY CASH", "HAPPY CASH",
            "BACK MARKET", "BACKMARKET", "RECOMMERCE", "SMAAART",
            "CERTIDEAL", "REMADE", "REBORN", "OCCASION", "SECOND",
            "SAVE ", "WEFIX", "WE FIX", "IRIPARO", "MOBILAX",
            "POINT SERVICE", "PSM ", "INFORMATIQUE", "DEPANNAGE",
            "MAINTENANCE",
        ],
        "mode": "loose",
    },
}

# ---------------------------------------------------------------------------
# PER-CHANNEL KNOWN RETAILERS — searched by name via SIRENE
# ---------------------------------------------------------------------------
# These are retailers we KNOW belong to each channel.  We search SIRENE for
# them by name (denominationUniteLegale) so we capture them even if the APE
# code query doesn't find them.  "type" is pre-assigned.

CHANNEL_KNOWN_RETAILERS = {
    "Photo": {
        "chains": [
            "PHOX", "CAMARA", "FNAC", "DARTY",
        ],
        "buying_groups": [],
    },
    "CE": {
        "chains": [
            "FNAC", "DARTY", "BOULANGER", "ELECTRO DEPOT", "LDLC",
            "MATERIEL NET", "CULTURA", "LICK", "HUBSIDE", "MICROMANIA",
            "SAMSUNG ELECTRONICS", "APPLE RETAIL",
        ],
        "buying_groups": [
            "EXPERT", "EURONICS", "GITEM",
            "PRO & CIE", "CONNEXION",
        ],
    },
    "MDA": {
        "chains": [
            "DARTY", "BOULANGER", "BUT", "CONFORAMA", "ELECTRO DEPOT",
        ],
        "buying_groups": [
            "EXPERT", "EURONICS", "GITEM", "PULSAT",
            "PRO & CIE", "CONNEXION", "MENAGER PLUS",
        ],
    },
    "SDA": {
        "chains": [
            "DARTY", "BOULANGER", "FNAC",
        ],
        "buying_groups": [
            "EXPERT", "EURONICS", "GITEM", "PULSAT",
            "PRO & CIE", "CONNEXION",
        ],
    },
    "Mobile": {
        "chains": [
            "ORANGE", "SFR", "BOUYGUES TELECOM", "FREE",
            "APPLE RETAIL", "SAMSUNG ELECTRONICS", "XIAOMI", "HUAWEI",
            "HUBSIDE",
        ],
        "buying_groups": [],
    },
    "Phone Accessories": {
        "chains": [
            "FNAC", "DARTY", "BOULANGER", "LICK",
        ],
        "buying_groups": [],
    },
    "Refurb": {
        "chains": [
            "CASH CONVERTERS", "EASY CASH", "HAPPY CASH", "CASH EXPRESS",
            "BACK MARKET", "RECOMMERCE", "SMAAART", "CERTIDEAL",
            "REMADE", "REBORN", "POINT SERVICE MOBILES", "WEFIX",
            "SAVE", "IRIPARO", "MOBILAX",
        ],
        "buying_groups": [],
    },
}

# ---------------------------------------------------------------------------
# APE CODE REFERENCE — descriptions in French, English, Swedish
# ---------------------------------------------------------------------------
# APE (Activité Principale Exercée) is the French equivalent of SIC/NACE.
# These are the codes we query, plus related codes found in results.

APE_DESCRIPTIONS = {
    # Codes we actively query
    "47.78C": {
        "fr": "Commerce de détail d'articles de sport en magasin spécialisé / Optique et photographie",
        "en": "Retail sale of photographic, optical and precision equipment",
        "sv": "Detaljhandel med foto- och optikvaror",
        "channel": "Photo",
        "queried": True,
    },
    "47.43Z": {
        "fr": "Commerce de détail de matériels audio et vidéo en magasin spécialisé",
        "en": "Retail sale of audio and video equipment in specialised stores",
        "sv": "Specialiserad detaljhandel med ljud- och bildanläggningar",
        "channel": "CE",
        "queried": True,
    },
    "47.41Z": {
        "fr": "Commerce de détail d'ordinateurs, d'unités périphériques et de logiciels",
        "en": "Retail sale of computers, peripheral units and software",
        "sv": "Specialiserad detaljhandel med datorer och tillbehör",
        "channel": "CE",
        "queried": True,
    },
    "47.54Z": {
        "fr": "Commerce de détail d'appareils électroménagers en magasin spécialisé",
        "en": "Retail sale of electrical household appliances in specialised stores",
        "sv": "Specialiserad detaljhandel med hushållsapparater",
        "channel": "MDA + SDA",
        "queried": True,
    },
    "47.42Z": {
        "fr": "Commerce de détail de matériels de télécommunication en magasin spécialisé",
        "en": "Retail sale of telecommunications equipment in specialised stores",
        "sv": "Specialiserad detaljhandel med telekommunikationsutrustning",
        "channel": "Mobile",
        "queried": True,
    },
    "47.59B": {
        "fr": "Commerce de détail d'autres équipements du foyer",
        "en": "Retail sale of other household equipment not elsewhere classified",
        "sv": "Detaljhandel med övrig hushållsutrustning",
        "channel": "Phone Accessories",
        "queried": True,
    },
    "95.11Z": {
        "fr": "Réparation d'ordinateurs et d'équipements périphériques",
        "en": "Repair of computers and peripheral equipment",
        "sv": "Reparation av datorer och kringutrustning",
        "channel": "Refurb",
        "queried": True,
    },
    "95.12Z": {
        "fr": "Réparation d'équipements de communication",
        "en": "Repair of communication equipment",
        "sv": "Reparation av kommunikationsutrustning",
        "channel": "Refurb",
        "queried": True,
    },
    # Related codes (not queried but may appear in omni-retailer sheet)
    "47.11F": {
        "fr": "Hypermarchés",
        "en": "Hypermarkets (>2,500 sqm)",
        "sv": "Stormarknader (>2 500 kvm)",
        "channel": "N/A — generic retail",
        "queried": False,
    },
    "47.19Z": {
        "fr": "Grands magasins",
        "en": "Department stores",
        "sv": "Varuhus",
        "channel": "N/A — generic retail",
        "queried": False,
    },
    "47.91B": {
        "fr": "Vente à distance sur catalogue spécialisé",
        "en": "Distance selling / e-commerce",
        "sv": "Distanshandel / e-handel",
        "channel": "N/A — e-commerce",
        "queried": False,
    },
    "47.11C": {
        "fr": "Supermarchés",
        "en": "Supermarkets (400-2,500 sqm)",
        "sv": "Snabbköp/Supermarknader (400-2 500 kvm)",
        "channel": "N/A — generic retail",
        "queried": False,
    },
}


def _write_styled_overview(wb):
    """Write a styled overview sheet directly to the workbook using openpyxl."""
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet("How This Works", 0)  # First sheet
    ws.sheet_properties.tabColor = "4472C4"

    # Style definitions
    title_font = Font(name="Calibri", size=18, bold=True, color="FFFFFF")
    title_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    section_font = Font(name="Calibri", size=13, bold=True, color="2F5496")
    section_fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
    label_font = Font(name="Calibri", size=11, bold=True, color="333333")
    body_font = Font(name="Calibri", size=11, color="333333")
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    green_font = Font(name="Calibri", size=11, bold=True, color="006100")
    yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    yellow_font = Font(name="Calibri", size=11, bold=True, color="9C6500")
    table_header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    table_header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin", color="B4C6E7"),
        right=Side(style="thin", color="B4C6E7"),
        top=Side(style="thin", color="B4C6E7"),
        bottom=Side(style="thin", color="B4C6E7"),
    )
    wrap = Alignment(wrap_text=True, vertical="top")

    # Column widths
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 85
    ws.column_dimensions["C"].width = 30

    row = 1

    def _title(text):
        nonlocal row
        ws.merge_cells(f"A{row}:C{row}")
        c = ws.cell(row=row, column=1, value=text)
        c.font = title_font
        c.fill = title_fill
        c.alignment = Alignment(vertical="center")
        ws.row_dimensions[row].height = 36
        row += 1

    def _section(text):
        nonlocal row
        row += 1  # blank spacer row
        ws.merge_cells(f"A{row}:C{row}")
        c = ws.cell(row=row, column=1, value=text)
        c.font = section_font
        c.fill = section_fill
        c.alignment = Alignment(vertical="center")
        ws.row_dimensions[row].height = 28
        row += 1

    def _row(label, desc, label_style=None, fill=None):
        nonlocal row
        c1 = ws.cell(row=row, column=1, value=label)
        c2 = ws.cell(row=row, column=2, value=desc)
        c1.font = label_style or label_font
        c2.font = body_font
        c1.alignment = wrap
        c2.alignment = wrap
        if fill:
            c1.fill = fill
            c2.fill = fill
        row += 1

    def _table(headers, rows_data):
        nonlocal row
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.font = table_header_font
            c.fill = table_header_fill
            c.border = thin_border
            c.alignment = Alignment(horizontal="center")
        row += 1
        for ri, data_row in enumerate(rows_data):
            stripe = PatternFill(start_color="EBF1FA", end_color="EBF1FA",
                                 fill_type="solid") if ri % 2 == 0 else None
            for ci, val in enumerate(data_row, 1):
                c = ws.cell(row=row, column=ci, value=val)
                c.font = body_font
                c.border = thin_border
                c.alignment = wrap
                if stripe:
                    c.fill = stripe
            row += 1

    # ----------------------------------------------------------------
    # Content
    # ----------------------------------------------------------------
    _title("French Retailer Database — Overview & Methodology")

    _section("What is this file?")
    _row("", "This Excel file contains a database of French retailers across 7 channels: "
         "Photo, CE (consumer electronics), MDA (major domestic appliances), SDA (small "
         "domestic appliances), Mobile, Phone Accessories, and Refurb (refurbished/repair). "
         "Data comes from official French government registries. Includes chains, buying "
         "groups, AND independent retailers.")

    _section("Data Sources")
    _table(
        ["Source", "What it provides", "Access"],
        [
            ["INSEE SIRENE", "National business registry: SIRET, company name, trade name, "
             "APE activity code, address, employee count, legal form, HQ flag, creation date",
             "Free API (portail-api.insee.fr)"],
            ["INPI RNE", "Annual accounts: real turnover (chiffre d'affaires), "
             "filed by companies. Updated daily, FY 2017-present.",
             "Free API (data.inpi.fr)"],
            ["BODACC", "Official commercial gazette: filing activity signals "
             "(depot des comptes = business is alive and filing).",
             "Free, no auth needed"],
        ],
    )

    _section("How retailers are discovered")
    _row("Step 1", "Search the SIRENE registry by APE activity codes mapped to each channel (see table below).")
    _row("Step 2", "Keep only active businesses with 10+ employees.")
    _row("Step 3", "Search SIRENE by name for known chains and buying groups per channel "
         "(e.g., FNAC, DARTY for CE) to catch retailers registered under different APE codes.")
    _row("Step 4", "Assign a confidence level based on keyword matching (see below).")
    _row("Step 5", "Enrich with turnover from INPI and filing status from BODACC.")

    _section("APE Codes by Channel")
    _table(
        ["Channel", "APE Code", "Description"],
        [
            ["Photo", "47.78C", "Other specialised retail (photo, optical, precision)"],
            ["CE", "47.43Z", "Audio and video equipment in specialised stores"],
            ["CE", "47.41Z", "Computers, peripherals, and software in specialised stores"],
            ["MDA", "47.54Z", "Electrical household appliances in specialised stores"],
            ["SDA", "47.54Z", "Same code as MDA — appliance stores sell both"],
            ["Mobile", "47.42Z", "Telecommunications equipment in specialised stores"],
            ["Phone Accessories", "47.59B", "Other specialised retail not elsewhere classified"],
            ["Refurb", "95.11Z", "Repair of computers and peripheral equipment"],
            ["Refurb", "95.12Z", "Repair of communication equipment"],
        ],
    )

    _section("Confidence Column")
    _row("High", "APE code matches + company name contains channel keywords + known "
         "retailer + employer. Score >= 70. Very likely relevant.")
    _row("Medium", "APE code matches + some positive signals but no keyword match. "
         "Score 45-69. Probably relevant, worth a quick check.")
    _row("Low", "Weak signals. Score < 45. May not be relevant to this channel.")
    _row("", "Scoring: APE code match +40, keyword match +30, known chain/buying "
         "group +20, established company (PME/ETI/GE) +5, employer +5. Max 100.")
    _row("", "Tip: sort or filter by the 'confidence' column to prioritise your review.")

    _section("Channel Definitions")
    _table(
        ["Channel", "Description", "Examples"],
        [
            ["Photo", "Specialist photography equipment", "Phox, Camara, photo labs"],
            ["CE", "Consumer electronics: TVs, audio, computers, peripherals",
             "Fnac, Darty, Boulanger, LDLC"],
            ["MDA", "Major domestic appliances: washing machines, fridges, ovens, "
             "dishwashers", "Darty, Boulanger, But, Conforama"],
            ["SDA", "Small domestic appliances: kettles, coffee machines, irons, "
             "vacuum cleaners, food processors", "Darty, Boulanger, Fnac"],
            ["Mobile", "Mobile phone specialist retailers and operator stores",
             "Orange, SFR, Bouygues, Apple"],
            ["Phone Accessories", "Phone cases, cables, chargers, screen protectors, "
             "earbuds", "Lick, Fnac, Darty"],
            ["Refurb", "Refurbished, repaired, and second-hand electronics",
             "Back Market, Cash Converters, Easy Cash"],
        ],
    )

    _section("Retailer Type Classification")
    _table(
        ["Type", "How determined", "Examples"],
        [
            ["Chain", "Name matches a known chain pattern OR the same parent SIREN "
             "has 3+ establishments in our data", "FNAC, DARTY, BOULANGER, ORANGE, SFR"],
            ["Buying Group", "Name matches a known French buying group. These are "
             "independent shops that group together for purchasing power.",
             "EXPERT, EURONICS, GITEM, PULSAT, PRO&CIE"],
            ["Independent", "Default: no chain or buying group pattern, and SIREN "
             "has fewer than 3 establishments.", "Single-location shops"],
        ],
    )

    _section("Column Reference")
    _table(
        ["Column", "Description"],
        [
            ["siret", "14-digit unique establishment identifier (SIREN + NIC)"],
            ["siren", "9-digit parent company identifier"],
            ["nic", "5-digit establishment number within the parent SIREN"],
            ["legal_name", "Official registered company name (denominationUniteLegale)"],
            ["trade_name", "Brand/trade name shown on the shop (enseigne)"],
            ["is_hq", "Whether this establishment is the company headquarters (siege)"],
            ["channel", "Which product channel(s) this retailer belongs to"],
            ["confidence", "How certain this retailer belongs to the channel (High/Medium/Low)"],
            ["retailer_type", "Chain, Buying Group, or Independent"],
            ["ape_code", "APE activity code for this establishment"],
            ["ape_code_company", "APE activity code at the parent company level"],
            ["employees_estab", "Employee count range for this establishment (e.g., 10-19)"],
            ["employees_company", "Employee count range for the entire company"],
            ["size_band", "INSEE employee band code for this establishment"],
            ["size_band_company", "INSEE employee band code for the entire company"],
            ["company_category", "PME (small/medium), ETI (mid-cap), or GE (large enterprise)"],
            ["legal_form_code", "Legal form code (e.g., 5710=SAS, 5499=SARL, 1000=Sole trader)"],
            ["is_employer", "Whether the establishment is an employer (O=Yes, N=No)"],
            ["date_created_estab", "Establishment creation date"],
            ["date_created_company", "Parent company creation date"],
            ["turnover_actual", "Real filed turnover from INPI annual accounts"],
            ["turnover_est_range", "Estimated turnover range based on employee count (fallback)"],
            ["turnover_year", "Fiscal year of the turnover figure"],
            ["turnover_source", "Where the turnover data comes from"],
            ["bodacc_filing", "Latest BODACC filing type (depot des comptes = healthy signal)"],
            ["bodacc_last_date", "Date of the latest BODACC filing"],
        ],
    )

    _section("Turnover Data")
    _row("turnover_actual", "Real chiffre d'affaires from INPI annual accounts. "
         "This is what the company declared. Not available for all — some file as "
         "confidential, some are too small to file.")
    _row("turnover_est_range", "Estimated turnover range when no real figure is available. "
         "Uses company-level employee band first, then establishment band, and refines "
         "with company_category (GE = €500M+, ETI = €50M-€500M, PME = €2M-€50M).")
    _row("", "The company_category field from SIRENE helps narrow the estimate: "
         "GE (Grande Entreprise) = large enterprise, ETI = mid-cap, PME = small/medium. "
         "Combined with employee count, this gives a better range than headcount alone.")

    _section("Online & Omni Retail Sheet")
    _row("", "Major e-commerce and hypermarket retailers that sell CE/MDA/Mobile but "
         "register under generic APE codes ('e-commerce' or 'hypermarket'). Manually "
         "curated. Examples: Amazon, Cdiscount, Rue du Commerce, Rakuten.")

    _section("Limitations & Accuracy")
    _table(
        ["Limitation", "Impact"],
        [
            ["APE codes can be wrong", "Companies self-declare and rarely update. "
             "INSEE estimates ~15-20% are inaccurate."],
            ["Hypermarkets are missing", "Carrefour, Auchan, Leclerc sell huge volumes "
             "of electronics but register as 'hypermarket' (47.11F). See Online & Omni sheet."],
            ["E-commerce is missing", "Amazon, Cdiscount register as 'distance selling' "
             "(47.91B). See Online & Omni sheet."],
            ["Size filter", "We exclude < 10 employees. Some real rural shops are smaller."],
            ["Turnover coverage", "Not all companies file public accounts. "
             "Better coverage for larger companies (SA, SAS, SARL)."],
        ],
    )


def build_ape_reference_sheet():
    """Build a DataFrame with APE code descriptions in FR/EN/SV."""
    rows = []
    for code, info in APE_DESCRIPTIONS.items():
        rows.append({
            "APE Code": code,
            "Channel": info["channel"],
            "Queried?": "Yes" if info["queried"] else "No (related code)",
            "Description (FR)": info["fr"],
            "Description (EN)": info["en"],
            "Description (SV)": info["sv"],
        })
    return pd.DataFrame(rows)


# Build a flat keyword list for backward compatibility
KEYWORDS = list({kw for ch in CHANNEL_KEYWORDS.values() for kw in ch["keywords"]})

SIZE_MIN = "11"  # trancheEffectifs code — means 10+ employees

FIELDS = [
    "siret",
    "siren",
    "denominationUniteLegale",
    "numeroVoieEtablissement",
    "typeVoieEtablissement",
    "libelleVoieEtablissement",
    "codePostalEtablissement",
    "libelleCommuneEtablissement",
    "activitePrincipaleEtablissement",
    "trancheEffectifsEtablissement",
    "etatAdministratifEtablissement",
]

BASE_URL = "https://api.insee.fr/api-sirene/3.11/siret"
PAGE_SIZE = 1000

# TEST MODE — set to True to fetch only 5 records per APE code (quick check)
TEST_MODE = False
TEST_LIMIT = 5

# Ordered size bands for comparison
SIZE_BANDS = [
    "NN", "00", "01", "02", "03",
    "11", "12", "21", "22", "31", "32",
    "41", "42", "51", "52", "53",
]

# Human-readable employee count ranges per band code
SIZE_BAND_LABELS = {
    "NN": "Unknown",
    "00": "0 (non-employer)",
    "01": "1-2",
    "02": "3-5",
    "03": "6-9",
    "11": "10-19",
    "12": "20-49",
    "21": "50-99",
    "22": "100-199",
    "31": "200-249",
    "32": "250-499",
    "41": "500-999",
    "42": "1,000-1,999",
    "51": "2,000-4,999",
    "52": "5,000-9,999",
    "53": "10,000+",
}

# ---------------------------------------------------------------------------
# TURNOVER ENRICHMENT CONFIGURATION
# ---------------------------------------------------------------------------
#
# Two sources, tried in priority order (both fully free):
#
# SOURCE 1 — INPI API (free, requires registration at data.inpi.fr)
#   Provides annual accounts directly from the RNE (Registre National des
#   Entreprises).  Updated daily.  Data available from 2017 to present
#   (currently up to fiscal year 2024/2025).  Requires INPI_USERNAME and
#   INPI_PASSWORD.  Quota: 10,000 requests/day.
#
# SOURCE 2 — Employee band estimate (always available, no auth needed)
#   Maps SIRENE trancheEffectifs to rough CE-sector turnover ranges.
#   Used as fallback when INPI returns no figure for a SIREN.
#
# Set SKIP_TURNOVER_API = True to skip INPI API calls and use only the
# employee band estimate.

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
# MAJOR ONLINE & OMNI-CHANNEL RETAILERS — static reference list
# ---------------------------------------------------------------------------
# These retailers sell CE/MDA/Mobile but register under generic APE codes
# (47.11F hypermarkets, 47.19Z department stores, 47.91B e-commerce) or have
# no physical retail APE at all.  They are NOT discovered by our APE queries.
# This list is manually curated from market research.
#
# SIRENs are best-effort — the script attempts a SIRENE API lookup to pull
# live registration data.  If a SIREN is wrong or the lookup fails, the row
# still appears with the static data and blank SIRENE fields.

MAJOR_ONLINE_OMNI_RETAILERS = [
    {
        "name": "Amazon France",
        "siren": "487773882",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Yes",
        "type": "E-commerce",
        "notes": "Dominant across all three. First- and third-party. "
                 "Largest single e-commerce platform in France by GMV.",
    },
    {
        "name": "Cdiscount",
        "siren": "424059332",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Yes",
        "type": "E-commerce",
        "notes": "Casino Group. Strong CE and MDA, significant mobile range. "
                 "Large marketplace. Top 2-3 e-commerce player in France.",
    },
    {
        "name": "Rue du Commerce",
        "siren": "422797857",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Yes",
        "type": "E-commerce",
        "notes": "Carrefour-owned. CE-heavy, good MDA range, mobile present. "
                 "Also a marketplace.",
    },
    {
        "name": "Rakuten France",
        "siren": "432647584",
        "sells_ce": "Yes", "sells_mda": "Partial", "sells_mobile": "Yes",
        "type": "E-commerce",
        "notes": "Primarily marketplace. CE and mobile strong (new and used). "
                 "MDA more limited — bigger items less popular on marketplace model.",
    },
    {
        "name": "Veepee (ex-Vente-Privée)",
        "siren": "434588947",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Partial",
        "type": "E-commerce",
        "notes": "Flash sale model. CE and MDA appear regularly in sales events. "
                 "Mobile handsets less frequent. Brands use for stock clearance.",
    },
    {
        "name": "La Redoute",
        "siren": "477180186",
        "sells_ce": "Partial", "sells_mda": "Yes", "sells_mobile": "No",
        "type": "E-commerce",
        "notes": "Home/lifestyle marketplace. MDA (especially SDA) is meaningful. "
                 "CE more selective. Not a real mobile destination.",
    },
    {
        "name": "LDLC",
        "siren": "403554181",
        "sells_ce": "Yes", "sells_mda": "No", "sells_mobile": "Partial",
        "type": "E-commerce + Stores",
        "notes": "Computing and components specialist. Monitors, peripherals, "
                 "PC hardware strong. Mobile accessories yes, handsets limited. "
                 "Also has physical stores.",
    },
    {
        "name": "Ubaldi",
        "siren": "422496450",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Partial",
        "type": "E-commerce",
        "notes": "Pure-play online, CE and MDA focused. Less well-known but "
                 "genuine volume, particularly in MDA.",
    },
    {
        "name": "ManoMano",
        "siren": "792439493",
        "sells_ce": "No", "sells_mda": "Partial", "sells_mobile": "No",
        "type": "E-commerce",
        "notes": "DIY and garden primary. But built-in appliances (hobs, ovens, "
                 "dishwashers) make it a real MDA channel for encastrable category.",
    },
    {
        "name": "ShowroomPrivé",
        "siren": "538811837",
        "sells_ce": "Partial", "sells_mda": "Partial", "sells_mobile": "No",
        "type": "E-commerce",
        "notes": "Flash sales like Veepee but smaller. CE and SDA appear in "
                 "brand sales events. Not a primary channel.",
    },
    {
        "name": "Costco France",
        "siren": "821227837",
        "sells_ce": "Yes", "sells_mda": "Yes", "sells_mobile": "Partial",
        "type": "Warehouse Club",
        "notes": "9 physical locations + online. Sells TVs, audio, MDA — "
                 "often good value on premium brands. Niche but real account.",
    },
]


# ---------------------------------------------------------------------------
# HELPERS — SIRENE
# ---------------------------------------------------------------------------


def size_band_gte(value, minimum):
    """Return True if *value* is at or above *minimum* in the SIZE_BANDS order."""
    try:
        return SIZE_BANDS.index(value) >= SIZE_BANDS.index(minimum)
    except ValueError:
        return False


def matches_keyword(name, channel=None):
    """Return True if *name* contains at least one keyword for the channel."""
    if not name:
        return False
    upper = name.upper()
    if channel and channel in CHANNEL_KEYWORDS:
        kws = CHANNEL_KEYWORDS[channel]["keywords"]
    else:
        kws = KEYWORDS
    return any(kw in upper for kw in kws)


def _flatten_record(rec):
    """Flatten a v3.11 SIRENE record so historized fields are at top level.

    The API nests historized fields (APE, status, trancheEffectifs, address)
    under periodesEtablissement and adresseEtablissement.  This promotes the
    most-recent period's values and address sub-fields to the top level so
    the rest of the code can access them uniformly.
    """
    flat = dict(rec)
    # Promote latest period fields
    periodes = rec.get("periodesEtablissement") or []
    if periodes:
        latest = periodes[0]
        for key in ("activitePrincipaleEtablissement",
                     "etatAdministratifEtablissement",
                     "trancheEffectifsEtablissement"):
            if key not in flat or not flat[key]:
                flat[key] = latest.get(key, "")
    # Promote uniteLegale fields
    ul = rec.get("uniteLegale") or {}
    if ul:
        for key in ("denominationUniteLegale", "trancheEffectifsUniteLegale"):
            if key not in flat or not flat[key]:
                flat[key] = ul.get(key, "")
    # Promote address sub-fields
    addr2 = rec.get("adresse2Etablissement") or {}
    addr = rec.get("adresseEtablissement") or {}
    for src in (addr, addr2):
        if isinstance(src, dict):
            for key, val in src.items():
                if key not in flat or not flat[key]:
                    flat[key] = val
    return flat


def _calc_confidence(name, channel, ape_code, is_known_retailer=False,
                     retailer_type="", cat_entreprise="", is_employer=""):
    """Calculate a confidence level that this establishment belongs to the channel.

    Scoring (internal, mapped to High/Medium/Low):
      +40  APE code matches the channel (base — they registered under this code)
      +30  Company name matches channel-specific keywords
      +20  Known chain or buying group for this channel
      +5   Company category is PME/ETI/GE (established business)
      +5   Is an employer (has employees on record)

    Returns: "High" (>=70), "Medium" (>=45), or "Low" (<45).
    """
    score = 0
    # Base: APE code assigned to this channel
    if ape_code in APE_CODES or ape_code in APE_CODES_EXTRA:
        score += 40
    # Keyword match
    if matches_keyword(name, channel=channel):
        score += 30
    # Known retailer
    if is_known_retailer:
        score += 20
    # Established business signals
    if cat_entreprise in ("PME", "ETI", "GE"):
        score += 5
    if is_employer in ("O", "true", True):
        score += 5
    score = min(score, 100)
    if score >= 70:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _get_field(rec, field):
    """Get a field that may be at top level or nested under adresseEtablissement."""
    val = rec.get(field)
    if val:
        return val
    addr = rec.get("adresseEtablissement")
    if isinstance(addr, dict):
        return addr.get(field, "")
    return ""


def _extract_row(rec, channel, ape_code, confidence="Medium", retailer_type=""):
    """Extract all useful fields from a flattened SIRENE record into a row dict."""
    ul = rec.get("uniteLegale") or {}
    siret = rec.get("siret", "")
    siren = siret[:9] if len(siret) >= 9 else rec.get("siren", "")

    # Legal name — try multiple fields
    legal_name = (
        rec.get("denominationUniteLegale")
        or ul.get("denominationUniteLegale", "")
    )
    # Trade/brand name (enseigne)
    trade_name = (
        rec.get("enseigne1Etablissement")
        or rec.get("enseigne2Etablissement")
        or rec.get("enseigne3Etablissement")
        or rec.get("denominationUsuelleEtablissement")
        or ul.get("denominationUsuelle1UniteLegale")
        or ""
    )
    # Legal form
    cat_juridique = rec.get("categorieJuridiqueUniteLegale") or ul.get("categorieJuridiqueUniteLegale", "")
    # Company category (PME, ETI, GE)
    cat_entreprise = rec.get("categorieEntreprise") or ul.get("categorieEntreprise", "")
    # Is HQ?
    is_siege = rec.get("etablissementSiege") or ""
    # Creation dates
    date_creation_etab = rec.get("dateCreationEtablissement", "")
    date_creation_ul = rec.get("dateCreationUniteLegale") or ul.get("dateCreationUniteLegale", "")
    # Employee bands — establishment and unit level
    size_band_etab = rec.get("trancheEffectifsEtablissement", "")
    size_band_ul = rec.get("trancheEffectifsUniteLegale") or ul.get("trancheEffectifsUniteLegale", "")
    # APE at unit level (may differ from establishment)
    ape_ul = rec.get("activitePrincipaleUniteLegale") or ul.get("activitePrincipaleUniteLegale", "")
    # Employer flag
    is_employer = rec.get("caractereEmployeurEtablissement", "")
    # SSE (social & solidarity economy)
    sse = rec.get("economieSocialeSolidaireUniteLegale") or ul.get("economieSocialeSolidaireUniteLegale", "")
    # NIC (establishment number within SIREN)
    nic = rec.get("nic", "")

    return {
        "siret": siret,
        "siren": siren,
        "nic": nic,
        "legal_name": legal_name,
        "trade_name": trade_name,
        "is_hq": "Yes" if is_siege in (True, "true", "True") else ("No" if is_siege in (False, "false", "False") else ""),
        "channel": channel,
        "ape_code": ape_code or rec.get("activitePrincipaleEtablissement", ""),
        "ape_code_company": ape_ul,
        "address": build_address(rec),
        "postcode": _get_field(rec, "codePostalEtablissement"),
        "city": _get_field(rec, "libelleCommuneEtablissement"),
        "size_band": size_band_etab,
        "employees_estab": SIZE_BAND_LABELS.get(size_band_etab, size_band_etab),
        "size_band_company": size_band_ul,
        "employees_company": SIZE_BAND_LABELS.get(size_band_ul, size_band_ul),
        "company_category": cat_entreprise,
        "legal_form_code": cat_juridique,
        "is_employer": is_employer,
        "date_created_estab": date_creation_etab,
        "date_created_company": date_creation_ul,
        "retailer_type": retailer_type,
        "confidence": confidence,
    }


def build_address(rec):
    """Concatenate address sub-fields into a single string."""
    # API v3.11 may nest address fields under adresseEtablissement or flatten them
    addr = rec.get("adresseEtablissement")
    if isinstance(addr, dict):
        src = addr
    else:
        src = rec
    parts = [
        src.get("numeroVoieEtablissement", ""),
        src.get("typeVoieEtablissement", ""),
        src.get("libelleVoieEtablissement", ""),
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


# Resolved auth headers — set by setup_sirene_auth() at startup
_sirene_headers = {}


def _try_token_generation():
    """
    Try to get a Bearer token from client_id + client_secret.
    The new portal (portail-api.insee.fr) may use different token endpoints.
    We try several known patterns.
    """
    token_urls = [
        "https://auth.insee.net/auth/realms/apim-gravitee/protocol/openid-connect/token",
        "https://portail-api.insee.fr/token",
        "https://api.insee.fr/token",
    ]
    for url in token_urls:
        try:
            resp = http.post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(SIRENE_CLIENT_ID, SIRENE_CLIENT_SECRET),
                timeout=15,
            )
            if resp.status_code == 200:
                token = resp.json().get("access_token")
                if token:
                    print(f"  [INSEE] Token generated via {url}")
                    return token
        except Exception:
            continue
    return None


def setup_sirene_auth():
    """
    Figure out which auth method to use and build headers.
    Returns True if auth is ready, False if no credentials found.
    """
    global _sirene_headers

    # Option A: direct API key (from portal app page)
    if SIRENE_API_KEY:
        _sirene_headers = {
            "Accept": "application/json",
            "X-INSEE-Api-Key-Integration": SIRENE_API_KEY,
        }
        print("  [INSEE] Using API key (X-INSEE-Api-Key-Integration)")
        return True

    # Option B: client_id + client_secret → generate token, use as API key
    if SIRENE_CLIENT_ID and SIRENE_CLIENT_SECRET:
        print("  [INSEE] Generating token from client_id + client_secret …")
        token = _try_token_generation()
        if token:
            _sirene_headers = {
                "Accept": "application/json",
                "X-INSEE-Api-Key-Integration": token,
            }
            return True
        # If token generation fails, try using client_secret directly as key
        print("  [INSEE] Token generation failed. Trying client_secret as API key …")
        _sirene_headers = {
            "Accept": "application/json",
            "X-INSEE-Api-Key-Integration": SIRENE_CLIENT_SECRET,
        }
        return True

    return False


def fetch_all_for_code(ape_code):
    """Paginate through SIRENE API for a single APE code. Returns (records, total)."""
    headers = _sirene_headers
    page_size = TEST_LIMIT if TEST_MODE else PAGE_SIZE
    # v3.11: historized fields must be wrapped in periode()
    params_base = {
        "q": (
            f"periode(activitePrincipaleEtablissement:{ape_code} "
            f"AND etatAdministratifEtablissement:A)"
        ),
        "nombre": page_size,
    }

    records = []
    curseur = "*"
    total = None

    while True:
        params = {**params_base, "curseur": curseur}
        time.sleep(2)  # 30 req/min limit on new portal
        resp = http.get(BASE_URL, headers=headers, params=params, timeout=30)

        if resp.status_code != 200:
            print(f"  [ERROR] APE {ape_code} — HTTP {resp.status_code}: {resp.text[:200]}")
            return records, 0

        data = resp.json()
        header = data.get("header", {})
        if total is None:
            total = header.get("total", 0)
            if TEST_MODE:
                print(f"  APE {ape_code}: {total} total in registry, "
                      f"fetching {TEST_LIMIT} (TEST MODE)")
            else:
                print(f"  APE {ape_code}: {total} total records found")

        batch = data.get("etablissements", [])
        if not batch:
            break

        records.extend(batch)

        # In test mode, stop after one page
        if TEST_MODE:
            break

        # Use cursor for next page
        curseur_suivant = header.get("curseurSuivant")
        if not curseur_suivant:
            break
        curseur = curseur_suivant

        if len(records) >= total:
            break

    return records, total


def search_by_name(name, limit=20):
    """Search SIRENE for active establishments matching a company name."""
    headers = _sirene_headers
    # Use wildcard search on denominationUniteLegale
    q = f'periode(etatAdministratifEtablissement:A) AND denominationUniteLegale:"{name}"*'
    params = {"q": q, "nombre": limit}
    time.sleep(2)
    try:
        resp = http.get(BASE_URL, headers=headers, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("etablissements", [])
    except Exception as exc:
        print(f"  [WARN] Name search '{name}': {exc}")
    return []


def fetch_known_retailers_for_channel(channel):
    """Search SIRENE for known chains/buying groups for a channel.

    Returns a list of (record, retailer_type) tuples.
    """
    known = CHANNEL_KNOWN_RETAILERS.get(channel, {})
    chains = known.get("chains", [])
    buying_groups = known.get("buying_groups", [])
    results = []
    seen_sirets = set()

    for name in chains:
        if TEST_MODE and len(results) >= TEST_LIMIT:
            break
        recs = search_by_name(name, limit=50)
        for rec in recs:
            siret = rec.get("siret", "")
            if siret and siret not in seen_sirets:
                seen_sirets.add(siret)
                results.append((rec, "Chain"))
        if recs:
            print(f"    Known chain '{name}': {len(recs)} establishments")

    for name in buying_groups:
        if TEST_MODE and len(results) >= TEST_LIMIT:
            break
        recs = search_by_name(name, limit=50)
        for rec in recs:
            siret = rec.get("siret", "")
            if siret and siret not in seen_sirets:
                seen_sirets.add(siret)
                results.append((rec, "Buying Group"))
        if recs:
            print(f"    Known buying group '{name}': {len(recs)} establishments")

    return results


# ---------------------------------------------------------------------------
# HELPERS — Turnover enrichment: INPI API
# ---------------------------------------------------------------------------


def _inpi_authenticate():
    """Authenticate with the INPI API and return a bearer token, or None."""
    if not INPI_USERNAME or not INPI_PASSWORD:
        return None
    try:
        resp = http.post(
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
        resp = http.get(
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

        detail_resp = http.get(
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
# HELPERS — Turnover enrichment: orchestrator
# ---------------------------------------------------------------------------


def load_turnover_data(sirens_needed):
    """
    Try each turnover source in priority order for the given SIRENs.
    Returns dict: siren -> {turnover_eur, year, source}.
    """
    if SKIP_TURNOVER_API:
        return {}
    # In test mode, still try INPI but cap to 10 lookups
    max_lookups = 10 if TEST_MODE else len(sirens_needed)

    turnover_map = {}
    sirens_remaining = set(sirens_needed)

    # --- Source 1: INPI API ---
    if INPI_USERNAME and INPI_PASSWORD:
        print("\n--- Turnover enrichment: INPI API ---")
        token = _inpi_authenticate()
        if token:
            to_query = list(sirens_remaining)[:max_lookups]
            print(f"  [INPI] Authenticated. Querying {len(to_query)} SIRENs …")
            done = 0
            for siren in to_query:
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
        resp = http.get(BODACC_URL, params=params, timeout=15)
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
# HELPERS — Online & omni-channel retailer enrichment
# ---------------------------------------------------------------------------


def enrich_omni_retailers():
    """
    Build a DataFrame of major online/omni retailers from the static list.
    For each, attempt a SIRENE API lookup by SIREN to pull live data.
    Returns a DataFrame ready to write as an Excel sheet.
    """
    if TEST_MODE:
        print("\n--- Online & omni-channel retailers (static only, TEST MODE) ---")
    else:
        print("\n--- Enriching online & omni-channel retailers via SIRENE ---")
    headers = _sirene_headers
    rows = []

    for entry in MAJOR_ONLINE_OMNI_RETAILERS:
        siren = entry["siren"]
        row = {
            "name": entry["name"],
            "siren": siren,
            "siret": "",
            "type": entry["type"],
            "sells_ce": entry["sells_ce"],
            "sells_mda": entry["sells_mda"],
            "sells_mobile": entry["sells_mobile"],
            "ape_code": "",
            "address": "",
            "postcode": "",
            "city": "",
            "size_band": "",
            "status": "",
            "notes": entry["notes"],
        }

        # Try SIRENE lookup by SIREN (skip in test mode)
        if TEST_MODE:
            rows.append(row)
            continue
        try:
            time.sleep(2)  # 30 req/min limit on new portal
            params = {
                "q": f"siren:{siren} AND periode(etatAdministratifEtablissement:A)",
                "nombre": 1,
            }
            resp = http.get(BASE_URL, headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                etabs = data.get("etablissements", [])
                if etabs:
                    rec = _flatten_record(etabs[0])
                    row["siret"] = rec.get("siret", "")
                    row["ape_code"] = rec.get("activitePrincipaleEtablissement", "")
                    row["size_band"] = rec.get("trancheEffectifsEtablissement", "")
                    row["size_band_company"] = rec.get("trancheEffectifsUniteLegale", "")
                    row["company_category"] = rec.get("categorieEntreprise", "")
                    row["legal_form_code"] = rec.get("categorieJuridiqueUniteLegale", "")
                    row["trade_name"] = (
                        rec.get("enseigne1Etablissement")
                        or rec.get("denominationUsuelleEtablissement")
                        or ""
                    )
                    row["status"] = rec.get("etatAdministratifEtablissement", "")
                    row["date_created"] = rec.get("dateCreationEtablissement", "")
                    row["address"] = build_address(rec)
                    row["postcode"] = _get_field(rec, "codePostalEtablissement")
                    row["city"] = _get_field(rec, "libelleCommuneEtablissement")
                    print(f"  [SIRENE] {entry['name']}: found (SIRET {row['siret']}, "
                          f"APE {row['ape_code']})")
                else:
                    print(f"  [SIRENE] {entry['name']}: SIREN {siren} — no active "
                          f"establishments found")
            else:
                print(f"  [SIRENE] {entry['name']}: HTTP {resp.status_code}")
        except Exception as exc:
            print(f"  [SIRENE] {entry['name']}: lookup failed — {exc}")

        rows.append(row)

    print(f"  Done: {sum(1 for r in rows if r['siret'])}/{len(rows)} "
          f"enriched with SIRENE data")
    return pd.DataFrame(rows)


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
    has_any_creds = SIRENE_API_KEY or (SIRENE_CLIENT_ID and SIRENE_CLIENT_SECRET)
    if not has_any_creds:
        print("ERROR: No INSEE credentials found in your .env file.")
        print()
        print("  Option A (easiest) — Simple app:")
        print("    1. Go to https://portail-api.insee.fr")
        print("    2. Create an account (free)")
        print("    3. Create a 'Simple' application")
        print("    4. Subscribe to the API Sirene")
        print("    5. Copy the API key → paste in .env as:")
        print("       SIRENE_API_KEY=your_key_here")
        print()
        print("  Option B — Machine-to-machine app:")
        print("    Same steps but create a 'Machine-to-machine' app.")
        print("    Paste both values in .env:")
        print("       SIRENE_CLIENT_ID=your_client_id")
        print("       SIRENE_CLIENT_SECRET=your_client_secret")
        return

    # Check SSL before any API calls
    _test_ssl()

    print(f"\n{'='*60}")
    print(DATA_NOTICE)
    if TEST_MODE:
        print(f"\n  *** TEST MODE: fetching {TEST_LIMIT} records per APE code ***")
        print(f"  *** INPI turnover capped to 10 lookups ***")
    print(f"{'='*60}\n")

    # Set up SIRENE authentication
    if not setup_sirene_auth():
        print("ERROR: Could not authenticate with INSEE.")
        print("       Try creating a 'Simple' app at https://portail-api.insee.fr")
        return

    # =====================================================================
    # PHASE 1 — Fetch & filter from SIRENE (APE code discovery)
    # =====================================================================
    all_rows = []
    seen_sirets = set()  # track across channels for dedup

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

        # Flatten nested v3.11 response structure
        records = [_flatten_record(r) for r in records]

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

        print(f"  Active: {len(active)}, with size >= {SIZE_MIN}: {len(sized)}")

        # NO keyword filtering — keep all. Confidence % is assigned instead.
        for rec in sized:
            ul = rec.get("uniteLegale") or {}
            name = (
                rec.get("denominationUniteLegale")
                or ul.get("denominationUniteLegale", "")
            )
            cat_e = rec.get("categorieEntreprise") or ul.get("categorieEntreprise", "")
            is_emp = rec.get("caractereEmployeurEtablissement", "")
            conf_pct = _calc_confidence(name, channel, ape_code,
                                        cat_entreprise=cat_e, is_employer=is_emp)

            row = _extract_row(rec, channel, ape_code, confidence=conf_pct)
            siret = row["siret"]
            seen_sirets.add(siret)
            all_rows.append(row)

        high = sum(1 for r in all_rows[-len(sized):] if r["confidence"] == "High")
        print(f"  Kept {len(sized)} records ({high} High confidence)")

    # Process extra APE code -> channel mappings (e.g., 47.54Z -> SDA)
    for ape_code, channel in APE_CODES_EXTRA.items():
        print(f"\n  Extra: assigning APE {ape_code} also to {channel} …")
        # Find records already fetched for this APE code and duplicate for the extra channel
        added = 0
        for existing in list(all_rows):
            if existing["ape_code"] == ape_code and existing["channel"] != channel:
                dup = dict(existing)
                dup["channel"] = channel
                # Recalculate confidence for the new channel
                dup["confidence"] = _calc_confidence(
                    dup["legal_name"], channel, ape_code,
                    cat_entreprise=dup.get("company_category", ""),
                    is_employer=dup.get("is_employer", ""),
                )
                all_rows.append(dup)
                added += 1
        print(f"    Added {added} records to {channel}")

    # =====================================================================
    # PHASE 1b — Search known chains & buying groups per channel
    # =====================================================================
    print("\n" + "=" * 60)
    print("Searching for known retailers by name …")
    print("=" * 60)
    all_channels = sorted(set(list(APE_CODES.values()) + list(APE_CODES_EXTRA.values())))
    for channel in all_channels:
        print(f"\n  [{channel}] known retailers:")
        known_results = fetch_known_retailers_for_channel(channel)
        added = 0
        for rec, rtype in known_results:
            rec = _flatten_record(rec)
            siret = rec.get("siret", "")
            if siret in seen_sirets:
                # Already found — upgrade its confidence
                for existing in all_rows:
                    if existing["siret"] == siret and existing["channel"] == channel:
                        existing["confidence"] = "High"
                        if not existing["retailer_type"]:
                            existing["retailer_type"] = rtype
                continue
            ul = rec.get("uniteLegale") or {}
            name = rec.get("denominationUniteLegale") or ul.get("denominationUniteLegale", "")
            ape = rec.get("activitePrincipaleEtablissement", "")
            cat_e = rec.get("categorieEntreprise") or ul.get("categorieEntreprise", "")
            is_emp = rec.get("caractereEmployeurEtablissement", "")
            conf_pct = _calc_confidence(name, channel, ape, is_known_retailer=True,
                                         retailer_type=rtype, cat_entreprise=cat_e,
                                         is_employer=is_emp)
            row = _extract_row(rec, channel, "", conf_pct, rtype)
            seen_sirets.add(siret)
            all_rows.append(row)
            added += 1
        print(f"  [{channel}] Added {added} new establishments from known retailer search")

    if not all_rows:
        print("\nNo records collected. Check your credentials and network.")
        return

    df = pd.DataFrame(all_rows)

    # =====================================================================
    # PHASE 2 — Retailer type classification
    # =====================================================================
    siren_counts = Counter(df["siren"])

    def _classify(row):
        # Keep pre-classified type from known retailer search
        if row.get("retailer_type"):
            return row["retailer_type"]
        return classify_retailer_type(
            (row["legal_name"] or "").upper(),
            row["siren"],
            siren_counts,
        )

    df["retailer_type"] = df.apply(_classify, axis=1)

    # =====================================================================
    # PHASE 3 — Channel collapsing & deduplication
    # =====================================================================
    def collapse(group):
        channels = sorted(set(group["channel"]))
        ape_codes = sorted(set(group["ape_code"]))
        result = group.iloc[0].copy()
        result["channel"] = " | ".join(channels)
        result["ape_code"] = " | ".join(ape_codes)
        # Keep best confidence level (High > Medium > Low)
        conf_vals = group["confidence"].values
        if "High" in conf_vals:
            result["confidence"] = "High"
        elif "Medium" in conf_vals:
            result["confidence"] = "Medium"
        else:
            result["confidence"] = "Low"
        return result

    df = (
        df.groupby("siret", group_keys=False)
        .apply(collapse)
        .reset_index(drop=True)
    )

    # =====================================================================
    # PHASE 4 — Turnover enrichment (INPI API -> band estimate fallback)
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

    def _estimate_turnover(row):
        """Better estimate using size_band + company_category + size_band_company."""
        if row.get("turnover_display"):
            return ""  # already have actual data
        # Try company-level band first (bigger picture), fall back to establishment
        band = row.get("size_band_company") or row.get("size_band", "")
        est = TURNOVER_ESTIMATE_BY_BAND.get(band, "")
        if not est:
            est = TURNOVER_ESTIMATE_BY_BAND.get(row.get("size_band", ""), "")
        # Refine with company category
        cat = row.get("company_category", "")
        if cat == "GE" and not est:
            est = "€500M+"
        elif cat == "ETI" and not est:
            est = "€50M – €500M"
        elif cat == "PME" and not est:
            est = "€2M – €50M"
        return est

    df["turnover_estimate"] = df.apply(_estimate_turnover, axis=1,
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
    # PHASE 6 — Online & omni-channel retailers (static + SIRENE lookup)
    # =====================================================================
    omni_df = enrich_omni_retailers()

    # =====================================================================
    # PHASE 7 — Output (per-channel sheets + All + Online/Omni + Metadata)
    # =====================================================================
    output_cols = [
        "siret", "siren", "nic",
        "legal_name", "trade_name", "is_hq",
        "channel", "confidence", "retailer_type",
        "ape_code", "ape_code_company",
        "address", "postcode", "city",
        "employees_estab", "employees_company",
        "size_band", "size_band_company", "company_category",
        "legal_form_code", "is_employer",
        "date_created_estab", "date_created_company",
        "turnover_actual", "turnover_est_range", "turnover_year",
        "turnover_source",
        "bodacc_filing", "bodacc_last_date",
    ]

    df = df.rename(columns={
        "turnover_display": "turnover_actual",
        "turnover_estimate": "turnover_est_range",
    })
    # Ensure all columns exist
    for col in output_cols:
        if col not in df.columns:
            df[col] = ""
    df = df[output_cols]

    # Sort: highest confidence first within each channel
    df = df.sort_values(["channel", "confidence", "legal_name"],
                        ascending=[True, False, True]).reset_index(drop=True)

    # Build per-channel DataFrames.  A retailer tagged "CE | Photo" appears
    # in both the CE sheet and the Photo sheet.
    channel_names = sorted(set(APE_CODES.values()))
    channel_dfs = {}
    for ch in channel_names:
        mask = df["channel"].str.contains(ch, regex=False)
        ch_df = df[mask].copy()
        if not ch_df.empty:
            channel_dfs[ch] = ch_df

    output_file = "france_retailers.xlsx"
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        # One sheet per channel
        for ch in channel_names:
            if ch in channel_dfs:
                sheet_name = ch.replace("/", "-")[:31]  # / invalid in Excel
                channel_dfs[ch].to_excel(writer, index=False, sheet_name=sheet_name)

        # Combined sheet with all retailers
        df.to_excel(writer, index=False, sheet_name="All Retailers")

        # Online & omni-channel retailers (static list + SIRENE enrichment)
        omni_df.to_excel(writer, index=False, sheet_name="Online & Omni Retail")

        # APE code reference sheet (FR / EN / SV descriptions)
        ape_ref_df = build_ape_reference_sheet()
        ape_ref_df.to_excel(writer, index=False, sheet_name="APE Code Reference")

        # Metadata sheet
        meta_rows = [
            ("Notice", DATA_NOTICE),
            ("Generated", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Source — retailers",
             "INSEE SIRENE API v3.11 — national business registry"),
            ("Source — online/omni retailers",
             "Static reference list based on market research. SIRENE API used "
             "to enrich with live registration data where SIREN is known. "
             "NOT discovered by APE code query — manually curated."),
            ("Source — turnover (primary)",
             "INPI RNE API (data.inpi.fr) — actual chiffre d'affaires "
             "from filed annual accounts, updated daily, covers FY 2017-present. "
             "Free, requires registration."),
            ("Source — turnover (fallback)",
             "Employee band estimate — SIRENE trancheEffectifs mapped "
             "to CE-sector turnover ranges. These are rough directional "
             "estimates, NOT real figures."),
            ("Source — filing status",
             "BODACC (bodacc-datadila.opendatasoft.com) — free, no auth. "
             "Official gazette, depot des comptes = company is alive and filing."),
            ("Size filter", f"trancheEffectifs >= {SIZE_MIN}"),
            ("APE codes queried", ", ".join(APE_CODES.keys())),
            ("Confidence — High (green)",
             "Company name matches channel-specific keywords OR is a known "
             "chain/buying group for that channel. Very likely relevant."),
            ("Confidence — Medium (yellow)",
             "Correct APE code and size, but name does not match channel "
             "keywords. Probably relevant but worth a quick manual check."),
            ("Retailer type — Chain",
             "Matched against known chain name list (FNAC, DARTY, BOULANGER, "
             "ORANGE, SFR, etc.) OR SIREN has >= "
             f"{CHAIN_SIREN_THRESHOLD} establishments in dataset. "
             "This is OUR classification, not from any API."),
            ("Retailer type — Buying Group",
             "Matched against known buying group name list (EXPERT, EURONICS, "
             "GITEM, PRO&CIE, PULSAT, CONNEXION, etc.). "
             "This is OUR classification, not from any API."),
            ("Retailer type — Independent",
             "Default when no chain or buying group pattern matches and SIREN "
             f"has < {CHAIN_SIREN_THRESHOLD} establishments."),
        ]
        meta = pd.DataFrame(meta_rows, columns=["Field", "Value"])
        meta.to_excel(writer, index=False, sheet_name="Metadata")

    # -----------------------------------------------------------------
    # Add styled overview sheet
    # -----------------------------------------------------------------
    from openpyxl import load_workbook

    wb = load_workbook(output_file)
    _write_styled_overview(wb)

    wb.save(output_file)
    wb.close()

    print(f"\nWrote {len(df)} rows to {output_file}")
    print(f"  Sheets: {', '.join(ch for ch in channel_names if ch in channel_dfs)}, "
          f"All Retailers, Online & Omni Retail, How This Works, "
          f"APE Code Reference, Metadata")

    # ----- Summary -----
    print(f"\n{'='*60}")
    print(DATA_NOTICE)
    print(f"{'='*60}")

    print("\n--- Channel breakdown (per sheet) ---")
    for ch in channel_names:
        count = channel_dfs[ch].shape[0] if ch in channel_dfs else 0
        print(f"  {ch:15s}: {count:>5}")

    print("\n--- Retailer type breakdown ---")
    for rtype, count in df["retailer_type"].value_counts().items():
        print(f"  {rtype:15s}: {count:>5}")

    n_actual = (df["turnover_actual"] != "").sum()
    n_est = (df["turnover_est_range"] != "").sum()
    n_bodacc = (df["bodacc_filing"] == "Yes").sum()
    print("\n--- Turnover enrichment ---")
    print(f"  Actual CA (INPI)        : {n_actual:>5} SIRETs  (real filed figures)")
    print(f"  Band estimate (fallback): {n_est:>5} SIRETs  (our rough estimate)")
    print(f"  No turnover info        : {len(df) - n_actual - n_est:>5} SIRETs")
    print(f"  BODACC filing detected  : {n_bodacc:>5} SIRENs  (health signal only)")

    omni_enriched = (omni_df["siret"] != "").sum() if not omni_df.empty else 0
    print(f"\n--- Online & omni-channel retailers ---")
    print(f"  Curated list          : {len(omni_df):>5} retailers")
    print(f"  SIRENE-enriched       : {omni_enriched:>5} (live data found)")

    print(f"\n  Total unique SIRETs (specialist): {len(df)}")
    print(f"  Total unique SIRENs (specialist): {df['siren'].nunique()}")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        TEST_MODE = True
    main()
