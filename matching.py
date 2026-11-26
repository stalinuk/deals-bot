"""Decide whether a listing / deal title is really the NEW product you asked for."""
from __future__ import annotations

import re

FILLER = {"apple", "the", "and", "with", "new", "for", "&", "-"}

# Always skip: not a new unit
CONDITION_WORDS = (
    "refurb", "refurbished", "restored", "renewed", "pre-owned", "preowned", "used",
    "open box", "open-box", "certified refurbished",
)
# Skip only when the listing IS the accessory ("Case for AirPods Pro 3",
# "AirPods Pro 3 Case") - not when the product merely comes with one
# ("Apple Watch Series 8 ... Aluminum Case with Sport Band").
ACCESSORY_WORDS = (
    "case", "cover", "band", "bands", "strap", "screen protector", "protector", "charger",
    "cable", "skin", "ear tips", "sticker", "holder", "mount", "dock", "replacement",
)
DEFAULT_EXCLUDE = CONDITION_WORDS  # kept for callers / user overrides


def keyword_patterns(text_or_words) -> list[re.Pattern]:
    words = re.split(r"[\s,]+", text_or_words) if isinstance(text_or_words, str) else text_or_words
    pats = []
    for w in words:
        w = str(w).lower().strip()
        if not w or w in FILLER:
            continue
        if w.isdigit():
            pats.append(re.compile(rf"(?<![\d$.]){w}(?![\d.]\d|\d|mm)"))  # "3" not "$139"/"3.5mm"
        else:
            stem = w[:-1] if w.endswith("s") and len(w) > 3 else w      # airpods ~ airpod
            pats.append(re.compile(rf"\b{re.escape(stem)}"))
    return pats


def _has_word(text: str, word: str) -> re.Match | None:
    return re.search(rf"\b{re.escape(word)}\b", text)


def title_matches(title: str, keywords: list[re.Pattern], exclude=None) -> bool:
    t = title.lower()
    hits = [k.search(t) for k in keywords]
    if not all(hits):
        return False
    first = min((h.start() for h in hits), default=0)
    last = max((h.end() for h in hits), default=0)

    for word in (exclude or CONDITION_WORDS):
        if _has_word(t, word):
            return False

    # accessory listing: "Case for/compatible with <product>", accessory word before
    # the product name, or right after it ("AirPods Pro 3 Case")
    before = t[:first]
    if re.search(r"\b(for|compatible|fits|fit for)\b", before):
        return False
    for word in ACCESSORY_WORDS:
        m = _has_word(t, word)
        if m and (m.start() < first or m.start() - last <= 12) and not re.search(
            r"\b(with|w/|includes?)\b", t[last:m.start()]
        ):
            return False
    if re.search(r"\bcompatible with\b", t):
        return False
    return True
