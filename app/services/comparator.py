import difflib
from collections import Counter

from app.services.text_normalizer import (
    extract_word_pairs,
    normalize_text,
    normalize_word_for_compare,
    words_equivalent,
)

# OCR часто «ломает» порядок слов из-за вёрстки — ищем совпадения в окне
MAX_LOOKAHEAD = 45
GLOBAL_SEARCH_WINDOW = 120
MIN_TRACKABLE_WORD_LEN = 4


def _words_to_display(words: list[str]) -> str:
    return " ".join(words)


def _ocr_matches_reference(reference_word: str, ocr_word: str | None) -> bool:
    return words_equivalent(reference_word, ocr_word, fuzzy=True)


def _is_trackable_word(word: str) -> bool:
    return len(normalize_word_for_compare(word)) >= MIN_TRACKABLE_WORD_LEN


def _find_match_index(
    word: str,
    candidates: list[str],
    start: int,
    *,
    window: int,
    used: set[int] | None = None,
) -> int | None:
    """Ищет эквивалентное слово в окне (перестановки / сдвиг OCR)."""
    if not word:
        return None

    end = min(len(candidates), start + window)
    search_from = max(0, start - 8)

    for index in range(search_from, end):
        if used is not None and index in used:
            continue
        if _ocr_matches_reference(word, candidates[index]):
            return index
    return None


def _consume_fuzzy_match(word: str, pool: list[str], used: set[int]) -> bool:
    for index, candidate in enumerate(pool):
        if index in used:
            continue
        if _ocr_matches_reference(word, candidate):
            used.add(index)
            return True
    return False


def _multiset_covers(reference_words: list[str], ocr_words: list[str]) -> bool:
    """Все слова эталона есть в OCR (с учётом fuzzy), порядок не важен."""
    if not reference_words:
        return True

    used: set[int] = set()
    for ref_word in reference_words:
        if not _consume_fuzzy_match(ref_word, ocr_words, used):
            return False
    return True


def align_reference_to_ocr(
    reference_text: str, ocr_text: str
) -> tuple[dict[int, str | None], list[str], set[int]]:
    """
    Сопоставляет слова эталона с OCR.
    При сдвige порядка слова ищутся вперёд в окне — лишние слова OCR не считаются ошибкой.
    """
    ref_originals = [orig for orig, _ in extract_word_pairs(reference_text)]
    ocr_originals = [orig for orig, _ in extract_word_pairs(ocr_text)]

    mapping: dict[int, str | None] = {}
    matched_ocr: set[int] = set()
    i_ref = 0
    i_ocr = 0

    while i_ref < len(ref_originals):
        ref_word = ref_originals[i_ref]

        if i_ocr >= len(ocr_originals):
            mapping[i_ref] = None
            i_ref += 1
            continue

        ocr_word = ocr_originals[i_ocr]

        if _ocr_matches_reference(ref_word, ocr_word):
            mapping[i_ref] = ocr_word
            matched_ocr.add(i_ocr)
            i_ref += 1
            i_ocr += 1
            continue

        found_ocr = _find_match_index(
            ref_word,
            ocr_originals,
            i_ocr + 1,
            window=MAX_LOOKAHEAD,
            used=matched_ocr,
        )
        if found_ocr is not None:
            i_ocr = found_ocr
            mapping[i_ref] = ocr_originals[i_ocr]
            matched_ocr.add(i_ocr)
            i_ref += 1
            i_ocr += 1
            continue

        found_ref = None
        for j in range(i_ref + 1, min(i_ref + MAX_LOOKAHEAD + 1, len(ref_originals))):
            if _ocr_matches_reference(ref_originals[j], ocr_word):
                found_ref = j
                break

        if found_ref is not None:
            for k in range(i_ref, found_ref):
                mapping[k] = None
            i_ref = found_ref
            continue

        mapping[i_ref] = ocr_word
        matched_ocr.add(i_ocr)
        i_ref += 1
        i_ocr += 1

    return mapping, ref_originals, matched_ocr


def _reference_mismatch_indices(
    reference_text: str, ocr_text: str
) -> tuple[set[int], list[str], dict[int, str | None]]:
    mapping, ref_originals, matched_ocr = align_reference_to_ocr(reference_text, ocr_text)
    ocr_originals = [orig for orig, _ in extract_word_pairs(ocr_text)]
    unmatched_ocr = [
        ocr_originals[i] for i in range(len(ocr_originals)) if i not in matched_ocr
    ]
    unmatched_used: set[int] = set()

    mismatches: set[int] = set()
    ocr_cursor = 0

    for index, ref_word in enumerate(ref_originals):
        mapped = mapping.get(index)
        if _ocr_matches_reference(ref_word, mapped):
            if mapped is not None and mapped in ocr_originals:
                try:
                    pos = ocr_originals.index(mapped, ocr_cursor)
                    ocr_cursor = pos + 1
                except ValueError:
                    pass
            continue

        if _is_trackable_word(ref_word):
            nearby = _find_match_index(
                ref_word,
                ocr_originals,
                ocr_cursor,
                window=GLOBAL_SEARCH_WINDOW,
            )
            if nearby is not None:
                ocr_cursor = nearby + 1
                continue

            if _consume_fuzzy_match(ref_word, unmatched_ocr, unmatched_used):
                continue

        elif mapped is not None and _ocr_matches_reference(ref_word, mapped):
            continue

        mismatches.add(index)

    return mismatches, ref_originals, mapping


