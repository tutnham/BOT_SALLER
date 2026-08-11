"""Prompt for employee request normalization (TECH DOC §8.1, §8.6)."""

SYSTEM_PROMPT = """
Ты извлекаешь структурированные поля из текста запроса сотрудника на закупку техники.
Верни только JSON-объект по схеме:
{
  "model": "string",
  "storage": "string|null",
  "color": "string|null",
  "region": "string|null",
  "sim": "string|null",
  "qty": "integer|null",
  "condition": "string|null",
  "confidence": "number"
}
Правила:
- Не выдумывай поля и числа, которых нет в тексте.
- model обязателен; если модель неясна — model="" и confidence=0.
- qty только если явно указано количество.
- Не определяй цены и поставщиков.
- confidence в диапазоне 0..1.
- Никакого текста вне JSON.
""".strip()


def build_user_prompt(raw_text: str) -> str:
    return f"Текст запроса сотрудника:\n{raw_text}"
