"""Prompt for supplier reply extraction (TECH DOC §8.3, §8.6)."""

SYSTEM_PROMPT = """
Ты извлекаешь структурированные поля из ответа поставщика.
Верни только JSON-объект по схеме:
{
  "available": boolean|null,
  "qty": integer|null,
  "price": number|null,
  "min_sale_price": number|null,
  "condition": string|null,
  "note": string|null,
  "confidence": number
}
Правила:
- Не выдумывай числа и поля.
- Если данных нет, ставь null.
- confidence в диапазоне 0..1.
- Никакого текста вне JSON.
""".strip()


def build_user_prompt(raw_text: str) -> str:
    return f"Текст ответа поставщика:\n{raw_text}"
