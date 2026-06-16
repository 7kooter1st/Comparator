from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.config import DEBUG
from app.services.text_normalizer import (
    FUZZY_MAX_EDITS,
    extract_word_pairs,
    normalize_word_for_compare,
    normalized_forms_equal,
    words_equivalent_for_ocr_locate,
)

# OCR часто «ломает» порядок слов из-за вёрстки — ищем совпадения в окне
MAX_LOOKAHEAD = 25
GLOBAL_SEARCH_WINDOW = 50
MIN_TRACKABLE_WORD_LEN = 4


@dataclass(frozen=True)
class ParsedText:
    pairs: list[tuple[str, str]]

    @classmethod
    def from_text(cls, text: str) -> "ParsedText":
        return cls(extract_word_pairs(text))

    @property
    def originals(self) -> list[str]:
        return [orig for orig, _ in self.pairs]

    @property
    def normalized_words(self) -> list[str]:
        return [norm for _, norm in self.pairs]

    @property
    def normalized_length(self) -> int:
        return sum(len(norm) for _, norm in self.pairs)


class WordIndex:
    """Индекс нормализованных слов для быстрого точного и fuzzy-поиска."""

    def __init__(self, originals: list[str], normalized: list[str]) -> None:
        self.originals = originals
        self.normalized = normalized
        self._exact: dict[str, list[int]] = defaultdict(list)
        self._by_length: dict[int, list[int]] = defaultdict(list)
        for index, norm in enumerate(normalized):
            self._exact[norm].append(index)
            self._by_length[len(norm)].append(index)

    @classmethod
    def from_originals(cls, originals: list[str]) -> "WordIndex":
        normalized = [normalize_word_for_compare(word) for word in originals]
        return cls(originals, normalized)

    def find_in_window(
        self,
        word: str,
        start: int,
        *,
        window: int,
        used: set[int] | None = None,
    ) -> int | None:
        if not word:
            return None

        end = min(len(self.originals), start + window)
        search_from = max(0, start - 8)
        norm = normalize_word_for_compare(word)

        for index in self._exact.get(norm, []):
            if search_from <= index < end and (used is None or index not in used):
                return index

        word_len = len(norm)
        candidates: set[int] = set()
        for delta in range(-FUZZY_MAX_EDITS, FUZZY_MAX_EDITS + 1):
            candidates.update(self._by_length.get(word_len + delta, []))

        for index in sorted(index for index in candidates if search_from <= index < end):
            if used is not None and index in used:
                continue
            if _ocr_locate_match(word, self.originals[index]):
                return index
        return None

    def consume_exact_match(self, word: str, used: set[int]) -> bool:
        norm = normalize_word_for_compare(word)
        for index, candidate_norm in enumerate(self.normalized):
            if index in used:
                continue
            if candidate_norm == norm:
                used.add(index)
                return True
        return False

    def has_exact_match(self, word: str) -> bool:
        norm = normalize_word_for_compare(word)
        return bool(self._exact.get(norm))

    def has_equivalent(self, word: str) -> bool:
        """Алиас для обратной совместимости (строгое совпадение)."""
        return self.has_exact_match(word)


def _words_to_display(words: list[str]) -> str:
    return " ".join(words)


def _ocr_locate_match(reference_word: str, ocr_word: str | None) -> bool:
    """Fuzzy — только для выравнивания / поиска позиции в OCR."""
    return words_equivalent_for_ocr_locate(reference_word, ocr_word)


def _words_match_exact(reference_word: str, other_word: str | None) -> bool:
    """Строгое совпадение — для фиксации изменений."""
    return normalized_forms_equal(reference_word, other_word)


def _is_trackable_word(word: str) -> bool:
    return len(normalize_word_for_compare(word)) >= MIN_TRACKABLE_WORD_LEN


def _similarity_from_mismatches(mismatch_count: int, word_count: int) -> float:
    if word_count == 0:
        return 100.0
    return round(max(0.0, (1 - mismatch_count / word_count) * 100), 2)


def _multiset_covers(
    reference_words: list[str],
    ocr_words: list[str],
    ocr_index: WordIndex | None = None,
) -> bool:
    if not reference_words:
        return True

    index = ocr_index or WordIndex.from_originals(ocr_words)
    used: set[int] = set()
    for ref_word in reference_words:
        if not index.consume_exact_match(ref_word, used):
            return False
    return True


