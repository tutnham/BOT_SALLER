"""LLM prompt for binding supplier reply to open request candidates."""

SYSTEM_PROMPT = (
    "You decide if a supplier message is a price quote for one of the listed open requests. "
    "Respond with JSON only: "
    '{"related": bool, "request_id": int|null, "price": int|null, '
    '"currency": "RUB"|null, "confidence": 0.0-1.0}. '
    "request_id MUST be one of the candidate ids or null. "
    "If unrelated (greeting, ok, ad), related=false."
)


def build_user_prompt(raw_text: str, candidates: list[dict]) -> str:
    lines = ["Candidates:"]
    for item in candidates:
        lines.append(
            f"- id={item['id']} model={item.get('model')} "
            f"storage={item.get('storage')} color={item.get('color')} "
            f"sim={item.get('sim')} region={item.get('region')} qty={item.get('qty')}"
        )
    lines.append(f"\nSupplier message:\n{raw_text}")
    return "\n".join(lines)
