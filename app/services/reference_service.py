from __future__ import annotations

"""
reference_service.py
--------------------
Loads the reference CSV (filename → expected bank details) once at startup,
then provides a compare() function used by the extraction router.

CSV expected columns (case-insensitive, extra columns are ignored):
    filename, bank, fi code, masteracc, subacc

The `filename` column stores values like "BBB-001227-24" (no extension).
Uploaded filenames like "JSB-000486-25_extracted.txt" are normalised to
"JSB-000486-25" before lookup.
"""

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

from models.schemas import ComparisonResult, FieldComparisonDetail

logger = logging.getLogger(__name__)

REFERENCE_CSV_PATH = Path("data/reference.csv")

# ---------------------------------------------------------------------------
# Canonical bank name map
# Approved output list:
#   Affin Bank, Al rajhi Bank, Alliance Bank, Alliance Islamic Bank,
#   Ambank, Ambank Islamic, Bank Islam, Bank Muamalat, Bank of China,
#   Bank Rakyat, BSN, CIMB Bank, Hong Leong Bank, Hong Leong Islamic Bank,
#   HSBC Amanah, HSBC Bank, Kuwait Finance House, Maybank, Maybank Islamic,
#   MBSB Bank, OCBC, OCBC Al-Amin, RHB Bank, RHB Islamic Bank,
#   Standard Chartered Bank, Standard Chartered Saadiq Islamic, UOB
# ---------------------------------------------------------------------------
_BANK_CANONICAL: dict[str, str] = {
    # BSN
    "bsn":                              "BSN",
    "banksimpanannasional":             "BSN",
    "banksimpanannasionalberhad":       "BSN",

    # Maybank
    "maybank":                          "Maybank",
    "maybankberhad":                    "Maybank",
    "malayanbankingberhad":             "Maybank",
    "maybankislamic":                   "Maybank Islamic",
    "maybankislamicberhad":             "Maybank Islamic",

    # CIMB — no Islamic entry; both map to "CIMB Bank"
    "cimb":                             "CIMB Bank",
    "cimbbank":                         "CIMB Bank",
    "cimbbankberhad":                   "CIMB Bank",
    "cimbislamic":                      "CIMB Bank",
    "cimbislamicberhad":                "CIMB Bank",

    # Alliance
    "alliancebank":                     "Alliance Bank",
    "alliancebankberhad":               "Alliance Bank",
    "alliancebankmalaysiaberhad":       "Alliance Bank",
    "alliancefinanceberhad":            "Alliance Bank",
    "allianceislamicbank":              "Alliance Islamic Bank",
    "allianceislamicbankberhad":        "Alliance Islamic Bank",

    # Hong Leong
    "hongleongbank":                    "Hong Leong Bank",
    "hongleongbankberhad":              "Hong Leong Bank",
    "hongleongislamicbank":             "Hong Leong Islamic Bank",
    "rhb":                              "RHB Bank",
    "rhbbank":                          "RHB Bank",
    "rhbbankberhad":                    "RHB Bank",
    "rhbislamicbank":                   "RHB Islamic Bank",
    "rhbislamicbankberhad":             "RHB Islamic Bank",

    # Ambank (lowercase b)
    "ambank":                           "Ambank",
    "ambankberhad":                     "Ambank",
    "ambankmberhad":                    "Ambank",
    "ammbankberhad":                    "Ambank",
    "ambankmalaysiaberhad":             "Ambank",
    "ambankislamic":                    "Ambank Islamic",
    "ambankislamicberhad":              "Ambank Islamic",
    "ambankislamicmalaysiaberhad":      "Ambank Islamic",

    # HSBC
    "hsbcbank":                         "HSBC Bank",
    "hsbcbankberhad":                   "HSBC Bank",
    "hsbcbankmalaysiaberhad":           "HSBC Bank",
    "hsbcamanah":                       "HSBC Amanah",
    "hsbcamanahmalaysiaberhad":         "HSBC Amanah",

    # OCBC — "OCBC Bank" → "OCBC" (no "Bank" in canonical form)
    "ocbc":                             "OCBC",
    "ocbcbank":                         "OCBC",
    "ocbcbankberhad":                   "OCBC",
    "ocbcbankmalaysiaberhad":           "OCBC",
    "ocbcalamin":                       "OCBC Al-Amin",
    "ocbcalaminbankberhad":             "OCBC Al-Amin",
    "ocbcalaminberhad":                 "OCBC Al-Amin",

    # Kuwait Finance House
    "kuwaitfinancehouse":               "Kuwait Finance House",
    "kuwaitfinancehouseberhad":         "Kuwait Finance House",
    "kfh":                              "Kuwait Finance House",
    "kfhmalaysiaberhad":                "Kuwait Finance House",

    # Standard Chartered
    "standardcharteredbank":                    "Standard Chartered Bank",
    "standardchartered":                        "Standard Chartered Bank",
    "standardcharteredbankmalaysiaberhad":      "Standard Chartered Bank",
    "standardchartteredsaadiq":                 "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqberhad":           "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqislamic":          "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqislamicberhad":    "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqmalaysiaberhad":   "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqislamicmalaysia":  "Standard Chartered Saadiq Islamic",
    "standardchartteredsaadiqislamicmalaysiaberhad": "Standard Chartered Saadiq Islamic",

    # Bank Islam / Muamalat
    "bankislam":                        "Bank Islam",
    "bankislammalaysiaberhad":          "Bank Islam",
    "bankmuamalat":                     "Bank Muamalat",
    "bankmuamalatmalaysiaberhad":       "Bank Muamalat",

    # Affin — no Islamic entry in approved list; both map to "Affin Bank"
    "affinbank":                        "Affin Bank",
    "affinbankberhad":                  "Affin Bank",
    "affinislamicbank":                 "Affin Bank",
    "affinislamicbankberhad":           "Affin Bank",

    # Bank Rakyat
    "bankrakyat":                       "Bank Rakyat",
    "bankrakyatberhad":                 "Bank Rakyat",
    "bankkeperjaanrakyat":              "Bank Rakyat",
    "bankkeperjaanrakyatberhad":        "Bank Rakyat",

    # UOB — canonical is "UOB" only, no "Bank"
    "uob":                              "UOB",
    "uobbank":                          "UOB",
    "uobbankberhad":                    "UOB",
    "unitedoverseasbank":               "UOB",
    "unitedoverseasbankberhad":         "UOB",
    "unitedoverseasbankmalaysiaberhad": "UOB",
    "unitedoverseasbank(malaysia)bhd":  "UOB",

    # MBSB
    "mbsb":                             "MBSB Bank",
    "mbsbbank":                         "MBSB Bank",
    "mbsbbankberhad":                   "MBSB Bank",
    "malaysiabuildingsociety":          "MBSB Bank",
    "malaysiabuildingsocietyberhad":    "MBSB Bank",

    # Bank of China
    "bankofchina":                      "Bank of China",
    "bankofchinaberhad":                "Bank of China",
    "bankofchinamalaysiaberhad":        "Bank of China",

    # Al Rajhi Bank
    "alrajhibank":                      "Al Rajhi Bank",
    "alrajhibank":                      "Al Rajhi Bank",
    "alrajhibankberhad":                "Al Rajhi Bank",
    "alrajhibankmaylaysiaberhad":       "Al Rajhi Bank",
    "alrajhibankmalaysiaberhad":        "Al Rajhi Bank",
    "alrajhibanking&ampinvestmentcorporation(malaysia)bhd": "Al Rajhi Bank",

    # Public Bank (sender — should not appear as answer, kept for completeness)
    "publicbankberhad":                 "Public Bank Berhad",
    "publicbank":                       "Public Bank Berhad",
    "publicislamicbank":                "Public Islamic Bank",
    "publicislamicbankberhad":          "Public Islamic Bank",
}


