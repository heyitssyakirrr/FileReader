from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.features.batch_router import router as batch_router
from app.features.router import router as extract_router
from app.features.ocr_router import router as ocr_router
from app.summary.router import router as summarise_router

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# OCR results directory — written by ocr_router and extract/ocr-only
# ---------------------------------------------------------------------------
_RESULTS_DIR = Path("results").resolve()
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Startup cleanup — delete .txt files older than ocr_results_max_age_days
# ---------------------------------------------------------------------------

def _cleanup_old_ocr_results() -> None:
    max_age_seconds = settings.ocr_results_max_age_days * 86_400
    cutoff = time.time() - max_age_seconds
    deleted = 0
    for f in _RESULTS_DIR.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception as exc:
            logger.warning("Could not delete old OCR result %s: %s", f.name, exc)

    if deleted:
        logger.info(
            "Startup cleanup: removed %d OCR result file(s) older than %d day(s)",
            deleted, settings.ocr_results_max_age_days,
        )
    else:
        logger.info(
            "Startup cleanup: no OCR result files older than %d day(s)",
            settings.ocr_results_max_age_days,
        )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    logger.info("LLM endpoint : %s", settings.llm_url)
    logger.info("OCR results  : %s (in-process PaddleOCR)", _RESULTS_DIR)
    logger.info("Batch API    : POST /extract/batch (max %d files)", settings.max_files_per_batch)
    _cleanup_old_ocr_results()
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(extract_router)
app.include_router(batch_router)
app.include_router(ocr_router)
app.include_router(summarise_router)


@app.get("/", tags=["UI"])
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
        },
    )


@app.get("/health", tags=["Meta"])
async def app_health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OCR text file download
#
# Serves .txt files written to results/ by the in-process PaddleOCR runs.
# A single route here replaces the previous duplicate that also existed in
# ocr_router.py — all OCR downloads go through this one endpoint.
#
# Security: filename is sanitised and the resolved path is confirmed to
# stay inside _RESULTS_DIR before serving (prevents path traversal).
# ---------------------------------------------------------------------------

@app.get("/ocr-download/{filename}", tags=["OCR"])
async def download_ocr_result(filename: str) -> FileResponse:
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    safe_name = "".join(
        c if c.isalnum() or c in "._- " else "_" for c in filename
    )
    file_path = (_RESULTS_DIR / safe_name).resolve()

    try:
        file_path.relative_to(_RESULTS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path.")

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"OCR result file '{safe_name}' not found.",
        )

    return FileResponse(
        path=str(file_path),
        media_type="text/plain",
        filename=safe_name,
    )


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": f"An unexpected error occurred: {exc}"},
    )