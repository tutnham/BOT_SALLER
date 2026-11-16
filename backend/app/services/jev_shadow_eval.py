"""Measured shadow comparison. Rates stay empty until employee outcomes exist."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from app.db.models import SupplierAiDecision


def summarize(rows: list[SupplierAiDecision]) -> dict[str, object]:
    reviewed = [row for row in rows if row.review_outcome]
    false_auto = [
        row
        for row in reviewed
        if row.recommended_action == "auto_bind" and row.review_outcome != "bound"
    ]
    # One sample per provider call. Sibling lines of a chunk share latency_ms.
    seen_latency: set[tuple[int, int]] = set()
    latencies: list[int] = []
    for row in rows:
        if row.latency_ms is None:
            continue
        key = (row.message_in_id, row.latency_ms)
        if key in seen_latency:
            continue
        seen_latency.add(key)
        latencies.append(row.latency_ms)
    errors = [row for row in rows if row.error_code]
    rate: str | None
    if not reviewed:
        rate = None
    else:
        rate = str((Decimal(len(false_auto)) / Decimal(len(reviewed))).quantize(Decimal("0.0001")))
    # Cost accounting: batch usage is stored once, on the first parsed line
    # of the chunk. Cache replays copy no tokens. Summing from_cache rows
    # would bill the same call twice.
    billable = [row for row in rows if not row.from_cache]
    input_tokens = sum(row.input_tokens or 0 for row in billable)
    output_tokens = sum(row.output_tokens or 0 for row in billable)
    ok_rows = [row for row in rows if not row.error_code]
    cache_hits = sum(1 for row in ok_rows if row.from_cache)
    cache_rate: str | None
    if not ok_rows:
        cache_rate = None
    else:
        cache_rate = str((Decimal(cache_hits) / Decimal(len(ok_rows))).quantize(Decimal("0.0001")))
    return {
        "decisions": len(rows),
        "employee_confirmed": len(reviewed),
        "false_auto_bind": len(false_auto),
        "false_auto_bind_rate": rate,
        "review_recommended": sum(1 for row in rows if row.recommended_action == "review"),
        "provider_errors": len(errors),
        "error_codes": dict(Counter(row.error_code for row in errors if row.error_code)),
        "reason_codes": dict(Counter(row.reason_code for row in rows if row.reason_code)),
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "input_tokens_total": input_tokens,
        "output_tokens_total": output_tokens,
        "cache_hits": cache_hits,
        "cache_hit_rate": cache_rate,
        "sufficient_employee_outcomes": bool(reviewed),
        "note": None
        if reviewed
        else "Недостаточно employee-confirmed исходов. Ставки precision/recall не считаются.",
    }
