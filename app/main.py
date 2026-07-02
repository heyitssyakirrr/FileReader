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
from app.features.extraction import router as extract_router
from app.features.extraction import drain_and_finalize, get_pending_tasks, start_ocr_worker
from app.features.extraction.concurrency import get_ocr_process_pool, shutdown_ocr_process_pool

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
    logger.info("Extract API   : POST /extract (max %d in-flight)", settings.extract_max_pending_tasks)

    get_ocr_process_pool()          # pre-warm: spins up the worker process at startup
                                    # instead of on the first uploaded file
    await start_ocr_worker()
    yield
    await drain_and_finalize(get_pending_tasks())
    await shutdown_ocr_process_pool()   # after drain, so any in-flight LLM tasks get their window first
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.include_router(extract_router)


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