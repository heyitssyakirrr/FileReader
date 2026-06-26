from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_EXTRACTIONS_PATTERN = re.compile(r"^(\d{8})_extractions\.csv$")
_DATE_FOLDER_PATTERN = re.compile(r"^\d{8}$")


def _cutoff_date(max_age_days: int):
    return datetime.now().date() - timedelta(days=max_age_days)


def enforce_extractions_retention(directory: Path, max_age_days: int) -> None:
    """Delete YYYYMMDD_extractions.csv files older than max_age_days."""
    if not directory.exists():
        return

    cutoff = _cutoff_date(max_age_days)
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _EXTRACTIONS_PATTERN.match(path.name)
        if not match:
            continue
        try:
            item_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if item_date >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
            logger.info("Retention: removed expired extractions file '%s'", path.name)
        except OSError as exc:
            logger.warning("Retention: failed to remove '%s': %s", path.name, exc)


def enforce_dated_folder_retention(directory: Path, max_age_days: int) -> None:
    """Delete YYYYMMDD folders older than max_age_days."""
    if not directory.exists():
        return

    cutoff = _cutoff_date(max_age_days)
    for path in directory.iterdir():
        if not path.is_dir() or not _DATE_FOLDER_PATTERN.match(path.name):
            continue
        try:
            item_date = datetime.strptime(path.name, "%Y%m%d").date()
        except ValueError:
            continue
        if item_date >= cutoff:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Retention: removed expired dated folder '%s'", path.name)
        except OSError as exc:
            logger.warning("Retention: failed to remove folder '%s': %s", path.name, exc)
