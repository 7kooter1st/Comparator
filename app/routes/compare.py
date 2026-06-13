from flask import Blueprint, jsonify, request

from app.config import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB
from app.services.document_compare import compare_uploaded_files
from app.services.file_converter import UnsupportedFormatError
from app.services.pdf_converter import TesseractNotFoundError
from app.services.paddle_converter import PaddleOcrNotAvailableError
from app.services.tesseract_setup import check_tesseract

compare_bp = Blueprint("compare", __name__)


def _read_upload(file, field_name: str) -> tuple[bytes | None, str | None, tuple | None]:
    if file is None or file.filename == "":
        return None, None, (
            jsonify({"error": f"Поле '{field_name}' обязательно"}),
            400,
        )

    content = file.read()
    if not content:
        return None, file.filename, (
            jsonify({"error": f"Файл '{file.filename}' пуст"}),
            400,
        )

    if len(content) > MAX_FILE_SIZE_BYTES:
        return None, file.filename, (
            jsonify(
                {
                    "error": (
                        f"Файл '{file.filename}' превышает лимит "
                        f"{MAX_FILE_SIZE_MB} МБ"
                    )
                }
            ),
            413,
        )

    return content, file.filename, None


def _resolve_upload_pair():
    file1 = request.files.get("file1") or request.files.get("docx")
    file2 = request.files.get("file2") or request.files.get("pdf")

    field1 = "file1" if request.files.get("file1") else "docx"
    field2 = "file2" if request.files.get("file2") else "pdf"

    if file1 is None and file2 is None:
        return None, (
            jsonify(
                {
                    "error": (
                        "Нужно передать два файла: file1 и file2 "
                        "(или docx и pdf для обратной совместимости)"
                    )
                }
            ),
            400,
        )

    return (file1, field1, file2, field2), None


@compare_bp.route("/health", methods=["GET"])
def health():
    tesseract = check_tesseract()
    paddle_available = True
    paddle_error = None
    try:
        import paddleocr  # noqa: F401
    except ImportError as exc:
        paddle_available = False
        paddle_error = str(exc)

    return jsonify(
        {
            "status": "ok" if tesseract["available"] and paddle_available else "degraded",
            "service": "PDF & DOCX Comparator",
            "tesseract": tesseract,
            "paddleocr": {
                "available": paddle_available,
                "error": paddle_error,
            },
        }
    )


@compare_bp.route("/compare", methods=["POST"])
def compare():
    """Принимает два файла (DOCX и/или PDF), определяет формат и сравнивает."""
    pair, pair_error = _resolve_upload_pair()
    if pair_error:
        return pair_error

    file1, field1, file2, field2 = pair

    content1, filename1, error1 = _read_upload(file1, field1)
    if error1:
        return error1

    content2, filename2, error2 = _read_upload(file2, field2)
    if error2:
        return error2

    try:
        result = compare_uploaded_files(filename1, content1, filename2, content2)
    except UnsupportedFormatError as exc:
        return jsonify({"error": str(exc)}), 400
    except TesseractNotFoundError as exc:
        return jsonify(
            {
                "error": str(exc),
                "hint": (
                    "Установите Tesseract OCR для Windows: "
                    "https://github.com/UB-Mannheim/tesseract/wiki "
                    "После установки добавьте путь в .env: "
                    "TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
                ),
            }
        ), 503
    except PaddleOcrNotAvailableError as exc:
        return jsonify(
            {
                "error": str(exc),
                "hint": "Установите зависимости: pip install paddleocr paddlepaddle",
            }
        ), 503
    except Exception as exc:
        return jsonify({"error": f"Ошибка обработки файлов: {exc}"}), 422

    return jsonify(result), 200
