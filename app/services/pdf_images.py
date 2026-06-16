from dataclasses import dataclass

import fitz
import numpy as np


@dataclass
class PageImage:
    rgb: np.ndarray
    png_bytes: bytes


def render_pdf_pages(content: bytes, *, zoom: float) -> list[PageImage]:
    """Рендерит страницы PDF для OCR (numpy RGB + PNG bytes)."""
    doc = fitz.open(stream=content, filetype="pdf")
    pages: list[PageImage] = []

    try:
        matrix = fitz.Matrix(zoom, zoom)
        for page_index in range(len(doc)):
            page = doc.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix)
            channels = pixmap.n

            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, channels
            )
            if channels == 4:
                image = image[:, :, :3]

            pages.append(
                PageImage(
                    rgb=image.copy(),
                    png_bytes=pixmap.tobytes("png"),
                )
            )
    finally:
        doc.close()

    return pages
