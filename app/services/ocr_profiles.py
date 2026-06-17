import os
from dataclasses import dataclass
from enum import Enum

from app.config import PDF_RENDER_ZOOM


class OcrMode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


@dataclass(frozen=True)
class OcrProfile:
    mode: OcrMode
    dual_ocr: bool
    zoom: float

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "dual_ocr": self.dual_ocr,
            "zoom": self.zoom,
        }


_PROFILES: dict[OcrMode, OcrProfile] = {
    OcrMode.FAST: OcrProfile(
        mode=OcrMode.FAST,
        dual_ocr=False,
        zoom=float(os.getenv("PDF_RENDER_ZOOM_FAST", "2.0")),
    ),
    OcrMode.ACCURATE: OcrProfile(
        mode=OcrMode.ACCURATE,
        dual_ocr=True,
        zoom=PDF_RENDER_ZOOM,
    ),
}

_DEFAULT_MODE_NAME = os.getenv("OCR_MODE_DEFAULT", OcrMode.ACCURATE.value)


def resolve_ocr_profile(mode_param: str | None) -> OcrProfile:
    """Возвращает профиль OCR по имени режима или значению по умолчанию."""
    if not mode_param or not str(mode_param).strip():
        try:
            default_mode = OcrMode(_DEFAULT_MODE_NAME.strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"Некорректный OCR_MODE_DEFAULT: {_DEFAULT_MODE_NAME!r}. "
                f"Доступны: {', '.join(m.value for m in OcrMode)}"
            ) from exc
        return _PROFILES[default_mode]

    normalized = str(mode_param).strip().lower()
    try:
        mode = OcrMode(normalized)
    except ValueError as exc:
        supported = ", ".join(m.value for m in OcrMode)
        raise ValueError(
            f"Неизвестный режим OCR: {mode_param!r}. Доступны: {supported}"
        ) from exc

    return _PROFILES[mode]
