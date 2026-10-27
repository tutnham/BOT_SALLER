"""Async REST adapter for POST /v1/systemone. One shared httpx client."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.integrations.jev.exceptions import (
    JevAuthError,
    JevError,
    JevInvalidResponse,
    JevRateLimited,
    JevTimeout,
    JevUnavailable,
)
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.integrations.jev.prompts import build_questions, build_state

_MAX_RESPONSE_BYTES = 256_000
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class JevClientProtocol(Protocol):
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: Sequence[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        ...


def _decimal_map(raw: object) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise JevInvalidResponse()
    parsed: dict[str, Decimal] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise JevInvalidResponse()
        try:
            parsed[key] = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise JevInvalidResponse() from None
    return parsed


class HttpJevClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        timeout = httpx.Timeout(
            self._settings.jev_timeout_seconds,
            connect=min(1.0, self._settings.jev_timeout_seconds),
        )
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: Sequence[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del correlation_id  # local only; not sent to the provider
        settings = self._settings
        candidate_list = list(candidates)
        questions: dict[str, Any] = build_questions(candidate_list)
        payload: dict[str, Any] = {
            "state": build_state(message_text, candidate_list),
            "model": settings.jev_model,
            "questions": questions,
        }
        binding_question = questions["binding"]
        if not isinstance(binding_question, dict):
            raise JevInvalidResponse()
        criteria = binding_question.get("criteria")
        if not isinstance(criteria, dict):
            raise JevInvalidResponse()
        allowed_binding = {str(key) for key in criteria}
        url = f"{settings.jev_api_base_url.rstrip('/')}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {settings.jev_api_key or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        attempts = settings.jev_max_retries + 1
        last_error: JevError = JevUnavailable()
        for attempt in range(attempts):
            try:
                response = await self._http.post(url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = JevTimeout()
            except httpx.HTTPError:
                last_error = JevUnavailable()
            else:
                mapped = self._map_status(response, allowed_binding)
                if isinstance(mapped, JevProviderResponse):
                    return mapped
                last_error = mapped
                if not _should_retry(last_error):
                    raise last_error from None
            if attempt + 1 < attempts and _should_retry(last_error):
                await asyncio.sleep(_backoff(attempt))
                continue
            raise last_error from None
        raise last_error from None

    def _map_status(
        self,
        response: httpx.Response,
        allowed_binding: set[str],
    ) -> JevProviderResponse | JevError:
        status = response.status_code
        if status in {401, 403}:
            return JevAuthError()
        if status == 429:
            return JevRateLimited()
        if status == 422:
            return JevInvalidResponse()
        if status in _RETRY_STATUSES or status >= 500:
            return JevUnavailable()
        if status >= 400:
            return JevInvalidResponse()
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            return JevInvalidResponse()
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return JevInvalidResponse("jev_response_too_large")
        try:
            data = response.json()
        except json.JSONDecodeError:
            return JevInvalidResponse()
        try:
            return _parse_success(data, allowed_binding)
        except JevInvalidResponse as exc:
            return exc


def _should_retry(error: JevError) -> bool:
    return isinstance(error, JevTimeout | JevRateLimited | JevUnavailable)


def _backoff(attempt: int) -> float:
    base = min(1.0, 0.05 * (2**attempt))
    jitter = float(random.uniform(0, 0.05))
    return float(base) + jitter


def _parse_success(data: object, allowed_binding: set[str]) -> JevProviderResponse:
    if not isinstance(data, dict):
        raise JevInvalidResponse()
    model = data.get("model")
    answers = data.get("answers")
    if not isinstance(model, str) or not model or not isinstance(answers, dict):
        raise JevInvalidResponse()
    message = answers.get("message_type")
    price = answers.get("is_price")
    binding = answers.get("binding")
    if not isinstance(message, dict) or not isinstance(price, dict) or not isinstance(binding, dict):
        raise JevInvalidResponse()
    choice = message.get("choice")
    noul = price.get("noul")
    binding_choice = binding.get("choice")
    probabilities = binding.get("probabilities")
    confidence = binding.get("confidence")
    if choice not in {"price_offer", "not_price", "ambiguous"}:
        raise JevInvalidResponse()
    if not isinstance(binding_choice, str) or binding_choice not in allowed_binding:
        raise JevInvalidResponse()
    try:
        price_probability = Decimal(str(noul))
        binding_confidence = Decimal(str(confidence))
    except (InvalidOperation, ValueError):
        raise JevInvalidResponse() from None
    prob_map = _decimal_map(probabilities)
    usage_obj = data.get("usage")
    usage: dict[str, Any] = usage_obj if isinstance(usage_obj, dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    try:
        return JevProviderResponse(
            model=model,
            message_choice=str(choice),
            price_probability=price_probability,
            binding_choice=binding_choice,
            binding_probabilities=prob_map,
            binding_confidence=binding_confidence,
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        )
    except ValidationError:
        raise JevInvalidResponse() from None


_client: JevClientProtocol | None = None


def get_jev_client() -> JevClientProtocol:
    global _client
    if _client is None:
        _client = HttpJevClient()
    return _client


def set_jev_client(client: JevClientProtocol | None) -> None:
    global _client
    _client = client


async def close_jev_client() -> None:
    global _client
    current = _client
    _client = None
    if isinstance(current, HttpJevClient):
        await current.aclose()
