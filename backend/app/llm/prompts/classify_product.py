"""LLM prompt for product category classification."""

SYSTEM_PROMPT = (
    "You classify hardware product text into exactly one category. "
    "Respond with JSON only: "
    '{"category": "apple|samsung|power_station|other|unknown", "confidence": 0.0-1.0}. '
    "apple: iPhone, iPad, MacBook, AirPods, Apple Watch. "
    "samsung: Samsung/Galaxy phones and tablets. "
    "power_station: portable power stations (EcoFlow, Bluetti, Jackery). "
    "other: other known brands. unknown: cannot determine."
)


def build_user_prompt(raw_text: str) -> str:
    return f"Product text:\n{raw_text}"
