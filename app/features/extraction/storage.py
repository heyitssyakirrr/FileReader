from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from app.features.extraction.concurrency import _csv_append_lock
from app.models.schemas import ExtractionResult

logger = logging.getLogger(__name__)

_EXTRACTIONS_CSV = Path("results/extractions.csv")
_FAILED_ROOT = Path("failed")

_EXTRACTIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
_FAILED_ROOT.mkdir(parents=True, exist_ok=True)

_SUCCESS_CSV_HEADER = "filename,bank_name,fi_num,master_account_number,sub_account_number\r\n"
_FAILED_CSV_HEADER  = "filename,error_message,timestamp\r\n"


def _escape_csv_field(value: str | None) -> str:
    if value is None:
        return ""
    s = str(value)
    if "," in s or '"' in s or "\n" in s or "\r" in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


async def append_success_row(result: ExtractionResult, filename: str) -> None:
    row = ",".join(_escape_csv_field(v) for v in [
        filename,
        result.bank_name,
        result.fi_num,
        result.master_account_number,
        result.sub_account_number,
    ]) + "\r\n"

    async with _csv_append_lock:
        write_header = not _EXTRACTIONS_CSV.exists() or _EXTRACTIONS_CSV.stat().st_size == 0
        with _EXTRACTIONS_CSV.open("a", encoding="utf-8", newline="") as fh:
            if write_header:
                fh.write(_SUCCESS_CSV_HEADER)
            fh.write(row)
            fh.flush()

    logger.info("Success row written to %s for file '%s'", _EXTRACTIONS_CSV, filename)


def _write_failure_sync(
    pdf_bytes: bytes,
    filename: str,
    error_message: str,
    timestamp: str,
) -> None:
    today = datetime.now().strftime("%d%m%Y")
    folder_name = f"{today}_FAILED"
    folder_path = _FAILED_ROOT / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    dest_pdf = folder_path / Path(filename).name
    dest_pdf.write_bytes(pdf_bytes)

    csv_path = folder_path / f"{folder_name}.csv"
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

    logger.info(
        "Failure recorded for '%s' in %s (error: %s)", filename, folder_path, error_message
    )


async def append_failure_row(
    pdf_bytes: bytes,
    filename: str,
    error_message: str,
) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _write_failure_sync,
        pdf_bytes,
        filename,
        error_message,
        timestamp,
    )