def _consume_exact_match_in_pool(
    word: str,
    pool: list[str],
    pool_norms: list[str],
    used: set[int],
) -> bool:
    norm = normalize_word_for_compare(word)
    for index, candidate_norm in enumerate(pool_norms):
        if index in used:
            continue
        if candidate_norm == norm:
            used.add(index)
            return True
    return False


def _consume_fuzzy_locate_in_pool(
    word: str,
    pool: list[str],
    pool_norms: list[str],
    used: set[int],
) -> int | None:
    """Ищет в пуле позицию через fuzzy; возвращает индекс или None."""
    norm = normalize_word_for_compare(word)
    for index, candidate_norm in enumerate(pool_norms):
        if index in used:
            continue
        if candidate_norm == norm:
            return index

    word_len = len(norm)
    for index, candidate in enumerate(pool):
        if index in used:
            continue
        if abs(len(pool_norms[index]) - word_len) > FUZZY_MAX_EDITS:
            continue
        if _ocr_locate_match(word, candidate):
            return index
    return None


def align_reference_to_ocr(
    ref_originals: list[str],
    ocr_originals: list[str],
    ocr_index: WordIndex | None = None,
) -> tuple[dict[int, str | None], set[int]]:
    index = ocr_index or WordIndex.from_originals(ocr_originals)

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

        if _words_match_exact(ref_word, ocr_word):
            mapping[i_ref] = ocr_word
            matched_ocr.add(i_ocr)
            i_ref += 1
            i_ocr += 1
            continue

        found_ocr = index.find_in_window(
            ref_word,
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
            if _ocr_locate_match(ref_originals[j], ocr_word):
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

    return mapping, matched_ocr


def _mismatch_indices_from_alignment(
    ref_originals: list[str],
    ocr_originals: list[str],
    mapping: dict[int, str | None],
    matched_ocr: set[int],
    ocr_index: WordIndex,
) -> set[int]:
    unmatched_ocr = [
        ocr_originals[i] for i in range(len(ocr_originals)) if i not in matched_ocr
    ]
    unmatched_norms = [normalize_word_for_compare(word) for word in unmatched_ocr]
    unmatched_used: set[int] = set()

    mismatches: set[int] = set()
    ocr_cursor = 0

    for index, ref_word in enumerate(ref_originals):
        mapped = mapping.get(index)
        if _words_match_exact(ref_word, mapped):
            if mapped is not None and mapped in ocr_originals:
                try:
                    pos = ocr_originals.index(mapped, ocr_cursor)
                    ocr_cursor = pos + 1
                except ValueError:
                    pass
            continue

        if _is_trackable_word(ref_word):
            nearby = ocr_index.find_in_window(
                ref_word,
                ocr_cursor,
                window=GLOBAL_SEARCH_WINDOW,
            )
            if nearby is not None and _words_match_exact(
                ref_word, ocr_originals[nearby]
            ):
                ocr_cursor = nearby + 1
                continue

            pool_index = _consume_fuzzy_locate_in_pool(
                ref_word, unmatched_ocr, unmatched_norms, unmatched_used
            )
            if pool_index is not None and _words_match_exact(
                ref_word, unmatched_ocr[pool_index]
            ):
                unmatched_used.add(pool_index)
                continue

        mismatches.add(index)

    return mismatches


def _reference_mismatch_indices(
    reference: ParsedText,
    ocr: ParsedText,
) -> tuple[set[int], dict[int, str | None]]:
    ref_originals = reference.originals
    ocr_originals = ocr.originals
    ocr_index = WordIndex.from_originals(ocr_originals)
    mapping, matched_ocr = align_reference_to_ocr(
        ref_originals, ocr_originals, ocr_index
    )
    mismatches = _mismatch_indices_from_alignment(
        ref_originals,
        ocr_originals,
        mapping,
        matched_ocr,
        ocr_index,
    )
    return mismatches, mapping


def _counter_identical_result(
    reference: ParsedText,
    ocr: ParsedText,
    *,
    reference_text: str,
    ocr_text: str,
) -> dict:
    return {
        "mismatches": set(),
        "mapping": {i: ocr.originals[i] for i in range(len(reference.originals))},
        "differences": [],
        "similarity_percent": 100.0,
        "content_identical": True,
        "normalized_file1_length": reference.normalized_length,
        "normalized_file2_length": ocr.normalized_length,
        "file1_text": reference_text,
        "file2_text": ocr_text,
        "diff_summary": {
            "total_differences": 0,
            "only_in_file1": 0,
            "only_in_file2": 0,
            "changed": 0,
        },
    }


def _block_is_reordering_only(
    block_ref: list[str],
    ocr_originals: list[str],
    ocr_index: WordIndex,
) -> bool:
    return _multiset_covers(block_ref, ocr_originals, ocr_index)


def _diff_blocks_from_indices(
    confirmed_indices: set[int],
    ref_originals: list[str],
    map_tesseract: dict[int, str | None],
    map_paddle: dict[int, str | None],
    tess_originals: list[str],
    paddle_originals: list[str],
    tess_index: WordIndex,
    paddle_index: WordIndex,
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
                tess_originals,
                paddle_originals,
                tess_index,
                paddle_index,
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
            tess_originals,
            paddle_originals,
            tess_index,
            paddle_index,
        )
    )
    return diff_entries


