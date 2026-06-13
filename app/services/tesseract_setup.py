import os
import shutil
from pathlib import Path

import pytesseract

WINDOWS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Tesseract-OCR\tesseract.exe",
]


def find_tesseract() -> str | None:
    """Ищет tesseract.exe: сначала в PATH, затем в типичных путях Windows."""
    from_path = shutil.which("tesseract")
    if from_path and Path(from_path).is_file():
        return from_path

    for candidate in WINDOWS_CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    return None


def configure_tesseract(explicit_cmd: str = "") -> str | None:
    """Настраивает pytesseract и возвращает путь к исполняемому файлу."""
    cmd = explicit_cmd.strip() or find_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd


def check_tesseract() -> dict:
    """Проверяет доступность Tesseract OCR."""
    configured = getattr(pytesseract.pytesseract, "tesseract_cmd", "") or ""
    cmd = configured if configured and Path(configured).is_file() else find_tesseract()
    if not cmd:
        return {
            "available": False,
            "path": None,
            "error": (
                "Tesseract OCR не найден. Установите его и укажите путь в .env:\n"
                "TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe\n\n"
                "Скачать: https://github.com/UB-Mannheim/tesseract/wiki"
            ),
        }

    if not Path(cmd).is_file():
        return {
            "available": False,
            "path": cmd,
            "error": f"Файл Tesseract не найден по пути: {cmd}",
        }

    try:
        version = pytesseract.get_tesseract_version()
        return {
            "available": True,
            "path": cmd,
            "version": str(version),
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "path": cmd,
            "error": str(exc),
        }
