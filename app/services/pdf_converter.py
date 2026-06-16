from concurrent.futures import ThreadPoolExecutor

from PIL import Image
import pytesseract

from app.config import TESSERACT_LANG, TESSERACT_OEM, TESSERACT_PSM
from app.services.pdf_images import PageImage
from app.services.tesseract_setup import check_tesseract


class TesseractNotFoundError(Exception):
    """Tesseract OCR не установлен или недоступен."""


def _tesseract_config() -> str:
    return (
        f"--oem {TESSERACT_OEM} --psm {TESSERACT_PSM} "
        "-c preserve_interword_spaces=1"
    )


def _tesseract_page(page: PageImage) -> str:
    image = Image.fromarray(page.rgb)
    return pytesseract.image_to_string(
        image,
        lang=TESSERACT_LANG,
        config=_tesseract_config(),
    ).strip()


def _tesseract_from_pages(pages: list[PageImage], *, page_workers: int) -> str:
    tesseract_status = check_tesseract(TESSERACT_LANG)
    if not tesseract_status["available"]:
        raise TesseractNotFoundError(tesseract_status["error"])

    if not pages:
        return ""

    workers = min(page_workers, len(pages))
    if workers <= 1:
        pages_text = [_tesseract_page(page) for page in pages]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pages_text = list(executor.map(_tesseract_page, pages))

    return "\n\n".join(pages_text).strip()


def pdf_to_text_tesseract(content: bytes, *, zoom: float, page_workers: int) -> str:
    """Распознаёт текст из PDF через Tesseract OCR."""
    from app.services.pdf_images import render_pdf_pages

    return _tesseract_from_pages(
        render_pdf_pages(content, zoom=zoom),
        page_workers=page_workers,
    )


def pdf_to_text(content: bytes) -> str:
    """Обратная совместимость: OCR через Tesseract."""
    from app.config import OCR_PAGE_WORKERS, PDF_RENDER_ZOOM_ACCURATE

    return pdf_to_text_tesseract(
        content,
        zoom=PDF_RENDER_ZOOM_ACCURATE,
        page_workers=OCR_PAGE_WORKERS,
    )
