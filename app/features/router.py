from __future__ import annotations

"""
router.py
---------
Single-file extraction routes:

    POST /extract/health
    POST /extract/from-file      — upload PDF or TXT, run OCR + LLM
    POST /extract/from-text      — accept pre-OCR'd text, run LLM only
    POST /extract/ocr-only       — run OCR on a PDF, return raw text
    POST /extract/audit-log      — receive batch results from browser,
                                   write XLSX audit log to disk

All timing (OCR duration, LLM duration) is measured server-side.
The browser never sends timestamps; it only POSTs extraction results
it received from this API.
"""

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from app.core.config import get_settings
from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractResponse, ExtractionMeta, ExtractionResult
from app.services.audit_service import AuditRecord, write_audit_xlsx
from app.services.file_service import decode_txt_bytes, validate_and_read_upload
from app.services.llm_client import LLMClient
from app.services.paddle_ocr import process_pdf
from app.services.reference_service import compare_extraction

router = APIRouter(prefix="/extract", tags=["Extraction"])
llm_client = LLMClient()
settings = get_settings()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OCR helper — runs PaddleOCR in a thread-pool executor so it does not
# block the asyncio event loop.  The ``timeout`` kwarg is accepted so this
# function is compatible with ``with_retry`` in batch_router.
# ---------------------------------------------------------------------------

async def _pdf_to_text_via_paddleocr(
    pdf_bytes: bytes,
    filename: str,
    timeout: float | None = None,
) -> str:
    """Write PDF bytes to a temp file, run PaddleOCR, return plain text."""
    logger.debug(
        "Running PaddleOCR on '%s' (%d bytes)", filename, len(pdf_bytes)
    )
    suffix = Path(filename).suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(pdf_bytes)
        tmp.close()
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, process_pdf, tmp.name)
        logger.debug(
            "PaddleOCR returned %d characters for '%s'", len(text), filename
        )
        return text
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Core extraction pipeline — called by single-file routes AND batch_router.
# Returns a fully populated ExtractResponse.
# The ``timeout`` kwarg is accepted so this function is compatible with
# ``with_retry`` in batch_router.
# ---------------------------------------------------------------------------

async def _run_extraction(
    original_text: str,
    source: str,
    timeout: float | None = None,
) -> ExtractResponse:
    prompt = build_extraction_prompt(original_text)
    llm_result = await llm_client.extract_fields(
        prompt,
        stop=[
            "} {", "\n} {", "\n}{", "}\n{", "}\r\n{",
            "}\n\n", "}\r\n\r\n", "}\n ", "} \n",
            "}\n#", "}\n`", "\n}\n ", "\n}\n#",
            "\n}\n`", "\n}\n\n", "\n}\r\n\r\n",
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
    """Upload a PDF or TXT file; runs OCR if needed, then LLM extraction."""
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
    """Accept pre-OCR'd text and a filename, run LLM extraction only."""
    return await _run_extraction(original_text=text, source=filename)


@router.post("/ocr-only")
async def ocr_only(file: UploadFile = File(...)) -> JSONResponse:
    """
    Run PaddleOCR on a PDF and return the extracted text.
    Does NOT call the LLM — the frontend uses this to decouple OCR from
    extraction so both can be parallelised client-side.

    Response shape:
        {
          "status":       "done" | "error",
          "text":         <str | null>,
          "txt_filename": <str | null>,
          "error":        <str | null>
        }
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
        stem = Path(filename).stem.lower().replace(" ", "_")
        txt_filename = f"paddle_{stem}.txt"
        return JSONResponse({
            "status": "done",
            "text": text,
            "txt_filename": txt_filename,
            "error": None,
        })
    except Exception as exc:
        logger.exception("OCR failed for '%s': %s", filename, exc)
        return JSONResponse({
            "status": "error",
            "text": None,
            "txt_filename": None,
            "error": str(exc),
        })


# ---------------------------------------------------------------------------
# Audit log endpoint
# ---------------------------------------------------------------------------

@router.post("/audit-log", include_in_schema=False)
async def save_audit_log(payload: dict) -> Response:
    """
    Receive a batch of extraction results from the browser and write an
    audit XLSX to disk.  Returns 204 — the response body is empty so the
    browser never reads back sensitive comparison data.

    Accepted payload shape:
        {
          "results": [
            {
              "filename":      "example.pdf",
              "extractResult": { ...ExtractResponse dict... } | null,
              "extractError":  "error message" | null
            },
            ...
          ]
        }

    Note: timing fields sent by the browser (ocrStart, ocrEnd, etc.)
    are intentionally ignored.  Server-side timing is the source of truth.
    """
    raw_records: list[dict] = payload.get("results") or []
    if not raw_records:
        return Response(status_code=204)

    records: list[AuditRecord] = [
        AuditRecord(
            filename=r.get("filename", ""),
            extract_result=r.get("extractResult") or None,
            extract_error=r.get("extractError") or None,
            # No timing fields — browser data is untrusted / unreliable.
            # The batch pipeline writes its own audit with accurate timestamps.
        )
        for r in raw_records
    ]

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, write_audit_xlsx, records)

    return Response(status_code=204)