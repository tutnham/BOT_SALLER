"""Map Pyrogram Message → post fields (doc §7)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.models import ParserContentType, ParserMediaType


def classify_content_type(message: Any) -> ParserContentType:
    """Classify message content_type per doc §7.3."""
    if getattr(message, "media_group_id", None) is not None:
        # media_group is additional flag; still classify underlying media for storage
        # Doc: media_group is a content_type value when album present.
        return ParserContentType.media_group

    if getattr(message, "photo", None):
        return ParserContentType.photo
    if getattr(message, "video", None):
        return ParserContentType.video
    if getattr(message, "document", None):
        return ParserContentType.document
    if getattr(message, "audio", None):
        return ParserContentType.audio
    if getattr(message, "voice", None):
        return ParserContentType.voice

    text = getattr(message, "text", None)
    media = getattr(message, "media", None)
    if text and media is None:
        return ParserContentType.text
    if text and not any(
        getattr(message, attr, None)
        for attr in ("photo", "video", "document", "audio", "voice")
    ):
        return ParserContentType.text

    if media is None and not text and not getattr(message, "caption", None):
        # empty / unsupported
        return ParserContentType.other

    if media is None:
        return ParserContentType.text

    return ParserContentType.other


def _message_to_raw_metadata(message: Any) -> dict[str, Any]:
    """Best-effort serializable snapshot for audit."""
    chat = getattr(message, "chat", None)
    date = getattr(message, "date", None)
    if isinstance(date, datetime):
        date_iso = date.isoformat()
    else:
        date_iso = str(date) if date is not None else None

    return {
        "id": getattr(message, "id", None),
        "chat_id": getattr(chat, "id", None) if chat else None,
        "date": date_iso,
        "text": getattr(message, "text", None),
        "caption": getattr(message, "caption", None),
        "media_group_id": getattr(message, "media_group_id", None),
        "link": getattr(message, "link", None),
        "has_photo": bool(getattr(message, "photo", None)),
        "has_video": bool(getattr(message, "video", None)),
        "has_document": bool(getattr(message, "document", None)),
        "has_audio": bool(getattr(message, "audio", None)),
        "has_voice": bool(getattr(message, "voice", None)),
    }


def map_message_to_post_fields(message: Any) -> dict[str, Any]:
    """Extract required post fields from Pyrogram Message (doc §7.1)."""
    chat = getattr(message, "chat", None)
    channel_id = getattr(chat, "id", None)
    if channel_id is None:
        raise ValueError("message.chat.id missing")

    message_id = getattr(message, "id", None)
    if message_id is None:
        raise ValueError("message.id missing")

    post_date = getattr(message, "date", None)
    if post_date is None:
        post_date = datetime.now(timezone.utc)
    elif post_date.tzinfo is None:
        post_date = post_date.replace(tzinfo=timezone.utc)

    raw_text = getattr(message, "text", None) or getattr(message, "caption", None)
    content_type = classify_content_type(message)

    return {
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "grouped_id": getattr(message, "media_group_id", None),
        "post_date": post_date,
        "content_type": content_type,
        "raw_text": raw_text,
        "message_link": getattr(message, "link", None),
        "raw_metadata": _message_to_raw_metadata(message),
    }


def extract_media_descriptors(message: Any) -> list[dict[str, Any]]:
    """Extract media file descriptors for parser_media_files rows."""
    descriptors: list[dict[str, Any]] = []

    def _add(media_obj: Any, media_type: ParserMediaType) -> None:
        if media_obj is None:
            return
        file_unique_id = getattr(media_obj, "file_unique_id", None)
        if not file_unique_id:
            # photos may be list of sizes — take largest
            if isinstance(media_obj, list) and media_obj:
                media_obj = media_obj[-1]
                file_unique_id = getattr(media_obj, "file_unique_id", None)
        if not file_unique_id:
            return
        descriptors.append(
            {
                "media_type": media_type,
                "file_unique_id": str(file_unique_id),
                "file_id": getattr(media_obj, "file_id", None),
                "file_size": getattr(media_obj, "file_size", None),
                "mime_type": getattr(media_obj, "mime_type", None),
            }
        )

    photo = getattr(message, "photo", None)
    if photo is not None:
        # Pyrogram Photo has file_unique_id on the object
        _add(photo, ParserMediaType.photo)
    _add(getattr(message, "video", None), ParserMediaType.video)
    _add(getattr(message, "document", None), ParserMediaType.document)
    _add(getattr(message, "audio", None), ParserMediaType.audio)
    _add(getattr(message, "voice", None), ParserMediaType.voice)

    return descriptors


MEDIA_CONTENT_TYPES = frozenset(
    {
        ParserContentType.photo,
        ParserContentType.video,
        ParserContentType.document,
        ParserContentType.audio,
        ParserContentType.voice,
        ParserContentType.media_group,
    }
)
