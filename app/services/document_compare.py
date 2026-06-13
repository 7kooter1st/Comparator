from dataclasses import dataclass

from app.services.comparator import compare_documents, compare_reference_with_dual_ocr
from app.services.docx_converter import docx_to_text
from app.services.file_converter import FileFormat, detect_format
from app.services.pdf_ocr import pdf_dual_ocr


@dataclass
class ExtractedFile:
    filename: str
    format: FileFormat
    text: str
    tesseract_text: str | None = None
    paddle_text: str | None = None
    paddle_fallback: bool = False
    paddle_error: str | None = None


def _extract_file(filename: str, content: bytes) -> ExtractedFile:
    fmt = detect_format(filename, content)

    if fmt == FileFormat.DOCX:
        text = docx_to_text(content)
        return ExtractedFile(filename=filename, format=fmt, text=text)

    dual = pdf_dual_ocr(content)
    return ExtractedFile(
        filename=filename,
        format=fmt,
        text=dual.tesseract_text,
        tesseract_text=dual.tesseract_text,
        paddle_text=dual.paddle_text,
        paddle_fallback=dual.paddle_fallback,
        paddle_error=dual.paddle_error,
    )


def _file_payload(file: ExtractedFile) -> dict:
    payload = {
        "filename": file.filename,
        "format": file.format.value,
        "text": file.text,
    }
    if file.format == FileFormat.PDF:
        payload["tesseract_text"] = file.tesseract_text
        payload["paddle_text"] = file.paddle_text
        payload["paddle_fallback"] = file.paddle_fallback
        payload["paddle_error"] = file.paddle_error
    return payload


def compare_uploaded_files(
    filename1: str,
    content1: bytes,
    filename2: str,
    content2: bytes,
) -> dict:
    file1 = _extract_file(filename1, content1)
    file2 = _extract_file(filename2, content2)

    docx_file: ExtractedFile | None = None
    pdf_file: ExtractedFile | None = None

    if file1.format == FileFormat.DOCX and file2.format == FileFormat.PDF:
        docx_file, pdf_file = file1, file2
    elif file2.format == FileFormat.DOCX and file1.format == FileFormat.PDF:
        docx_file, pdf_file = file2, file1

    if docx_file and pdf_file and pdf_file.tesseract_text and pdf_file.paddle_text:
        if pdf_file.paddle_fallback:
            result = compare_documents(docx_file.text, pdf_file.tesseract_text)
            message = (
                f"PaddleOCR недоступен ({pdf_file.paddle_error}). "
                "Сравнение только через Tesseract."
            )
            ocr_engine = "tesseract"
        else:
            result = compare_reference_with_dual_ocr(
                reference_text=docx_file.text,
                ocr_tesseract=pdf_file.tesseract_text,
                ocr_paddle=pdf_file.paddle_text,
            )
            ocr_engine = "dual_confirmed"
            if result["differences"]:
                message = (
                    "Найдены расхождения, подтверждённые обеими OCR-моделями "
                    "(Tesseract и PaddleOCR)."
                )
            else:
                message = (
                    "Документы совпадают. На каждой позиции хотя бы одна "
                    "OCR-модель подтвердила эталонный текст."
                )

        response = {
            "file1": _file_payload(file1),
            "file2": _file_payload(file2),
            "content_identical": result["content_identical"],
            "similarity_percent": result["similarity_percent"],
            "normalized_file1_length": result["normalized_file1_length"],
            "normalized_file2_length": result["normalized_file2_length"],
            "diff_summary": result["diff_summary"],
            "differences": result["differences"],
            "ocr_engine_used": ocr_engine,
            "comparison_mode": "reference_docx_vs_dual_ocr",
            "message": message,
        }
        if "ocr_filter_stats" in result:
            response["ocr_filter_stats"] = result["ocr_filter_stats"]
        return response

    result = compare_documents(file1.text, file2.text)
    return {
        "file1": _file_payload(file1),
        "file2": _file_payload(file2),
        "content_identical": result["content_identical"],
        "similarity_percent": result["similarity_percent"],
        "normalized_file1_length": result["normalized_file1_length"],
        "normalized_file2_length": result["normalized_file2_length"],
        "diff_summary": result["diff_summary"],
        "differences": result["differences"],
        "ocr_engine_used": "direct",
        "comparison_mode": "direct",
    }
