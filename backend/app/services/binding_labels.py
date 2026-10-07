"""Human-readable labels for binding UI."""

from __future__ import annotations

from app.db.models import Request


def candidate_label(request: Request) -> str:
    normalized = request.normalized_json or {}
    model = normalized.get("model")
    if model:
        parts = [str(model)]
        for key in ("storage", "color"):
            value = normalized.get(key)
            if value:
                parts.append(str(value))
        return " ".join(parts)
    return (request.source_text or "").strip()[:48]
