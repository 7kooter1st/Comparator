from enum import Enum
from pathlib import Path

from app.services.docx_converter import docx_to_text
from app.services.pdf_converter import TesseractNotFoundError, pdf_to_text


class FileFormat(str, Enum):
    DOCX = "docx"
    PDF = "pdf"


class UnsupportedFormatError(ValueError):
    """Неподдерживаемый формат файла."""


def detect_format(filename: str, content: bytes) -> FileFormat:
    """Определяет формат файла по содержимому и расширению."""
    if not content:
        raise UnsupportedFormatError("Файл пуст")

    if content.startswith(b"%PDF"):
        return FileFormat.PDF

    if content[:2] == b"PK":
        return FileFormat.DOCX

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        return FileFormat.PDF
    if suffix == ".docx":
        return FileFormat.DOCX

    raise UnsupportedFormatError(
        f"Не удалось определить формат файла '{filename}'. "
        "Поддерживаются только .docx и .pdf"
    )


def file_to_text(filename: str, content: bytes) -> tuple[FileFormat, str]:
    """Определяет формат и извлекает текст из файла."""
    fmt = detect_format(filename, content)

    if fmt == FileFormat.DOCX:
        return fmt, docx_to_text(content)
    return fmt, pdf_to_text(content)
