"""Jev transport errors. Messages never include the API key or response bodies."""


class JevError(Exception):
    """Base provider failure. ``code`` is a bounded metric label."""

    code = "jev_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(self.code)


class JevTimeout(JevError):
    code = "jev_timeout"


class JevRateLimited(JevError):
    code = "jev_rate_limited"


class JevAuthError(JevError):
    code = "jev_auth_error"


class JevInvalidResponse(JevError):
    code = "jev_invalid_response"


class JevUnavailable(JevError):
    code = "jev_unavailable"


def redact(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned
