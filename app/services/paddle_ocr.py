from __future__ import annotations

import os
os.environ["FLAGS_use_mkldnn"] = "0"

import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is not None:
        return _ocr_instance

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        raise RuntimeError("PaddleOCR not installed.")

    model_base = Path(__file__).resolve().parent.parent.parent / "models"
    det_dir = model_base / "en_PP-OCRv3_det_infer"
    rec_dir = model_base / "en_PP-OCRv3_rec_infer"
    cls_dir = model_base / "ch_ppocr_mobile_v2.0_cls_infer"

    local_kwargs = {}
    if det_dir.exists() and rec_dir.exists():
        logger.info("Using local models from %s", model_base)
        local_kwargs = {
            "det_model_dir": str(det_dir),
            "rec_model_dir": str(rec_dir),
            "cls_model_dir": str(cls_dir) if cls_dir.exists() else None,
        }
        local_kwargs = {k: v for k, v in local_kwargs.items() if v is not None}

    logger.info("Loading PP-OCRv5 model (CPU)...")
    _ocr_instance = PaddleOCR(
        lang="en",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_thresh=0.3,
        text_det_box_thresh=0.5,
        text_det_unclip_ratio=1.8,
        text_recognition_batch_size=6,
        text_rec_score_thresh=0.0,
        enable_mkldnn=False,
        **local_kwargs,
    )
    logger.info("Model loaded.")
    return _ocr_instance


