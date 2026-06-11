from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from features.prompt import build_extraction_prompt
from models.schemas import ExtractResponse, ExtractionMeta, ExtractionResult
from services.docling_client import DoclingClient
from services.file_service import decode_txt_bytes, validate_and_read_upload
from services.llm_client import LLMClient
from services.reference_service import compare_extraction

router = APIRouter(prefix="/extract", tags=["Extraction"])
llm_client = LLMClient()
docling_client = DoclingClient()


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
            # ↓ these are the missing ones — closing brace preceded by newline (pretty-printed JSON)
            "\n}\n ",      # ← your exact case: newline + } + newline + space + "To extract..."
            "\n}\n#",      # ← newline + } + newline + markdown heading
            "\n}\n`",      # ← newline + } + newline + code block
            "\n}\n\n",     # ← newline + } + blank line
            "\n}\r\n\r\n", # ← same, Windows line endings
        ],
        timeout=timeout
    )

    extracted = ExtractionResult(
        name=llm_result.get("name"),
        master_account_number=llm_result.get("master_account_number"),
        sub_account_number=llm_result.get("sub_account_number"),
        address=llm_result.get("address"),
        fi_num=llm_result.get("fi_num"),
        bank_name=llm_result.get("bank_name"),   # NEW
    )

    # Compare extracted fields against the reference CSV using the filename
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


@router.post("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/from-file", response_model=ExtractResponse)
async def extract_from_file(file: UploadFile = File(...)) -> ExtractResponse:
    raw_bytes, ext = await validate_and_read_upload(file)
    filename = file.filename or "uploaded_file"

    if ext == ".pdf":
        original_text = await docling_client.pdf_to_text(raw_bytes, filename)
    else:
        original_text = decode_txt_bytes(raw_bytes)

    return await _run_extraction(original_text=original_text, source=filename)
