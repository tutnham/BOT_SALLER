"""Parse explicit request id in supplier inbound text."""

from __future__ import annotations

import re

_BATCH_LINE_RE = re.compile(r"#\s*(\d+)\s*-\s*(\d{2})\b", re.IGNORECASE)


def parse_batch_line_code(raw_text: str) -> str | None:
    text = (raw_text or "").strip()
    match = _BATCH_LINE_RE.search(text)
    if match is None:
        return None
    return f"#{int(match.group(1))}-{int(match.group(2)):02d}"


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
    if _BATCH_LINE_RE.search(text):
        return None
    for pattern in _EXPLICIT_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return int(match.group(1))
    return None
