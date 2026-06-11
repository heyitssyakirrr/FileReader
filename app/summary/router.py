from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, File, UploadFile

from app.core.config import get_settings
from app.summary.prompt import build_summary_prompt
from app.models.schemas import (
    DailySummary,
    ExtractionMeta,
    MonthlySummary,
    SummaryResponse,
    SummaryResult,
)
from app.services.file_service import decode_txt_bytes, validate_and_read_upload
from app.services.llm_client import LLMClient

router = APIRouter(prefix="/summarise", tags=["Summarisation"])
llm_client = LLMClient()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constant — adjust based on LLM thread/batch capacity
# ---------------------------------------------------------------------------
ROWS_PER_CHUNK = 20  # 20 preprocessed rows per chunk ~ safe output token count


# ---------------------------------------------------------------------------
# Pre-processing — strip everything except date + balance before LLM sees it
# ---------------------------------------------------------------------------

def _preprocess_statement(text: str) -> list[str]:
    """
    Parse the markdown table in Python and return only the meaningful lines.
    Each output line: "DDMMYY | 1,234.56DR"

    Returns a list of clean row strings (not a single joined string) so the
    caller can chunk them freely without re-splitting.
    """
    rows = []
    for line in text.splitlines():
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.match(r'^\|[-| :]+\|$', line):
            continue

        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 2:
            continue

        date_col = cols[0]
        if not re.match(r'^\d{6}$|^\d{4}-\d{2}-\d{2}$', date_col):
            continue

        balance_col = cols[-1]
        if not re.search(r'\d', balance_col):
            continue

        rows.append(f"{date_col} | {balance_col}")

    return rows


def _chunk_rows(rows: list[str], chunk_size: int) -> list[str]:
    """
    Split list of preprocessed row strings into chunks.
    Each chunk is a single joined string ready to embed in a prompt.
    """
    chunks = []
    for i in range(0, len(rows), chunk_size):
        chunk_text = "\n".join(rows[i:i + chunk_size])
        chunks.append(chunk_text)
    return chunks


# ---------------------------------------------------------------------------
# Pure-Python arithmetic — no LLM involvement
# ---------------------------------------------------------------------------

def _to_decimal(value: str) -> Decimal | None:
    try:
        cleaned = value.strip().lstrip("RMrm $").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, AttributeError):
        return None


def _fmt(d: Decimal) -> str:
    return f"{d:.2f}"


def _compute_summaries(raw_rows: list[dict]) -> SummaryResult:
    date_balances: dict[str, list[Decimal]] = defaultdict(list)

    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        date = (row.get("date") or "").strip()
        raw_bal = row.get("balance") or ""
        bal = _to_decimal(raw_bal)
        if date and bal is not None:
            date_balances[date].append(bal)

    if not date_balances:
        return SummaryResult()

    daily_summaries: list[DailySummary] = []
    for date in sorted(date_balances.keys()):
        balances = date_balances[date]
        daily_summaries.append(
            DailySummary(
                date=date,
                min_balance=_fmt(min(balances)),
                max_balance=_fmt(max(balances)),
                closing_balance=_fmt(balances[-1]),
            )
        )

    month_dailies: dict[str, list[DailySummary]] = defaultdict(list)
    for ds in daily_summaries:
        month = ds.date[:7]
        month_dailies[month].append(ds)

    monthly_summaries: list[MonthlySummary] = []
    for month in sorted(month_dailies.keys()):
        days = month_dailies[month]
        all_mins = [Decimal(d.min_balance) for d in days]
        all_maxs = [Decimal(d.max_balance) for d in days]
        monthly_summaries.append(
            MonthlySummary(
                month=month,
                min_balance=_fmt(min(all_mins)),
                max_balance=_fmt(max(all_maxs)),
                closing_balance=days[-1].closing_balance,
            )
        )

    all_bal_decimals = [
        bal for balances in date_balances.values() for bal in balances
    ]
    last_daily = daily_summaries[-1]

    return SummaryResult(
        daily_summaries=daily_summaries,
        monthly_summaries=monthly_summaries,
        overall_min_balance=_fmt(min(all_bal_decimals)),
        overall_max_balance=_fmt(max(all_bal_decimals)),
        overall_closing_balance=last_daily.closing_balance,
    )


# ---------------------------------------------------------------------------
# Parallel LLM calls
# ---------------------------------------------------------------------------

async def _call_llm_chunk(chunk_text: str, index: int, total: int) -> list[dict]:
    """
    Send one chunk to the LLM and return its rows.
    Retries once with a 3-second back-off on any failure.
    Returns empty list on failure so one bad chunk never kills the whole request.
    """
    logger.debug("Chunk %d/%d — sending %d lines", index + 1, total, chunk_text.count("\n") + 1)

    last_exc = None
    for attempt in range(2):
        if attempt > 0:
            logger.warning("Chunk %d/%d - attempt %d failed: %s — retrying in 3s", index +1 , total, attempt, last_exc)
            await asyncio.sleep(3)
        try:
            prompt = build_summary_prompt(chunk_text)
            llm_result = await llm_client.extract_fields(
                prompt,
                stop=["} {", "\n} {", "\n}{"]
            )
            rows = llm_result.get("rows") or []
            logger.debug("Chunk %d/%d — received %d rows", index + 1, total, len(rows))
            return rows
        except Exception as exc:
            last_exc = exc

    logger.warning("Chunk %d/%d - all attempts failed: %s — skipping", index + 1, total, last_exc)
    return []


async def _run_summarisation(original_text: str, source: str) -> SummaryResponse:
    settings = get_settings()

    # Step 1 — preprocess: strip everything except date + balance
    preprocessed_rows = _preprocess_statement(original_text)
    logger.debug(
        "Preprocessed %d rows from %d input chars",
        len(preprocessed_rows), len(original_text)
    )

    if not preprocessed_rows:
        logger.warning("No transaction rows found after preprocessing")
        return SummaryResponse(
            success=False,
            message="No transaction rows found in the uploaded statement.",
            data=SummaryResult(),
            meta=ExtractionMeta(
                input_characters=len(original_text),
                llm_called=False,
                source=source,
            ),
        )

    # Step 2 — chunk the preprocessed rows
    chunks = _chunk_rows(preprocessed_rows, ROWS_PER_CHUNK)
    logger.debug(
        "%d rows split into %d chunks of up to %d rows each",
        len(preprocessed_rows), len(chunks), ROWS_PER_CHUNK
    )

    # Step 3 — fire all chunks to LLM in parallel
    tasks = [
        _call_llm_chunk(chunk, i, len(chunks))
        for i, chunk in enumerate(chunks)
    ]
    results = await asyncio.gather(*tasks)

    # Step 4 — merge all rows in original order
    all_rows = [row for chunk_rows in results for row in chunk_rows]
    logger.debug("Total rows collected across all chunks: %d", len(all_rows))

    # Step 5 — Python arithmetic, unchanged
    summary_result = _compute_summaries(all_rows)

    return SummaryResponse(
        success=True,
        message="Summarisation completed successfully.",
        data=summary_result,
        meta=ExtractionMeta(
            input_characters=len(original_text),
            llm_called=True,
            source=source,
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/from-file", response_model=SummaryResponse)
async def summarise_from_file(file: UploadFile = File(...)) -> SummaryResponse:
    raw_bytes, ext = await validate_and_read_upload(file)
    filename = file.filename or "uploaded_file"
    original_text = decode_txt_bytes(raw_bytes)
    return await _run_summarisation(original_text=original_text, source=filename)
