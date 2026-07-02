from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "LLM Extraction Service"
    app_version: str = "1.0.0"
    debug: bool = False

    # ---------------------------------------------------------------------------
    # LLM microservice configuration
    # ---------------------------------------------------------------------------
    llm_base_url: str = ""
    llm_extract_endpoint: str = ""
    llm_timeout_seconds: float = 600.0
    llm_max_concurrent: int = 3             # matches pod count, controls semaphore
    llm_max_retries: int = 2                # retries per file before marking error
    llm_retry_base_backoff: float = 2.0     # seconds, doubles each retry
    llm_api_key: str | None = None
    llm_model_name: str = ""
    helper_id: str = ""
    max_tokens: int = 2048

    # ---------------------------------------------------------------------------
    # PaddleOCR (in-process) configuration
    # ---------------------------------------------------------------------------
    ocr_timeout_seconds: float = 300.0

    # ---------------------------------------------------------------------------
    # Input safety
    # ---------------------------------------------------------------------------
    max_input_characters: int = 50_000

    # ---------------------------------------------------------------------------
    # File upload
    # ---------------------------------------------------------------------------
    allowed_upload_extensions: list[str] = [".pdf"]
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB

    max_files_per_batch: int = 500 # not used since no more batch processing

    # ---------------------------------------------------------------------------
    # /extract — "Submit & Forget" concurrency cap/rate limiting
    # ---------------------------------------------------------------------------
    extract_max_pending_tasks: int = 50

    # Seconds to wait for in-flight single-extraction tasks to finish during
    # graceful shutdown before giving up and letting the process exit anyway.
    extract_shutdown_drain_seconds: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # ---------------------------------------------------------------------------
    # File retention (results / failed / OCR outputs)
    # ---------------------------------------------------------------------------
    retention_max_days: int = 90

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("llm_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _positive_llm_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("llm_timeout_seconds must be positive")
        return v

    @field_validator("ocr_timeout_seconds")
    @classmethod
    def _positive_ocr_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("ocr_timeout_seconds must be positive")
        return v

    @field_validator("max_files_per_batch")
    @classmethod
    def _positive_max_files(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_files_per_batch must be positive")
        return v

    @field_validator("extract_max_pending_tasks")
    @classmethod
    def _positive_extract_max_pending(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("extract_max_pending_tasks must be positive")
        return v
    
    @field_validator("retention_max_days")
    @classmethod
    def _positive_retention_max_days(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("retention_max_days must be positive")
        return v

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------
    @property
    def llm_url(self) -> str:
        return f"{self.llm_base_url}{self.llm_extract_endpoint}"


@lru_cache
def get_settings() -> Settings:
    return Settings()