def _canonical_bank(raw: str | None) -> str | None:
    """
    Normalise a raw bank name string to its canonical form.
    """
    if not raw:
        return raw
    key = re.sub(r'[^a-z0-9]', '', raw.lower())
    return _BANK_CANONICAL.get(key, raw.strip())


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def _normalise_key(raw: str) -> str:
    name = Path(raw).stem
    name = re.sub(r'_extracted$', '', name, flags=re.IGNORECASE).strip()
    return name


def _normalise_header(h: str) -> str:
    return h.strip().lower().replace(" ", "")


def _load_csv(path: Path) -> dict[str, dict]:
    if not path.exists():
        logger.warning("Reference CSV not found at %s — comparisons will be skipped", path)
        return {}

    records: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            logger.warning("Reference CSV appears empty")
            return {}

        header_map = {_normalise_header(h): h for h in reader.fieldnames}

        col = {
            "filename":  header_map.get("filename"),
            "bank":      header_map.get("bank"),
            "fi_code":   header_map.get("ficode") or header_map.get("fi_code") or header_map.get("finum"),
            "masteracc": header_map.get("masteracc") or header_map.get("master_account_number"),
            "subacc":    header_map.get("subacc") or header_map.get("sub_account_number"),
        }

        missing = [k for k, v in col.items() if v is None]
        if missing:
            logger.warning("Reference CSV is missing expected columns: %s", missing)

        for row in reader:
            raw_filename = row.get(col["filename"] or "", "").strip()
            if not raw_filename:
                continue
            key = _normalise_key(raw_filename)
            records[key] = {
                "filename":  raw_filename,
                "bank":      row.get(col["bank"] or "", "").strip() if col["bank"] else None,
                "fi_code":   row.get(col["fi_code"] or "", "").strip() if col["fi_code"] else None,
                "masteracc": row.get(col["masteracc"] or "", "").strip() if col["masteracc"] else None,
                "subacc":    row.get(col["subacc"] or "", "").strip() if col["subacc"] else None,
            }

    logger.info("Loaded %d rows from reference CSV at %s", len(records), path)
    return records


