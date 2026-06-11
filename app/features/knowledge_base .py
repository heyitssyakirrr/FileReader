from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Per-bank knowledge entries
# ---------------------------------------------------------------------------
# Each entry contains:
#   "canonical"  – the exact string the LLM must return for bank_name
#   "keywords"   – lowercase substrings used to detect the bank in raw text
#                  (order matters: more-specific patterns listed first)
#   "examples"   – 5 known-good rows shown to the LLM as few-shot examples
#   "pattern"    – one-line description of fi_num / account structure
# ---------------------------------------------------------------------------

_BANK_KB: list[dict] = [
    {
        "canonical": "Affin Bank",
        "keywords": ["affin islamic", "affin bank", "affin"],
        "examples": [
            "fi_num=023207097  master=0000001071301956140212001  sub=0000000401070018650",
            "fi_num=034707062  master=0000006072902892170888001  sub=0000000406070003660",
            "fi_num=034714088  master=0000005282901205300888001  sub=0000000405280010127",
            "fi_num=034701136  master=0000006751700444840888001  sub=0000000406750016647",
            "fi_num=023214107  master=0000000281801339980212001  sub=0000000400280032058",
        ],
        "pattern": (
            "fi_num is 9 digits starting with 02 or 03; master and sub are long "
            "numeric strings (24-25 digits) with different values. "
            "Note: Affin Islamic maps to canonical 'Affin Bank'."
        ),
    },
    {
        "canonical": "Al Rajhi Bank",
        "keywords": ["al rajhi"],
        "examples": [
            "fi_num=035014011  master=102000040000000236756  sub=102000040000000236756",
            "fi_num=035014011  master=162000040000000237956  sub=162000040000000237956",
            "fi_num=035014011  master=123000040000000219568  sub=123000040000000219568",
            "fi_num=035014011  master=125000040000000222067  sub=125000040000000222067",
            "fi_num=035014011  master=101000040000000247345  sub=101000040000000247345",
        ],
        "pattern": (
            "ALL Al Rajhi accounts share fi_num=035014011; "
            "master and sub are IDENTICAL 21-digit numbers."
        ),
    },
    {
        "canonical": "Alliance Islamic Bank",
        "keywords": ["alliance islamic"],
        "examples": [
            "fi_num=035312016  master=1201600003000122981DS1001  sub=620100052154291",
            "fi_num=035310058  master=600520010052602             sub=600520010052602",
            "fi_num=035312070  master=1207000002200134556A16001  sub=620830052022657",
            "fi_num=035312164  master=1216400002200299908A14001  sub=641080052012083",
            "fi_num=035314074  master=1407400001300284627DS1001  sub=640870053050415",
        ],
        "pattern": (
            "fi_num starts with 0353 (9 digits); master ends in DS1001/A16001 style suffix; "
            "sub is a different shorter number. "
            "CCRIS note: when fi_num appears as '1035312016, 035312016' take the value "
            "starting with 0 → 035312016."
        ),
    },
    {
        "canonical": "Alliance Bank",
        "keywords": ["alliance bank", "alliance"],
        "examples": [
            "fi_num=021214158  master=1415800003500050253SL7001  sub=141580052015239",
            "fi_num=021201037  master=0103700002200331294SL7001  sub=010370052079510",
            "fi_num=021214176  master=1417600001800122659Z56001  sub=141760052102643",
            "fi_num=021214037  master=1403700003000201403SL7001  sub=140370052044757",
            "fi_num=021201103  master=0110300002400166602Z57001  sub=011030052017543",
        ],
        "pattern": (
            "fi_num starts with 021 (9 digits); master ends in a suffix like "
            "SL7001/Z56001/LC1001; sub is a shorter pure-digit string."
        ),
    },
    {
        "canonical": "Ambank Islamic",
        "keywords": ["ambank islamic", "am bank islamic", "amislamic"],
        "examples": [
            "fi_num=034907013  master=88820006220322  sub=00088820006220322",
            "fi_num=034910068  master=88820005694815  sub=00088820005694815",
            "fi_num=034911029  master=88820007094644  sub=00088820007094644",
            "fi_num=034910022  master=88820003892229  sub=00088820003892229",
            "fi_num=034901011  master=2120077502771   sub=00002120077502771",
        ],
        "pattern": (
            "fi_num starts with 0349 (9 digits); sub is the master padded with leading zeros."
        ),
    },
    {
        "canonical": "Ambank",
        "keywords": ["ambank", "am bank"],
        "examples": [
            "fi_num=020807012  master=250600011808    sub=00000250600011808",
            "fi_num=020814010  master=88820002322930  sub=00088820002322930",
            "fi_num=020807021  master=88820006854268  sub=00088820006854268",
            "fi_num=020812221  master=88820004660536  sub=00088820004660536",
            "fi_num=020801010  master=88820004260221  sub=00088820004260221",
        ],
        "pattern": (
            "fi_num starts with 0208 (9 digits); master is a shorter number; "
            "sub is the master padded with leading zeros (typically 5 extra zeros)."
        ),
    },
    {
        "canonical": "Bank Islam",
        "keywords": ["bank islam"],
        "examples": [
            "fi_num=034012122  master=0000012122080025676  sub=0000012122080025676",
            "fi_num=034001078  master=0000001078080044011  sub=0000001078080044011",
            "fi_num=034008013  master=0000008013080095326  sub=0000008013080095326",
            "fi_num=034005067  master=0000005067080005260  sub=0000005067080005260",
            "fi_num=034008013  master=0000008013080047172  sub=0000008013080047183",
        ],
        "pattern": (
            "fi_num starts with 0340 (9 digits); master and sub are 19-digit numbers, "
            "often identical but may differ in the last few digits."
        ),
    },
    {
        "canonical": "Bank Muamalat",
        "keywords": ["bank muamalat", "muamalat"],
        "examples": [
            "fi_num=034107028  master=07020003401750000  sub=00",
            "fi_num=034103011  master=03010017291754000  sub=00",
            "fi_num=034102069  master=02060000869758000  sub=00",
            "fi_num=034112040  master=12040004857759000  sub=00",
            "fi_num=034110028  master=10020001569750000  sub=00",
        ],
        "pattern": (
            "fi_num starts with 0341 (9 digits); sub is ALWAYS '00' — "
            "do NOT copy master into sub."
        ),
    },
    {
        "canonical": "Bank of China",
        "keywords": ["bank of china"],
        "examples": [
            "fi_num=024201010  master=6020115757000057165701  sub=100000403038945",
            "fi_num=024214010  master=6020109163000051650984  sub=100000402574331",
            "fi_num=024201029  master=6020063352000041105405  sub=100000401827636",
            "fi_num=024201010  master=6020115757000057165701  sub=100000403038945",
            "fi_num=024214010  master=6020109163000051650984  sub=100000402574331",
        ],
        "pattern": (
            "fi_num starts with 0242 (9 digits); master is 22 digits starting with 602; "
            "sub is a shorter 15-digit number starting with 1000004."
        ),
    },
    {
        "canonical": "Bank Rakyat",
        "keywords": ["bank rakyat", "bank kerjasama rakyat", "kerjasama rakyat"],
        "examples": [
            "fi_num=331162044  master=7000169103   sub=7000169103",
            "fi_num=331151178  master=7000530900   sub=7000530900",
            "fi_num=331164114  master=7000452753   sub=7000452753",
            "fi_num=331164066  master=7000483268   sub=7000483268",
            "fi_num=331155046  master=700013374-1  sub=700013374-1",
        ],
        "pattern": (
            "fi_num starts with 331 (9 digits); master and sub are typically identical "
            "10-digit numbers starting with 7000; may include trailing hyphen+digit."
        ),
    },
    {
        "canonical": "BSN",
        "keywords": ["bank simpanan nasional", "bsn"],
        "examples": [
            "fi_num=331014022  master=07215-72-952646-82   sub=07215-72-952646-82",
            "fi_num=331002010  master=02100-72-039240-34   sub=02100-72-039240-34",
            "fi_num=331014022  master=141006702640456      sub=141006702640456",
            "fi_num=331008012  master=08112-72-673530-31   sub=08112-72-673530-31",
            "fi_num=331015019  master=101007204433790      sub=101007204433790",
        ],
        "pattern": (
            "fi_num starts with 331 (9 digits); master and sub are IDENTICAL; "
            "format varies — hyphenated (XX-XX-XXXXXX-XX) or plain 15-digit number."
        ),
    },
    {
        "canonical": "CIMB Bank",
        "keywords": ["cimb islamic", "cimb bank", "cimb"],
        "examples": [
            "fi_num=023507313  master=0000000000003352797  sub=0000000001009887791",
            "fi_num=023512719  master=0000000000003895100  sub=0000000001011356318",
            "fi_num=034412834  master=0000000000002767422  sub=0000000001604327125",
            "fi_num=023514179  master=0000000000002378961  sub=0000000001007966791",
            "fi_num=023501302  master=0000000000003733618  sub=0000000001011197446",
        ],
        "pattern": (
            "fi_num starts with 0235 or 0344 (9 digits); master is 19 digits starting "
            "with 000000000000; sub is 19 digits starting with 0000000001 or 0000000008. "
            "Note: CIMB Islamic maps to canonical 'CIMB Bank'."
        ),
    },
    {
        "canonical": "Hong Leong Islamic Bank",
        "keywords": ["hong leong islamic"],
        "examples": [
            "fi_num=034504019  master=MLK/MG/2016/L0051843956001  sub=26791008566",
            "fi_num=034512408  master=PJC/MG/2017/L0091645956001  sub=18291035611",
            "fi_num=034508018  master=IPH/MG/2017/L0053225956001  sub=01291004670",
            "fi_num=034501215  master=BIN/MG/2017/L0054354956001  sub=32391005389",
            "fi_num=034512041  master=PCG/MG/2017/L0052036956001  sub=27091004214",
        ],
        "pattern": (
            "fi_num starts with 0345 (9 digits); master uses CODE/MG/YEAR/L format "
            "with suffix ending in 956001; sub is an 11-digit number."
        ),
    },
    {
        "canonical": "Hong Leong Bank",
        "keywords": ["hong leong bank", "hong leong"],
        "examples": [
            "fi_num=022407050  master=BYB/MG/2021/L0059234217001  sub=05481064677",
            "fi_num=022405029  master=SBN/MG/2023/L0058274212001  sub=01181065712",
            "fi_num=022412344  master=DJK/MG/2015/L0051295212001  sub=14381017718",
            "fi_num=022414046  master=202800283616/5202840450001   sub=33181057539",
            "fi_num=022401049  master=KOT/MG/2014/L0050992212002  sub=00881017236",
        ],
        "pattern": (
            "fi_num starts with 0224 (9 digits); master uses CODE/MG/YEAR/L format "
            "or numeric/numeric format; sub is an 11-digit number."
        ),
    },
    {
        "canonical": "HSBC Amanah",
        "keywords": ["hsbc amanah"],
        "examples": [
            "fi_num=035607048  master=075139907O/D55  sub=075139907021MH3MYR",
            "fi_num=035602016  master=031109168O/D88  sub=031109168021MH7MYR",
            "fi_num=035610011  master=392363941O/D88  sub=092173954021MH3MYR",
            "fi_num=035614064  master=017093642O/D88  sub=017093642021MH3MYR",
            "fi_num=035611054  master=040013625O/D66  sub=040013625022MH3MYR",
        ],
        "pattern": (
            "fi_num starts with 0356 (9 digits); master is 9 digits + 'O/D' + 2 digits "
            "(letter O, not zero); sub shares base digits + 021MH3MYR or similar suffix."
        ),
    },
    {
        "canonical": "HSBC Bank",
        "keywords": ["hsbc bank", "hsbc"],
        "examples": [
            "fi_num=022204026  master=205340722O/D88  sub=342385069101SLUMYR",
            "fi_num=022214025  master=303678189O/D55  sub=303678189101SLUMYR",
            "fi_num=022206039  master=362067258O/D88  sub=362067258101SLUMYR",
            "fi_num=022212106  master=316222116O/D88  sub=316222116101SLUMYR",
            "fi_num=022211024  master=322248576O/D90  sub=322248576101HM5MYR",
        ],
        "pattern": (
            "fi_num starts with 0222 (9 digits); master is 9 digits + 'O/D' + 2 digits "
            "(letter O, not zero); sub may share base digits + 101SLUMYR/101HM5MYR suffix "
            "or be a completely different number."
        ),
    },
    {
        "canonical": "Kuwait Finance House",
        "keywords": ["kuwait finance house", "kfh"],
        "examples": [
            "fi_num=034614012  master=013462        sub=000001",
            "fi_num=034614012  master=005521002670  sub=005521002670",
            "fi_num=034614012  master=0103/002084   sub=0103/002084",
            "fi_num=034614012  master=0103/001796   sub=0103/001796",
            "fi_num=034614012  master=031974        sub=000001",
        ],
        "pattern": (
            "ALL KFH accounts share fi_num=034614012; account formats vary widely — "
            "short numeric, long numeric, or slash-separated (0103/XXXXXX)."
        ),
    },
    {
        "canonical": "Maybank Islamic",
        "keywords": ["maybank islamic", "malayan banking islamic"],
        "examples": [
            "fi_num=035404023   master=454026025998  sub=454026025998",
            "fi_num=0354_08058  master=458051077315  sub=458051077315",
            "fi_num=0354_02056  master=452068410407  sub=452068410407",
            "fi_num=0354_15010  master=465125934266  sub=465125934266",
            "fi_num=035414527   master=464762126205  sub=464762126205",
        ],
        "pattern": (
            "fi_num is '0354' + underscore+5 digits OR fused 9 digits; "
            "master and sub are IDENTICAL 12-digit numbers starting with 4."
        ),
    },
    {
        "canonical": "Maybank",
        "keywords": ["maybank", "malayan banking"],
        "examples": [
            "fi_num=0227_04067  master=404067081922  sub=404067081922",
            "fi_num=0227_07040  master=407040345497  sub=407040345497",
            "fi_num=0227_14589  master=414589025766  sub=414589025766",
            "fi_num=0227-11038  master=411038953967  sub=411038953967",
            "fi_num=022712192   master=412192626348  sub=412192626348",
        ],
        "pattern": (
            "fi_num is '0227' + underscore+5 digits, hyphen+5 digits, OR fused 9 digits; "
            "master and sub are IDENTICAL 12-digit numbers starting with 4."
        ),
    },
    {
        "canonical": "MBSB Bank",
        "keywords": ["mbsb bank", "malaysia building society", "mbsb"],
        "examples": [
            "fi_num=035207028  master=45002001897300000  sub=45002001897300000",
            "fi_num=035207037  master=40002001212900000  sub=40002001212900000",
            "fi_num=035210019  master=45022007538600000  sub=45022007538600000",
            "fi_num=035201026  master=45016002974600000  sub=45016002974600000",
            "fi_num=035211025  master=45023009450800000  sub=45023009450800000",
        ],
        "pattern": (
            "fi_num starts with 0352 (9 digits); master and sub are IDENTICAL "
            "17-digit numbers ending in 00000."
        ),
    },
    {
        "canonical": "OCBC Al-Amin",
        "keywords": ["ocbc al-amin", "ocbc al amin", "ocbc alamin"],
        "examples": [
            "fi_num=035702025  master=174-403175-1-00000  sub=174-403175-1-00000",
            "fi_num=035712079  master=171-400233-8-00000  sub=171-400233-8-00000",
            "fi_num=035712060  master=172-412188-7-00000  sub=172-412188-7-00000",
            "fi_num=035702025  master=174-402143-8-00000  sub=174-402143-8-00000",
            "fi_num=035701073  master=175-403905-4-00000  sub=175-403905-4-00000",
        ],
        "pattern": (
            "fi_num starts with 0357 (9 digits); master and sub are IDENTICAL; "
            "format is XXX-XXXXXX-X-00000 or XXXXXXXXXX-00000."
        ),
    },
    {
        "canonical": "OCBC",
        "keywords": ["ocbc bank", "ocbc"],
        "examples": [
            "fi_num=022907019  master=730-414883-8-00000  sub=730-414883-8-00000",
            "fi_num=022901044  master=7114160292-00000    sub=7114160292-00000",
            "fi_num=022908016  master=7204127324-00000    sub=7204127324-00000",
            "fi_num=022905015  master=740-407580-8-00000  sub=740-407580-8-00000",
            "fi_num=022914017  master=7014473027-00000    sub=7014473027-00000",
        ],
        "pattern": (
            "fi_num starts with 0229 (9 digits); master and sub are IDENTICAL; "
            "format is XXX-XXXXXX-X-00000 or XXXXXXXXXX-00000."
        ),
    },
    {
        "canonical": "RHB Islamic Bank",
        "keywords": ["rhb islamic"],
        "examples": [
            "fi_num=034312505  master=76251300012188  sub=76251300012188",
            "fi_num=034306045  master=75604400017228  sub=75604400017228",
            "fi_num=034307024  master=75702300017927  sub=75702300017927",
            "fi_num=034308106  master=75810500006861  sub=75810500006861",
            "fi_num=034314246  master=76437500006499  sub=76437500006499",
        ],
        "pattern": (
            "fi_num starts with 0343 (9 digits); master and sub are IDENTICAL "
            "14-digit numbers starting with 75 or 76."
        ),
    },
    {
        "canonical": "RHB Bank",
        "keywords": ["rhb bank", "rhb"],
        "examples": [
            "fi_num=021812590  master=71259000031266  sub=71259000031266",
            "fi_num=021808043  master=70804300034215  sub=70804300034215",
            "fi_num=021812022  master=71202200108145  sub=71202200108145",
            "fi_num=021814062  master=71406200062132  sub=71406200062132",
            "fi_num=021806012  master=70601200081753  sub=70601200081753",
        ],
        "pattern": (
            "fi_num starts with 0218 (9 digits); master and sub are IDENTICAL "
            "14-digit numbers starting with 7."
        ),
    },
    {
        "canonical": "Standard Chartered Saadiq Islamic",
        "keywords": ["standard chartered saadiq", "saadiq"],
        "examples": [
            "fi_num=035814015  master=42489709  sub=42489709",
            "fi_num=035812075  master=44627122  sub=44627122",
            "fi_num=035812048  master=42772982  sub=42772982",
            "fi_num=035812066  master=44493223  sub=44493223",
            "fi_num=035812075  master=44255284  sub=44255284",
        ],
        "pattern": (
            "fi_num starts with 0358 (9 digits); master and sub are IDENTICAL 8-digit numbers."
        ),
    },
    {
        "canonical": "Standard Chartered Bank",
        "keywords": ["standard chartered bank", "standard chartered"],
        "examples": [
            "fi_num=021401024  master=45076146  sub=45076146",
            "fi_num=021407017  master=44873506  sub=44873506",
            "fi_num=021412123  master=42294363  sub=42294363",
            "fi_num=021414079  master=44968477  sub=44968477",
            "fi_num=021407017  master=45047839  sub=45047839",
        ],
        "pattern": (
            "fi_num starts with 0214 (9 digits); master and sub are IDENTICAL 8-digit numbers."
        ),
    },
    {
        "canonical": "UOB",
        "keywords": ["united overseas bank", "uob bank", "uob"],
        "examples": [
            "fi_num=022607074  master=3000188334  sub=389801837500000",
            "fi_num=022614120  master=3000456815  sub=508800502400000",
            "fi_num=022601072  master=3000196368  sub=378801053000000",
            "fi_num=022611017  master=3000309470  sub=3000309470",
            "fi_num=022612078  master=3000170346  sub=387805903200000",
        ],
        "pattern": (
            "fi_num starts with 0226 (9 digits); master is a 10-digit number starting "
            "with 3000; sub is either the same 10-digit number OR a longer 15-digit number."
        ),
    },
]

