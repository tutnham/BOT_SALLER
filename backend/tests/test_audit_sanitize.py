from app.services.audit_sanitize import sanitize_audit_state


def test_sanitize_drops_secrets() -> None:
    result = sanitize_audit_state(
        {"active": True, "webhook_secret": "abc", "name": "x"}
    )
    assert result == {"active": True, "name": "x"}
