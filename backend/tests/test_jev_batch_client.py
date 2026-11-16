"""Batch fan-out HTTP adapter: one request, per-line answers. No live API calls."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.integrations.jev.client import HttpJevClient
from app.integrations.jev.exceptions import JevInvalidResponse, JevTimeout
from app.integrations.jev.models import JevCandidate, JevProviderResponse


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "telegram_bot_token": "tok",
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "webhook_secret": "sec",
        "telegram_webhook_secret_token": "sec-telegram",
        "metrics_token": "metrics",
        "jev_enabled": True,
        "jev_api_key": "k",
        "jev_api_base_url": "https://api.typesafe.ai",
        "jev_model": "jev-1.14.0",
        "jev_timeout_seconds": 1,
        "jev_max_retries": 0,
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)  # type: ignore[arg-type]


def _candidates() -> list[JevCandidate]:
    return [
        JevCandidate(candidate_id="request:7", description="iPhone 17 Pro 256 Blue"),
        JevCandidate(candidate_id="request:8", description="iPhone 15 128 Black"),
    ]


def _line_answers(
    line_no: int,
    *,
    noul: float = 0.99,
    choice: str = "request:7",
    matches: float | None = 0.9,
) -> dict[str, object]:
    answers: dict[str, object] = {
        f"is_price_line_{line_no}": {"type": "noul", "noul": noul},
        f"binding_line_{line_no}": {
            "type": "choice",
            "choice": choice,
            "probabilities": {choice: 0.98, "none": 0.01, "ambiguous": 0.01},
            "confidence": 0.96,
        },
    }
    if matches is not None:
        answers[f"matches_listed_order_line_{line_no}"] = {"type": "noul", "noul": matches}
    return answers


def _batch_body(per_line: dict[int, dict[str, object]]) -> dict[str, object]:
    answers: dict[str, object] = {}
    for line_answers in per_line.values():
        answers.update(line_answers)
    return {
        "model": "jev-1.14.0",
        "answers": answers,
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def _client(handler: object, **overrides: object) -> HttpJevClient:
    return HttpJevClient(
        settings=_settings(**overrides),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_one_request_for_many_lines() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_batch_body(
                {
                    1: _line_answers(1, noul=0.99, choice="request:7"),
                    2: _line_answers(2, noul=0.01, choice="none", matches=0.1),
                }
            ),
        )

    client = _client(handler)
    result = await client.classify_supplier_lines(
        lines={1: "iPhone 17 Pro 256 Blue 117900", 2: "привет"},
        candidates=_candidates(),
        correlation_id="c1",
    )
    await client.aclose()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["state"]["lines"] == {
        "line_1": "iPhone 17 Pro 256 Blue 117900",
        "line_2": "привет",
    }
    assert len(payload["state"]["candidates"]) == 2
    assert "is_price_line_1" in payload["questions"]
    assert "binding_line_2" in payload["questions"]
    assert "message_type" not in payload["questions"]

    first = result[1]
    second = result[2]
    assert isinstance(first, JevProviderResponse)
    assert isinstance(second, JevProviderResponse)
    assert first.binding_choice == "request:7"
    assert first.price_probability == Decimal("0.99")
    assert first.message_choice == "price_offer"
    assert first.matches_listed_order == Decimal("0.9")
    # The noul is authoritative: low probability reads not_price in code.
    assert second.message_choice == "not_price"
    assert second.price_probability == Decimal("0.01")
    # Usage запроса целиком — на первой разобранной строке, не делится по строкам.
    assert first.input_tokens == 120
    assert first.output_tokens == 40
    assert second.input_tokens is None
    assert second.output_tokens is None


@pytest.mark.asyncio
async def test_unsettled_noul_reads_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_batch_body({3: _line_answers(3, noul=0.5)}))

    client = _client(handler)
    result = await client.classify_supplier_lines(
        lines={3: "117900"},
        candidates=_candidates(),
        correlation_id="c1",
    )
    await client.aclose()
    answer = result[3]
    assert isinstance(answer, JevProviderResponse)
    assert answer.message_choice == "ambiguous"


@pytest.mark.asyncio
async def test_partial_invalid_line_keeps_other_answers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_batch_body(
                {
                    1: _line_answers(1),
                    2: _line_answers(2, choice="request:999"),
                }
            ),
        )

    client = _client(handler)
    result = await client.classify_supplier_lines(
        lines={1: "117900", 2: "68000"},
        candidates=_candidates(),
        correlation_id="c1",
    )
    await client.aclose()
    assert isinstance(result[1], JevProviderResponse)
    assert result[1].input_tokens == 120
    assert result[1].output_tokens == 40
    assert isinstance(result[2], JevInvalidResponse)


@pytest.mark.asyncio
async def test_missing_line_answer_is_invalid_for_that_line() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_batch_body({1: _line_answers(1)}))

    client = _client(handler)
    result = await client.classify_supplier_lines(
        lines={1: "117900", 2: "68000"},
        candidates=_candidates(),
        correlation_id="c1",
    )
    await client.aclose()
    assert isinstance(result[1], JevProviderResponse)
    assert result[1].input_tokens == 120
    assert result[1].output_tokens == 40
    assert isinstance(result[2], JevInvalidResponse)


@pytest.mark.asyncio
async def test_malformed_whole_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": []})

    client = _client(handler)
    with pytest.raises(JevInvalidResponse):
        await client.classify_supplier_lines(
            lines={1: "117900"},
            candidates=_candidates(),
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_raises_for_whole_chunk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client(handler)
    with pytest.raises(JevTimeout):
        await client.classify_supplier_lines(
            lines={1: "117900", 2: "68000"},
            candidates=_candidates(),
            correlation_id="c1",
        )
    await client.aclose()