import os
from pathlib import Path

from dotenv import load_dotenv

from app.services.tesseract_setup import configure_tesseract, configure_tessdata

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "rus")
TESSERACT_PSM = int(os.getenv("TESSERACT_PSM", "6"))
TESSERACT_OEM = int(os.getenv("TESSERACT_OEM", "1"))

PDF_RENDER_ZOOM = float(os.getenv("PDF_RENDER_ZOOM", "3.0"))

# Предобработка изображений через Leptonica перед OCR (Linux: libleptonica-dev)
LEPTONICA_PREPROCESS = os.getenv("LEPTONICA_PREPROCESS", "true").lower() in (
    "1",
    "true",
    "yes",
)
LEPTONICA_DESKEW = os.getenv("LEPTONICA_DESKEW", "true").lower() in ("1", "true", "yes")
LEPTONICA_DENOISE = os.getenv("LEPTONICA_DENOISE", "true").lower() in (
    "1",
    "true",
    "yes",
)
LEPTONICA_BINARIZE = os.getenv("LEPTONICA_BINARIZE", "true").lower() in (
    "1",
    "true",
    "yes",
)
LEPTONICA_MEDIAN_KERNEL = max(1, int(os.getenv("LEPTONICA_MEDIAN_KERNEL", "2")))
LEPTONICA_OTSU_TILE = max(8, int(os.getenv("LEPTONICA_OTSU_TILE", "20")))
PADDLEOCR_LANG = os.getenv("PADDLEOCR_LANG", "ru")
PADDLEOCR_USE_ANGLE_CLS = os.getenv("PADDLEOCR_USE_ANGLE_CLS", "").lower() in (
    "1",
    "true",
    "yes",
)

# PaddlePaddle не работает с кириллицей в пути — задаём ASCII-путь для моделей
_default_paddle_home = "C:/paddleocr_cache"
PADDLEOCR_HOME = os.getenv("PADDLEOCR_HOME", _default_paddle_home)
os.environ.setdefault("PADDLEOCR_HOME", PADDLEOCR_HOME)

# Автопоиск tesseract.exe при старте (PATH или стандартные пути Windows)
TESSERACT_PATH = configure_tesseract(TESSERACT_CMD)
configure_tessdata(BASE_DIR, TESSERACT_LANG)

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

_default_frontend_dist = BASE_DIR / "dist"
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", str(_default_frontend_dist))).resolve()
