from __future__ import annotations

_KNOWN_BANKS = """\
- Affin Bank
- Al Rajhi Bank
- Alliance Bank
- Alliance Islamic Bank
- Ambank
- Ambank Islamic
- Bank Islam
- Bank Muamalat
- Bank of China
- Bank Rakyat
- BSN
- CIMB Bank
- Hong Leong Bank
- Hong Leong Islamic Bank
- HSBC Amanah
- HSBC Bank
- Kuwait Finance House
- Maybank
- Maybank Islamic
- MBSB Bank
- OCBC
- OCBC Al-Amin
- RHB Bank
- RHB Islamic Bank
- Standard Chartered Bank
- Standard Chartered Saadiq Islamic
- UOB"""


def build_extraction_prompt(text: str) -> str:
    """Prompt for customer detail extraction."""
    return f"""\
You are a data extraction assistant for a Malaysian bank's internal system.
Your task is to extract exactly six fields from a bank document.

=== RULES ===
0. You are in JSON-only mode. Your entire response must be a single JSON object. Stop immediately after the closing brace. No introduction, no explanation, no conclusion.
1. Return ONLY a raw JSON object — no markdown, no code blocks, no explanation.
2. Use the full context of the document to determine the correct values.
3. Do NOT repeat or explain the fields. Output the JSON object and nothing else.

================================================================================
FIELD 1 — NAME
================================================================================
- The full name of the customer / account holder.
- Malaysian names are typically printed in FULL CAPITAL LETTERS.
- Common formats:
    Malay  : AHMAD BIN HASSAN, SITI BINTI ALI
    Chinese: LEE CHONG WEI, TAN AH KOW
    Indian : RAMESH A/L RAJENDRAN, KAVITHA A/P SUBRAMANIAM
- Ignore names that are clearly a bank branch, law firm, company, or staff member.

================================================================================
FIELDS 2, 3, 4 — FI CODE, MASTER ACCOUNT, SUB ACCOUNT
================================================================================
These three values always appear together in a dedicated section near the top or
bottom of the document, under a heading such as:
    "FI CODE (ONLY APPLICABLE FOR REFINANCING)"
    "FACILITY ACCOUNT NO.", "ACCOUNT DETAILS", "CCRIS", or similar.
    They may also appear in a header table near the borrower/account details.

The section ALWAYS lists the three values with these logical roles:
    FI Code              → fi_num
    Master Account No.   → master_account_number
    Sub Account No.      → sub_account_number

This section appears in ONE OF SIX layouts. Detect which one is present:

--- LAYOUT A: pipe/markdown table ---
All three values appear as cells in a single table row.
The SHORTEST purely-numeric value in the row is ALWAYS the fi_num, regardless
of column position. The table header or column order may differ between documents.
Example (fi_num on the right because it is shorter):
    | 71259000031266 | 021812590 |
      ^master_acc      ^fi_num
Example (fi_num on the left):
    | 022204026 | 205340722O/D88 | 342385069101SLUMYR |
      ^fi_num     ^master_acc      ^sub_acc
Always assign fi_num to the SHORTEST value. If only two values appear in the
table row, there is no sub account — return null for sub_account_number.

--- LAYOUT B: labeled key-value lines ---
Each value has its own line with an explicit label.
Example:
    FI CODE :            034907013
    MASTER ACCOUNT NO :  88820006220322
    SUB ACCOUNT NO :     00088820006220322

--- LAYOUT C: labeled key-value with irregular / inline punctuation ---
Labels and values appear on the same line separated by repeated colons or
run-together without clean spacing. Identify each value by its nearest label
keyword (FI CODE / MASTER ACCOUNT / SUB ACCOUNT) and take everything between
that label and the next label keyword.
Example:
    Fi CODE : 020807012 : MASTER ACCOUNT NO : 250600011808 : SUB ACCOUNT NO : 00000250600011808

--- LAYOUT D: lettered list  a) / b) / c) ---
The three values are listed under lettered items, each with its own label.
Read each lettered label independently — do NOT assume two items share a value.
Example:
    a) Fi Code           : 022612078
    b) Master Account No : 3000170346
    c) Sub Account No    : 387805903200000
    → fi_num = "022612078"
    → master_account_number = "3000170346"   ← value on the (b) line ONLY
    → sub_account_number    = "387805903200000"  ← value on the (c) line ONLY
CRITICAL: master_account_number is the value that follows the "Master Account"
label. sub_account_number is the value that follows the "Sub Account" label.
These are TWO DIFFERENT values from TWO DIFFERENT lines. Never assign the sub
account value to master_account_number. Never copy (c)'s value into (b)'s field.

--- LAYOUT F: account listed first under "Account(s)", FI code below ---
Some documents show the account number first (under "Account No" or "Account(s)")
and the FI code on a separate line below it, with no "Master/Sub" label split.
In this case both master and sub account are the SAME value — the account number
shown under "Account(s)". The FI code appears below it labelled "Fi Code" or
"FI CODE" and is the shorter value starting with 0 or 3.
CRITICAL: Do NOT assign the FI code value to master/sub account, and do NOT
assign the account number to fi_num. Use the LABEL to determine the role,
not the position or length when labels are present.
Example:
    Account(s) : 387805903200000
    Fi Code    : 022612078
    → fi_num = "022612078"
    → master_account_number = "387805903200000"
    → sub_account_number    = "387805903200000"  (same as master when only one account listed)
Some documents list CCRIS fields where each line contains multiple values
separated by commas. Take values as follows:
    - fi_num              ← from the "CCRIS Fi Code" line: the value that starts
                            with 0 or 3 (ignore any value starting with 1 or other digits)
    - master_account_number ← from the "CCRIS Master Account No" line:
                            take the FIRST value before the first comma;
                            copy EXACTLY as printed including spaces and
                            alphanumeric suffixes (e.g. "120 116000030001 22981DS1001")
    - sub_account_number  ← from the "CCRIS Sub-Account No" line:
                            take the FIRST value before the first comma;
                            strip any leading colon or punctuation character
Example:
    CCRIS Fl Code             : 1035312016, 035312016
    CCRIS Master Account No.  : 120 116000030001 22981DS1001, 120 116000030001 22981DS6001
    CCRIS Sub-Account No.     : 620100052154291, 620100052154309
    → fi_num = "035312016"  (starts with 0, not 1)
    → master_account_number = "120 116000030001 22981DS1001"  (first value, exact)
    → sub_account_number = "620100052154291"  (first value, strip leading colon)

In ALL layouts the logical roles are:
    fi_num → master_account_number → sub_account_number

--- FI CODE (fi_num) ---
- A short institution/routing code — typically 7 to 9 digits.
- ALWAYS the shortest of the three values.
- Starts with 0 (most banks) or 3 (BSN, Bank Rakyat). NEVER starts with 1.
- May contain underscores or hyphens (Maybank only): "0227_13014", "0227-11038".
- NOT a phone number, postcode, reference number, or RENTAS clearing account.
- If multiple comma-separated candidates exist, choose the one starting with 0 or 3.
- If unsure between two candidates, choose the shorter purely-numeric one.

--- MASTER ACCOUNT NUMBER ---
- The primary loan/account identifier for the third-party bank.
- Usually longer than the FI code; may contain letters, slashes, suffixes, or spaces.
- Copy EXACTLY as printed — preserve all characters: leading zeros, letters,
  slashes, dashes, spaces, and suffixes (e.g. "120 116000030001 22981DS1001").
- If multiple comma-separated values appear on the master account line, take
  the FIRST value only.
- Strip any parenthetical descriptors such as "(Term Financing-i)" that follow
  the account number — copy only the account identifier itself.
- NEVER use the RENTAS/IBG payment account as the master account.
  The RENTAS account is labelled under a "RENTAS" or "IBG" payment instruction
  block (e.g. "Account No. 309-909570-005") and belongs to the bank's internal
  clearing system. It will NOT appear inside the FI Code section.

--- SUB ACCOUNT NUMBER ---
- A secondary account number in the same section, listed after the master account.
- Often differs from the master by a prefix, suffix, or leading zeros.
  Example: master = "205340722O/D88", sub = "342385069101SLUMYR" (different format).
- Some master and sub account numbers can also be the same number on some banks.
- Copy EXACTLY as printed.
- If multiple comma-separated values appear on the sub account line, take the
  FIRST value only; strip any leading colon or punctuation character.
- Strip any parenthetical descriptors such as "(Term Financing-i)".
- COMBINED LABEL RULE: If the document uses a combined label such as
  "MASTER / SUB ACCOUNT NO." or "MASTER/SUB ACCOUNT NO" followed by a single
  account number, then BOTH master_account_number AND sub_account_number are
  that same single value. A digit immediately after "NO." (e.g. "NO. 2") is a
  footnote reference marker — it is NOT the sub account value. Strip it.
  Example:
      MASTER / SUB ACCOUNT NO. 2 : 172-412188-7-00000(Term Financing-i)
      → master_account_number = "172-412188-7-00000"
      → sub_account_number    = "172-412188-7-00000"  (same value)
- Return null ONLY if the section genuinely contains no account value at all.
- Do NOT duplicate the master account number as the sub account unless the
  document explicitly repeats the same value for both fields OR uses a combined
  "MASTER / SUB" label.

================================================================================
FIELD 5 — ADDRESS
================================================================================
- The customer's mailing or residential address.
- May span multiple lines — combine into one string separated by commas.
- Ignore bank branch addresses and law firm / third-party addresses (cc: blocks).
- Return null if no customer address is present.

================================================================================
FIELD 6 — BANK NAME
================================================================================
These documents are letters SENT BY Public Bank Berhad or Public Islamic Bank
TO a customer, about a third-party bank account being redeemed or refinanced.

- "Public Bank Berhad" and "Public Islamic Bank" are the SENDER — do NOT return
  them as the bank name unless they are genuinely the only bank in the document.
- The THIRD-PARTY bank is the one whose FI Code / Master Account / Sub Account
  appear in the dedicated section above. Its name appears in one of:
    * Signature block  : "for <Bank Name> Berhad", "Yours faithfully, <Bank Name>"
    * RENTAS/IBG block : "PAYABLE TO: <Bank Name>" or "Beneficiary: <Bank Name>"
    * Letterhead / address block near the FI Code section
- You MUST return the EXACT canonical form from the known bank list below.
  No other spelling, casing, or suffix is acceptable.
- If the document shows an Islamic variant of a bank, return the Islamic canonical
  name (e.g. "Maybank Islamic", "HSBC Amanah", "OCBC Al-Amin").
- Return null ONLY if no third-party bank can be identified at all.

MANDATORY MAPPING RULES — apply these before returning any bank name:
  "United Overseas Bank ..."          → "UOB"
  "UOB Bank ..."                      → "UOB"
  "OCBC Bank ..."                     → "OCBC"           (drop the word "Bank")
  "OCBC Al-Amin ..."                  → "OCBC Al-Amin"
  "CIMB ..." (non-Islamic)            → "CIMB Bank"      (add "Bank")
  "CIMB Islamic ..."                  → "CIMB Bank"      (CIMB has no Islamic entry; use "CIMB Bank")
  "AmBank ..." / "Am Bank ..."        → "Ambank"         (lowercase b)
  "AmBank Islamic ..." / "Am Islamic" → "Ambank Islamic" (lowercase b)
  "AmBank (M) Berhad"                 → "Ambank"
  "BSN" / "Bank Simpanan Nasional"    → "BSN"
  "Al Rajhi Bank ..."                 → "Al Rajhi Bank"
  "Affin Islamic Bank ..."            → "Affin Bank"     (no Islamic entry; use "Affin Bank")
  "Agro Bank ..." / "Bank Pertanian"  → null             (not in list)
  "Citibank ..."                      → null             (not in list)
  "Standard Chartered Saadiq ..."     → "Standard Chartered Saadiq Islamic"
  "RHB Islamic ..."                   → "RHB Islamic Bank"
  "Hong Leong Islamic ..."            → "Hong Leong Islamic Bank"
  "Alliance Islamic ..."              → "Alliance Islamic Bank"
  "Bank Islam ..."                    → "Bank Islam"
  "Bank Muamalat ..."                 → "Bank Muamalat"
  "Kuwait Finance House ..."          → "Kuwait Finance House"
  "MBSB ..." / "Malaysia Building"    → "MBSB Bank"
  "Bank of China ..."                 → "Bank of China"
  "Bank Rakyat ..." / "Bank Kerjasama"→ "Bank Rakyat"
  "Maybank Islamic ..."               → "Maybank Islamic"
  "Malayan Banking ..." (non-Islamic) → "Maybank"
  "HSBC Amanah ..."                   → "HSBC Amanah"
  "HSBC Bank ..."                     → "HSBC Bank"

Known bank names — return the EXACT canonical form from this list:
{_KNOWN_BANKS}

================================================================================
O vs 0 DISAMBIGUATION RULE
================================================================================
OCR sometimes renders the letter O as the digit 0. Apply these rules:
- Suffixes such as "/D88", "/D66", "/D90" are ALWAYS preceded by the letter O:
    write "O/D88" — NOT "0/D88".
- Inside a purely numeric sequence with no suffix, treat ambiguous characters as 0.
- When in doubt at a suffix boundary, use the letter O.

================================================================================
DOCUMENT
================================================================================
\"\"\"
{text}
\"\"\"

================================================================================
STEP-BY-STEP EXTRACTION CHECKLIST
================================================================================
Work through these steps before writing the JSON:

STEP 1 — LOCATE THE FI CODE SECTION
  Search the entire document (top and bottom) for a section containing the words
  "FI CODE", "FI NUM", "FACILITY ACCOUNT", "ACCOUNT DETAILS", or "CCRIS".
  Also check any header table near the borrower/account details at the top.

STEP 2 — DETECT THE LAYOUT
  Pipe characters (|) → Layout A: assign fi_num to the SHORTEST value in the row.
  Label lines (FI CODE :, MASTER ACCOUNT NO :) → Layout B: read each label's value.
  Repeated inline colons with label keywords on one line → Layout C.
  Lettered list (a) / b) / c)) → Layout D: each letter is a separate field;
      (b) master and (c) sub are DIFFERENT values from DIFFERENT lines.
  Account number listed first under "Account(s)", FI code on separate line below → Layout F:
      both master and sub = the account number; fi_num = the shorter labelled value.
  Combined "MASTER / SUB ACCOUNT NO." label with one value → both master and sub
      share that value; digit after "NO." is a footnote marker, not the sub account.
  "CCRIS" prefix with comma-separated values → Layout E: fi_num starts with 0 or 3
      (first value only per line).

STEP 3 — ASSIGN THE THREE VALUES
  fi_num                ← shortest value, starts with 0 or 3, 7–9 chars
  master_account_number ← as described above for the detected layout
  sub_account_number    ← as described above for the detected layout (or null)

STEP 4 — SANITY-CHECK
  • fi_num starts with 0 or 3, is 7–9 characters, never longer than master account.
  • fi_num never starts with 1.
  • master_account_number ≠ fi_num.
  • In Layout D, confirm master_account_number came from the (b) "Master Account"
    line and sub_account_number came from the (c) "Sub Account" line — they are
    different values; if they are identical double-check the document.
  • In Layout F, confirm fi_num came from the labelled "Fi Code" line, NOT from
    the account number line; master and sub are both the account number.
  • A combined "MASTER / SUB ACCOUNT NO." label means master = sub = that value.
    A digit like "2" immediately after "NO." is a footnote marker — not a value.
  • sub_account_number may equal master_account_number only when the document uses
    a combined label or explicitly repeats the same value for both fields.
  • None of these is a phone number, postcode, or RENTAS clearing account.
  • If a value had comma-separated candidates, confirm only the first (or the
    0/3-prefixed one for fi_num) was taken.
  • Parenthetical text like "(Term Financing-i)" has been stripped.

STEP 5 — FIND THE BANK NAME
  Check: signature block → RENTAS/IBG beneficiary label → letterhead near FI section.
  Map to canonical form. The sender (Public Bank / Public Islamic Bank) is NOT the answer.

STEP 6 — FIND THE CUSTOMER NAME
  All-caps Malaysian name. Exclude staff, bank, and law firm names.

STEP 7 — FIND THE CUSTOMER ADDRESS (if any)
  Exclude bank branch and law firm (cc:) addresses.

STEP 8 — OUTPUT THE JSON.

================================================================================
OUTPUT
================================================================================
DO NOT EXPLAIN all the steps taken to extract the required field from the instructions above.
JUST RETURN ONLY this JSON object with no other text in the response:
{{
    "name": "<full customer name or null>",
    "master_account_number": "<master account number copied exactly as printed, or null>",
    "sub_account_number": "<sub account number copied exactly as printed, or null>",
    "address": "<full address or null>",
    "fi_num": "<FI number copied exactly as printed, or null>",
    "bank_name": "<canonical bank name from the known list, or null>"
}}
"""
