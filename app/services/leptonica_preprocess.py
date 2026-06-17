"""Предобработка изображений через Leptonica (ctypes) перед OCR."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from ctypes import POINTER, c_float, c_int, c_size_t, c_ubyte, c_void_p
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import (
    LEPTONICA_BINARIZE,
    LEPTONICA_DENOISE,
    LEPTONICA_DESKEW,
    LEPTONICA_MEDIAN_KERNEL,
    LEPTONICA_OTSU_TILE,
    LEPTONICA_PREPROCESS,
    TESSERACT_PATH,
)
from app.services.pdf_images import PageImage

logger = logging.getLogger(__name__)

PIX = c_void_p
_lept = None
_lept_error: str | None = None
_warned_unavailable = False


@dataclass(frozen=True)
class LeptonicaStatus:
    available: bool
    library: str | None = None
    error: str | None = None
    preprocess_enabled: bool = False


@dataclass(frozen=True)
class PreprocessOptions:
    deskew: bool = LEPTONICA_DESKEW
    denoise: bool = LEPTONICA_DENOISE
    binarize: bool = LEPTONICA_BINARIZE
    median_kernel: int = LEPTONICA_MEDIAN_KERNEL
    otsu_tile: int = LEPTONICA_OTSU_TILE


class _Pix:
    """Обёртка над PIX* с корректным pixDestroy."""

    __slots__ = ("_ptr",)

    def __init__(self, ptr: int | c_void_p | None = None) -> None:
        if isinstance(ptr, c_void_p):
            self._ptr = ptr
        elif ptr:
            self._ptr = c_void_p(ptr)
        else:
            self._ptr = c_void_p()

    @property
    def ptr(self) -> c_void_p:
        return self._ptr

    @property
    def valid(self) -> bool:
        return bool(self._ptr)

    def release(self) -> None:
        if self._ptr and _lept is not None:
            _lept.pixDestroy(ctypes.byref(self._ptr))

    def replace(self, new_ptr: int | c_void_p | None) -> None:
        self.release()
        if isinstance(new_ptr, c_void_p):
            self._ptr = new_ptr
        elif new_ptr:
            self._ptr = c_void_p(new_ptr)
        else:
            self._ptr = c_void_p()

    def __del__(self) -> None:
        self.release()


def _library_candidates() -> list[str]:
    names: list[str] = []
    if sys.platform == "win32":
        if TESSERACT_PATH:
            tess_dir = Path(TESSERACT_PATH).parent
            for dll_name in (
                "leptonica-1.82.0.dll",
                "leptonica-1.83.0.dll",
                "leptonica-1.84.0.dll",
                "liblept-5.dll",
            ):
                names.append(str(tess_dir / dll_name))
        names.extend(
            [
                "leptonica-1.82.0.dll",
                "leptonica-1.83.0.dll",
                "liblept-5.dll",
            ]
        )
    else:
        names.extend(["liblept.so.5", "liblept.so", "leptonica"])
    return names


def _bind_functions(lib: ctypes.CDLL) -> None:
    def bind(name: str, argtypes: list, restype) -> None:
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype

    bind("pixReadMem", [POINTER(c_ubyte), c_size_t], PIX)
    bind("pixDestroy", [POINTER(PIX)], None)
    bind("pixConvertRGBToGray", [PIX, c_float, c_float, c_float], PIX)
    bind("pixConvertTo8", [PIX, c_int], PIX)
    bind("pixDeskew", [PIX, c_int], PIX)
    bind(
        "pixOtsuAdaptiveThreshold",
        [PIX, c_int, c_int, c_int, c_int, c_float, POINTER(PIX), POINTER(PIX)],
        c_int,
    )
    bind("pixMedianFilter", [PIX, c_int, c_int], PIX)
    bind("pixConvert8To32", [PIX], PIX)
    bind(
        "pixWriteMemPng",
        [POINTER(POINTER(c_ubyte)), POINTER(c_size_t), PIX, c_float],
        c_int,
    )
    bind("pixGetDimensions", [PIX, POINTER(c_int), POINTER(c_int), POINTER(c_int)], c_int)
    bind("pixGetWpl", [PIX], c_int)
    bind("pixGetData", [PIX], POINTER(c_ubyte))
    bind("pixGetDepth", [PIX], c_int)


def _load_leptonica() -> ctypes.CDLL | None:
    global _lept, _lept_error
    if _lept is not None:
        return _lept
    if _lept_error is not None:
        return None

    last_error = "библиотека Leptonica не найдена"
    for name in _library_candidates():
        paths = [name]
        if "/" not in name and "\\" not in name:
            found = ctypes.util.find_library(name)
            if found:
                paths.insert(0, found)
        for path in paths:
            try:
                lib = ctypes.CDLL(path)
                _bind_functions(lib)
                _lept = lib
                _lept_error = None
                logger.debug("Leptonica loaded from %s", path)
                return lib
            except OSError as exc:
                last_error = str(exc)

    _lept_error = last_error
    return None


def _resolved_library_path() -> str | None:
    for name in _library_candidates():
        if "/" in name or "\\" in name:
            if Path(name).exists():
                return name
        found = ctypes.util.find_library(name)
        if found:
            return found
    return None


def check_leptonica() -> LeptonicaStatus:
    lib = _load_leptonica()
    if lib is None:
        return LeptonicaStatus(
            available=False,
            error=_lept_error,
            preprocess_enabled=LEPTONICA_PREPROCESS,
        )
    return LeptonicaStatus(
        available=True,
        library=_resolved_library_path(),
        preprocess_enabled=LEPTONICA_PREPROCESS,
    )


def _warn_once(message: str) -> None:
    global _warned_unavailable
    if _warned_unavailable:
        return
    _warned_unavailable = True
    logger.warning(message)


def _replace_pix(pix: _Pix, new_ptr: int | c_void_p | None) -> None:
    if new_ptr and pix.ptr.value != (new_ptr.value if isinstance(new_ptr, c_void_p) else new_ptr):
        pix.replace(new_ptr)


def _pix_from_png(png_bytes: bytes) -> _Pix:
    buf = (c_ubyte * len(png_bytes)).from_buffer_copy(png_bytes)
    ptr = _lept.pixReadMem(buf, len(png_bytes))
    if not ptr:
        raise RuntimeError("Leptonica: не удалось прочитать PNG")
    return _Pix(ptr)


def _to_grayscale(pix: _Pix) -> None:
    depth = _lept.pixGetDepth(pix.ptr)
    if depth == 8:
        return
    if depth == 32:
        gray = _lept.pixConvertRGBToGray(pix.ptr, 0.0, 0.0, 0.0)
        if not gray:
            raise RuntimeError("Leptonica: pixConvertRGBToGray failed")
        _replace_pix(pix, gray)
        return
    gray8 = _lept.pixConvertTo8(pix.ptr, 0)
    if not gray8:
        raise RuntimeError("Leptonica: pixConvertTo8 failed")
    _replace_pix(pix, gray8)


def _deskew(pix: _Pix) -> None:
    deskewed = _lept.pixDeskew(pix.ptr, 0)
    if not deskewed:
        raise RuntimeError("Leptonica: pixDeskew failed")
    _replace_pix(pix, deskewed)


def _denoise(pix: _Pix, kernel: int) -> None:
    k = max(1, kernel)
    filtered = _lept.pixMedianFilter(pix.ptr, k, k)
    if not filtered:
        raise RuntimeError("Leptonica: pixMedianFilter failed")
    _replace_pix(pix, filtered)


def _binarize(pix: _Pix, tile: int) -> None:
    tile_size = max(8, tile)
    threshold = PIX()
    binary = PIX()
    rc = _lept.pixOtsuAdaptiveThreshold(
        pix.ptr,
        tile_size,
        tile_size,
        0,
        0,
        0.0,
        ctypes.byref(threshold),
        ctypes.byref(binary),
    )
    if threshold:
        _Pix(threshold).release()
    if rc != 0 or not binary:
        raise RuntimeError(f"Leptonica: pixOtsuAdaptiveThreshold failed ({rc})")
    _replace_pix(pix, binary)
    gray8 = _lept.pixConvertTo8(pix.ptr, 0)
    if not gray8:
        raise RuntimeError("Leptonica: pixConvertTo8 failed")
    _replace_pix(pix, gray8)


def _pix8_to_outputs(pix8: _Pix) -> tuple[np.ndarray, bytes]:
    rgb32 = _lept.pixConvert8To32(pix8.ptr)
    if not rgb32:
        raise RuntimeError("Leptonica: pixConvert8To32 failed")
    rgb_holder = _Pix(rgb32)
    try:
        width = c_int()
        height = c_int()
        depth = c_int()
        if _lept.pixGetDimensions(
            rgb_holder.ptr, ctypes.byref(width), ctypes.byref(height), ctypes.byref(depth)
        ) != 0:
            raise RuntimeError("Leptonica: pixGetDimensions failed")
        words_per_line = _lept.pixGetWpl(rgb_holder.ptr)
        data = _lept.pixGetData(rgb_holder.ptr)
        if not data:
            raise RuntimeError("Leptonica: pixGetData failed")
        bytes_per_line = words_per_line * 4
        raw = np.ctypeslib.as_array(data, shape=(height.value, bytes_per_line))
        rgb = raw[:, : width.value * 4].reshape(height.value, width.value, 4)[:, :, :3].copy()

        out_ptr = POINTER(c_ubyte)()
        out_size = c_size_t()
        if _lept.pixWriteMemPng(ctypes.byref(out_ptr), ctypes.byref(out_size), pix8.ptr, 0.0) != 0:
            raise RuntimeError("Leptonica: pixWriteMemPng failed")
        png_bytes = bytes(ctypes.string_at(out_ptr, out_size.value))
        return rgb, png_bytes
    finally:
        rgb_holder.release()


def preprocess_page_image(
    page: PageImage,
    options: PreprocessOptions | None = None,
) -> PageImage:
    """Предобработка одной страницы: grayscale → deskew → denoise → binarize."""
    opts = options or PreprocessOptions()
    if _load_leptonica() is None:
        raise RuntimeError(_lept_error or "Leptonica недоступна")

    pix = _pix_from_png(page.png_bytes)
    try:
        _to_grayscale(pix)
        if opts.deskew:
            _deskew(pix)
        if opts.denoise:
            _denoise(pix, opts.median_kernel)
        if opts.binarize:
            _binarize(pix, opts.otsu_tile)
        rgb, png_bytes = _pix8_to_outputs(pix)
        return PageImage(rgb=rgb, png_bytes=png_bytes)
    finally:
        pix.release()


def preprocess_pages(
    pages: list[PageImage],
    options: PreprocessOptions | None = None,
) -> list[PageImage]:
    """Предобработка страниц перед OCR. При недоступной Leptonica возвращает исходные."""
    if not pages or not LEPTONICA_PREPROCESS:
        return pages

    status = check_leptonica()
    if not status.available:
        _warn_once(
            "Leptonica недоступна, OCR без предобработки изображений. "
            f"Причина: {status.error}. "
            "Linux: sudo apt install libleptonica-dev"
        )
        return pages

    processed: list[PageImage] = []
    opts = options or PreprocessOptions()
    for index, page in enumerate(pages):
        try:
            processed.append(preprocess_page_image(page, opts))
        except Exception as exc:
            logger.warning(
                "Leptonica: страница %s не обработана (%s), используется оригинал",
                index + 1,
                exc,
            )
            processed.append(page)
    return processed
