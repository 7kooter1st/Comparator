import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app.config import PADDLEOCR_HOME, PADDLEOCR_LANG, PADDLEOCR_USE_ANGLE_CLS, PADDLEOCR_USE_GPU
from app.services.ocr_profiles import resolve_paddle_use_gpu
from app.services.pdf_images import PageImage

_paddle_ocr_cache: dict[tuple[bool, bool, str], object] = {}
_paddle_lock = threading.Lock()


class PaddleOcrNotAvailableError(Exception):
    """PaddleOCR недоступен."""


def _ensure_paddleocr_home() -> None:
    import os

    home = PADDLEOCR_HOME.rstrip("/\\") + os.sep
    os.makedirs(home, exist_ok=True)
    os.environ["PADDLEOCR_HOME"] = home

    import paddleocr.paddleocr as paddleocr_module

    paddleocr_module.BASE_DIR = home


def get_paddle_ocr(*, use_gpu: bool):
    cache_key = (use_gpu, PADDLEOCR_USE_ANGLE_CLS, PADDLEOCR_LANG)
    if cache_key in _paddle_ocr_cache:
        return _paddle_ocr_cache[cache_key]

    try:
        import paddle  # noqa: F401
    except ImportError as exc:
        raise PaddleOcrNotAvailableError(
            "paddlepaddle не установлен. Нужен Python 3.10–3.12."
        ) from exc

    _ensure_paddleocr_home()

    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise PaddleOcrNotAvailableError(
            "PaddleOCR не установлен: pip install -r requirements-paddle.txt"
        ) from exc

    instance = PaddleOCR(
        use_angle_cls=PADDLEOCR_USE_ANGLE_CLS,
        lang=PADDLEOCR_LANG,
        show_log=False,
        use_gpu=use_gpu,
    )
    _paddle_ocr_cache[cache_key] = instance
    return instance


def _unwrap_page_detections(ocr_result) -> list:
    if not ocr_result:
        return []
    if len(ocr_result) == 1:
        page = ocr_result[0]
        if page is None:
            return []
        if isinstance(page, list) and page and len(page[0]) == 2:
            return page
    if ocr_result and len(ocr_result[0]) == 2:
        return ocr_result
    return []


def _box_top_left(box) -> tuple[float, float]:
    if box is None:
        return 0.0, 0.0
    try:
        arr = np.asarray(box, dtype=float)
        if arr.ndim == 2 and arr.shape[1] >= 2:
            return float(arr[:, 1].min()), float(arr[:, 0].min())
    except (TypeError, ValueError):
        pass
    tops, lefts = [], []
    for point in box:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            lefts.append(float(point[0]))
            tops.append(float(point[1]))
    if tops:
        return min(tops), min(lefts)
    return 0.0, 0.0


def _detection_text(detection) -> str:
    if not detection or len(detection) < 2:
        return ""
    text_info = detection[1]
    if isinstance(text_info, (tuple, list)) and text_info:
        return str(text_info[0]).strip()
    return str(text_info).strip()


def _lines_from_page_result(ocr_result) -> list[list[str]]:
    detections = _unwrap_page_detections(ocr_result)
    entries: list[tuple[float, float, str]] = []

    for detection in detections:
        text = _detection_text(detection)
        if not text:
            continue
        top, left = _box_top_left(detection[0])
        entries.append((top, left, text))

    entries.sort(key=lambda item: (round(item[0] / 15), item[1]))

    lines: list[list[str]] = []
    current_line: list[str] = []
    current_row: int | None = None

    for top, _left, text in entries:
        row = round(top / 15)
        if current_row is None:
            current_row = row
        if row != current_row and current_line:
            lines.append(current_line)
            current_line = []
            current_row = row
        current_line.append(text)

    if current_line:
        lines.append(current_line)

    return lines


def _paddle_page_lines(page: PageImage, ocr) -> list[list[str]]:
    with _paddle_lock:
        result = ocr.ocr(page.rgb, cls=PADDLEOCR_USE_ANGLE_CLS)
    return _lines_from_page_result(result)


def _paddle_from_pages(
    pages: list[PageImage],
    *,
    page_workers: int,
    use_gpu: bool,
) -> str:
    if not pages:
        return ""

    ocr = get_paddle_ocr(use_gpu=use_gpu)
    workers = 1 if use_gpu else min(page_workers, len(pages))

    if workers <= 1:
        all_lines: list[list[str]] = []
        for page in pages:
            all_lines.extend(_paddle_page_lines(page, ocr))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            page_results = list(
                executor.map(
                    lambda page: _paddle_page_lines(page, ocr),
                    pages,
                )
            )
        all_lines = [line for page_lines in page_results for line in page_lines]

    return "\n".join(" ".join(line) for line in all_lines).strip()


def paddle_gpu_enabled() -> bool:
    return resolve_paddle_use_gpu(PADDLEOCR_USE_GPU)
