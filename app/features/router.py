from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractResponse, ExtractionMeta, ExtractionResult
from app.services.file_service import decode_txt_bytes, validate_and_read_upload
from app.services.llm_client import LLMClient
from app.services.reference_service import compare_extraction
from app.core.config import get_settings

router = APIRouter(prefix="/extract", tags=["Extraction"])
llm_client = LLMClient()
settings = get_settings()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-worker executor — serialises OCR runs so concurrent requests don't
# all hammer the CPU at once (fix for issue 4)
# ---------------------------------------------------------------------------
_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paddleocr")

# ---------------------------------------------------------------------------
# Results directory — .txt files written here after OCR, served by main.py
# ---------------------------------------------------------------------------
_RESULTS_DIR = Path("results").resolve()
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# PaddleOCR helper — calls process_pdf() directly in a thread-pool executor
# ---------------------------------------------------------------------------

async def _pdf_to_text_via_paddleocr(pdf_bytes: bytes, filename: str, timeout: float | None = None) -> str:
    """
    Run PaddleOCR in-process (blocking CPU work runs in a dedicated
    single-worker executor so concurrent calls are serialised).

    Fixes applied:
      1. Uses tempfile.mkstemp() for a safe, guaranteed-unique temp path.
      2. asyncio.wait_for() enforces the timeout so a hung PDF can't block forever.
      4. _OCR_EXECUTOR (max_workers=1) prevents concurrent OCR runs from
         competing for CPU.
    """
    from app.services.paddle_ocr import process_pdf

    # Issue 1 fix: use the OS temp dir — always exists, no manual mkdir needed
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=f"_{Path(filename).name}")
    tmp_path = Path(tmp_str)
    try:
        import os
        os.close(tmp_fd)
        tmp_path.write_bytes(pdf_bytes)

        loop = asyncio.get_event_loop()
        coro = loop.run_in_executor(
            _OCR_EXECUTOR,                          # issue 4 fix: serialised executor
            partial(process_pdf, str(tmp_path), 300),
        )

        # Issue 2 fix: honour the timeout so a bad PDF can't hang forever
        if timeout is not None:
            text = await asyncio.wait_for(coro, timeout=timeout)
        else:
            text = await coro
    finally:
        tmp_path.unlink(missing_ok=True)

    # Persist .txt result so /ocr-download/<filename> can serve it
    stem = Path(filename).stem.lower().replace(" ", "_")
    txt_filename = f"paddle_{stem}.txt"
    (_RESULTS_DIR / txt_filename).write_text(text, encoding="utf-8")
    logger.info("OCR result saved: %s (%d chars)", txt_filename, len(text))

    return text


def _txt_filename_for(filename: str) -> str:
    stem = Path(filename).stem.lower().replace(" ", "_")
    return f"paddle_{stem}.txt"


# ---------------------------------------------------------------------------
# Core extraction pipeline (called by both single-file and batch routes)
# ---------------------------------------------------------------------------

async def _run_extraction(original_text: str, source: str, timeout: float | None = None) -> ExtractResponse:
    prompt = build_extraction_prompt(original_text)
    llm_result = await llm_client.extract_fields(
        prompt,
        stop=[
            "} {",
            "\n} {",
            "\n}{",
            "}\n{",
            "}\r\n{",
            "}\n\n",
            "}\r\n\r\n",
            "}\n ",
            "} \n",
            "}\n#",
            "}\n`",
            "\n}\n ",
            "\n}\n#",
            "\n}\n`",
            "\n}\n\n",
            "\n}\r\n\r\n",
        ],
        timeout=timeout,
    )

    extracted = ExtractionResult(
        name=llm_result.get("name"),
        master_account_number=llm_result.get("master_account_number"),
        sub_account_number=llm_result.get("sub_account_number"),
        address=llm_result.get("address"),
        fi_num=llm_result.get("fi_num"),
        bank_name=llm_result.get("bank_name"),
    )

    comparison = compare_extraction(
        filename_raw=source,
        bank_name=extracted.bank_name,
        fi_num=extracted.fi_num,
        master_account_number=extracted.master_account_number,
        sub_account_number=extracted.sub_account_number,
    )

    return ExtractResponse(
        success=True,
        message="Extraction completed successfully.",
        data=extracted,
        meta=ExtractionMeta(
            input_characters=len(original_text),
            llm_called=True,
            source=source,
        ),
        comparison=comparison,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/from-file", response_model=ExtractResponse)
async def extract_from_file(file: UploadFile = File(...)) -> ExtractResponse:
    raw_bytes, ext = await validate_and_read_upload(file)
    filename = file.filename or "uploaded_file"

    if ext == ".pdf":
        original_text = await _pdf_to_text_via_paddleocr(raw_bytes, filename)
    else:
        original_text = decode_txt_bytes(raw_bytes)

    return await _run_extraction(original_text=original_text, source=filename)


@router.post("/from-text", response_model=ExtractResponse)
async def extract_from_text(
    text: str = Form(...),
    filename: str = Form(...),
) -> ExtractResponse:
    """Accept raw OCR text and filename, run LLM extraction, return result."""
    return await _run_extraction(original_text=text, source=filename)


@router.post("/ocr-only")
async def ocr_only(file: UploadFile = File(...)) -> JSONResponse:
    """
    Run PaddleOCR on a PDF and return the extracted text.
    Also writes a .txt file to results/ so the UI download table works.
    Does NOT call the LLM.
    """
    raw_bytes, ext = await validate_and_read_upload(file)
    filename = file.filename or "uploaded_file"

    if ext != ".pdf":
        return JSONResponse({
            "status": "error",
            "text": None,
            "txt_filename": None,
            "error": "Only PDF files are supported.",
        })

    try:
        text = await _pdf_to_text_via_paddleocr(raw_bytes, filename)
        txt_filename = _txt_filename_for(filename)
        return JSONResponse({
            "status": "done",
            "text": text,
            "txt_filename": txt_filename,
            "error": None,
        })
    except Exception as exc:
        return JSONResponse({
            "status": "error",
            "text": None,
            "txt_filename": None,
            "error": str(exc),
        })