# ---------------------------------------------------------------------------
# Fast lookup: canonical name → entry  (built once at import time)
# ---------------------------------------------------------------------------
_BY_CANONICAL: dict[str, dict] = {e["canonical"]: e for e in _BANK_KB}

# Full canonical list as a formatted string (used as fallback in prompts)
ALL_CANONICAL_NAMES: str = "\n".join(f"- {e['canonical']}" for e in _BANK_KB)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_bank(text: str) -> dict | None:
    """
    Scan raw document text and return the first matching bank entry, or None.

    Strategy:
      1. Lowercase the text once.
      2. Walk _BANK_KB in order (more-specific Islamic variants listed before
         their conventional siblings so they win on ambiguous text).
      3. Return the first entry whose any keyword appears as a whole-word match.

    Whole-word matching prevents "rhb" from firing inside "rhb islamic" text
    when the Islamic entry hasn't matched yet — because Islamic is listed first.
    """
    lower = text.lower()
    for entry in _BANK_KB:
        for kw in entry["keywords"]:
            # Use word-boundary regex for robustness
            if re.search(r'\b' + re.escape(kw) + r'\b', lower):
                return entry
    return None


def build_knowledge_block(text: str) -> tuple[str, str]:
    """
    Given raw document text, return a (few_shot_block, known_bank_line) tuple
    scoped to only the detected bank.

    Returns:
        few_shot_block  – the KNOWLEDGE BASE section to inject into the prompt,
                          containing only the matched bank's examples + pattern.
                          Empty string if no bank detected (fallback: LLM infers).
        known_bank_line – the single canonical bank name string, or the full
                          ALL_CANONICAL_NAMES list if detection failed.
    """
    entry = detect_bank(text)

    if entry is None:
        # Detection failed — pass the full list so the LLM can still try
        return "", ALL_CANONICAL_NAMES

    examples_block = "\n".join(
        f"Example {i + 1} : {ex}" for i, ex in enumerate(entry["examples"])
    )
    few_shot_block = (
        "================================================================================\n"
        "KNOWLEDGE BASE — KNOWN-GOOD EXTRACTION EXAMPLES\n"
        "================================================================================\n"
        "These examples show the EXACT canonical values expected for this bank.\n"
        "Use them to calibrate fi_num format, account-number length and structure.\n"
        "Do NOT copy these values into the output — extract from the document only.\n"
        "\n"
        f"--- {entry['canonical']} ---\n"
        f"{examples_block}\n"
        f"Key pattern: {entry['pattern']}"
    )

    known_bank_line = f"- {entry['canonical']}"

    return few_shot_block, known_bank_line
