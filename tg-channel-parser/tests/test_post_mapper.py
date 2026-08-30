"""post_mapper content_type classification (doc §7.3)."""

from __future__ import annotations

from types import SimpleNamespace

from app.db.models import ParserContentType
from app.extraction.post_mapper import classify_content_type, map_message_to_post_fields
from tests.conftest import make_message


def test_text_message() -> None:
    msg = make_message(text="price 100", media=None)
    assert classify_content_type(msg) == ParserContentType.text


def test_photo() -> None:
    msg = make_message(text=None, caption="cap", photo=SimpleNamespace(file_unique_id="p1"))
    assert classify_content_type(msg) == ParserContentType.photo


def test_video() -> None:
    msg = make_message(text=None, video=SimpleNamespace(file_unique_id="v1"))
    assert classify_content_type(msg) == ParserContentType.video


def test_document() -> None:
    msg = make_message(text=None, document=SimpleNamespace(file_unique_id="d1"))
    assert classify_content_type(msg) == ParserContentType.document


def test_audio() -> None:
    msg = make_message(text=None, audio=SimpleNamespace(file_unique_id="a1"))
    assert classify_content_type(msg) == ParserContentType.audio


def test_voice() -> None:
    msg = make_message(text=None, voice=SimpleNamespace(file_unique_id="vo1"))
    assert classify_content_type(msg) == ParserContentType.voice


def test_media_group_overrides() -> None:
    msg = make_message(
        text=None,
        caption="album",
        photo=SimpleNamespace(file_unique_id="p1"),
        media_group_id=42,
    )
    assert classify_content_type(msg) == ParserContentType.media_group


def test_other_unsupported() -> None:
    msg = make_message(text=None, caption=None, media=object())
    assert classify_content_type(msg) == ParserContentType.other


def test_map_fields() -> None:
    msg = make_message(chat_id=-1005, message_id=77, text="iPhone 70000")
    fields = map_message_to_post_fields(msg)
    assert fields["channel_id"] == -1005
    assert fields["message_id"] == 77
    assert fields["raw_text"] == "iPhone 70000"
    assert fields["content_type"] == ParserContentType.text
    assert fields["message_link"] == "https://t.me/c/123/1"
    assert "raw_metadata" in fields
