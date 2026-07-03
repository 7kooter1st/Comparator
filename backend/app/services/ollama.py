import json
import re


class OllamaError(Exception):
    pass


def parse_comparison_result(ollama_response: dict) -> dict:
    content = ollama_response.get("message", {}).get("content", "")
    if not content or not content.strip():
        raise OllamaError("Ollama вернула пустой ответ")

    text = content.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ответ модели не является JSON: {content[:300]}") from exc

    if not isinstance(data, dict):
        raise OllamaError("JSON ответа должен быть объектом")
    if "identical" not in data or "differences" not in data:
        raise OllamaError('JSON должен содержать поля "identical" и "differences"')
    if not isinstance(data["differences"], list):
        raise OllamaError('Поле "differences" должно быть массивом')

    return data
