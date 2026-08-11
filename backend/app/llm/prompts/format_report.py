"""Prompt for report formatting (TECH DOC §8.4)."""

SYSTEM_PROMPT = """
Сформируй краткий отчёт в Telegram HTML.
ЖЁСТКИЕ ПРАВИЛА:
1) Используй только числа из входного JSON.
2) Ничего не досчитывай, не оценивай, не придумывай.
3) Если поле null, пиши «н/д».
4) Верни только HTML-текст, без markdown и без пояснений.
""".strip()


def build_user_prompt(metrics_json: str) -> str:
    return f"Метрики отчёта JSON:\n{metrics_json}"
