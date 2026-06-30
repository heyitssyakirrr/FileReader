from __future__ import annotations

import asyncio
import logging
import random
import tempfile
import time
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.features.extraction.concurrency import (
    _llm_semaphore,
    _ocr_queue,
    _track_task,
)
from app.features.extraction.context import FileProcessingContext
from app.features.extraction.lifecycle import mark_terminal, recover_orphaned_files
from app.features.extraction.storage import append_failure_row, append_success_row, append_ocr_output
from app.features.extraction.summary_logs import append_file_summary
from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractionResult
from app.services.llm_client import LLMClient
from app.services.paddle_ocr import process_pdf

logger = logging.getLogger(__name__)
settings = get_settings()

_llm_client = LLMClient()


def _error_message(exc: Exception) -> str:
    return str(getattr(exc, "detail", None) or exc)


async def _record_failure(
    ctx: FileProcessingContext,
    pdf_bytes: bytes,
    stage: str,
    error_message: str,
) -> None:
    ctx.final_status = "failed"
    ctx.failed_stage = stage
    ctx.storage_status = "failure_written"
    failed_pdf_path, failed_csv_path = await append_failure_row(pdf_bytes, ctx.filename, error_message)
    ctx.failed_pdf_path = str(failed_pdf_path)
    ctx.failed_csv_path = str(failed_csv_path)
    ctx.completed_at = datetime.now()
    await append_file_summary(ctx)
    if ctx.intake_id:
        await mark_terminal(ctx.intake_id, ctx.filename, "failed")


async def _run_ocr(ctx: FileProcessingContext, pdf_bytes: bytes) -> str:
    suffix = Path(ctx.filename).suffix or ".pdf"
    tmp_path: Path | None = None
    started = time.perf_counter()
    ctx.ocr_status = "running"

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        loop = asyncio.get_running_loop()
        text: str = await asyncio.wait_for(
            loop.run_in_executor(None, process_pdf, str(tmp_path)),
            timeout=settings.ocr_timeout_seconds,
        )

        ctx.ocr_duration_ms = int((time.perf_counter() - started) * 1000)
        ctx.ocr_char_count = len(text)
        ctx.ocr_status = "completed"
        logger.debug(
            "OCR completed for '%s' (run=%s): %d characters extracted",
            ctx.filename,
            ctx.processing_timestamp,
            len(text),
        )
        return text

    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


async def _run_llm_extraction(ctx: FileProcessingContext, ocr_text: str) -> ExtractionResult:
    prompt = build_extraction_prompt(ocr_text)
    ctx.llm_prompt_length_chars = len(prompt)

    llm_result, metadata = await _llm_client.extract_fields(
        prompt,
        stop=[
            "} {", "\n} {", "\n}{", "}\n{", "}\r\n{",
            "}\n\n", "}\r\n\r\n", "}\n ", "} \n",
            "}\n#", "}\n`", "\n}\n ", "\n}\n#",
            "\n}\n`", "\n}\n\n", "\n}\r\n\r\n",
        ],
    )

    ctx.llm_http_duration_ms = metadata.http_duration_ms
    ctx.llm_status_code = metadata.status_code
    ctx.llm_response_length_chars = metadata.response_length_chars
    ctx.llm_parse_strategy = metadata.parse_strategy
    ctx.llm_json_objects_found = metadata.json_objects_found
    ctx.llm_response_truncated = metadata.response_truncated
    ctx.llm_keys_present = metadata.keys_present or []
    ctx.llm_keys_missing = metadata.keys_missing or []

    return ExtractionResult(
        name=llm_result.get("name"),
        master_account_number=llm_result.get("master_account_number"),
        sub_account_number=llm_result.get("sub_account_number"),
        fi_num=llm_result.get("fi_num"),
        bank_name=llm_result.get("bank_name"),
    )


async def _llm_stage(ctx: FileProcessingContext, ocr_text: str, pdf_bytes: bytes) -> None:
    try:
        await _llm_stage_inner(ctx, ocr_text, pdf_bytes)
    except Exception as exc:
        logger.error(
            "Unexpected crash in _llm_stage for '%s' (run=%s): %s",
            ctx.filename, ctx.processing_timestamp, exc, exc_info=True,
        )

        if ctx.final_status != "failed" or not ctx.failed_csv_path:
            try:
                await _record_failure(
                    ctx, pdf_bytes, ctx.failed_stage or "unexpected",
                    f"Unexpected error: {_error_message(exc)}",
                )
            except Exception:
                logger.exception(
                    "Failed to record failure for '%s' (run=%s) after an "
                    "unexpected crash; this file will be recovered from "
                    "its inflight copy on next startup instead.",
                    ctx.filename, ctx.processing_timestamp,
                )
        return

    # _llm_stage_inner already handled its own terminal recording
    # (success or anticipated failure) on every normal return path.


