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

_TIMING_FIELDS = [
    "OCR Start", "OCR End", "OCR Duration",
    "LLM Start", "LLM End", "LLM Duration",
]


def _fmt_duration(start_iso: str | None, end_iso: str | None) -> str:
    from datetime import datetime, timezone
    if not start_iso or not end_iso:
        return ""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        secs = (e - s).total_seconds()
        if secs >= 60:
            m = int(secs // 60)
            s2 = secs % 60
            return f"{m}m {s2:.1f}s"
        return f"{secs:.1f}s"
    except Exception:
        return ""


def _fmt_ts(iso: str | None) -> str:
    from datetime import datetime
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _write_audit_csv(records: list[dict]) -> None:
    """Write one audit XLSX per batch run to audit_logs/ with colour-coded expected columns."""
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _AUDIT_DIR / f"audit_{timestamp}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "Audit"

    # --- Styles ---
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    green_font = Font(color="006100")
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    red_font = Font(color="9C0006")
    total_font = Font(bold=True, size=11)
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    # --- Header row ---
    header = ["File Name"]
    for _key, label in _AUDIT_FIELDS:
        header += [f"Extracted {label}", f"Expected {label}"]
    header.append("Accurate Count")
    header += _TIMING_FIELDS

    ws.append(header)
    for col_idx, _ in enumerate(header, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # --- Data rows ---
    total_accurate = 0
    total_fields = 0

    # Track which columns are "Expected" so we can colour them
    # Expected columns are at indices: 3, 5, 7, 9 (1-based) i.e. every even field col
    expected_col_indices = [2 + i * 2 + 1 for i in range(len(_AUDIT_FIELDS))]  # 1-based: 3,5,7,9

    for record in records:
        filename    = record.get("filename", "")
        extract_res = record.get("extractResult") or {}
        extract_err = record.get("extractError")

        if extract_err or not extract_res:
            row_data = [filename] + ["ERROR", ""] * len(_AUDIT_FIELDS) + [0]
            ocr_start = record.get("ocrStart")
            ocr_end   = record.get("ocrEnd")
            llm_start = record.get("llmStart")
            llm_end   = record.get("llmEnd")
            row_data += [
                _fmt_ts(ocr_start),
                _fmt_ts(ocr_end),
                _fmt_duration(ocr_start, ocr_end),
                _fmt_ts(llm_start),
                _fmt_ts(llm_end),
                _fmt_duration(llm_start, llm_end),
            ]
            ws.append(row_data)
            row_num = ws.max_row
            for col_idx in range(1, len(row_data) + 1):
                ws.cell(row=row_num, column=col_idx).border = thin_border
            for col_idx in expected_col_indices:
                cell = ws.cell(row=row_num, column=col_idx)
                cell.fill = red_fill
                cell.font = red_font
            total_fields += len(_AUDIT_FIELDS)
            continue

        data = extract_res.get("data") or {}
        cmp  = extract_res.get("comparison") or {}

        row_data = [filename]
        matches = []
        accurate_count = 0
        for key, _label in _AUDIT_FIELDS:
            extracted = (data.get(key) or "").strip()
            expected  = ((cmp.get(key) or {}).get("expected") or "").strip()
            row_data += [extracted, expected]
            is_match = bool(extracted and expected and extracted == expected)
            matches.append(is_match)
            if is_match:
                accurate_count += 1

        row_data.append(accurate_count)
        ocr_start = record.get("ocrStart")
        ocr_end   = record.get("ocrEnd")
        llm_start = record.get("llmStart")
        llm_end   = record.get("llmEnd")
        row_data += [
            _fmt_ts(ocr_start),
            _fmt_ts(ocr_end),
            _fmt_duration(ocr_start, ocr_end),
            _fmt_ts(llm_start),
            _fmt_ts(llm_end),
            _fmt_duration(llm_start, llm_end),
        ]
        total_accurate += accurate_count
        total_fields += len(_AUDIT_FIELDS)
        ws.append(row_data)

        row_num = ws.max_row
        for col_idx in range(1, len(row_data) + 1):
            ws.cell(row=row_num, column=col_idx).border = thin_border

        # Colour expected columns green/red
        for i, col_idx in enumerate(expected_col_indices):
            cell = ws.cell(row=row_num, column=col_idx)
            if matches[i]:
                cell.fill = green_fill
                cell.font = green_font
            else:
                cell.fill = red_fill
                cell.font = red_font

    # --- Summary row ---
    if total_fields > 0:
        pct = (total_accurate / total_fields) * 100
    else:
        pct = 0.0

    summary_row = ["TOTAL"] + [""] * (len(_AUDIT_FIELDS) * 2)
    summary_row.append(f"{total_accurate}/{total_fields} ({pct:.2f}%)")
    ws.append(summary_row)
    row_num = ws.max_row
    for col_idx in range(1, len(summary_row) + 1):
        cell = ws.cell(row=row_num, column=col_idx)
        cell.font = total_font
        cell.border = thin_border

     # --- BATCH SUMMARY row (timing) ---
    all_ocr_starts = [r.get("ocrStart") for r in records if r.get("ocrStart")]
    all_ocr_ends   = [r.get("ocrEnd")   for r in records if r.get("ocrEnd")]
    all_llm_starts = [r.get("llmStart") for r in records if r.get("llmStart")]
    all_llm_ends   = [r.get("llmEnd")   for r in records if r.get("llmEnd")]

    batch_ocr_start = min(all_ocr_starts) if all_ocr_starts else None
    batch_ocr_end   = max(all_ocr_ends)   if all_ocr_ends   else None
    batch_llm_start = min(all_llm_starts) if all_llm_starts else None
    batch_llm_end   = max(all_llm_ends)   if all_llm_ends   else None

    batch_row = ["BATCH SUMMARY"] + [""] * (len(_AUDIT_FIELDS) * 2 + 1)
    batch_row += [
        _fmt_ts(batch_ocr_start),
        _fmt_ts(batch_ocr_end),
        _fmt_duration(batch_ocr_start, batch_ocr_end),
        _fmt_ts(batch_llm_start),
        _fmt_ts(batch_llm_end),
        _fmt_duration(batch_llm_start, batch_llm_end),
    ]
    ws.append(batch_row)
    row_num = ws.max_row
    batch_fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    batch_font = Font(bold=True, size=11)
    for col_idx in range(1, len(batch_row) + 1):
        cell = ws.cell(row=row_num, column=col_idx)
        cell.fill = batch_fill
        cell.font = batch_font
        cell.border = thin_border

    # --- Auto-fit column widths ---
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    wb.save(str(path))
    logger.info("Audit log saved: %s (%d record(s), accuracy %.2f%%)", path, len(records), pct if total_fields else 0.0)


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