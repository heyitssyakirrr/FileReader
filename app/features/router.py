from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pathlib import Path

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
# PaddleOCR helper — calls the local paddle_ocr FastAPI service
# ---------------------------------------------------------------------------

async def _pdf_to_text_via_paddleocr(pdf_bytes: bytes, filename: str, timeout: float | None = None) -> str:
    """
    Send a PDF to the local PaddleOCR FastAPI service (app.py, default port 5001)
    and return the extracted plain text.

    The paddle_ocr service exposes POST /upload which accepts multipart files
    and returns JSON: { "results": [{ "status": "done"|"error", "output": "<filename>.txt", ... }] }

    We then fetch the .txt content from GET /download/<filename>.
    """
    paddle_base_url = getattr(settings, "paddle_ocr_base_url", "http://127.0.0.1:5001")
    effective_timeout = timeout if timeout is not None else settings.llm_timeout_seconds

    logger.debug("Sending PDF '%s' (%d bytes) to PaddleOCR at %s", filename, len(pdf_bytes), paddle_base_url)

    async with httpx.AsyncClient(timeout=effective_timeout, verify=False) as client:
        # Stage 1: upload + OCR
        try:
            upload_resp = await client.post(
                f"{paddle_base_url}/upload",
                files={"files": (filename, pdf_bytes, "application/pdf")},
                data={"dpi": "300"},
            )
            upload_resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="PaddleOCR service timed out during OCR.") from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"PaddleOCR service error: HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Unable to connect to PaddleOCR service.") from exc

        data = upload_resp.json()
        results = data.get("results", [])
        if not results:
            raise HTTPException(status_code=502, detail="PaddleOCR returned empty results.")

        result = results[0]
        if result.get("status") == "error":
            raise HTTPException(status_code=502, detail=f"PaddleOCR error: {result.get('error', 'unknown')}")

        output_filename = result.get("output")
        if not output_filename:
            raise HTTPException(status_code=502, detail="PaddleOCR did not return an output filename.")

        # Stage 2: download the extracted .txt
        try:
            dl_resp = await client.get(f"{paddle_base_url}/download/{output_filename}")
            dl_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"Could not download OCR result: HTTP {exc.response.status_code}") from exc

        text = dl_resp.text
        logger.debug("PaddleOCR returned %d characters for '%s'", len(text), filename)
        return text


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