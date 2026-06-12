from __future__ import annotations

"""
batch_router.py
---------------
POST /extract/batch
GET  /extract/batch/download/{year}/{month}/{day}/{filename}

Accepts multiple PDF files in a single multipart/form-data request.
Processes each file through a two-stage concurrent pipeline:

    Stage 1 — PaddleOCR   (runs sequentially; one PDF at a time)
    Stage 2 — LLM extract (fires independently per file as OCR completes)

Both stages retry up to _MAX_ATTEMPTS times (via the shared ``with_retry``
utility) before writing an error row and moving on.

Timing is owned entirely by the server:
    - ``ocr_start / ocr_end`` are set around the OCR call.
    - ``llm_start / llm_end`` are set around the LLM call.
    - At the end of every batch an audit XLSX is written automatically
      using ``AuditRecord`` objects populated with those timestamps.
    - The browser never sends timing data.

CSV columns: filename, bank_name, fi_num, master_account_number, sub_account_number
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.features.router import _run_extraction, _pdf_to_text_via_paddleocr
from app.services.audit_service import AuditRecord, write_audit_xlsx
from app.services.file_service import decode_txt_bytes, validate_and_read_upload
from app.services.retry import with_retry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract", tags=["Batch Extraction"])
settings = get_settings()

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
_OUTPUT_ROOT = Path("batch_outputs")

# ---------------------------------------------------------------------------
# Pipeline sentinel — signals end of OCR queue to the consumer loop
# ---------------------------------------------------------------------------
_DONE = object()

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

_CSV_HEADER = "filename,bank_name,fi_num,master_account_number,sub_account_number\r\n"


def _escape_csv_field(value: str | None) -> str:
    if value is None:
        return ""
    s = str(value)
    if any(c in s for c in (',', '"', '\n', '\r')):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _make_data_row(filename: str, result) -> str:
    d = result.data
    fields = [
        filename,
        d.bank_name,
        d.fi_num,
        d.master_account_number,
        d.sub_account_number,
    ]
    return ",".join(_escape_csv_field(f) for f in fields) + "\r\n"


def _make_error_row(filename: str) -> str:
    return ",".join(_escape_csv_field(f) for f in [filename, "", "", "", ""]) + "\r\n"


def _comment(message: str) -> str:
    return f"# {message}\r\n"


# ---------------------------------------------------------------------------
# Stage 1 — PaddleOCR (sequential, one PDF at a time)
#
# Each item placed on the queue is a 6-tuple:
#   (index, filename, text | None, ocr_start, ocr_end, exc | None)
# ---------------------------------------------------------------------------

async def _stage_ocr(files: list[UploadFile], queue: asyncio.Queue) -> None:
    for index, upload in enumerate(files, start=1):
        filename = upload.filename or f"file_{index}"
        ocr_start = datetime.now(tz=timezone.utc)
        try:
            raw_bytes, ext = await validate_and_read_upload(upload)

            if ext == ".pdf":
                text = await with_retry(
                    f"OCR {filename}",
                    settings.ocr_timeout_seconds,
                    _pdf_to_text_via_paddleocr,
                    raw_bytes,
                    filename,
                )
            else:
                text = decode_txt_bytes(raw_bytes)

            del raw_bytes
            ocr_end = datetime.now(tz=timezone.utc)
            await queue.put((index, filename, text, ocr_start, ocr_end, None))
            logger.debug("OCR done [%d] %s — queued for LLM", index, filename)

        except Exception as exc:
            ocr_end = datetime.now(tz=timezone.utc)
            await queue.put((index, filename, None, ocr_start, ocr_end, exc))
            logger.warning("OCR permanently failed [%d] %s: %s", index, filename, exc)

    await queue.put(_DONE)


# ---------------------------------------------------------------------------
# Core streaming generator
# ---------------------------------------------------------------------------

async def _stream_batch(files: list[UploadFile]) -> AsyncGenerator[str, None]:
    total       = len(files)
    ok_count    = 0
    error_count = 0
    batch_start = time.monotonic()

    # One audit record per file; populated as the pipeline progresses
    audit_records: list[AuditRecord] = []

    # ── Output CSV setup ────────────────────────────────────────────────────
    now         = datetime.now()
    date_folder = _OUTPUT_ROOT / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    date_folder.mkdir(parents=True, exist_ok=True)
    csv_filename = now.strftime("extraction_%Y-%m-%d.csv")
    csv_path     = date_folder / csv_filename
    download_url = (
        f"/extract/batch/download"
        f"/{now.strftime('%Y')}/{now.strftime('%m')}/{now.strftime('%d')}"
        f"/{csv_filename}"
    )

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    try:
        with csv_path.open("w", encoding="utf-8", newline="") as csv_fh:
            csv_fh.write(_CSV_HEADER)
            csv_fh.flush()

            ocr_task = asyncio.create_task(_stage_ocr(files, queue))

            while True:
                item = await queue.get()
                if item is _DONE:
                    break

                index, filename, text, ocr_start, ocr_end, ocr_error = item

                # Create the audit record for this file; llm timing added below
                record = AuditRecord(
                    filename=filename,
                    ocr_start=ocr_start,
                    ocr_end=ocr_end,
                )
                audit_records.append(record)

                yield _comment(f"[{index}/{total}] processing LLM: {filename}")

                if ocr_error is not None:
                    # OCR failed — no LLM call
                    record.extract_error = str(ocr_error)
                    error_count += 1
                    csv_fh.write(_make_error_row(filename))
                    csv_fh.flush()
                    yield _comment(
                        f"[{index}/{total}] error (OCR): {filename} — {ocr_error}"
                    )
                    await asyncio.sleep(0)
                    continue

                # ── LLM extraction with server-side timing ──────────────────
                llm_start = datetime.now(tz=timezone.utc)
                try:
                    result = await with_retry(
                        f"LLM {filename}",
                        settings.llm_timeout_seconds,
                        _run_extraction,
                        original_text=text,
                        source=filename,
                    )
                    llm_end = datetime.now(tz=timezone.utc)

                    record.llm_start      = llm_start
                    record.llm_end        = llm_end
                    record.extract_result = result.model_dump()

                    ok_count += 1
                    csv_fh.write(_make_data_row(filename, result))
                    csv_fh.flush()

                    elapsed = (llm_end - ocr_start).total_seconds()
                    yield _comment(
                        f"[{index}/{total}] done: {filename} — {elapsed:.1f}s"
                    )
                    logger.info(
                        "Batch [%d/%d] %s — %.1fs", index, total, filename, elapsed
                    )

                except Exception as exc:
                    llm_end = datetime.now(tz=timezone.utc)
                    record.llm_start     = llm_start
                    record.llm_end       = llm_end
                    record.extract_error = str(exc)

                    error_count += 1
                    csv_fh.write(_make_error_row(filename))
                    csv_fh.flush()

                    elapsed = (llm_end - ocr_start).total_seconds()
                    yield _comment(
                        f"[{index}/{total}] error (LLM): {filename} — {elapsed:.1f}s — {exc}"
                    )
                    logger.warning(
                        "Batch [%d/%d] %s LLM failed in %.1fs: %s",
                        index, total, filename, elapsed, exc,
                    )

                await asyncio.sleep(0)

            await ocr_task

        # ── Write audit XLSX with accurate server-side timestamps ────────────
        if audit_records:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, write_audit_xlsx, audit_records)

        total_elapsed = time.monotonic() - batch_start
        yield _comment(
            f"done. {ok_count} ok / {error_count} error — total: {total_elapsed:.1f}s"
        )
        yield _comment(f"download: {download_url}")

        logger.info(
            "Batch complete — %d ok / %d error — %.1fs — saved: %s",
            ok_count, error_count, total_elapsed, csv_path,
        )

    except Exception as exc:
        logger.exception("Batch stream failed: %s", exc)
        yield _comment(f"fatal error: {exc}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/batch",
    response_class=StreamingResponse,
    summary="Batch extract fields from multiple PDF files",
)
async def extract_batch(
    files: list[UploadFile] = File(..., description="PDF files to process"),
) -> StreamingResponse:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided.")

    max_files = settings.max_files_per_batch
    if len(files) > max_files:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too many files. Received {len(files)}, "
                f"maximum allowed per request is {max_files}."
            ),
        )

    logger.info("Batch request received — %d file(s)", len(files))

    return StreamingResponse(
        _stream_batch(files),
        media_type="text/plain",
        headers={
            "X-Batch-File-Count": str(len(files)),
            "Cache-Control": "no-cache",
        },
    )


@router.get(
    "/batch/download/{year}/{month}/{day}/{filename}",
    summary="Download the CSV result for a completed batch run",
)
async def download_batch_result(
    year: str,
    month: str,
    day: str,
    filename: str,
) -> FileResponse:
    for segment in (year, month, day, filename):
        if ".." in segment or "/" in segment or "\\" in segment:
            raise HTTPException(status_code=400, detail="Invalid path.")

    csv_path = _OUTPUT_ROOT / year / month / day / filename

    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {year}/{month}/{day}/{filename}",
        )

    return FileResponse(
        path=csv_path,
        media_type="text/csv",
        filename=filename,
    )