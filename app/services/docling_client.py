from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class DoclingClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def pdf_to_text(self, pdf_bytes: bytes, filename: str, timeout: float | None = None) -> str:
        """
        Send a PDF to the Docling OCR service and return the extracted text.
        The response comes back as markdown.
        """
        logger.debug(
            "Sending PDF '%s' (%d bytes) to Docling at %s",
            filename,
            len(pdf_bytes),
            self.settings.docling_ocr_url,
        )

        try:
            effective_timeout = timeout if timeout is not None else self.settings.docling_timeout_seconds
            async with httpx.AsyncClient(timeout=effective_timeout, verify=False) as client:
                response = await client.post(
                    self.settings.docling_ocr_url,
                    files={"files": (filename, pdf_bytes, "application/pdf")},
                    data={
                        "ocr_engine": "tesserocr",
                        "to_formats": "md",
                        "from_formats": "pdf",
                    },
                )
            response.raise_for_status()

            logger.debug("Raw Docling response: %s", response.text)

            extracted_text = _parse_docling_response(response)
            logger.debug("Docling returned %d characters for '%s'", len(extracted_text), filename)
            return extracted_text

        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504,
                detail="Docling OCR service timed out. The PDF may be too large or complex.",
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Docling OCR service error: HTTP {exc.response.status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail="Unable to connect to Docling OCR service.",
            ) from exc


def _parse_docling_response(response: httpx.Response) -> str:
    """
    Parse the Docling /v1/convert/file response.
    The service returns JSON, then extract the markdown content from it.
    """
    try:
        data = response.json()
        extracted = (
            data.get("document", {}).get("md_content")
            or data.get("content")
            or data.get("text")
        )

        if not extracted:
            logger.error("Unexpected Docling response shape: %s", data)
            raise HTTPException(
                status_code=502,
                detail="Docling OCR returned an empty or unrecognised response. Check the debug log for the raw response shape.",
            )

        return extracted

    except (ValueError, KeyError) as exc:
        # Fallback: maybe it returned plain text/markdown directly
        text = response.text.strip()
        if text:
            return text
        raise HTTPException(
            status_code=502,
            detail="Failed to parse Docling OCR response.",
        ) from exc