def _finalize_blocks(
    start: int,
    end: int,
    ref_originals: list[str],
    map_tesseract: dict[int, str | None],
    map_paddle: dict[int, str | None],
    tess_originals: list[str],
    paddle_originals: list[str],
    tess_index: WordIndex,
    paddle_index: WordIndex,
) -> list[dict]:
    block_ref = ref_originals[start : end + 1]

    if _block_is_reordering_only(block_ref, tess_originals, tess_index) or _block_is_reordering_only(
        block_ref, paddle_originals, paddle_index
    ):
        return []

    return [
        _build_diff_block(start, end, ref_originals, map_tesseract, map_paddle)
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


def _dual_ocr_counter_identical_result(
    reference: ParsedText,
    reference_text: str,
    ocr_tesseract: str,
    tess_parsed: ParsedText,
    paddle_parsed: ParsedText,
) -> dict:
    return {
        "content_identical": True,
        "similarity_percent": 100.0,
        "normalized_file1_length": reference.normalized_length,
        "normalized_file2_length": max(
            tess_parsed.normalized_length, paddle_parsed.normalized_length
        ),
        "file1_text": reference_text,
        "file2_text": ocr_tesseract,
        "differences": [],
        "diff_summary": {
            "total_differences": 0,
            "only_in_file1": 0,
            "only_in_file2": 0,
            "changed": 0,
        },
        "ocr_filter_stats": {
            "tesseract_mismatch_words": 0,
            "paddle_mismatch_words": 0,
            "confirmed_both_mismatch_words": 0,
            "confirmed_diff_blocks": 0,
        },
    }


def compare_reference_with_dual_ocr(
    reference_text: str,
    ocr_tesseract: str,
    ocr_paddle: str,
) -> dict:
    reference = ParsedText.from_text(reference_text)
    tess_parsed = ParsedText.from_text(ocr_tesseract)
    paddle_parsed = ParsedText.from_text(ocr_paddle)

    ref_counter = Counter(reference.normalized_words)
    if (
        ref_counter
        and ref_counter == Counter(tess_parsed.normalized_words)
        and ref_counter == Counter(paddle_parsed.normalized_words)
    ):
        result = _dual_ocr_counter_identical_result(
            reference,
            reference_text,
            ocr_tesseract,
            tess_parsed,
            paddle_parsed,
        )
        if DEBUG:
            result["ocr_filter_stats"]["tesseract_only_diff_blocks"] = 0
            result["ocr_filter_stats"]["paddle_only_diff_blocks"] = 0
        return result

    tess_index = WordIndex.from_originals(tess_parsed.originals)
    paddle_index = WordIndex.from_originals(paddle_parsed.originals)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tess_future = executor.submit(
            _reference_mismatch_indices, reference, tess_parsed
        )
        paddle_future = executor.submit(
            _reference_mismatch_indices, reference, paddle_parsed
        )
        tess_bad, map_tess = tess_future.result()
        paddle_bad, map_paddle = paddle_future.result()

    ref_originals = reference.originals
    confirmed = tess_bad & paddle_bad
    differences = _diff_blocks_from_indices(
        confirmed,
        ref_originals,
        map_tess,
        map_paddle,
        tess_parsed.originals,
        paddle_parsed.originals,
        tess_index,
        paddle_index,
    )

    mismatch_count = len(confirmed)
    word_count = len(ref_originals)

    ocr_filter_stats = {
        "tesseract_mismatch_words": len(tess_bad),
        "paddle_mismatch_words": len(paddle_bad),
        "confirmed_both_mismatch_words": mismatch_count,
        "confirmed_diff_blocks": len(differences),
    }

    if DEBUG:
        tess_only = compare_documents(reference_text, ocr_tesseract)
        paddle_only = compare_documents(reference_text, ocr_paddle)
        ocr_filter_stats["tesseract_only_diff_blocks"] = tess_only[
            "diff_summary"
        ]["total_differences"]
        ocr_filter_stats["paddle_only_diff_blocks"] = paddle_only[
            "diff_summary"
        ]["total_differences"]

    return {
        "content_identical": not differences,
        "similarity_percent": _similarity_from_mismatches(mismatch_count, word_count),
        "normalized_file1_length": reference.normalized_length,
        "normalized_file2_length": max(
            tess_parsed.normalized_length, paddle_parsed.normalized_length
        ),
        "file1_text": reference_text,
        "file2_text": ocr_tesseract,
        "differences": differences,
        "diff_summary": {
            "total_differences": len(differences),
            "only_in_file1": sum(1 for d in differences if d["type"] == "only_in_file1"),
            "only_in_file2": sum(1 for d in differences if d["type"] == "only_in_file2"),
            "changed": sum(1 for d in differences if d["type"] == "changed"),
        },
        "ocr_filter_stats": ocr_filter_stats,
    }


def _flexible_diff_from_alignment(
    originals1: list[str],
    originals2: list[str],
    mapping: dict[int, str | None],
    matched_in_2: set[int],
    ocr_index: WordIndex,
    ref_index: WordIndex | None = None,
) -> list[dict]:
    ref_lookup = ref_index or WordIndex.from_originals(originals1)
    diff_entries: list[dict] = []
    unmatched_2 = [
        originals2[i] for i in range(len(originals2)) if i not in matched_in_2
    ]
    unmatched_norms = [normalize_word_for_compare(word) for word in unmatched_2]
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

        if _multiset_covers(block_ref, originals2, ocr_index):
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

    for index_ref, ref_word in enumerate(originals1):
        mapped = mapping.get(index_ref)
        if _words_match_exact(ref_word, mapped):
            flush_block()
            continue

        if _is_trackable_word(ref_word) and _consume_exact_match_in_pool(
            ref_word, unmatched_2, unmatched_norms, unmatched_2_used
        ):
            flush_block()
            continue

        if block_start is None:
            block_start = index_ref
        block_ref.append(ref_word)
        if mapped is not None:
            block_ocr.append(mapped)

    flush_block()

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

    for index_ocr, word in enumerate(originals2):
        if index_ocr in matched_in_2:
            flush_extra()
            continue

        if _is_trackable_word(word) and ref_lookup.has_exact_match(word):
            flush_extra()
            continue

        extra_block.append(word)

    flush_extra()
    return diff_entries


def _flexible_diff_words(
    originals1: list[str],
    originals2: list[str],
    ocr_index: WordIndex | None = None,
) -> list[dict]:
    index = ocr_index or WordIndex.from_originals(originals2)
    mapping, matched_in_2 = align_reference_to_ocr(originals1, originals2, index)
    return _flexible_diff_from_alignment(
        originals1, originals2, mapping, matched_in_2, index
    )


def compare_documents(text1: str, text2: str) -> dict:
    parsed1 = ParsedText.from_text(text1)
    parsed2 = ParsedText.from_text(text2)

    originals1 = parsed1.originals
    originals2 = parsed2.originals
    norm_words1 = parsed1.normalized_words
    norm_words2 = parsed2.normalized_words

    if norm_words1 and norm_words2 and Counter(norm_words1) == Counter(norm_words2):
        return _counter_identical_result(
            parsed1,
            parsed2,
            reference_text=text1,
            ocr_text=text2,
        )

    ocr_index = WordIndex.from_originals(originals2)
    ref_index = WordIndex.from_originals(originals1)
    mapping, matched_ocr = align_reference_to_ocr(originals1, originals2, ocr_index)
    mismatches = _mismatch_indices_from_alignment(
        originals1,
        originals2,
        mapping,
        matched_ocr,
        ocr_index,
    )
    diff_entries = _flexible_diff_from_alignment(
        originals1, originals2, mapping, matched_ocr, ocr_index, ref_index
    )

    return {
        "content_identical": not mismatches and not diff_entries,
        "similarity_percent": _similarity_from_mismatches(
            len(mismatches), len(originals1)
        ),
        "normalized_file1_length": parsed1.normalized_length,
        "normalized_file2_length": parsed2.normalized_length,
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
