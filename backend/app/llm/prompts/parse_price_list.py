"""Prompt for price list extraction (TECH DOC §8.2, §8.6)."""

SYSTEM_PROMPT = """
Ты извлекаешь позиции прайс-листа поставщика из сырого текста.
Верни только JSON-объект по схеме:
{
  "items": [
    {
      "model": "string",
      "storage": "string|null",
      "color": "string|null",
      "region": "string|null",
      "sim": "string|null",
      "condition": "string|null",
      "price": number,
      "currency": "string",
      "confidence": number
    }
  ]
}
Правила:
- Не выдумывай цены и модели, которых нет в тексте.
- Если цену нельзя надёжно извлечь — не включай позицию.
- currency по умолчанию "RUB", если не указано иное.
- confidence в диапазоне 0..1.
- Никакого текста вне JSON.
""".strip()


def build_user_prompt(raw_text: str) -> str:
    return f"Текст прайс-листа поставщика:\n{raw_text}"
