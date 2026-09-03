import base64
import io
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

# Константы Word (без импорта win32.constants для совместимости)
_WD_STATISTIC_PAGES = 2
_WD_GOTO_PAGE = 1
_WD_GOTO_ABSOLUTE = 1


class PdfConversionError(Exception):
    pass


class DocxConversionError(Exception):
    pass


@dataclass
class PreparedFile:
    filename: str
    format: str
    text: str | None
    images: list[bytes]
    text_pages: list[str] = field(default_factory=list)


@dataclass
class ChunkPart:
    """Один фрагмент документа: текст или изображение страницы."""

    content_type: str  # "text" | "image"
    content: str  # текст или base64 PNG
    filename: str
    format: str


def detect_format(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    raise ValueError(f"Неподдерживаемый формат: {ext or '(нет расширения)'}")


def _require_win32():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise DocxConversionError(
            "Не установлен pywin32. Выполните: pip install pywin32"
        ) from exc
    return pythoncom, win32com.client


def _get_docx_page_range(word, doc, page: int, total_pages: int):
    """Возвращает Range одной страницы документа Word."""
    word.Selection.GoTo(What=_WD_GOTO_PAGE, Which=_WD_GOTO_ABSOLUTE, Count=page)
    start = word.Selection.Start

    if page < total_pages:
        word.Selection.GoTo(What=_WD_GOTO_PAGE, Which=_WD_GOTO_ABSOLUTE, Count=page + 1)
        end = word.Selection.Start
    else:
        end = doc.Content.End

    return doc.Range(Start=start, End=end)


def docx_to_page_texts(content: bytes) -> list[str]:
    """
    Извлекает текст DOCX постранично через Microsoft Word (как в split_docx_by_pages).
    Требует Windows + установленный Word + pywin32.
    """
    pythoncom, win32 = _require_win32()

    tmp_path: Path | None = None
    word = None
    com_initialized = False

    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name).resolve()

        pythoncom.CoInitialize()
        com_initialized = True

        word = win32.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        doc = word.Documents.Open(str(tmp_path), ReadOnly=True)
        try:
            total_pages = int(doc.ComputeStatistics(_WD_STATISTIC_PAGES))
            if total_pages < 1:
                raise DocxConversionError(
                    "Word не смог определить количество страниц в документе."
                )

            pages: list[str] = []
            for page in range(1, total_pages + 1):
                page_range = _get_docx_page_range(word, doc, page, total_pages)
                text = (page_range.Text or "").replace("\r", "\n").strip()
                pages.append(text)

            logger.info(
                "DOCX разбит по страницам Word: pages=%s file=%s",
                len(pages),
                tmp_path.name,
            )
            return pages or [""]
        finally:
            doc.Close(SaveChanges=False)
    except DocxConversionError:
        raise
    except Exception as exc:
        raise DocxConversionError(
            f"Не удалось разбить DOCX по страницам через Word: {exc}"
        ) from exc
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                logger.exception("Не удалось закрыть Word.Application")
        if com_initialized:
            pythoncom.CoUninitialize()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _resize_png(image_bytes: bytes) -> bytes:
    max_width = settings.image_max_width
    if max_width <= 0:
        return image_bytes

    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.width <= max_width:
            return image_bytes
        ratio = max_width / img.width
        new_size = (max_width, max(1, int(img.height * ratio)))
        resized = img.resize(new_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        resized.save(out, format="PNG", optimize=True)
        return out.getvalue()


def pdf_to_images(content: bytes, dpi: int | None = None) -> list[bytes]:
    """Конвертирует PDF в PNG-изображения страниц (PyMuPDF, без Poppler)."""
    dpi = dpi or settings.pdf_render_dpi
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise PdfConversionError(f"Не удалось открыть PDF: {exc}") from exc

    images: list[bytes] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    try:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            images.append(_resize_png(pix.tobytes("png")))
    except Exception as exc:
        raise PdfConversionError(f"Не удалось конвертировать PDF в изображения: {exc}") from exc
    finally:
        doc.close()

    if not images:
        raise PdfConversionError("PDF не содержит страниц")

    return images


def prepare_file(content: bytes, filename: str) -> PreparedFile:
    file_format = detect_format(filename)
    if file_format == "docx":
        pages = docx_to_page_texts(content)
        return PreparedFile(
            filename=filename,
            format=file_format,
            text="\n\n".join(pages),
            images=[],
            text_pages=pages,
        )
    return PreparedFile(
        filename=filename,
        format=file_format,
        text=None,
        images=pdf_to_images(content),
    )


def file_to_chunks(prepared: PreparedFile) -> list[ChunkPart]:
    """DOCX → по одному текстовому чанку на страницу Word; PDF → PNG на страницу."""
    if prepared.format == "docx":
        pages = prepared.text_pages or [prepared.text or ""]
        return [
            ChunkPart(
                content_type="text",
                content=page,
                filename=prepared.filename,
                format=prepared.format,
            )
            for page in pages
        ]

    return [
        ChunkPart(
            content_type="image",
            content=base64.b64encode(image_bytes).decode("ascii"),
            filename=prepared.filename,
            format=prepared.format,
        )
        for image_bytes in prepared.images
    ]