@lru_cache(maxsize=1)
def get_reference_data() -> dict[str, dict]:
    return _load_csv(REFERENCE_CSV_PATH)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm_fi(v: str | None) -> str:
    """
    Normalise a FI code for comparison.

    The reference CSV and LLM output may differ in separator style only:
      "0227_13014"  vs  "0227-13014"  vs  "022713014"

    Strategy:
      1. Strip all whitespace, underscores, and hyphens → pure digit/alpha string
      2. Compare the stripped forms

    Leading zeros ARE significant — do not strip them.
    """
    if v is None:
        return ""
    # Remove spaces, underscores, hyphens only — keep all other chars (digits, letters)
    return re.sub(r'[\s_\-]', '', v).lower()


def _norm_account(v: str | None) -> str:
    """
    Normalise an account number for comparison.

    Account numbers in this system can contain:
      - Pure digits with leading zeros:    "0000008013080047172"
      - Alphanumeric with slashes/dashes:  "KIP/MG/2007/00000332212001"
      - HSBC-style with suffix:            "316222116O/D88"
      - BSN-style with dashes:             "07215-72-952646-82"
      - OCBC-style:                        "740-407580-8-00000"

    Strategy: strip only whitespace — keep all other chars (digits, letters,
    slashes, dashes) but lowercase everything so casing doesn't matter.

    This preserves the structural characters that distinguish account numbers
    while allowing the LLM output and reference to differ only in case.
    """
    if v is None:
        return ""
    return re.sub(r'\s', '', v).lower()


def _norm_account_loose(v: str | None) -> str:
    """
    Extra-loose normalisation: strip ALL non-alphanumeric characters.
    Used as a fallback when strict normalisation fails, to catch cases where
    the LLM drops separators (e.g. "316222116OD88" vs "316222116O/D88").
    """
    if v is None:
        return ""
    return re.sub(r'[^a-z0-9]', '', v.lower())


def _field_match(extracted: str | None, expected: str | None) -> FieldComparisonDetail:
    """
    Match with strict normalisation first, then loose fallback.
    Reports the match result but always stores the original extracted value.
    """
    strict_match = _norm_account(extracted) == _norm_account(expected)
    loose_match  = _norm_account_loose(extracted) == _norm_account_loose(expected)
    match = strict_match or loose_match

    if not strict_match and loose_match:
        logger.debug(
            "Loose account match: extracted=%r expected=%r", extracted, expected
        )

    return FieldComparisonDetail(
        extracted=extracted,
        expected=expected,
        match=match,
    )


def _fi_field_match(extracted: str | None, expected: str | None) -> FieldComparisonDetail:
    """
    Match FI codes with separator-agnostic normalisation.
    """
    match = _norm_fi(extracted) == _norm_fi(expected)
    return FieldComparisonDetail(
        extracted=extracted,
        expected=expected,
        match=match,
    )


def _bank_field_match(extracted: str | None, expected: str | None) -> FieldComparisonDetail:
    """
    Like _field_match but first canonicalises the extracted bank name.
    """
    canonical_extracted = _canonical_bank(extracted)
    match = _norm_account_loose(canonical_extracted) == _norm_account_loose(expected)
    return FieldComparisonDetail(
        extracted=canonical_extracted,
        expected=expected,
        match=match,
    )


def compare_extraction(
    filename_raw: str,
    bank_name: str | None,
    fi_num: str | None,
    master_account_number: str | None,
    sub_account_number: str | None,
) -> ComparisonResult:
    key = _normalise_key(filename_raw)
    reference = get_reference_data()
    row = reference.get(key)

    if row is None:
        logger.warning("Filename key '%s' not found in reference CSV", key)
        return ComparisonResult(
            filename_key=key,
            csv_row_found=False,
            bank_name=FieldComparisonDetail(extracted=_canonical_bank(bank_name), expected=None, match=False),
            fi_num=FieldComparisonDetail(extracted=fi_num, expected=None, match=False),
            master_account_number=FieldComparisonDetail(extracted=master_account_number, expected=None, match=False),
            sub_account_number=FieldComparisonDetail(extracted=sub_account_number, expected=None, match=False),
            all_match=False,
        )

    bank_cmp   = _bank_field_match(bank_name,            row.get("bank"))
    fi_cmp     = _fi_field_match(fi_num,                 row.get("fi_code"))
    master_cmp = _field_match(master_account_number,     row.get("masteracc"))
    sub_cmp    = _field_match(sub_account_number,        row.get("subacc"))

    all_match = all([
        bank_cmp.match,
        fi_cmp.match,
        master_cmp.match,
        sub_cmp.match,
    ])

    return ComparisonResult(
        filename_key=key,
        csv_row_found=True,
        bank_name=bank_cmp,
        fi_num=fi_cmp,
        master_account_number=master_cmp,
        sub_account_number=sub_cmp,
        all_match=all_match,
    )
