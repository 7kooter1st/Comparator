from dataclasses import dataclass

from app.services.ocr_profiles import OcrProfile
from app.services.paddle_converter import (
    PaddleOcrNotAvailableError,
    _paddle_from_pages,
)
from app.services.pdf_converter import _tesseract_from_pages
from app.services.leptonica_preprocess import preprocess_pages
from app.services.pdf_images import render_pdf_pages


@dataclass
class PdfOcrResult:
    tesseract_text: str
    paddle_text: str | None
    paddle_fallback: bool = False
    paddle_error: str | None = None
    profile: OcrProfile | None = None


def pdf_ocr(content: bytes, profile: OcrProfile) -> PdfOcrResult:
    pages = preprocess_pages(render_pdf_pages(content, zoom=profile.zoom))

    tesseract_text = _tesseract_from_pages(pages)

    if not profile.dual_ocr:
        return PdfOcrResult(
            tesseract_text=tesseract_text,
            paddle_text=None,
            profile=profile,
        )

    try:
        paddle_text = _paddle_from_pages(pages)
        return PdfOcrResult(
            tesseract_text=tesseract_text,
            paddle_text=paddle_text,
            profile=profile,
        )
    except PaddleOcrNotAvailableError:
        raise
    except Exception as exc:
        return PdfOcrResult(
            tesseract_text=tesseract_text,
            paddle_text=tesseract_text,
            paddle_fallback=True,
            paddle_error=str(exc),
            profile=profile,
        )


def pdf_dual_ocr(content: bytes, profile: OcrProfile) -> PdfOcrResult:
    """Обратная совместимость: OCR с явным профилем."""
    return pdf_ocr(content, profile)
