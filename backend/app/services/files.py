import base64
import io
import re
from dataclasses import dataclass
from pathlib import Path

import docx2txt
import fitz
from PIL import Image

from app.config import settings


class PdfConversionError(Exception):
    pass


@dataclass
class PreparedFile:
    filename: str
    format: str
    text: str | None
    images: list[bytes]


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


def docx_to_text(content: bytes) -> str:
    return (docx2txt.process(io.BytesIO(content)) or "").strip()


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


def split_text_to_chunks(text: str, max_chars: int | None = None) -> list[str]:
    """Разбивает текст на логические фрагменты, не превышающие лимит символов."""
    max_chars = max_chars or settings.text_chunk_max_chars
    text = text.strip()
    if not text:
        return [""]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + max_chars].strip())
                start += max_chars
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = paragraph

    if current:
        chunks.append(current.strip())

    return chunks or [""]


def prepare_file(content: bytes, filename: str) -> PreparedFile:
    file_format = detect_format(filename)
    if file_format == "docx":
        return PreparedFile(
            filename=filename,
            format=file_format,
            text=docx_to_text(content),
            images=[],
        )
    return PreparedFile(
        filename=filename,
        format=file_format,
        text=None,
        images=pdf_to_images(content),
    )


def file_to_chunks(prepared: PreparedFile) -> list[ChunkPart]:
    """DOCX → текстовые чанки; PDF → по одному PNG на страницу."""
    if prepared.format == "docx":
        text_chunks = split_text_to_chunks(prepared.text or "")
        return [
            ChunkPart(
                content_type="text",
                content=chunk,
                filename=prepared.filename,
                format=prepared.format,
            )
            for chunk in text_chunks
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
