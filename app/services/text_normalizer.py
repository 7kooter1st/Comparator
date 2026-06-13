import re
import unicodedata

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

try:
    from transliterate import translit
except ImportError:
    translit = None

TABLE_NOISE_CHARS = "|—–-"
WORD_PUNCTUATION = "‘’`.,;:!?«»\"'()[]{}…"

LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А", "a": "а", "B": "В", "b": "в", "C": "С", "c": "с",
        "D": "Д", "d": "д", "E": "Е", "e": "е", "H": "Н", "h": "н",
        "I": "И", "i": "и", "K": "К", "k": "к", "L": "Л", "l": "л",
        "M": "М", "m": "м", "N": "Н", "n": "н", "O": "О", "o": "о",
        "P": "Р", "p": "р", "R": "Р", "r": "р", "S": "С", "s": "с",
        "T": "Т", "t": "т", "U": "У", "u": "у", "V": "В", "v": "в",
        "X": "Х", "x": "х", "Y": "У", "y": "у", "W": "Ш", "w": "ш",
        "Z": "З", "z": "з",
    }
)

CYRILLIC_LETTERS = re.compile(r"[а-яА-ЯёЁ]")
LATIN_LETTERS = re.compile(r"[A-Za-z]")
DIGIT_OR_SECTION = re.compile(r"^[\d.]+$")
FUZZY_MIN_RATIO = 88
FUZZY_MAX_EDITS = 2


def has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_LETTERS.search(text))


def has_latin(text: str) -> bool:
    return bool(LATIN_LETTERS.search(text))


def preprocess_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    text = re.sub(r"\s*-\s*", "-", text)
    # Разлепляем слова, приклеенные через ; : — «отношениях;-все» → «отношениях; -все»
    text = re.sub(r"([;:])(-?[А-Яа-яA-Za-z])", r"\1 \2", text)
    return text


def _expand_glued_token(token: str) -> list[str]:
    """Дробит склеенные токены вроде «отношениях;-все» на отдельные слова."""
    token = token.strip()
    if not token:
        return []

    match = re.match(r"^(.+?[;:])(-\S+)$", token)
    if match:
        return [part for part in (match.group(1), match.group(2)) if part]

    match = re.match(r"^(.+?[;:])(\S+)$", token)
    if match and re.search(r"[А-Яа-яA-Za-z]", match.group(2)):
        head, tail = match.group(1), match.group(2)
        if normalize_word_part(tail):
            return [head, tail]

    return [token]


def _strip_noise(word: str) -> str:
    cleaned = word.strip(WORD_PUNCTUATION)
    if cleaned.startswith("-") and len(cleaned) > 1 and cleaned[1].isalnum():
        cleaned = cleaned[1:]
    for char in TABLE_NOISE_CHARS.replace("-", ""):
        cleaned = cleaned.replace(char, "")
    return cleaned.strip(WORD_PUNCTUATION)


def _latin_to_cyrillic_lookalikes(text: str) -> str:
    return text.translate(LATIN_TO_CYRILLIC)


def _transliterate_to_cyrillic(text: str) -> str:
    if not text:
        return ""
    mapped = _latin_to_cyrillic_lookalikes(text)
    if has_cyrillic(mapped):
        return mapped.lower()
    if translit is not None:
        try:
            converted = translit(text, "ru", reversed=True)
            if has_cyrillic(converted):
                return converted.lower()
        except Exception:
            pass
    return mapped.lower()


def normalize_word_part(part: str) -> str:
    part = _strip_noise(part)
    if not part:
        return ""
    if DIGIT_OR_SECTION.fullmatch(part):
        return part.rstrip(".")
    if has_cyrillic(part):
        return _latin_to_cyrillic_lookalikes(part).lower()
    if has_latin(part):
        return _transliterate_to_cyrillic(part).lower()
    return part.lower()


def normalize_word_for_compare(word: str) -> str:
    word = _strip_noise(word)
    if not word:
        return ""
    if "-" in word:
        parts = [normalize_word_part(p) for p in word.split("-")]
        parts = [p for p in parts if p]
        return "-".join(parts)
    return normalize_word_part(word)


def extract_word_pairs(text: str) -> list[tuple[str, str]]:
    text = preprocess_text(text)
    pairs: list[tuple[str, str]] = []
    for word in re.findall(r"\S+", text):
        for part in _expand_glued_token(word):
            normalized = normalize_word_for_compare(part)
            if normalized:
                pairs.append((part, normalized))
    return pairs


def normalize_text(text: str) -> str:
    pairs = extract_word_pairs(text)
    return "".join(normalized for _, normalized in pairs)


def words_equivalent(word1: str | None, word2: str | None, *, fuzzy: bool = True) -> bool:
    if word1 is None or word2 is None:
        return word1 is None and word2 is None

    n1 = normalize_word_for_compare(word1)
    n2 = normalize_word_for_compare(word2)

    if not n1 or not n2:
        return not n1 and not n2
    if n1 == n2:
        return True
    if n1.startswith(n2) or n2.startswith(n1):
        longer, shorter = (n1, n2) if len(n1) >= len(n2) else (n2, n1)
        if longer == shorter or not longer[len(shorter) :].strip("-;:"):
            return True
    if DIGIT_OR_SECTION.fullmatch(n1) and DIGIT_OR_SECTION.fullmatch(n2):
        return n1 == n2
    if not fuzzy:
        return False
    if fuzz.ratio(n1, n2) >= FUZZY_MIN_RATIO:
        return True
    if abs(len(n1) - len(n2)) <= FUZZY_MAX_EDITS:
        if Levenshtein.distance(n1, n2) <= FUZZY_MAX_EDITS:
            return True
    return False
