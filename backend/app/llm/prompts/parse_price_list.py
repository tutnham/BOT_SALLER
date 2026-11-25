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
- Во входе может быть несколько сообщений канала подряд (разделитель ---) — разбирай все.
- Наименование: model (и storage/color если есть в строке, напр. MacBook Air 13" M2 8GB/256GB).
- Голая модель без бренда — это iPhone: «18 pro 512gb» → model «iPhone 18 Pro», storage «512GB». То же для 13–19 Pro/Max/Plus/Air/E и для «14 128GB».
- Слово custom не пиши.
- Цена: только число из прайсовой строки; при двух валютах (у.е. / USD и ₽) бери **₽ после слэша**.
- Строки с •, —, эмодзи и «(N шт)» — нормальный прайс; заголовки без цены пропускай.
- Не выдумывай позиции.
- currency всегда "RUB", если в строке есть ₽; иначе "RUB" по умолчанию.
- confidence в диапазоне 0..1.
- Никакого текста вне JSON.
""".strip()


def build_user_prompt(raw_text: str) -> str:
    return f"Текст прайс-листа поставщика:\n{raw_text}"
