"""textnorm.py - text normalisation shared by the OCR reference builder and the scorer.

Korean OCR output is split per syllable and LLM output per word, so nothing here relies on word
boundaries: comparison is done on normalised strings (NFKC, lower-case, no whitespace/punctuation)
and on character-bigram sets. Digits and Latin identifiers get their own extractors because those
are the places where an LLM silently "fixes" what it reads.

    norm(s) / units(s) / bigrams(s) / overlap / fuzzy_in    comparison of any two texts (never folded:
                                                            lecture-knowledge-pack keys its registry on norm())
    digit_seqs(s) / latin_tokens(s)                          extractors for the digit / identifier checks
    fold(s)     OCR look-alikes to ASCII: 〈〉 -> <>, every dash -> '-', 十 -> +, Ø -> 0, curly quotes -> straight,
                「 -> r next to Latin letters. Case and punctuation are kept.
    ocr_key(s)  norm(fold(s)) with i / 1 / | -> l and 0 -> o: the key for comparing OCR output with clean text
    skel(s)     ocr_key(s) with 5 -> s as well: the loosest key (line anchoring, copy detection)
"""
from __future__ import annotations

import re
import unicodedata

PUNCT_RE = re.compile(r"[\s\u00b7\u2022\u318d\u2026\-_()\[\]{}'\"`.,:;!?/\|<>=+*~^%$#@&\u203b\u2192\u2190\u2191\u2193"
                      r"\u300c\u300d\u300e\u300f\u3010\u3011\u3008\u3009\u300a\u300b\uff08\uff09\uff0c\uff0e]+")
DIGIT_RE = re.compile(r"\d+(?:[.,:]\d+)*")
LATIN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def norm(s: str) -> str:
    """NFKC, lower-case, strip whitespace and punctuation. 'SELECT id,' -> 'selectid'."""
    return PUNCT_RE.sub("", nfkc(s).lower())


def units(s: str) -> list:
    """Split on whitespace/punctuation, normalise, drop units shorter than 2 chars."""
    return [u for u in (norm(x) for x in PUNCT_RE.split(nfkc(s))) if len(u) >= 2]


def bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def digit_seqs(s: str) -> list:
    """Digit sequences with separators removed: '1,024' and '1.024' both -> '1024'."""
    return [re.sub(r"\D", "", m) for m in DIGIT_RE.findall(nfkc(s))]


def latin_tokens(s: str) -> list:
    return [t.lower() for t in LATIN_RE.findall(nfkc(s))]


def overlap(needle: str, hay_bigrams: set) -> float:
    """Fraction of the needle's bigrams present in the haystack's bigram set."""
    nb = bigrams(needle)
    if not nb:
        return 0.0
    return len(nb & hay_bigrams) / len(nb)


def fuzzy_in(needle: str, hay: str, hay_bigrams: set, thresh: float) -> bool:
    """needle is already normalised. Substring match first, then bigram overlap."""
    if not needle:
        return False
    if needle in hay:
        return True
    if len(needle) < 3:
        return False
    return overlap(needle, hay_bigrams) >= thresh


# --------------------------------------------------------------------------- OCR look-alike folding
# Only for comparisons against OCR output. Windows OCR (ko) reads r as 「, l as I or 1, o as 0 and v-if as v—if.
_FOLD = str.maketrans({"〈": "<", "〉": ">", "〈": "<", "〉": ">", "《": "<", "》": ">",
                       **{chr(c): "-" for c in range(0x2010, 0x2016)}, "−": "-",
                       "十": "+", "Ø": "0", "ø": "0",
                       "“": '"', "”": '"', "‘": "'", "’": "'"})
_LAT_BRACKET = re.compile(r"(?<=[A-Za-z])「|「(?=[A-Za-z])")
OCR_KEY_TABLE = str.maketrans({"i": "l", "1": "l", "|": "l", "0": "o"})
SKEL_TABLE = str.maketrans({"i": "l", "1": "l", "|": "l", "0": "o", "5": "s"})


def fold(s: str) -> str:
    return _LAT_BRACKET.sub("r", nfkc(s)).translate(_FOLD)


def ocr_key(s: str) -> str:
    return norm(fold(s)).translate(OCR_KEY_TABLE)


def skel(s: str) -> str:
    return norm(fold(s)).translate(SKEL_TABLE)