def _block_is_reordering_only(
    block_ref: list[str],
    ocr_text: str,
) -> bool:
    ocr_originals = [orig for orig, _ in extract_word_pairs(ocr_text)]
    return _multiset_covers(block_ref, ocr_originals)


def _diff_blocks_from_indices(
    confirmed_indices: set[int],
    ref_originals: list[str],
    map_tesseract: dict[int, str | None],
    map_paddle: dict[int, str | None],
    ocr_tesseract: str,
    ocr_paddle: str,
) -> list[dict]:
    diff_entries: list[dict] = []
    if not confirmed_indices:
        return diff_entries

    sorted_indices = sorted(confirmed_indices)
    block_start = sorted_indices[0]
    block_end = block_start

    for index in sorted_indices[1:]:
        if index == block_end + 1:
            block_end = index
            continue

        diff_entries.extend(
            _finalize_blocks(
                block_start,
                block_end,
                ref_originals,
                map_tesseract,
                map_paddle,
                ocr_tesseract,
                ocr_paddle,
            )
        )
        block_start = index
        block_end = index

    diff_entries.extend(
        _finalize_blocks(
            block_start,
            block_end,
            ref_originals,
            map_tesseract,
            map_paddle,
            ocr_tesseract,
            ocr_paddle,
        )
    )
    return diff_entries


def _finalize_blocks(
    start: int,
    end: int,
    ref_originals: list[str],
    map_tesseract: dict[int, str | None],
    map_paddle: dict[int, str | None],
    ocr_tesseract: str,
    ocr_paddle: str,
) -> list[dict]:
    block_ref = ref_originals[start : end + 1]

    if _block_is_reordering_only(block_ref, ocr_tesseract) or _block_is_reordering_only(
        block_ref, ocr_paddle
    ):
        return []

    return [
        _build_diff_block(
            start, end, ref_originals, map_tesseract, map_paddle
        )
    ]


def _build_diff_block(
    start: int,
    end: int,
    ref_originals: list[str],
    map_tesseract: dict[int, str | None],
    map_paddle: dict[int, str | None],
) -> dict:
    block_ref = ref_originals[start : end + 1]
    block_tess = [
        map_tesseract[i]
        for i in range(start, end + 1)
        if map_tesseract.get(i) is not None
    ]
    block_paddle = [
        map_paddle[i]
        for i in range(start, end + 1)
        if map_paddle.get(i) is not None
    ]

    file2_text = _words_to_display(block_tess) or _words_to_display(block_paddle) or None
    return {
        "type": "changed" if file2_text else "only_in_file1",
        "file1": _words_to_display(block_ref),
        "file2": file2_text,
    }


def compare_reference_with_dual_ocr(
    reference_text: str,
    ocr_tesseract: str,
    ocr_paddle: str,
) -> dict:
    tess_bad, ref_originals, map_tess = _reference_mismatch_indices(
        reference_text, ocr_tesseract
    )
    paddle_bad, _, map_paddle = _reference_mismatch_indices(
        reference_text, ocr_paddle
    )

    confirmed = tess_bad & paddle_bad
    differences = _diff_blocks_from_indices(
        confirmed,
        ref_originals,
        map_tess,
        map_paddle,
        ocr_tesseract,
        ocr_paddle,
    )

    normalized_ref = normalize_text(reference_text)
    normalized_tess = normalize_text(ocr_tesseract)
    normalized_paddle = normalize_text(ocr_paddle)

    tess_only = compare_documents(reference_text, ocr_tesseract)
    paddle_only = compare_documents(reference_text, ocr_paddle)

    return {
        "content_identical": not differences,
        "similarity_percent": round(
            max(
                difflib.SequenceMatcher(None, normalized_ref, normalized_tess).ratio(),
                difflib.SequenceMatcher(None, normalized_ref, normalized_paddle).ratio(),
            )
            * 100,
            2,
        ),
        "normalized_file1_length": len(normalized_ref),
        "normalized_file2_length": max(len(normalized_tess), len(normalized_paddle)),
        "file1_text": reference_text,
        "file2_text": ocr_tesseract,
        "differences": differences,
        "diff_summary": {
            "total_differences": len(differences),
            "only_in_file1": sum(1 for d in differences if d["type"] == "only_in_file1"),
            "only_in_file2": sum(1 for d in differences if d["type"] == "only_in_file2"),
            "changed": sum(1 for d in differences if d["type"] == "changed"),
        },
        "ocr_filter_stats": {
            "tesseract_mismatch_words": len(tess_bad),
            "paddle_mismatch_words": len(paddle_bad),
            "confirmed_both_mismatch_words": len(confirmed),
            "tesseract_only_diff_blocks": tess_only["diff_summary"]["total_differences"],
            "paddle_only_diff_blocks": paddle_only["diff_summary"]["total_differences"],
            "confirmed_diff_blocks": len(differences),
        },
    }


