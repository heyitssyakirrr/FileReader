from __future__ import annotations

import logging

from fastapi import HTTPException, UploadFile

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

        
def _get_extension(filename: str | None) -> str:
    if not filename:
        return ""
    return "." + filename.rsplit(".",1)[-1].lower() if "." in filename else ""


async def validate_and_read_upload(file: UploadFile) -> tuple[bytes, str]:
    ext = _get_extension(file.filename)

    if ext not in settings.allowed_upload_extensions:
        allowed = ", ".join(settings.allowed_upload_extensions)
        raise HTTPException(
                status_code=422,
                detail=f"Unsupported file type '{ext}'. Allowed types: {allowed}.",
            )

    raw_bytes = await file.read()

    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the maximum allowed size of {settings.max_upload_bytes // (1024 * 1024)} MB.",
        )
    
    return raw_bytes, ext

# need to delete since not used anywhere 
def decode_txt_bytes(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("latin-1")
