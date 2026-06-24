from __future__ import annotations

"""
single_router.py
----------------
POST /extract/single

"Submit & Forget" endpoint: accepts one PDF, validates it, schedules a
background OCR→LLM extraction task, and immediately returns a plain JSON
acknowledgement.  The HTTP connection closes before any OCR or LLM work
begins — the client never learns the outcome.

Pipeline (server-side, detached from the HTTP connection):
  1. Acquire global OCR lock  (one PDF through OCR at a time, across ALL users)
  2. Run PaddleOCR            (single attempt, no retry; timeout enforced)
  3. Release OCR lock
  4. Acquire LLM semaphore    (capped at settings.llm_max_concurrent)
  5. Run LLM extraction       (up to settings.llm_max_retries + 1 attempts,
                               exponential back-off + jitter between attempts)
  6. Release LLM semaphore

On success  → append one row to single/permanent extractions.csv
On failure  → copy failed PDF + append one row to failed/{DDMMYYYY}_FAILED/
"""

import asyncio
import logging
import random
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractionResult
from app.services.file_service import validate_and_read_upload
from app.services.llm_client import LLMClient
from app.services.paddle_ocr import process_pdf

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/extract", tags=["Single Extraction"])

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

# Single, permanently-appended success CSV — never rotated, never date-split.
_EXTRACTIONS_CSV = Path("single_outputs/extractions.csv")

# Root of the per-day failed-files folders.
_FAILED_ROOT = Path("failed")

# Ensure base directories exist at import time so first-write never has to
# create them (avoids a race between concurrent background tasks).
_EXTRACTIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
_FAILED_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Concurrency primitives
# ---------------------------------------------------------------------------

# OCR runs one file at a time, globally, across ALL  users.
# Queue enforces strict FIFO order
_ocr_queue: asyncio.Queue = asyncio.Queue()

# LLM semaphore: mirrors the pattern in router.py; capped at
# settings.llm_max_concurrent (default 3, matching pod count).
_llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(settings.llm_max_concurrent)

# Guards concurrent appends to _EXTRACTIONS_CSV so two LLM completions
# arriving at nearly the same instant cannot interleave their writes.
_csv_append_lock: asyncio.Lock = asyncio.Lock()

_ocr_worker_task: asyncio.Task | None = None

def start_ocr_worker() -> None:
    global _ocr_worker_task
    _ocr_worker_task = asyncio.create_task(_ocr_worker(), name="ocr_worker")
    logger.info("Single-file OCR worker started.")

# ---------------------------------------------------------------------------
# In-flight background task tracking (bounded concurrency + graceful drain)
# ---------------------------------------------------------------------------

# Every scheduled background task is tracked here from creation until it
# finishes (success, failure, or cancellation). Used for two purposes:
#   1. Capacity check in the endpoint: reject new uploads with 503 once
#      len(_pending_tasks) >= settings.single_max_pending_tasks.
#   2. Graceful shutdown: app/main.py awaits drain_pending_tasks() so
#      in-flight OCR/LLM work gets a bounded chance to finish (and write
#      its CSV/failed-folder row) instead of being abandoned mid-pipeline
#      when the process exits.
_pending_tasks: set[asyncio.Task] = set()


def pending_task_count() -> int:
    """Total work in flight: queued for OCR + active LLM tasks."""
    return _ocr_queue.qsize() + len(_pending_tasks)


async def drain_pending_tasks(timeout: float | None = None) -> None:
    """
    Wait for all currently in-flight single-extraction tasks to finish,
    up to `timeout` seconds (defaults to settings.single_shutdown_drain_seconds).

    Intended to be called from app/main.py's lifespan shutdown phase so a
    redeploy/restart doesn't silently drop PDFs that are mid-OCR or mid-LLM —
    each task is responsible for writing its own success/failure record once
    it completes, so giving it a chance to finish is what prevents data loss.

    Tasks still running after the timeout are left to be cancelled by the
    event loop shutdown itself; this is a best-effort grace period, not a
    guarantee, since a single OCR call can itself take up to
    settings.ocr_timeout_seconds.
    """
    effective_timeout = timeout if timeout is not None else settings.single_shutdown_drain_seconds

    if not _pending_tasks:
        return

    logger.info(
        "Draining %d in-flight single-extraction task(s) (timeout=%.0fs)...",
        len(_pending_tasks), effective_timeout,
    )
    done, pending = await asyncio.wait(_pending_tasks, timeout=effective_timeout)

    if pending:
        logger.warning(
            "%d single-extraction task(s) did not finish within the drain "
            "window and will be abandoned on shutdown.", len(pending),
        )
    else:
        logger.info("All in-flight single-extraction task(s) finished cleanly.")


