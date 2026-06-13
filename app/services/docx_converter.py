import os
import tempfile

import docx2txt


def docx_to_text(content: bytes) -> str:
    """Конвертирует DOCX в plain text через docx2txt."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        text = docx2txt.process(tmp_path) or ""
        return text.strip()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
