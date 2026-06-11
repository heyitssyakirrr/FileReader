from __future__ import annotations

import json
import logging
import re
import time

import httpx
from fastapi import HTTPException

from core.config import get_settings

logger = logging.getLogger(__name__)


def _strip_trailing_commas(text: str) -> str:
    """
    Remove trailing commas before closing brackets/braces.
    Handles: [..., ] and {..., }
    """
    text = re.sub(r',\s*(\])', r'\1', text)
    text = re.sub(r',\s*(\})', r'\1', text)
    return text


def _extract_last_json_object(text: str) -> str | None:
    """
    Walk through the text tracking brace depth, ignoring braces inside strings.
    Returns the last complete top-level {...} block found.
    """
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
            continue  # ignore { and } inside string values

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
    """
    Return all complete top-level JSON object blocks in order.
    Correctly ignores braces that appear inside string values.
    """
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
            continue  # ignore { and } inside string values

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
    """
    Merge dicts from left to right, only overriding when the new value is non-empty.
    Helps when the LLM emits multiple JSON objects and some fields are null/missing
    in later duplicates.
    """
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
    """
    Normalise all keys to lowercase with underscores so that keys like
    'Bank_Name', 'BANK_NAME', or 'bankname' all resolve to 'bank_name'.
    This guards against LLMs that vary casing or spacing in key names.
    """
    normalised: dict = {}
    for key, value in d.items():
        # strip spaces, lower-case, replace spaces/hyphens with underscores
        clean_key = re.sub(r'[\s\-]+', '_', key.strip()).lower()
        normalised[clean_key] = value
    return normalised


_EXPECTED_KEYS = {
    "name",
    "master_account_number",
    "sub_account_number",
    "address",
    "fi_num",
    "bank_name",
}


def _filter_expected_keys(d: dict) -> dict:
    """Remove any keys the LLM hallucinated that aren't part of the schema."""
    return {k: v for k, v in d.items() if k in _EXPECTED_KEYS}


def _normalize_llm_output(response_json: dict) -> dict:
    logger.debug("Raw LLM Response: %s", response_json)
    
    try:
        # Support OpenAI chat completion format: choices[0].message.content
        if "choices" in response_json:
            raw = response_json["choices"][0]["message"]["content"].strip()
        # Fallback: legacy format with top-level "text" key
        elif "text" in response_json:
            raw = response_json["text"].strip()
        else:
            raise ValueError("Unexpected LLM response format: no 'choices' or 'text' key found.")

        # Strategy 1
        code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code_blocks:
            logger.debug("Parsing from code block (last of %d found)", len(code_blocks))
            parsed = json.loads(_strip_trailing_commas(code_blocks[-1]))
            result = _filter_expected_keys(_normalise_keys(parsed))  # ← add filter
            logger.debug("Parsed keys: %s", list(result.keys()))
            return result

        # Strategy 2
        json_blocks = _extract_json_objects(raw)
        if json_blocks:
            logger.debug("Parsing from brace-depth extraction (%d object(s))", len(json_blocks))
            parsed_dicts: list[dict] = []
            for block in json_blocks:
                try:
                    parsed = json.loads(_strip_trailing_commas(block))
                    if isinstance(parsed, dict):
                        parsed_dicts.append(_filter_expected_keys(_normalise_keys(parsed)))  # ← add filter
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse JSON block: %s | block: %.200s", e, block)
                    continue

            if parsed_dicts:
                merged = _merge_non_empty_dicts(parsed_dicts)
                logger.debug("Merged keys from %d object(s): %s", len(parsed_dicts), list(merged.keys()))
                return merged

        # Strategy 3
        logger.debug("Attempting truncation repair")
        repaired = raw
        if not repaired.endswith("}"):
            repaired = repaired.rstrip() + "\n}"
        last_json = _extract_last_json_object(repaired)
        if last_json:
            logger.debug("Parsing from repaired truncated JSON")
            parsed = json.loads(_strip_trailing_commas(last_json))
            result = _filter_expected_keys(_normalise_keys(parsed))  # ← add filter
            logger.debug("Repaired parsed keys: %s", list(result.keys()))
            return result

        raise ValueError("No JSON object found in LLM response text.")

    except (KeyError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Failed to parse LLM response. Check the 'text' field in the response.",
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

    async def extract_fields(self, prompt: str, stop: list[str] | None = None, timeout: float | None = None) -> dict:
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
            t0 = time.time()
            effective_timeout = timeout if timeout is not None else self.settings.llm_timeout_seconds
            async with httpx.AsyncClient(timeout=effective_timeout, verify=False) as client:
                response = await client.post(
                    self.settings.llm_url,
                    headers=self._build_headers(),
                    json=payload,
                )
            response.raise_for_status()
            elapsed = time.time() - t0
            logger.debug("LLM HTTP call took %.1fs, raw response length: %d chars", elapsed, len(response.text))

            result = _normalize_llm_output(response.json())
            logger.debug("Final extracted dict keys: %s", list(result.keys()))
            return result

        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="LLM microservice timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"LLM microservice error: HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Unable to connect to LLM microservice.") from exc
