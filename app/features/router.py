from __future__ import annotations

import logging
import tempfile

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pathlib import Path

from app.features.prompt import build_extraction_prompt
from app.models.schemas import ExtractResponse, ExtractionMeta, ExtractionResult
from app.services.file_service import decode_txt_bytes, validate_and_read_upload
from app.services.llm_client import LLMClient
from app.services.paddle_ocr import process_pdf
from app.services.reference_service import compare_extraction
from app.core.config import get_settings

router = APIRouter(prefix="/extract", tags=["Extraction"])
llm_client = LLMClient()
settings = get_settings()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PaddleOCR helper — runs OCR in-process via paddle_ocr.process_pdf
# ---------------------------------------------------------------------------

async def _pdf_to_text_via_paddleocr(pdf_bytes: bytes, filename: str, timeout: float | None = None) -> str:
    """
    Write PDF bytes to a temp file, run PaddleOCR in-process, and return
    the extracted plain text.
    """
    import asyncio

    logger.debug("Running in-process PaddleOCR on '%s' (%d bytes)", filename, len(pdf_bytes))

    suffix = Path(filename).suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(pdf_bytes)
        tmp.close()

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, process_pdf, tmp.name)

        logger.debug("PaddleOCR returned %d characters for '%s'", len(text), filename)
        return text
    finally:
        Path(tmp.name).unlink(missing_ok=True)


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
    Does NOT call the LLM. The frontend uses this to decouple OCR from extraction.
    
    Response: { "status": "done"|"error", "text": str, "txt_filename": str, "error": str|null }
    """
    from fastapi.responses import JSONResponse

    raw_bytes, ext = await validate_and_read_upload(file)
    filename = file.filename or "uploaded_file"

    if ext != ".pdf":
        return JSONResponse({"status": "error", "text": None, "txt_filename": None, "error": "Only PDF files are supported."})

    try:
        text = await _pdf_to_text_via_paddleocr(raw_bytes, filename)
        stem = Path(filename).stem.lower().replace(" ", "_")
        txt_filename = f"paddle_{stem}.txt"
        return JSONResponse({"status": "done", "text": text, "txt_filename": txt_filename, "error": None})
    except Exception as exc:
        return JSONResponse({"status": "error", "text": None, "txt_filename": None, "error": str(exc)})
    
    # ---------------------------------------------------------------------------
# Audit log — developer-only, never served back to the browser
# ---------------------------------------------------------------------------

_AUDIT_DIR = Path("audit_logs").resolve()
_AUDIT_DIR.mkdir(parents=True, exist_ok=True)

_AUDIT_FIELDS = [
    ("bank_name",             "Bank Name"),
    ("fi_num",                "FI Code"),
    ("master_account_number", "Master Account No."),
    ("sub_account_number",    "Sub Account No."),
]


def _write_audit_csv(records: list[dict]) -> None:
    """Write one audit CSV per batch run to audit_logs/. Never exposed via HTTP."""
    import csv
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _AUDIT_DIR / f"audit_{timestamp}.csv"

    header = ["File Name"]
    for _key, label in _AUDIT_FIELDS:
        header += [f"Extracted {label}", f"Expected {label}"]

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)

        for record in records:
            filename     = record.get("filename", "")
            extract_res  = record.get("extractResult") or {}
            extract_err  = record.get("extractError")

            if extract_err or not extract_res:
                row = [filename] + ["ERROR", ""] * len(_AUDIT_FIELDS)
                writer.writerow(row)
                continue

            data = extract_res.get("data") or {}
            cmp  = extract_res.get("comparison") or {}

            row = [filename]
            for key, _label in _AUDIT_FIELDS:
                extracted = data.get(key) or ""
                expected  = (cmp.get(key) or {}).get("expected") or ""
                row += [extracted, expected]

            writer.writerow(row)

    logger.info("Audit log saved: %s (%d record(s))", path, len(records))


@router.post("/audit-log", include_in_schema=False)
async def save_audit_log(payload: dict) -> JSONResponse:
    """
    Receives the full batch result from the browser and writes an audit CSV
    to audit_logs/ on disk. Returns 204 — the response body is intentionally
    empty so the browser never reads sensitive comparison data back.
    """
    from fastapi.responses import Response

    records = payload.get("results") or []
    if records:
        # Run the blocking file-write in a thread so we don't block the event loop
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_audit_csv, records)

    return Response(status_code=204)