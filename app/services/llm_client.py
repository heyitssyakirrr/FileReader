from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_EXPECTED_KEYS = {
    "name",
    "master_account_number",
    "sub_account_number",
    "address",
    "fi_num",
    "bank_name",
}


@dataclass
class LLMExtractionMetadata:
    http_duration_ms: int | None = None
    status_code: int | None = None
    response_length_chars: int | None = None
    parse_strategy: str | None = None
    json_objects_found: int | None = None
    response_truncated: bool = False
    keys_present: list[str] | None = None
    keys_missing: list[str] | None = None


def _strip_trailing_commas(text: str) -> str:
    text = re.sub(r",\s*(\])", r"\1", text)
    text = re.sub(r",\s*(\})", r"\1", text)
    return text


def _extract_last_json_object(text: str) -> str | None:
    depth = 0
    start = None
    last_candidate = None
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                last_candidate = text[start:i + 1]

    return last_candidate


def _extract_json_objects(text: str) -> list[str]:
    depth = 0
    start = None
    candidates: list[str] = []
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])

    return candidates


def _merge_non_empty_dicts(dicts: list[dict]) -> dict:
    merged: dict = {}
    for item in dicts:
        for key, value in item.items():
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            merged[key] = value
    return merged


def _normalise_keys(d: dict) -> dict:
    normalised: dict = {}
    for key, value in d.items():
        clean_key = re.sub(r"[\s\-]+", "_", key.strip()).lower()
        normalised[clean_key] = value
    return normalised


def _filter_expected_keys(d: dict) -> dict:
    return {k: v for k, v in d.items() if k in _EXPECTED_KEYS}


def _metadata_for_result(
    result: dict,
    parse_strategy: str,
    json_objects_found: int,
    response_truncated: bool,
) -> LLMExtractionMetadata:
    keys_present = [
        key for key in sorted(_EXPECTED_KEYS)
        if result.get(key) not in (None, "")
    ]
    keys_missing = [
        key for key in sorted(_EXPECTED_KEYS)
        if result.get(key) in (None, "")
    ]
    return LLMExtractionMetadata(
        parse_strategy=parse_strategy,
        json_objects_found=json_objects_found,
        response_truncated=response_truncated,
        keys_present=keys_present,
        keys_missing=keys_missing,
    )


def _normalize_llm_output(response_json: dict) -> tuple[dict, LLMExtractionMetadata]:
    try:
        if "choices" in response_json:
            raw = response_json["choices"][0]["message"]["content"].strip()
        elif "text" in response_json:
            raw = response_json["text"].strip()
        else:
            raise ValueError("Unexpected LLM response format: no 'choices' or 'text' key found.")

        code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code_blocks:
            parsed = json.loads(_strip_trailing_commas(code_blocks[-1]))
            result = _filter_expected_keys(_normalise_keys(parsed))
            metadata = _metadata_for_result(result, "code_block", len(code_blocks), False)
            return result, metadata

        json_blocks = _extract_json_objects(raw)
        if json_blocks:
            parsed_dicts: list[dict] = []
            for block in json_blocks:
                try:
                    parsed = json.loads(_strip_trailing_commas(block))
                    if isinstance(parsed, dict):
                        parsed_dicts.append(_filter_expected_keys(_normalise_keys(parsed)))
                except json.JSONDecodeError as exc:
                    logger.warning("Failed to parse one LLM JSON block: %s", exc)
                    continue

            if parsed_dicts:
                merged = _merge_non_empty_dicts(parsed_dicts)
                metadata = _metadata_for_result(merged, "brace_extraction", len(json_blocks), False)
                return merged, metadata

        repaired = raw
        response_truncated = not repaired.endswith("}")
        if response_truncated:
            repaired = repaired.rstrip() + "\n}"
        last_json = _extract_last_json_object(repaired)
        if last_json:
            parsed = json.loads(_strip_trailing_commas(last_json))
            result = _filter_expected_keys(_normalise_keys(parsed))
            metadata = _metadata_for_result(result, "truncation_repair", 1, response_truncated)
            return result, metadata

        raise ValueError("No JSON object found in LLM response text.")

    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Failed to parse LLM response.",
        ) from exc


class LLMClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        if self.settings.helper_id:
            headers["X-PBAI-Helper-Id"] = self.settings.helper_id
        return headers

    async def extract_fields(
        self,
        prompt: str,
        stop: list[str] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict, LLMExtractionMetadata]:
        payload = {
            "model": self.settings.llm_model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.settings.max_tokens,
            "temperature": 0.3,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop

        logger.debug("Prompt length: %d characters", len(prompt))
        logger.debug("Calling LLM microservice at %s", self.settings.llm_url)

        try:
            t0 = time.perf_counter()
            effective_timeout = timeout if timeout is not None else self.settings.llm_timeout_seconds
            async with httpx.AsyncClient(timeout=effective_timeout, verify=False) as client:
                response = await client.post(
                    self.settings.llm_url,
                    headers=self._build_headers(),
                    json=payload,
                )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            response.raise_for_status()

            logger.debug(
                "LLM HTTP call completed: status=%d, duration=%dms, response_length=%d chars",
                response.status_code,
                elapsed_ms,
                len(response.text),
            )

            result, metadata = _normalize_llm_output(response.json())
            metadata.http_duration_ms = elapsed_ms
            metadata.status_code = response.status_code
            metadata.response_length_chars = len(response.text)
            return result, metadata

        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="LLM microservice timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"LLM microservice error: HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Unable to connect to LLM microservice.") from exc