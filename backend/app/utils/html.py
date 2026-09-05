"""Helpers for safe Telegram HTML output."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

TELEGRAM_HTML_PARSE_MODE = "HTML"

_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "pre",
        "s",
        "strike",
        "strong",
        "u",
    }
)


def sanitize_telegram_html(text: str) -> str:
    """
    Conservative sanitizer for Telegram parse_mode HTML.

    Escapes user-controlled fragments to prevent tag injection.
    """
    return escape(text or "", quote=False)


def _safe_href(href: str) -> bool:
    href = (href or "").strip()
    if not href:
        return False
    parsed = urlparse(href)
    return parsed.scheme in {"http", "https", "tg", "mailto"} and bool(parsed.scheme)


class _TelegramReportHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open_a = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower not in _ALLOWED_TAGS:
            self._skip_depth += 1
            return
        if tag_lower == "a":
            href = None
            for key, value in attrs:
                if key.lower() == "href" and value and _safe_href(value):
                    href = value
                    break
            if href is None:
                self._skip_depth += 1
                return
            self._parts.append(f'<a href="{escape(href, quote=True)}">')
            self._open_a = True
            return
        self._parts.append(f"<{tag_lower}>")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if self._skip_depth > 0:
            if tag_lower not in _ALLOWED_TAGS or tag_lower == "a":
                self._skip_depth -= 1
            return
        if tag_lower not in _ALLOWED_TAGS:
            return
        if tag_lower == "a":
            if not self._open_a:
                return
            self._open_a = False
        self._parts.append(f"</{tag_lower}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._parts.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def get_result(self) -> str:
        return "".join(self._parts)


def sanitize_telegram_report_html(text: str) -> str:
    """Whitelist sanitizer for LLM/fallback report HTML before Telegram send."""
    if not text:
        return ""
    parser = _TelegramReportHTMLSanitizer()
    parser.feed(text)
    parser.close()
    return parser.get_result().strip()
