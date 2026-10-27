"""Compare a typed or ROM-derived title with the titles that sources return. Used by every provider and by search."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# words that mark a store entry as something other than the game itself
NOISE_WORDS = frozenset("soundtrack ost demo dlc bundle pack artbook playtest beta upgrade wallpapers".split())


def normalize(text) -> str:
    """Lowercase words separated by single spaces: accents folded, '&' -> 'and', apostrophes dropped.

    'Café Quokka & Friends' -> 'cafe quokka and friends'. Letters outside the Latin script (Japanese, ...) are kept as they are.
    """
    if not isinstance(text, str):
        return ""
    folded = []
    for ch in text:
        if unicodedata.category(ch).startswith("L"):
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            if base.isascii():
                ch = base
        folded.append(ch)
    text = "".join(folded).casefold().replace("&", " and ")
    text = re.sub(r"['’`´]", "", text)                 # "Bramble's" -> "brambles"
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def title_score(query, candidate) -> float:
    """0..1: how well a source's title matches the text searched for. 1.0 means the same words."""
    q, c = normalize(query).split(), normalize(candidate).split()
    q, c = (q[1:] if len(q) > 1 and q[0] == "the" else q), (c[1:] if len(c) > 1 and c[0] == "the" else c)
    if not q or not c:
        return 0.0
    score = SequenceMatcher(None, " ".join(q), " ".join(c), autojunk=False).ratio()
    if (set(c) & NOISE_WORDS) - set(q):                  # soundtrack / demo / DLC the user did not ask for
        score *= 0.7
    if {w for w in q if w.isdigit()} != {w for w in c if w.isdigit()}:       # 'Chapter 4' is not 'Chapter 5'
        score *= 0.8
    return round(score, 4)
