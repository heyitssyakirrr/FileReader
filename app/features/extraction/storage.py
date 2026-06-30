from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.features.extraction.concurrency import _csv_append_lock
from app.features.extraction.retention import (
    enforce_dated_folder_retention,
    enforce_extractions_retention,
)
from app.models.schemas import ExtractionResult

logger = logging.getLogger(__name__)
settings = get_settings()

_RESULTS_ROOT = Path("results")
_FAILED_ROOT = Path("failed")
_OCR_OUTPUTS_ROOT = Path("OCR_Outputs")

_RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
_FAILED_ROOT.mkdir(parents=True, exist_ok=True)
_OCR_OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)

_SUCCESS_CSV_HEADER = "timestamp,filename,bank_name,fi_num,master_account_number,sub_account_number\r\n"
_FAILED_CSV_HEADER = "filename,error_message,timestamp\r\n"


def _escape_csv_field(value: str | None) -> str:
    if value is None:
        return ""
    s = str(value)
    if "," in s or '"' in s or "\n" in s or "\r" in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


async def append_success_row(result: ExtractionResult, filename: str) -> Path:
    now = datetime.now()
    timestamp = now.isoformat(timespec="seconds")
    today = now.strftime("%Y%m%d")
    csv_path = _RESULTS_ROOT / f"{today}_extractions.csv"

    row = ",".join(_escape_csv_field(v) for v in [
        timestamp,
        filename,
        result.bank_name,
        result.fi_num,
        result.master_account_number,
        result.sub_account_number,
    ]) + "\r\n"

    async with _csv_append_lock:
        is_new_file = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", encoding="utf-8", newline="") as fh:
            if is_new_file:
                fh.write(_SUCCESS_CSV_HEADER)
            fh.write(row)
            fh.flush()

        enforce_extractions_retention(_RESULTS_ROOT, settings.retention_max_days)

    logger.info("Success row written to %s for file '%s'", csv_path, filename)
    return csv_path


def _write_failure_sync(
    pdf_bytes: bytes,
    filename: str,
    error_message: str,
    timestamp: str,
    today: str,
) -> tuple[Path, Path]:
    day_folder = _FAILED_ROOT / today
    files_folder = day_folder / "failed_files"
    files_folder.mkdir(parents=True, exist_ok=True)

    # Disambiguate by time-of-day so two failures sharing the same original
    # filename on the same day never overwrite each other's copy.
    time_part = timestamp.split("T", 1)[-1].replace(":", "")
    original_name = Path(filename).name
    dest_pdf = files_folder / f"{time_part}__{original_name}"
    suffix = 1
    while dest_pdf.exists():
        dest_pdf = files_folder / f"{time_part}_{suffix}__{original_name}"
        suffix += 1
    dest_pdf.write_bytes(pdf_bytes)

    csv_path = day_folder / "failed.csv"
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        if write_header:
            fh.write(_FAILED_CSV_HEADER)
        row = ",".join(_escape_csv_field(v) for v in [
            filename,
            error_message,
            timestamp,
        ]) + "\r\n"
        fh.write(row)
        fh.flush()

    enforce_dated_folder_retention(_FAILED_ROOT, settings.retention_max_days)

    logger.info("Failure recorded for '%s' in %s (error: %s)", filename, day_folder, error_message)
    return dest_pdf, csv_path


async def append_failure_row(
    pdf_bytes: bytes,
    filename: str,
    error_message: str,
) -> tuple[Path, Path]:
    now = datetime.now()
    timestamp = now.isoformat(timespec="seconds")
    today = now.strftime("%Y%m%d")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _write_failure_sync,
        pdf_bytes,
        filename,
        error_message,
        timestamp,
        today,
    )


def _write_ocr_output_sync(
    text: str,
    filename: str,
    processing_timestamp: str,
    today: str,
) -> Path:
    day_folder = _OCR_OUTPUTS_ROOT / today
    day_folder.mkdir(parents=True, exist_ok=True)

    txt_name = f"{Path(filename).stem}_{processing_timestamp}.txt"
    dest_path = day_folder / txt_name
    dest_path.write_text(text, encoding="utf-8")

    enforce_dated_folder_retention(_OCR_OUTPUTS_ROOT, settings.retention_max_days)

    logger.info("OCR output written to %s", dest_path)
    return dest_path


async def append_ocr_output(
    text: str,
    filename: str,
    processing_timestamp: str,
) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _write_ocr_output_sync,
        text,
        filename,
        processing_timestamp,
        today,
    )