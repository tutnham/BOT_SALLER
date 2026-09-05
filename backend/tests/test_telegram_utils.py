"""Unit tests for Telegram payload helpers."""

from app.utils.telegram import (
    extract_forwarded_user_id,
    parse_telegram_id_text,
)


def test_parse_telegram_id_text_valid() -> None:
    assert parse_telegram_id_text("123456789") == 123456789


def test_parse_telegram_id_text_rejects_non_digits() -> None:
    assert parse_telegram_id_text("123abc") is None
    assert parse_telegram_id_text("@user") is None
    assert parse_telegram_id_text("") is None
    assert parse_telegram_id_text("0") is None


def test_extract_forwarded_user_id_from_forward_from() -> None:
    message = {"forward_from": {"id": 42424242, "is_bot": False}}
    assert extract_forwarded_user_id(message) == 42424242


def test_extract_forwarded_user_id_from_forward_origin_user() -> None:
    message = {
        "forward_origin": {
            "type": "user",
            "sender_user": {"id": 51515151, "is_bot": False},
        }
    }
    assert extract_forwarded_user_id(message) == 51515151


def test_extract_forwarded_user_id_hidden_origin() -> None:
    message = {
        "forward_origin": {
            "type": "hidden_user",
            "sender_user_name": "Hidden",
        }
    }
    assert extract_forwarded_user_id(message) is None
