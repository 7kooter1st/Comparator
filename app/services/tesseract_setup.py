import os
import shutil
import subprocess
from pathlib import Path

import pytesseract

WINDOWS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Tesseract-OCR\tesseract.exe",
]

LINUX_TESSDATA_PREFIXES = (
    Path("/usr/share/tesseract-ocr/5"),
    Path("/usr/share/tesseract-ocr/4.00"),
    Path("/usr/share/tesseract-ocr"),
)


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


def _system_tessdata_dir(lang: str) -> Path | None:
    for prefix in LINUX_TESSDATA_PREFIXES:
        tessdata_dir = prefix / "tessdata"
        if (tessdata_dir / f"{lang}.traineddata").is_file():
            return tessdata_dir
    return None


def _list_tesseract_langs(cmd: str) -> set[str]:
    try:
        result = subprocess.run(
            [cmd, "--list-langs"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()

    langs: set[str] = set()
    for line in result.stdout.splitlines():
        token = line.strip()
        if not token or token.startswith("List of available"):
            continue
        if token.startswith("tessdata/"):
            token = token.split("/", 1)[1]
        langs.add(token)
    return langs


def configure_tessdata(base_dir: Path, lang: str) -> Path | None:
    """
    Указывает Tesseract, где искать traineddata.
    Приоритет: TESSDATA_PREFIX из .env → системный tessdata → tessdata/ в проекте.
    """
    if os.getenv("TESSDATA_PREFIX"):
        return Path(os.environ["TESSDATA_PREFIX"])

    system_tessdata = _system_tessdata_dir(lang)
    if system_tessdata is not None:
        os.environ["TESSDATA_PREFIX"] = f"{system_tessdata}{os.sep}"
        return system_tessdata

    local_tessdata = base_dir / "tessdata"
    if (local_tessdata / f"{lang}.traineddata").is_file():
        resolved = local_tessdata.resolve()
        os.environ["TESSDATA_PREFIX"] = f"{resolved}{os.sep}"
        return resolved

    return None


def check_tesseract(lang: str = "rus") -> dict:
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
        langs = _list_tesseract_langs(cmd)
        if lang not in langs:
            local_hint = (
                f"Установите пакет: sudo apt install tesseract-ocr-{lang}\n"
                f"Или положите {lang}.traineddata в tessdata/ проекта."
            )
            return {
                "available": False,
                "path": cmd,
                "version": str(version),
                "error": (
                    f"Язык '{lang}' не найден в Tesseract. Доступны: {', '.join(sorted(langs)) or 'нет'}.\n"
                    f"{local_hint}"
                ),
            }
        return {
            "available": True,
            "path": cmd,
            "version": str(version),
            "languages": sorted(langs),
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "path": cmd,
            "error": str(exc),
        }
