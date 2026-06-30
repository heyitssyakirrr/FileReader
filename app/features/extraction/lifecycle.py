from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# On-disk layout (all under one root so ops/backup is a single folder)
# ---------------------------------------------------------------------------
_LIFECYCLE_ROOT = Path("uploads") / "inflight"
_FILES_DIR = _LIFECYCLE_ROOT / "files"
_RECORDS_DIR = _LIFECYCLE_ROOT / "records"

_FILES_DIR.mkdir(parents=True, exist_ok=True)
_RECORDS_DIR.mkdir(parents=True, exist_ok=True)

# Filenames are attacker-controlled (HTTP upload). Strip path separators
# and anything that isn't a safe filename character before ever using the
# value to build a path on disk.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_component(filename: str) -> str:
    name = Path(filename).name  # drop any directory components
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip("._") or "upload.pdf"
    return name[:150]  # keep total path length sane


@dataclass(frozen=True)
class IntakeRecord:
    """Returned to the caller right after a file is durably accepted."""

    intake_id: str
    inflight_path: Path


def _intake_filename(processing_timestamp: str, intake_id: str, filename: str) -> str:
    safe_name = _safe_filename_component(filename)
    return f"{processing_timestamp}__{intake_id}__{safe_name}"


def _record_path(intake_id: str) -> Path:
    return _RECORDS_DIR / f"{intake_id}.json"


def _write_record_sync(intake_id: str, record: dict[str, Any]) -> None:
    # Write-to-temp-then-rename so a hard kill mid-write can never leave a
    # half-written record file behind -- Path.replace is atomic on the
    # same filesystem, so the record file always either fully exists with
    # valid JSON, or doesn't exist at all.
    final_path = _record_path(intake_id)
    tmp_path = final_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(final_path)


async def _write_record(intake_id: str, record: dict[str, Any]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _write_record_sync, intake_id, record)


# ---------------------------------------------------------------------------
# Public API: intake
# ---------------------------------------------------------------------------
async def register_intake(filename: str, processing_timestamp: str, pdf_bytes: bytes) -> IntakeRecord:
    """
    Durably persist an accepted upload BEFORE it is queued for processing.

    Must be called synchronously in the request path, before the file is
    handed to the OCR queue. Once this returns, the file is guaranteed to
    be recoverable even if the process dies one line later.
    """
    intake_id = uuid.uuid4().hex[:12]
    inflight_name = _intake_filename(processing_timestamp, intake_id, filename)
    inflight_path = _FILES_DIR / inflight_name

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, inflight_path.write_bytes, pdf_bytes)

    await _write_record(intake_id, {
        "intake_id": intake_id,
        "filename": filename,
        "processing_timestamp": processing_timestamp,
        "inflight_path": str(inflight_path),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })

    logger.debug("Intake persisted for '%s' (intake_id=%s) at %s", filename, intake_id, inflight_path)
    return IntakeRecord(intake_id=intake_id, inflight_path=inflight_path)


# ---------------------------------------------------------------------------
# Public API: terminal state + cleanup
# ---------------------------------------------------------------------------
async def mark_terminal(intake_id: str, filename: str, final_status: str) -> None:
    """
    Call exactly once a file reaches success or failure. Deletes its record
    file and its staged PDF copy
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _clear_inflight_sync, intake_id)
    logger.debug(
        "Lifecycle resolved for '%s' (intake_id=%s, final_status=%s)",
        filename, intake_id, final_status,
    )


def _clear_inflight_sync(intake_id: str) -> None:
    _record_path(intake_id).unlink(missing_ok=True)
    for path in _FILES_DIR.glob(f"*__{intake_id}__*"):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Public API: startup recovery
# ---------------------------------------------------------------------------
def _read_all_records_sync() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record_path in _RECORDS_DIR.glob("*.json"):
        try:
            records.append(json.loads(record_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # A record file that is missing or unparseable (e.g. a hard
            # kill landed mid-write, before the atomic rename in
            # _write_record_sync completed) means that specific upload's
            # bytes are still sitting in files/ but we cannot recover its
            # filename/metadata. Log loudly; this affects only this one
            # file, never any other record.
            logger.error(
                "Inflight record at %s is missing or unreadable; the "
                "corresponding staged file (if any) cannot be "
                "auto-recovered into failed.csv.", record_path,
            )
    return records


async def recover_orphaned_files(record_failure_fn) -> int:
    """
    Run once at startup, before the OCR worker begins accepting new work.
    Returns the number of orphaned files recovered.
    """
    loop = asyncio.get_running_loop()
    records = await loop.run_in_executor(None, _read_all_records_sync)
    if not records:
        return 0

    logger.warning(
        "Recovering %d orphaned file(s) left in-flight by a previous "
        "run that did not shut down cleanly.", len(records),
    )

    recovered = 0
    for record in records:
        intake_id = record.get("intake_id", "")
        inflight_path = Path(record.get("inflight_path", ""))
        filename = record.get("filename") or inflight_path.name or "unknown.pdf"

        if not inflight_path.exists():
            # Record says "accepted" but the staged copy is gone (e.g.
            # disk was cleared manually). Nothing to recover the bytes
            # from; log it loudly, drop the stale record, and move on
            # rather than retrying forever.
            logger.error(
                "Orphaned record for '%s' (intake_id=%s) has no matching "
                "staged file at %s -- cannot recover its bytes; leaving "
                "it out of failed.csv.", filename, intake_id, inflight_path,
            )
            await loop.run_in_executor(None, _clear_inflight_sync, intake_id)
            continue

        try:
            pdf_bytes = await loop.run_in_executor(None, inflight_path.read_bytes)
            await record_failure_fn(
                filename,
                pdf_bytes,
                "interrupted",
                "Processing was interrupted by an application shutdown or "
                "crash before this file reached a terminal state; "
                "recovered automatically on the next startup.",
            )
            await mark_terminal(intake_id, filename, "failed")
            recovered += 1
        except Exception:
            # Leave the record + staged file in place -- they will be
            # retried on the NEXT startup rather than lost. Repeated
            # failures here (e.g. disk full) are visible in logs each
            # boot until resolved.
            logger.exception(
                "Failed to recover orphaned file '%s' (intake_id=%s); will "
                "retry on next startup.", filename, intake_id,
            )

    return recovered


# ---------------------------------------------------------------------------
# Public API: shutdown drain
# ---------------------------------------------------------------------------
async def drain_and_finalize(pending_tasks: "set[asyncio.Task]", timeout: float | None = None) -> None:
    """
    Anything that doesn't finish within the timeout is left exactly as-is 
    (staged bytes + record file already on disk) and will be picked up by 
    `recover_orphaned_files` on the next startup.
    """
    effective_timeout = timeout if timeout is not None else settings.extract_shutdown_drain_seconds

    if not pending_tasks:
        return

    logger.info(
        "Draining %d in-flight task(s) (timeout=%.0fs)...",
        len(pending_tasks), effective_timeout,
    )
    started = time.perf_counter()
    done, pending = await asyncio.wait(pending_tasks, timeout=effective_timeout)
    elapsed = time.perf_counter() - started

    if pending:
        logger.warning(
            "%d task(s) did not finish within the %.0fs drain window "
            "(elapsed %.1fs) and will be recovered automatically on next "
            "startup instead.", len(pending), effective_timeout, elapsed,
        )
    else:
        logger.info("All in-flight task(s) finished cleanly in %.1fs.", elapsed)