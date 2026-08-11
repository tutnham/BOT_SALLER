"""LLM client abstraction with strict validation and safe fallbacks."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.llm.prompts.format_report import (
    SYSTEM_PROMPT as REPORT_SYSTEM_PROMPT,
)
from app.llm.prompts.format_report import (
    build_user_prompt as build_report_user_prompt,
)
from app.llm.prompts.normalize_request import (
    SYSTEM_PROMPT as NORMALIZE_SYSTEM_PROMPT,
)
from app.llm.prompts.normalize_request import (
    build_user_prompt as build_normalize_user_prompt,
)
from app.llm.prompts.parse_price_list import (
    SYSTEM_PROMPT as PRICE_LIST_SYSTEM_PROMPT,
)
from app.llm.prompts.parse_price_list import (
    build_user_prompt as build_price_list_user_prompt,
)
from app.llm.prompts.parse_supplier_reply import (
    SYSTEM_PROMPT as SUPPLIER_SYSTEM_PROMPT,
)
from app.llm.prompts.parse_supplier_reply import (
    build_user_prompt as build_supplier_user_prompt,
)
from app.llm.schemas import (
    NormalizedRequest,
    ParsedPriceList,
    ParsedSupplierReply,
    ReportMetrics,
    validate_price_list_payload,
)


class LLMProviderError(Exception):
    """Provider unavailable or transport-level error."""


class LLMClientProtocol(Protocol):
    async def parse_supplier_reply(self, raw_text: str) -> ParsedSupplierReply:
        ...

    async def normalize_request(self, raw_text: str) -> NormalizedRequest:
        ...

    async def parse_price_list(self, raw_text: str) -> ParsedPriceList:
        ...

    async def format_report(self, metrics: dict[str, Any]) -> str:
        ...


def validate_supplier_reply_payload(payload: Any) -> ParsedSupplierReply:
    """Validate provider payload. Invalid shape maps to confidence=0."""
    if not isinstance(payload, dict):
        return ParsedSupplierReply(confidence=0.0)
    try:
        return ParsedSupplierReply.model_validate(payload)
    except ValidationError:
        return ParsedSupplierReply(confidence=0.0)


def validate_normalized_request_payload(payload: Any) -> NormalizedRequest:
    """Validate normalize_request payload. Invalid shape maps to confidence=0."""
    if not isinstance(payload, dict):
        return NormalizedRequest(model="", confidence=0.0)
    try:
        return NormalizedRequest.model_validate(payload)
    except ValidationError:
        return NormalizedRequest(model="", confidence=0.0)


class DefaultLLMClient:
    """HTTP-based provider switch: openai-compatible or ollama."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=get_settings().llm_timeout_seconds)

    async def parse_supplier_reply(self, raw_text: str) -> ParsedSupplierReply:
        payload = await self._call_json(
            system_prompt=SUPPLIER_SYSTEM_PROMPT,
            user_prompt=build_supplier_user_prompt(raw_text),
            expect_json=True,
        )
        return validate_supplier_reply_payload(payload)

    async def normalize_request(self, raw_text: str) -> NormalizedRequest:
        payload = await self._call_json(
            system_prompt=NORMALIZE_SYSTEM_PROMPT,
            user_prompt=build_normalize_user_prompt(raw_text),
            expect_json=True,
        )
        return validate_normalized_request_payload(payload)

    async def parse_price_list(self, raw_text: str) -> ParsedPriceList:
        payload = await self._call_json(
            system_prompt=PRICE_LIST_SYSTEM_PROMPT,
            user_prompt=build_price_list_user_prompt(raw_text),
            expect_json=True,
        )
        return validate_price_list_payload(payload)

    async def format_report(self, metrics: dict[str, Any]) -> str:
        validated = ReportMetrics.model_validate(metrics)
        payload = await self._call_json(
            system_prompt=REPORT_SYSTEM_PROMPT,
            user_prompt=build_report_user_prompt(validated.model_dump_json()),
            expect_json=False,
        )
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            text = payload.get("text")
            if isinstance(text, str):
                return text.strip()
        raise LLMProviderError("invalid_report_response")

    async def _call_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        expect_json: bool,
    ) -> Any:
        settings = get_settings()
        provider = (settings.llm_provider or "").strip().lower()
        if not provider:
            raise LLMProviderError("llm_provider_not_configured")

        if provider in {"openai", "openai_compatible", "openrouter"}:
            return await self._call_openai_compatible(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                expect_json=expect_json,
            )

        if provider == "ollama":
            return await self._call_ollama(
                model=settings.llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                expect_json=expect_json,
            )

        raise LLMProviderError(f"unsupported_llm_provider:{provider}")

    async def _call_openai_compatible(
        self,
        *,
        api_key: str | None,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        expect_json: bool,
    ) -> Any:
        if not api_key or not model:
            raise LLMProviderError("llm_api_key_or_model_missing")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {api_key}"}
        base_url = (get_settings().llm_base_url or "").rstrip("/")
        if not base_url:
            raise LLMProviderError("llm_base_url_missing")
        try:
            response = await self._post_with_retry(
                f"{base_url}/chat/completions",
                payload=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"llm_transport_error:{exc}") from exc

        if response.status_code >= 400:
            raise LLMProviderError(f"llm_http_{response.status_code}")

        if len(response.content) > 5_000_000:
            raise LLMProviderError("llm_response_too_large")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError("llm_empty_choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("llm_empty_content")

        if expect_json:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {}
        return content

    async def _call_ollama(
        self,
        *,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        expect_json: bool,
    ) -> Any:
        if not model:
            raise LLMProviderError("llm_model_missing")

        prompt = f"{system_prompt}\n\n{user_prompt}"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        if expect_json:
            payload["format"] = "json"

        base_url = (get_settings().ollama_base_url or "").rstrip("/")
        if not base_url:
            raise LLMProviderError("ollama_base_url_missing")
        try:
            response = await self._post_with_retry(
                f"{base_url}/api/generate",
                payload=payload,
                headers=None,
            )
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"llm_transport_error:{exc}") from exc

        if response.status_code >= 400:
            raise LLMProviderError(f"llm_http_{response.status_code}")

        if len(response.content) > 5_000_000:
            raise LLMProviderError("llm_response_too_large")
        data = response.json()
        content = data.get("response")
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("llm_empty_content")
        if expect_json:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {}
        return content

    async def _post_with_retry(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(1, 4):
            response = await self._http.post(url, json=payload, headers=headers)
            if response.status_code < 500:
                break
            if attempt < 3:
                continue
        assert response is not None
        return response


_client: LLMClientProtocol | None = None


def get_llm_client() -> LLMClientProtocol:
    global _client
    if _client is None:
        _client = DefaultLLMClient()
    return _client


def set_llm_client(client: LLMClientProtocol | None) -> None:
    global _client
    _client = client