async def _llm_stage_inner(ctx: FileProcessingContext, ocr_text: str, pdf_bytes: bytes) -> None:
    last_exc: Exception | None = None
    total_attempts = settings.llm_max_retries + 1
    ctx.llm_status = "running"
    ctx.llm_max_attempts = total_attempts
    llm_started = time.perf_counter()

    for attempt in range(total_attempts):
        ctx.llm_attempts = attempt + 1
        if attempt > 0:
            backoff = settings.llm_retry_base_backoff * (2 ** (attempt - 1))
            jitter = random.uniform(0.0, 1.0)
            wait = backoff + jitter
            logger.warning(
                "LLM retry %d/%d for '%s' (run=%s) - sleeping %.1fs",
                attempt,
                settings.llm_max_retries,
                ctx.filename,
                ctx.processing_timestamp,
                wait,
            )
            await asyncio.sleep(wait)

        async with _llm_semaphore:
            logger.debug(
                "LLM semaphore acquired for '%s' (run=%s, attempt %d/%d)",
                ctx.filename,
                ctx.processing_timestamp,
                attempt + 1,
                total_attempts,
            )
            try:
                result = await _run_llm_extraction(ctx, ocr_text)
            except Exception as exc:
                last_exc = exc
                ctx.llm_last_error_type = type(exc).__name__
                ctx.llm_last_error_message = _error_message(exc)
                logger.warning(
                    "LLM attempt %d/%d failed for '%s' (run=%s): %s",
                    attempt + 1,
                    total_attempts,
                    ctx.filename,
                    ctx.processing_timestamp,
                    _error_message(exc),
                )
                continue

        ctx.llm_status = "succeeded"
        ctx.llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)
        logger.info(
            "LLM extraction succeeded for '%s' (run=%s, attempt %d/%d)",
            ctx.filename,
            ctx.processing_timestamp,
            attempt + 1,
            total_attempts,
        )
        csv_path = await append_success_row(result, ctx.filename)
        ctx.storage_status = "written"
        ctx.storage_output_path = str(csv_path)
        ctx.final_status = "success"
        ctx.completed_at = datetime.now()
        await append_file_summary(ctx)
        if ctx.intake_id:
            await mark_terminal(ctx.intake_id, ctx.filename, "success")
        return

    msg = f"LLM failed after {total_attempts} attempt(s): {_error_message(last_exc) if last_exc else 'unknown error'}"
    ctx.llm_status = "permanently_failed"
    ctx.llm_duration_ms = int((time.perf_counter() - llm_started) * 1000)
    logger.error("LLM permanently failed for '%s' (run=%s): %s", ctx.filename, ctx.processing_timestamp, msg)
    await _record_failure(ctx, pdf_bytes, "llm", msg)


async def _ocr_worker() -> None:
    while True:
        ctx, pdf_bytes = await _ocr_queue.get()
        logger.info("OCR worker picked up '%s' (run=%s)", ctx.filename, ctx.processing_timestamp)

        try:
            ocr_text = await _run_ocr(ctx, pdf_bytes)

        except asyncio.TimeoutError:
            msg = f"OCR timed out after {settings.ocr_timeout_seconds}s"
            ctx.ocr_status = "timeout"
            ctx.ocr_error_type = "TimeoutError"
            ctx.ocr_error_message = msg
            logger.error("OCR timeout for '%s' (run=%s): %s", ctx.filename, ctx.processing_timestamp, msg)
            await _record_failure(ctx, pdf_bytes, "ocr", msg)
            _ocr_queue.task_done()
            continue

        except Exception as exc:
            msg = f"OCR failed: {exc}"
            ctx.ocr_status = "failed"
            ctx.ocr_error_type = type(exc).__name__
            ctx.ocr_error_message = str(exc)
            logger.error("OCR error for '%s' (run=%s): %s", ctx.filename, ctx.processing_timestamp, exc, exc_info=True)
            await _record_failure(ctx, pdf_bytes, "ocr", msg)
            _ocr_queue.task_done()
            continue

        if not ocr_text or not ocr_text.strip():
            msg = "OCR returned empty text - no content extracted from PDF"
            ctx.ocr_status = "empty"
            ctx.ocr_error_type = "EmptyOCROutput"
            ctx.ocr_error_message = msg
            logger.warning("Empty OCR output for '%s' (run=%s)", ctx.filename, ctx.processing_timestamp)
            await _record_failure(ctx, pdf_bytes, "ocr", msg)
            _ocr_queue.task_done()
            continue

        try:
            ocr_output_path = await append_ocr_output(ocr_text, ctx.filename, ctx.processing_timestamp)
            ctx.ocr_output_path = str(ocr_output_path)
        except Exception as exc:
            logger.warning("Failed to persist OCR output for '%s' (run=%s): %s", ctx.filename, ctx.processing_timestamp, exc)

        failure_bytes = pdf_bytes
        pdf_bytes = None

        task = asyncio.create_task(
            _llm_stage(ctx, ocr_text, failure_bytes),
            name=f"llm_stage:{ctx.filename}:{ctx.processing_timestamp}",
        )
        _track_task(task)
        _ocr_queue.task_done()


_ocr_worker_task: asyncio.Task | None = None


async def _recovery_record_failure(
    filename: str,
    pdf_bytes: bytes,
    stage: str,
    error_message: str,
) -> None:
    now = datetime.now()
    ctx = FileProcessingContext(
        filename=filename,
        processing_timestamp=now.strftime("%Y%m%d_%H%M%S_%f")[:-3],
        received_at=now,
        file_size_bytes=len(pdf_bytes),
        queue_depth_at_upload=0,
    )
    ctx.final_status = "failed"
    ctx.failed_stage = stage
    ctx.storage_status = "failure_written"
    failed_pdf_path, failed_csv_path = await append_failure_row(pdf_bytes, filename, error_message)
    ctx.failed_pdf_path = str(failed_pdf_path)
    ctx.failed_csv_path = str(failed_csv_path)
    ctx.completed_at = datetime.now()
    await append_file_summary(ctx)


async def start_ocr_worker() -> None:
    global _ocr_worker_task

    recovered_count = await recover_orphaned_files(_recovery_record_failure)
    if recovered_count:
        logger.warning(
            "Startup recovery: %d file(s) left in-flight by a previous "
            "run were written to failed.csv (stage='interrupted').",
            recovered_count,
        )

    _ocr_worker_task = asyncio.create_task(_ocr_worker(), name="ocr_worker")
    logger.info("Single-file OCR worker started.")