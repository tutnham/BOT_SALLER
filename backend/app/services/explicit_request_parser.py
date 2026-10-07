"""Parse explicit request id in supplier inbound text."""

from __future__ import annotations

import re

_EXPLICIT_PATTERNS = (
    re.compile(r"#\s*(\d+)", re.IGNORECASE),
    re.compile(r"заявк[аеи]\s*[#№]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"на\s*#\s*(\d+)", re.IGNORECASE),
    re.compile(r"на\s*#\s*(\d+)\s*цена", re.IGNORECASE),
)


def parse_explicit_request_id(raw_text: str) -> int | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    for pattern in _EXPLICIT_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return int(match.group(1))
    return None
