from dataclasses import dataclass

from app.services.paddle_converter import PaddleOcrNotAvailableError, _paddle_from_pages
from app.services.pdf_converter import _tesseract_from_pages
from app.services.pdf_images import render_pdf_pages


@dataclass
class PdfDualOcrResult:
    tesseract_text: str
    paddle_text: str
    paddle_fallback: bool = False
    paddle_error: str | None = None


def pdf_dual_ocr(content: bytes) -> PdfDualOcrResult:
    pages = render_pdf_pages(content)
    tesseract_text = _tesseract_from_pages(pages)

    try:
        paddle_text = _paddle_from_pages(pages)
        return PdfDualOcrResult(
            tesseract_text=tesseract_text,
            paddle_text=paddle_text,
            paddle_fallback=False,
        )
    except PaddleOcrNotAvailableError:
        raise
    except Exception as exc:
        return PdfDualOcrResult(
            tesseract_text=tesseract_text,
            paddle_text=tesseract_text,
            paddle_fallback=True,
            paddle_error=str(exc),
        )
