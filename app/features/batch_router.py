from __future__ import annotations

"""
batch_router.py
---------------
POST /extract/batch
GET  /extract/batch/download/{year}/{month}/{day}/{filename}

Accepts multiple PDF or TXT files in a single multipart/form-data request.
Processes each file through a two-stage pipeline:
  Stage 1: PaddleOCR  (runs concurrently with Stage 2)
  Stage 2: LLM extraction (runs concurrently with Stage 1)

Both stages retry up to _MAX_ATTEMPTS times before writing an error row and
moving on.

CSV columns: filename, bank_name, fi_num, master_account_number, sub_account_number
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import get_settings
from app.features.router import _run_extraction, _pdf_to_text_via_paddleocr
from app.services.file_service import decode_txt_bytes, validate_and_read_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract", tags=["Batch Extraction"])
settings = get_settings()

# ---------------------------------------------------------------------------
# Output directory root
# ---------------------------------------------------------------------------
_OUTPUT_ROOT = Path("batch_outputs")

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 3
_RETRY_TIMEOUT_INCREMENT = 180.0

# ---------------------------------------------------------------------------
# Pipeline sentinel
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
    if "," in s or '"' in s or "\n" in s or "\r" in s:
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
    fields = [filename, "", "", "", ""]
    return ",".join(_escape_csv_field(f) for f in fields) + "\r\n"


def _comment(message: str) -> str:
    return f"# {message}\r\n"


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

async def _with_retry(label: str, base_timeout: float, coro_fn, *args, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        current_timeout = base_timeout + (attempt - 1) * _RETRY_TIMEOUT_INCREMENT
        try:
            logger.debug(
                "%s — attempt %d/%d timeout=%.0fs",
                label, attempt, _MAX_ATTEMPTS, current_timeout,
            )
            return await coro_fn(*args, timeout=current_timeout, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "%s — attempt %d/%d failed (timeout=%.0fs): %s — retrying",
                    label, attempt, _MAX_ATTEMPTS, current_timeout, exc,
                )
            else:
                logger.error("%s — all %d attempts failed: %s", label, _MAX_ATTEMPTS, exc)
    raise last_exc


# ---------------------------------------------------------------------------
# Pipeline Stage 1 — PaddleOCR with retry
# Uses settings.ocr_timeout_seconds (dedicated OCR timeout, not LLM timeout)
# ---------------------------------------------------------------------------

async def _stage_ocr(files: list[UploadFile], queue: asyncio.Queue) -> None:
    for index, upload in enumerate(files, start=1):
        filename = upload.filename or f"file_{index}"
        file_start = time.monotonic()
        try:
            raw_bytes, ext = await validate_and_read_upload(upload)

            if ext == ".pdf":
                text = await _with_retry(
                    f"OCR {filename}",
                    settings.ocr_timeout_seconds,       # dedicated OCR timeout
                    _pdf_to_text_via_paddleocr, raw_bytes, filename,
                )
            else:
                # TXT — instant decode, no OCR needed
                text = decode_txt_bytes(raw_bytes)

            del raw_bytes
            await queue.put((index, filename, text, file_start, None))
            logger.debug("OCR done [%d] %s — queued for LLM", index, filename)

        except Exception as exc:
            await queue.put((index, filename, None, file_start, exc))
            logger.warning("OCR permanently failed [%d] %s: %s", index, filename, exc)

    await queue.put(_DONE)


# ---------------------------------------------------------------------------
# Core streaming generator
# ---------------------------------------------------------------------------

async def _stream_batch(files: list[UploadFile]) -> AsyncGenerator[str, None]:
    total = len(files)
    ok_count = 0
    error_count = 0
    batch_start = time.monotonic()

    now = datetime.now()
    date_folder = _OUTPUT_ROOT / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    date_folder.mkdir(parents=True, exist_ok=True)
    csv_filename = now.strftime("extraction_%Y-%m-%d.csv")
    csv_path = date_folder / csv_filename
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

                index, filename, text, file_start, ocr_error = item

                yield _comment(f"[{index}/{total}] processing LLM: {filename}")

                try:
                    if ocr_error is not None:
                        raise ocr_error

                    result = await _with_retry(
                        f"LLM {filename}",
                        settings.llm_timeout_seconds,   # LLM keeps its own timeout
                        _run_extraction,
                        original_text=text,
                        source=filename,
                    )

                    elapsed = time.monotonic() - file_start
                    ok_count += 1

                    csv_fh.write(_make_data_row(filename, result))
                    csv_fh.flush()

                    yield _comment(f"[{index}/{total}] done: {filename} — {elapsed:.1f}s")
                    logger.info("Batch [%d/%d] %s — %.1fs", index, total, filename, elapsed)

                except Exception as exc:
                    elapsed = time.monotonic() - file_start
                    error_count += 1

                    csv_fh.write(_make_error_row(filename))
                    csv_fh.flush()

                    yield _comment(
                        f"[{index}/{total}] error: {filename} — {elapsed:.1f}s — {exc}"
                    )
                    logger.warning(
                        "Batch [%d/%d] %s failed in %.1fs: %s",
                        index, total, filename, elapsed, exc,
                    )

                await asyncio.sleep(0)

            await ocr_task

        total_elapsed = time.monotonic() - batch_start
        yield _comment(f"done. {ok_count} ok / {error_count} error — total: {total_elapsed:.1f}s")
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
    summary="Batch extract fields from multiple files",
)
async def extract_batch(
    files: list[UploadFile] = File(..., description="PDF or TXT files to process"),
) -> StreamingResponse:
    if not files:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="No files provided.")

    max_files = settings.max_files_per_batch
    if len(files) > max_files:
        from fastapi import HTTPException
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
    year: str, month: str, day: str, filename: str
) -> FileResponse:
    from fastapi import HTTPException

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