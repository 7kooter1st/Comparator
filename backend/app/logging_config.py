import json
import logging
import sys


def _truncate_value(value: object, max_len: int = 200) -> object:
    if isinstance(value, str):
        if len(value) <= max_len:
            return value
        return f"{value[:max_len]}… ({len(value)} chars)"
    if isinstance(value, dict):
        return {k: _truncate_value(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_value(item, max_len) for item in value[:5]] + (
            [f"… +{len(value) - 5} items"] if len(value) > 5 else []
        )
    return value


def format_payload_for_log(payload: object, max_len: int = 200) -> str:
    """Сериализует payload для консоли, обрезая большие поля (base64 и т.д.)."""
    try:
        if isinstance(payload, str):
            text = payload
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return _truncate_value(text, max_len)  # type: ignore[return-value]
        return json.dumps(_truncate_value(payload, max_len), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)[: max_len + 50]


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
