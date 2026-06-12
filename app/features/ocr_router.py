from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from app.services.paddle_ocr import process_pdf
from app.services.file_service import validate_and_read_upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ocr", tags=["OCR Service"])

# Results are saved here so they can be served by the /ocr-download/{filename}
# route registered on the main app (app/main.py).
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

    Response shape::

        {
          "results": [
            {
              "input":  "example.pdf",
              "output": "example.txt",
              "status": "done",
              "error":  null
            }
          ]
        }
    """
    results: list[dict] = []

    for upload in files:
        filename = upload.filename or "uploaded.pdf"
        stem     = Path(filename).stem
        txt_name = f"{stem}.txt"

        try:
            raw_bytes, ext = await validate_and_read_upload(upload)

            if ext != ".pdf":
                results.append({
                    "input":  filename,
                    "output": None,
                    "status": "error",
                    "error":  "Only PDF files are supported.",
                })
                continue

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(raw_bytes)
            tmp.close()

            try:
                loop = asyncio.get_running_loop()
                text = await loop.run_in_executor(None, process_pdf, tmp.name, dpi)
            finally:
                Path(tmp.name).unlink(missing_ok=True)

            out_path = _RESULTS_DIR / txt_name
            out_path.write_text(text, encoding="utf-8")
            logger.info("OCR result saved: %s (%d chars)", out_path, len(text))

            results.append({
                "input":  filename,
                "output": txt_name,
                "status": "done",
                "error":  None,
            })

        except Exception as exc:
            logger.exception("OCR failed for %s", filename)
            results.append({
                "input":  filename,
                "output": None,
                "status": "error",
                "error":  str(exc),
            })

    return JSONResponse({"results": results})