def _track_task(task: asyncio.Task) -> None:
    """Register a background task for capacity counting + graceful drain,
    and ensure it's automatically removed once it finishes."""
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)

# ---------------------------------------------------------------------------
# LLM client (one shared instance — LLMClient is stateless/reentrant)
# ---------------------------------------------------------------------------
_llm_client = LLMClient()

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

_SUCCESS_CSV_HEADER = "filename,bank_name,fi_num,master_account_number,sub_account_number\r\n"
_FAILED_CSV_HEADER  = "filename,error_message,timestamp\r\n"


def _escape_csv_field(value: str | None) -> str:
    """RFC-4180 CSV field escaping: wrap in quotes if the value contains
    commas, double-quotes, or newlines; escape inner double-quotes by doubling."""
    if value is None:
        return ""
    s = str(value)
    if "," in s or '"' in s or "\n" in s or "\r" in s:
        s = '"' + s.replace('"', '""') + '"'
    return s


async def _append_success_row(result: ExtractionResult, filename: str) -> None:
    """Append one success row to the permanent extractions.csv.

    Guarded by _csv_append_lock so concurrent LLM completions never
    interleave writes.  Opens in append mode each time so the file handle
    is held only for the duration of a single write — safe across restarts.
    """
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
    """Synchronous helper (called via run_in_executor) that:
      1. Creates/reuses today's failed-files folder.
      2. Writes the failed PDF bytes there (original filename, no renaming).
      3. Appends one row to that day's failure CSV.

    Both operations happen together so a failure never produces one without
    the other.  The PDF overwrite tradeoff (two same-day same-name failures)
    is accepted per plan Section 5.
    """
    today = datetime.now().strftime("%d%m%Y")
    folder_name = f"{today}_FAILED"
    folder_path = _FAILED_ROOT / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    # 1. Copy failed PDF — original filename kept verbatim.
    dest_pdf = folder_path / Path(filename).name
    dest_pdf.write_bytes(pdf_bytes)

    # 2. Append to the day's failure CSV.
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


async def _append_failure_row(
    pdf_bytes: bytes,
    filename: str,
    error_message: str,
) -> None:
    """Schedule the synchronous failure-write off the event loop so it
    doesn't block other background tasks during disk I/O."""
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

# ---------------------------------------------------------------------------
# OCR wrapper
# ---------------------------------------------------------------------------

async def _run_ocr(pdf_bytes: bytes, filename: str) -> str:
    """Write PDF bytes to a temp file, run PaddleOCR via executor, return text.

    Wrapped in asyncio.wait_for to enforce settings.ocr_timeout_seconds —
    without this a single malformed PDF would block the OCR lock indefinitely
    and stall every other queued file (open item #11 from the plan, resolved
    here by always enforcing the timeout).
    """
    suffix = Path(filename).suffix or ".pdf"
    tmp_path: Path | None = None

    try:
        # Write to a named temp file (PaddleOCR needs a file path, not bytes).
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        loop = asyncio.get_running_loop()

        # Enforce OCR timeout so a hanging PDF cannot monopolise the OCR lock.
        text: str = await asyncio.wait_for(
            loop.run_in_executor(None, process_pdf, str(tmp_path)),
            timeout=settings.ocr_timeout_seconds,
        )

        logger.debug(
            "OCR completed for '%s': %d characters extracted", filename, len(text)
        )
        return text

    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# LLM extraction wrapper
