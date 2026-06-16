import os
from dataclasses import dataclass
from enum import Enum

from app.config import (
    DEFAULT_OCR_MODE,
    OCR_PAGE_WORKERS,
    PDF_RENDER_ZOOM_ACCURATE,
    PDF_RENDER_ZOOM_FAST,
)


class OcrMode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


@dataclass(frozen=True)
class OcrProfile:
    mode: OcrMode
    zoom: float
    dual_ocr: bool
    page_workers: int
    paddle_use_gpu: bool

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "zoom": self.zoom,
            "dual_ocr": self.dual_ocr,
            "page_workers": self.page_workers,
            "paddle_use_gpu": self.paddle_use_gpu,
        }


def paddle_gpu_available() -> bool:
    try:
        import paddle

        return paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
    except Exception:
        return False


def resolve_paddle_use_gpu(env_value: str) -> bool:
    normalized = env_value.strip().lower()
    if normalized in ("1", "true", "yes"):
        return True
    if normalized in ("0", "false", "no"):
        return False
    return paddle_gpu_available()


def parse_ocr_mode(value: str | None) -> OcrMode:
    if not value:
        try:
            return OcrMode(DEFAULT_OCR_MODE)
        except ValueError:
            return OcrMode.ACCURATE
    normalized = value.strip().lower()
    if normalized in (OcrMode.FAST.value, "быстрый", "quick"):
        return OcrMode.FAST
    if normalized in (OcrMode.ACCURATE.value, "точный", "precise", "full"):
        return OcrMode.ACCURATE
    raise ValueError(
        f"Неизвестный режим OCR: {value!r}. Допустимо: fast, accurate"
    )


def build_ocr_profile(mode: OcrMode, *, paddle_use_gpu: bool) -> OcrProfile:
    if mode == OcrMode.FAST:
        return OcrProfile(
            mode=mode,
            zoom=PDF_RENDER_ZOOM_FAST,
            dual_ocr=False,
            page_workers=OCR_PAGE_WORKERS,
            paddle_use_gpu=False,
        )
    return OcrProfile(
        mode=mode,
        zoom=PDF_RENDER_ZOOM_ACCURATE,
        dual_ocr=True,
        page_workers=OCR_PAGE_WORKERS,
        paddle_use_gpu=paddle_use_gpu,
    )


def resolve_ocr_profile(mode_value: str | None, paddle_use_gpu: bool) -> OcrProfile:
    mode = parse_ocr_mode(mode_value)
    return build_ocr_profile(mode, paddle_use_gpu=paddle_use_gpu)
