import os
from pathlib import Path

from dotenv import load_dotenv

from app.services.tesseract_setup import configure_tesseract

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "rus+eng")

PDF_RENDER_ZOOM = float(os.getenv("PDF_RENDER_ZOOM", "2.0"))
PADDLEOCR_LANG = os.getenv("PADDLEOCR_LANG", "ru")

# PaddlePaddle не работает с кириллицей в пути — задаём ASCII-путь для моделей
_default_paddle_home = "C:/paddleocr_cache"
PADDLEOCR_HOME = os.getenv("PADDLEOCR_HOME", _default_paddle_home)
os.environ.setdefault("PADDLEOCR_HOME", PADDLEOCR_HOME)

# Автопоиск tesseract.exe при старте (PATH или стандартные пути Windows)
TESSERACT_PATH = configure_tesseract(TESSERACT_CMD)
