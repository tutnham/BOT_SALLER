"""Jev shadow summary stays empty until employees confirm outcomes."""

from types import SimpleNamespace

from app.services.jev_shadow_eval import summarize


def _row(**kwargs: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "message_in_id": 1,
        "review_outcome": None,
        "recommended_action": "review",
        "error_code": None,
        "reason_code": "jev_ambiguous",
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "from_cache": False,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_summarize_without_employee_outcomes() -> None:
    report = summarize([])
    assert report["false_auto_bind_rate"] is None
    assert report["sufficient_employee_outcomes"] is False
    assert report["note"]


def test_summarize_counts_batch_usage_and_cache_once() -> None:
    rows = [
        _row(message_in_id=1, latency_ms=400, input_tokens=100, output_tokens=20),
        _row(message_in_id=1, latency_ms=400),
        _row(
            message_in_id=2,
            from_cache=True,
            input_tokens=100,
            output_tokens=20,
            latency_ms=400,
        ),
    ]
    report = summarize(rows)
    assert report["input_tokens_total"] == 100
    assert report["output_tokens_total"] == 20
    assert report["cache_hits"] == 1
    assert report["avg_latency_ms"] == 400
