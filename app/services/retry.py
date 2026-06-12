from __future__ import annotations

"""
retry.py
--------
Shared async retry utility used by batch_router, summary router, and any
future pipeline stage that needs fault-tolerant coroutine execution.
"""

import logging
from collections.abc import Callable, Awaitable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_ATTEMPTS = 3
_RETRY_TIMEOUT_INCREMENT = 180.0  # extra seconds added per retry attempt


async def with_retry(
    label: str,
    base_timeout: float,
    coro_fn: Callable[..., Awaitable[T]],
    *args,
    max_attempts: int = _MAX_ATTEMPTS,
    timeout_increment: float = _RETRY_TIMEOUT_INCREMENT,
    **kwargs,
) -> T:
    """
    Call ``coro_fn(*args, timeout=current_timeout, **kwargs)`` up to
    ``max_attempts`` times.

    Each successive attempt increases the timeout by ``timeout_increment``
    seconds, giving slower documents more headroom on retries.

    Args:
        label:             Human-readable label used in log messages.
        base_timeout:      Timeout (seconds) for the first attempt.
        coro_fn:           Async callable that accepts a ``timeout`` kwarg.
        *args:             Positional arguments forwarded to ``coro_fn``.
        max_attempts:      Maximum number of attempts before re-raising.
        timeout_increment: Extra seconds added per retry.
        **kwargs:          Keyword arguments forwarded to ``coro_fn``
                           (``timeout`` is injected automatically).

    Returns:
        The return value of ``coro_fn`` on the first successful attempt.

    Raises:
        The last exception raised by ``coro_fn`` after all attempts fail.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        current_timeout = base_timeout + (attempt - 1) * timeout_increment
        try:
            logger.debug(
                "%s — attempt %d/%d timeout=%.0fs",
                label, attempt, max_attempts, current_timeout,
            )
            return await coro_fn(*args, timeout=current_timeout, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                logger.warning(
                    "%s — attempt %d/%d failed (timeout=%.0fs): %s — retrying",
                    label, attempt, max_attempts, current_timeout, exc,
                )
            else:
                logger.error(
                    "%s — all %d attempts failed: %s",
                    label, max_attempts, exc,
                )

    raise last_exc  # type: ignore[misc]