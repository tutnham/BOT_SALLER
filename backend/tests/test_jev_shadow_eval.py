"""Jev shadow summary stays empty until employees confirm outcomes."""

from app.services.jev_shadow_eval import summarize


def test_summarize_without_employee_outcomes() -> None:
    report = summarize([])
    assert report["false_auto_bind_rate"] is None
    assert report["sufficient_employee_outcomes"] is False
    assert report["note"]
