from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_EXTRACTIONS_PATTERN = re.compile(r"^(\d{8})_extractions\.csv$")
_DATE_FOLDER_PATTERN = re.compile(r"^\d{8}$")


def enforce_extractions_retention(directory: Path, max_count: int) -> None:
    """
    Keep at most `max_count` dated files named YYYYMMDD_extractions.csv inside
    `directory`. Deletes the oldest dated file(s) first, ranked by the date
    encoded in the filename — count-based (most recent N dates with data),
    not calendar-age-based.
    """
    candidates: list[tuple[Path, str]] = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        m = _EXTRACTIONS_PATTERN.match(p.name)
        if m:
            candidates.append((p, m.group(1)))

    candidates.sort(key=lambda item: item[1])  # oldest date first

    excess = len(candidates) - max_count
    if excess <= 0:
        return

    for path, date_key in candidates[:excess]:
        try:
            path.unlink(missing_ok=True)
            logger.info("Retention: removed old extractions file '%s' (date %s)", path.name, date_key)
        except OSError as exc:
            logger.warning("Retention: failed to remove '%s': %s", path.name, exc)


def enforce_dated_folder_retention(directory: Path, max_count: int) -> None:
    """
    Keep at most `max_count` dated subfolders named YYYYMMDD inside `directory`
    (used for failed/ and OCR_Outputs/). Deletes the oldest dated folder(s)
    first — count-based, same semantics as enforce_extractions_retention.
    """
    candidates = [
        p for p in directory.iterdir()
        if p.is_dir() and _DATE_FOLDER_PATTERN.match(p.name)
    ]
    candidates.sort(key=lambda p: p.name)  # YYYYMMDD sorts lexically == chronologically

    excess = len(candidates) - max_count
    if excess <= 0:
        return

    for path in candidates[:excess]:
        try:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Retention: removed old dated folder '%s'", path.name)
        except OSError as exc:
            logger.warning("Retention: failed to remove folder '%s': %s", path.name, exc)