# ---------------------------------------------------------------------------

async def _run_llm_extraction(ocr_text: str, filename: str) -> ExtractionResult:
    """Build the prompt, call the LLM, and return a typed ExtractionResult.

    The semaphore is *not* held here — callers acquire/release it around
    this call so they can control the retry/backoff cycle independently.
    """
    prompt = build_extraction_prompt(ocr_text)
    llm_result = await _llm_client.extract_fields(
        prompt,
        stop=[
            "} {", "\n} {", "\n}{", "}\n{", "}\r\n{",
            "}\n\n", "}\r\n\r\n", "}\n ", "} \n",
            "}\n#", "}\n`", "\n}\n ", "\n}\n#",
            "\n}\n`", "\n}\n\n", "\n}\r\n\r\n",
        ],
    )
    return ExtractionResult(
        name=llm_result.get("name"),
        master_account_number=llm_result.get("master_account_number"),
        sub_account_number=llm_result.get("sub_account_number"),
        address=llm_result.get("address"),
        fi_num=llm_result.get("fi_num"),
        bank_name=llm_result.get("bank_name"),
    )

# ---------------------------------------------------------------------------
# Background pipeline task
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OCR worker — single consumer, drains _ocr_queue strictly in FIFO order
# ---------------------------------------------------------------------------

async def _ocr_worker() -> None:
    """
    The sole consumer of _ocr_queue. Processes one file at a time,
    in the exact order uploads arrived. After OCR completes, fires
    _llm_stage as an independent background task so the worker can
    immediately pick up the next file — OCR and LLM run in parallel
    across different files.
    """
    while True:
        pdf_bytes, filename = await _ocr_queue.get()
        logger.info("OCR worker picked up '%s'", filename)

        try:
            ocr_text = await _run_ocr(pdf_bytes, filename)

        except asyncio.TimeoutError:
            msg = f"OCR timed out after {settings.ocr_timeout_seconds}s"
            logger.error("OCR timeout for '%s': %s", filename, msg)
            await _append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        except Exception as exc:
            msg = f"OCR failed: {exc}"
            logger.error("OCR error for '%s': %s", filename, exc, exc_info=True)
            await _append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        if not ocr_text or not ocr_text.strip():
            msg = "OCR returned empty text — no content extracted from PDF"
            logger.warning("Empty OCR output for '%s'", filename)
            await _append_failure_row(pdf_bytes, filename, msg)
            _ocr_queue.task_done()
            continue

        # OCR succeeded — release the large buffer from this scope.
        # Pass it to _llm_stage only for the LLM-failure path.
        # If LLM succeeds, _llm_stage lets it go out of scope naturally.
        failure_bytes = pdf_bytes
        pdf_bytes = None  # allow GC on the local reference

        task = asyncio.create_task(
            _llm_stage(ocr_text, filename, failure_bytes),
            name=f"llm_stage:{filename}",
        )
        _track_task(task)
        _ocr_queue.task_done()


# ---------------------------------------------------------------------------
# LLM stage — runs concurrently across files, capped by _llm_semaphore
# ---------------------------------------------------------------------------

