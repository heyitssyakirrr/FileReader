from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.services.paddle_ocr import process_pdf
from app.services.file_service import validate_and_read_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ocr", tags=["OCR Service"])

# Persist OCR text results so they can be downloaded later
_RESULTS_DIR = Path("results").resolve()
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload")
async def ocr_upload(
    files: list[UploadFile] = File(...),
    dpi: int = Form(300),
) -> JSONResponse:
    """
    Accept one or more PDF files, run PaddleOCR in-process, save the
    extracted text to ``results/``, and return a summary.

    Response mirrors the old standalone PaddleOCR service contract::

        {
          "results": [
            {
              "input": "example.pdf",
              "output": "example.txt",
              "status": "done",
              "error": null
            }
          ]
        }
    """
    results: list[dict] = []

    for upload in files:
        filename = upload.filename or "uploaded.pdf"
        stem = Path(filename).stem
        txt_name = f"{stem}.txt"

        try:
            raw_bytes, ext = await validate_and_read_upload(upload)
            if ext != ".pdf":
                results.append({
                    "input": filename,
                    "output": None,
                    "status": "error",
                    "error": "Only PDF files are supported.",
                })
                continue

            # Write to a temp file, run OCR, clean up
            import tempfile

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(raw_bytes)
            tmp.close()

            try:
                loop = asyncio.get_running_loop()
                text = await loop.run_in_executor(None, process_pdf, tmp.name, dpi)
            finally:
                Path(tmp.name).unlink(missing_ok=True)

            # Save result
            out_path = _RESULTS_DIR / txt_name
            out_path.write_text(text, encoding="utf-8")
            logger.info("OCR result saved: %s (%d chars)", out_path, len(text))

            results.append({
                "input": filename,
                "output": txt_name,
                "status": "done",
                "error": None,
            })

        except Exception as exc:
            logger.exception("OCR failed for %s", filename)
            results.append({
                "input": filename,
                "output": None,
                "status": "error",
                "error": str(exc),
            })

    return JSONResponse({"results": results})


@router.get("/download/{filename}")
async def ocr_download(filename: str) -> FileResponse:
    """
    Download a previously generated OCR text file from ``results/``.
    """
    # Sanitise to prevent path traversal
    safe_name = Path(filename).name
    file_path = _RESULTS_DIR / safe_name

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File '{safe_name}' not found.")

    return FileResponse(
        path=str(file_path),
        media_type="text/plain",
        filename=safe_name,
    )