def _flexible_diff_words(
    originals1: list[str], originals2: list[str]
) -> list[dict]:
    """Сравнение с учётом перестановок слов (OCR / вёрстка)."""
    mapping, _, matched_in_2 = align_reference_to_ocr(
        " ".join(originals1),
        " ".join(originals2),
    )

    diff_entries: list[dict] = []
    unmatched_2 = [
        originals2[i] for i in range(len(originals2)) if i not in matched_in_2
    ]
    unmatched_2_used: set[int] = set()

    block_start: int | None = None
    block_ref: list[str] = []
    block_ocr: list[str] = []

    def flush_block() -> None:
        nonlocal block_start, block_ref, block_ocr
        if not block_ref:
            block_start = None
            block_ocr = []
            return

        if _multiset_covers(block_ref, originals2):
            block_start = None
            block_ref = []
            block_ocr = []
            return

        if len(block_ref) == 1 and len(block_ocr) == 1:
            diff_entries.append(
                {"type": "changed", "file1": block_ref[0], "file2": block_ocr[0]}
            )
        elif block_ref and block_ocr:
            diff_entries.append(
                {
                    "type": "changed",
                    "file1": _words_to_display(block_ref),
                    "file2": _words_to_display(block_ocr),
                }
            )
        elif block_ref:
            diff_entries.append(
                {
                    "type": "only_in_file1",
                    "file1": _words_to_display(block_ref),
                    "file2": None,
                }
            )

        block_start = None
        block_ref = []
        block_ocr = []

    for index, ref_word in enumerate(originals1):
        mapped = mapping.get(index)
        if _ocr_matches_reference(ref_word, mapped):
            flush_block()
            continue

        if _is_trackable_word(ref_word) and _consume_fuzzy_match(
            ref_word, unmatched_2, unmatched_2_used
        ):
            flush_block()
            continue

        if block_start is None:
            block_start = index
        block_ref.append(ref_word)
        if mapped is not None:
            block_ocr.append(mapped)

    flush_block()

    used_unmatched: set[int] = set()
    extra_block: list[str] = []

    def flush_extra() -> None:
        nonlocal extra_block
        if not extra_block:
            return
        if not _multiset_covers(extra_block, originals1):
            diff_entries.append(
                {
                    "type": "only_in_file2",
                    "file1": None,
                    "file2": _words_to_display(extra_block),
                }
            )
        extra_block = []

    for index, word in enumerate(originals2):
        if index in matched_in_2:
            flush_extra()
            continue

        if _is_trackable_word(word) and _multiset_covers([word], originals1):
            flush_extra()
            continue

        extra_block.append(word)

    flush_extra()
    return diff_entries


def compare_documents(text1: str, text2: str) -> dict:
    pairs1 = extract_word_pairs(text1)
    pairs2 = extract_word_pairs(text2)

    normalized_1 = "".join(norm for _, norm in pairs1)
    normalized_2 = "".join(norm for _, norm in pairs2)

    originals1 = [orig for orig, _ in pairs1]
    originals2 = [orig for orig, _ in pairs2]

    norm_words1 = [norm for _, norm in pairs1]
    norm_words2 = [norm for _, norm in pairs2]

    if norm_words1 and norm_words2 and Counter(norm_words1) == Counter(norm_words2):
        return {
            "content_identical": True,
            "similarity_percent": 100.0,
            "normalized_file1_length": len(normalized_1),
            "normalized_file2_length": len(normalized_2),
            "file1_text": text1,
            "file2_text": text2,
            "differences": [],
            "diff_summary": {
                "total_differences": 0,
                "only_in_file1": 0,
                "only_in_file2": 0,
                "changed": 0,
            },
        }

    diff_entries = _flexible_diff_words(originals1, originals2)

    content_identical = normalized_1 == normalized_2 and not diff_entries
    similarity = round(
        difflib.SequenceMatcher(None, normalized_1, normalized_2).ratio() * 100, 2
    )

    return {
        "content_identical": content_identical,
        "similarity_percent": similarity,
        "normalized_file1_length": len(normalized_1),
        "normalized_file2_length": len(normalized_2),
        "file1_text": text1,
        "file2_text": text2,
        "differences": diff_entries,
        "diff_summary": {
            "total_differences": len(diff_entries),
            "only_in_file1": sum(1 for d in diff_entries if d["type"] == "only_in_file1"),
            "only_in_file2": sum(1 for d in diff_entries if d["type"] == "only_in_file2"),
            "changed": sum(1 for d in diff_entries if d["type"] == "changed"),
        },
    }
