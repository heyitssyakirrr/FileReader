from typing import List
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Customer detail extraction schemas
# ---------------------------------------------------------------------------

class ExtractionResult(BaseModel):
    name: str | None = Field(default=None)
    master_account_number: str | None = Field(default=None)
    sub_account_number: str | None = Field(default=None)
    address: str | None = Field(default=None)
    fi_num: str | None = Field(default=None)
    bank_name: str | None = Field(default=None)   # NEW: extracted by LLM


class ExtractionMeta(BaseModel):
    input_characters: int
    llm_called: bool
    source: str


# ---------------------------------------------------------------------------
# Comparison schemas  (NEW)
# ---------------------------------------------------------------------------

class FieldComparisonDetail(BaseModel):
    """Per-field match result."""
    extracted: str | None
    expected: str | None
    match: bool


class ComparisonResult(BaseModel):
    """
    Populated when the uploaded filename is found in the reference CSV.
    None when no matching CSV row exists (unknown file).
    """
    filename_key: str                           # e.g. "JSB-000486-25"
    csv_row_found: bool

    # Individual field verdicts (only present when csv_row_found=True)
    bank_name:            FieldComparisonDetail | None = None
    fi_num:               FieldComparisonDetail | None = None
    master_account_number: FieldComparisonDetail | None = None
    sub_account_number:   FieldComparisonDetail | None = None

    # Overall verdict
    all_match: bool = False


class ExtractResponse(BaseModel):
    success: bool
    message: str
    data: ExtractionResult
    meta: ExtractionMeta
    comparison: ComparisonResult | None = Field(default=None)   # NEW


# ---------------------------------------------------------------------------
# Bank statement summary schemas
# ---------------------------------------------------------------------------

class RawBalanceRow(BaseModel):
    """Single row returned by the LLM: just date + running balance."""
    date: str
    balance: str


class DailySummary(BaseModel):
    """Computed by Python from raw LLM rows — one entry per unique date."""
    date: str
    min_balance: str
    max_balance: str
    closing_balance: str   # last balance recorded on that date


class MonthlySummary(BaseModel):
    """Computed by Python — aggregated from DailySummary entries."""
    month: str             # YYYY-MM
    min_balance: str
    max_balance: str
    closing_balance: str   # last daily closing balance of the month


class SummaryResult(BaseModel):
    daily_summaries: List[DailySummary] = Field(default_factory=list)
    monthly_summaries: List[MonthlySummary] = Field(default_factory=list)
    overall_min_balance: str | None = Field(default=None)
    overall_max_balance: str | None = Field(default=None)
    overall_closing_balance: str | None = Field(default=None)


class SummaryResponse(BaseModel):
    success: bool
    message: str
    data: SummaryResult
    meta: ExtractionMeta


# ---------------------------------------------------------------------------
# Shared LLM communication schemas
# ---------------------------------------------------------------------------

class LLMRequestPayload(BaseModel):
    prompt: str
    model: str | None = None


class LLMRawResponse(BaseModel):
    content: str


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
