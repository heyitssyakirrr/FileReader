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


def get_pending_tasks() -> "set[asyncio.Task]":
    """
    Snapshot of currently-tracked background tasks, for the shutdown
    drain in lifecycle.py. Returns a copy so the caller can iterate/wait
    on it without it mutating from under them as tasks finish.
    """
    return set(_pending_tasks)


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