async def _llm_stage(ocr_text: str, filename: str, pdf_bytes: bytes) -> None:
    """
    Runs LLM extraction with exponential back-off retry.
    Up to settings.llm_max_concurrent of these run simultaneously.
    On success  → appends row to extractions.csv
    On failure  → copies PDF + appends row to failed/ folder
    """
    last_exc: Exception | None = None
    total_attempts = settings.llm_max_retries + 1

    for attempt in range(total_attempts):
        if attempt > 0:
            backoff = settings.llm_retry_base_backoff * (2 ** (attempt - 1))
            jitter = random.uniform(0.0, 1.0)
            wait = backoff + jitter
            logger.warning(
                "LLM retry %d/%d for '%s' — sleeping %.1fs",
                attempt, settings.llm_max_retries, filename, wait,
            )
            # Semaphore NOT held during sleep — other tasks compete freely.
            await asyncio.sleep(wait)

        async with _llm_semaphore:
            logger.debug(
                "LLM semaphore acquired for '%s' (attempt %d/%d)",
                filename, attempt + 1, total_attempts,
            )
            try:
                result = await _run_llm_extraction(ocr_text, filename)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "LLM attempt %d/%d failed for '%s': %s",
                    attempt + 1, total_attempts, filename, exc,
                )
                continue  # semaphore released by async-with

        # ------ SUCCESS — semaphore already released ------
        logger.info(
            "LLM extraction succeeded for '%s' (attempt %d/%d)",
            filename, attempt + 1, total_attempts,
        )
        await _append_success_row(result, filename)
        return  # pdf_bytes goes out of scope here, GC reclaims it

    # ------ ALL ATTEMPTS EXHAUSTED ------
    msg = f"LLM failed after {total_attempts} attempt(s): {last_exc}"
    logger.error(
        "LLM permanently failed for '%s': %s", filename, last_exc, exc_info=True,
    )
    await _append_failure_row(pdf_bytes, filename, msg)

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/single",
    summary="Submit & Forget — queue one PDF for background OCR → LLM extraction",
    response_class=JSONResponse,
    responses={
        200: {
            "description": "File accepted and queued. Connection closes immediately.",
            "content": {"application/json": {"example": {"success": True, "message": "File queued successfully."}}},
        },
        422: {
            "description": "Validation failure (unsupported type, file too large, etc.).",
            "content": {"application/json": {"example": {"success": False, "message": "<reason>"}}},
        },
        503: {
            "description": "Server is at capacity — too many files already queued/processing.",
            "content": {"application/json": {"example": {"success": False, "message": "Server is at capacity. Please retry shortly."}}},
        },
    },
)
async def extract_single(
    file: UploadFile = File(..., description="Single PDF file to process"),
) -> JSONResponse:
    """Accept one PDF, validate it, schedule a background extraction task,
    and return immediately.

    The client's connection closes as soon as this response is sent.  No
    further communication about this file will ever be sent to the caller —
    OCR and LLM results are handled entirely server-side.

    Success output  → single_outputs/extractions.csv
    Failure output  → failed/{DDMMYYYY}_FAILED/ folder

    Capacity guard: if settings.single_max_pending_tasks background tasks
    are already in flight, the upload is rejected with 503 rather than
    being accepted and piling up unbounded in memory. Checked before reading
    the file body so an over-capacity request is rejected as cheaply as
    possible.
    """
    # ------------------------------------------------------------------
    # Capacity guard — reject before doing any work if we're already at
    # the in-flight task ceiling.
    # ------------------------------------------------------------------
    if pending_task_count() >= settings.single_max_pending_tasks:
        logger.warning(
            "Rejecting upload '%s' — at capacity (%d/%d in-flight tasks)",
            file.filename, pending_task_count(), settings.single_max_pending_tasks,
        )
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "Server is at capacity. Please retry shortly.",
            },
        )

    # ------------------------------------------------------------------
    # Validate: extension + size.  validate_and_read_upload raises
    # HTTPException on invalid input, which FastAPI turns into the
    # appropriate 4xx response — nothing extra needed here.
    # ------------------------------------------------------------------
    try:
        pdf_bytes, _ext = await validate_and_read_upload(file)
    except Exception as exc:
        # Re-raise as a structured JSON response with success=false so
        # callers that check the body (rather than HTTP status) still get
        # a consistent shape.  FastAPI's global exception handler will also
        # catch HTTPException if we let it propagate — either is fine, but
        # this makes the response shape unambiguous for API consumers.
        logger.warning("Validation failed for '%s': %s", file.filename, exc)
        return JSONResponse(
            status_code=getattr(exc, "status_code", 422),
            content={"success": False, "message": getattr(exc, "detail", str(exc))},
        )

    filename = file.filename or "uploaded_file.pdf"
    logger.info("File accepted and queued for background processing: '%s'", filename)

    await _ocr_queue.put((pdf_bytes, filename))
    logger.info("File queued for OCR: '%s' (queue size now ~%d)", filename, _ocr_queue.qsize())

    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "File queued successfully."},
    )