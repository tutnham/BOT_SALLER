"""Split multiline employee batch text into stable line records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BatchSourceLine:
    line_no: int
    source_text: str


def split_batch_source_text(source_text: str) -> list[BatchSourceLine]:
    """Normalize line endings, drop empty lines, assign stable line_no from 1."""
    normalized = (source_text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[BatchSourceLine] = []
    line_no = 0
    for raw in normalized.split("\n"):
        segment = raw.strip()
        if not segment:
            continue
        line_no += 1
        lines.append(BatchSourceLine(line_no=line_no, source_text=segment))
    return lines


def nonempty_line_count(source_text: str) -> int:
    return len(split_batch_source_text(source_text))
