import os
import tempfile

import pytesseract

from app.config import PDF_RENDER_ZOOM, TESSERACT_LANG, TESSERACT_OEM, TESSERACT_PSM
from app.services.leptonica_preprocess import preprocess_pages
from app.services.pdf_images import PageImage, render_pdf_pages
from app.services.tesseract_setup import check_tesseract


class TesseractNotFoundError(Exception):
    """Tesseract OCR не установлен или недоступен."""


def _tesseract_config() -> str:
    return (
        f"--oem {TESSERACT_OEM} --psm {TESSERACT_PSM} "
        "-c preserve_interword_spaces=1"
    )


def _tesseract_from_pages(pages: list[PageImage]) -> str:
    tesseract_status = check_tesseract(TESSERACT_LANG)
    if not tesseract_status["available"]:
        raise TesseractNotFoundError(tesseract_status["error"])

    pages_text: list[str] = []
    for page in pages:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(page.png_bytes)
                tmp_path = tmp.name
            page_text = pytesseract.image_to_string(
                tmp_path,
                lang=TESSERACT_LANG,
                config=_tesseract_config(),
            )
            pages_text.append(page_text.strip())
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return "\n\n".join(pages_text).strip()


def pdf_to_text_tesseract(content: bytes) -> str:
    """Распознаёт текст из PDF через Tesseract OCR."""
    pages = preprocess_pages(render_pdf_pages(content, zoom=PDF_RENDER_ZOOM))
    return _tesseract_from_pages(pages)


def pdf_to_text(content: bytes) -> str:
    """Обратная совместимость: OCR через Tesseract."""
    return pdf_to_text_tesseract(content)
