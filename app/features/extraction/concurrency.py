from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_ocr_queue: asyncio.Queue = asyncio.Queue()
_llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(settings.llm_max_concurrent)
_csv_append_lock: asyncio.Lock = asyncio.Lock()
_pending_tasks: set[asyncio.Task] = set()


def pending_task_count() -> int:
    return _ocr_queue.qsize() + len(_pending_tasks)


async def drain_pending_tasks(timeout: float | None = None) -> None:
    effective_timeout = timeout if timeout is not None else settings.extract_shutdown_drain_seconds

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


def _on_task_done(task: asyncio.Task) -> None:
    _pending_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Unhandled exception in background task '%s': %s",
            task.get_name(),
            task.exception(),
            exc_info=task.exception(),
        )

def _track_task(task: asyncio.Task) -> None:
    _pending_tasks.add(task)
    task.add_done_callback(_on_task_done)