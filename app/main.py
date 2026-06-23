from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.features.batch_router import router as batch_router
from app.features.router import router as extract_router
from app.features.router import _start_cleanup_scheduler
from app.features.ocr_router import router as ocr_router
from app.summary.router import router as summarise_router
from app.features.single_router import router as single_router
from app.features.single_router import drain_pending_tasks

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    logger.info("LLM endpoint : %s", settings.llm_url)
    logger.info("OCR results  : results/ (in-process PaddleOCR, cleaned up by router.py)")    
    logger.info("OCR service  : POST /ocr/upload, GET /ocr/download/<file>")
    logger.info("Batch API    : POST /extract/batch (max %d files)", settings.max_files_per_batch)
    logger.info(
        "Single API   : POST /extract/single (max %d in-flight)",
        settings.single_max_pending_tasks,
    )
    _start_cleanup_scheduler()
    yield
    # Give in-flight /extract/single background tasks (OCR/LLM pipelines
    # already accepted but not yet finished) a bounded grace period to
    # complete and write their CSV/failed-folder record, rather than
    # abandoning them mid-pipeline on shutdown/redeploy.
    await drain_pending_tasks()
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
app.include_router(single_router)


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


@app.exception_handler(Exception)
async def global_exception_handler(_, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": f"An unexpected error occurred: {exc}"},
    )