def _pdf_to_images(pdf_path: str, dpi: int = 300) -> list[tuple[int, object]]:
    """Render every page of a PDF to a numpy array using pypdfium2."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise RuntimeError("pypdfium2 not installed. Run: pip install pypdfium2")

    import numpy as np

    doc = pdfium.PdfDocument(pdf_path)
    scale = dpi / 72
    images = []

    for page_num, page in enumerate(doc, start=1):
        bitmap = page.render(scale=scale, rotation=0)
        img_array = bitmap.to_numpy()

        if img_array.shape[2] == 4:
            img_array = img_array[:, :, :3]

        images.append((page_num, img_array))
        logger.debug("Page %d rendered at %d DPI (%dx%d px)",
                     page_num, dpi, img_array.shape[1], img_array.shape[0])

    doc.close()
    return images


# ---------------------------------------------------------------------------
# Spatial reconstruction helpers
# ---------------------------------------------------------------------------

def _box_to_xywh(box) -> tuple[float, float, float, float]:
    """
    Convert PaddleOCR bounding box (4 corner points) to (x, y, w, h).
    box = [[x0,y0],[x1,y1],[x2,y2],[x3,y3]] — top-left going clockwise.
    """
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x = min(xs)
    y = min(ys)
    w = max(xs) - x
    h = max(ys) - y
    return x, y, w, h


def _group_into_rows(
    tokens: list[tuple[float, float, float, float, str]],
    row_gap_factor: float = 0.6,
) -> list[list[tuple[float, float, float, float, str]]]:
    """
    Cluster tokens into horizontal rows based on vertical proximity.

    tokens: list of (x, y, w, h, text)
    row_gap_factor: a new row starts when the vertical gap between consecutive
                    token tops exceeds (row_gap_factor * median_height).

    Returns: list of rows, each row sorted left-to-right by x.
    """
    if not tokens:
        return []

    # Sort all tokens top-to-bottom by their vertical centre
    sorted_tokens = sorted(tokens, key=lambda t: t[1] + t[3] / 2)

    # Estimate median token height to set the row-gap threshold
    heights = [t[3] for t in sorted_tokens if t[3] > 0]
    median_h = sorted(heights)[len(heights) // 2] if heights else 20.0
    gap_threshold = row_gap_factor * median_h

    rows: list[list] = []
    current_row: list = [sorted_tokens[0]]
    current_row_bottom = sorted_tokens[0][1] + sorted_tokens[0][3]

    for token in sorted_tokens[1:]:
        token_top = token[1]
        # Gap between this token's top and the current row's bottom
        gap = token_top - current_row_bottom
        if gap > gap_threshold:
            rows.append(sorted(current_row, key=lambda t: t[0]))  # sort L→R
            current_row = [token]
            current_row_bottom = token[1] + token[3]
        else:
            current_row.append(token)
            current_row_bottom = max(current_row_bottom, token[1] + token[3])

    rows.append(sorted(current_row, key=lambda t: t[0]))
    return rows


def _tokens_to_line(row: list[tuple[float, float, float, float, str]],
                    col_gap_factor: float = 1.5) -> str:
    """
    Convert a sorted row of tokens into a single text line.

    Inserts a TAB character between tokens whose horizontal gap is large
    (indicating separate table columns), and a single space otherwise.
    This preserves column separation in flat CCRIS tables (Layout G).

    col_gap_factor: gap > (col_gap_factor * avg_char_width) → TAB separator
    """
    if not row:
        return ""

    # Estimate average character width from token widths and text lengths
    char_widths = []
    for x, y, w, h, text in row:
        if text:
            char_widths.append(w / max(len(text), 1))
    avg_char_w = sum(char_widths) / len(char_widths) if char_widths else 10.0
    gap_threshold = col_gap_factor * avg_char_w

    parts = [row[0][4]]
    for i in range(1, len(row)):
        prev_x, _, prev_w, _, _ = row[i - 1]
        curr_x = row[i][0]
        gap = curr_x - (prev_x + prev_w)
        separator = "\t" if gap > gap_threshold else " "
        parts.append(separator + row[i][4])

    return "".join(parts)


def _reconstruct_page_text(page_result) -> list[str]:
    """
    Given raw PaddleOCR output for one page, reconstruct spatially-ordered lines.

    Spatial reconstruction:
      1. Parse bounding boxes to get (x, y, w, h) for each token.
      2. Group tokens into horizontal rows using vertical proximity.
      3. Within each row, sort tokens left-to-right.
      4. Insert TAB separators between tokens with large horizontal gaps
         (this marks column boundaries in flat tables).

    Returns a list of text lines for this page.
    """
    if not page_result:
        return []

    tokens: list[tuple[float, float, float, float, str]] = []

    for line in page_result:
        if line is None:
            continue
        box = line[0]      # [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]
        text_info = line[1]  # (text, confidence)

        text = str(text_info[0]).strip()
        if not text:
            continue

        x, y, w, h = _box_to_xywh(box)
        tokens.append((x, y, w, h, text))

    if not tokens:
        return []

    rows = _group_into_rows(tokens)
    return [_tokens_to_line(row) for row in rows if row]


# ---------------------------------------------------------------------------
# Public API — drop-in replacement for process_pdf
# ---------------------------------------------------------------------------

def process_pdf(pdf_path: str, dpi: int = 300) -> str:
    """
    OCR a PDF and return spatially-reconstructed plain text.

    Key improvements over the original implementation:
    - Bounding boxes are used to sort tokens into correct left-to-right order
      within each visual row, fixing Layout G (flat CCRIS table) mislabelling.
    - Large horizontal gaps between tokens produce TAB characters, which the
      LLM prompt's LAYOUT G section can detect as column separators.
    - Rows are grouped by vertical proximity, not by PaddleOCR's internal
      line order (which can be non-deterministic for multi-column tables).
    """
    pdf_path = str(pdf_path)
    logger.info("Processing: %s at %d DPI", pdf_path, dpi)

    images = _pdf_to_images(pdf_path, dpi=dpi)
    ocr = _get_ocr()

    all_lines: list[str] = []
    t0 = time.time()

    for page_num, img_array in images:
        logger.debug("OCR on page %d...", page_num)

        try:
            results = ocr.ocr(img_array, cls=True)
        except Exception as exc:
            logger.warning("Page %d OCR error: %s", page_num, exc)
            continue

        if not results:
            logger.debug("Page %d: no text detected.", page_num)
            continue

        for page_result in results:
            page_lines = _reconstruct_page_text(page_result)
            all_lines.extend(page_lines)

    elapsed = time.time() - t0
    logger.info("Done in %.1fs — %d lines extracted", elapsed, len(all_lines))
    return "\n".join(all_lines)