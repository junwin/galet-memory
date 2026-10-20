from __future__ import annotations

import re
from collections import Counter
from typing import Protocol, Sequence


class KeywordExtractor(Protocol):
    """Derive ordered keywords from text, independent of any application."""

    def extract(self, text: str, *, top_n: int = 10) -> list[str]: ...


_WORD_RE = re.compile(r"[a-z][a-z0-9'-]{2,}")

_STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because
    been before being below between both but by could did do does doing down
    during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more
    most my myself no nor not now of off on once only or other our ours
    ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why
    will with you your yours yourself yourselves""".split()
)


class SimpleKeywordExtractor:
    """Dependency-free keyword extractor built on token frequency.

    Lowercases ASCII word tokens, drops a small built-in stopword list, and
    ranks by frequency with first-occurrence order as the tie break. It is a
    conservative default; applications can supply their own extractor (for
    example a spaCy/NLTK-based one) through the same port.
    """

    def __init__(self, *, stopwords: Sequence[str] = ()) -> None:
        extra = {w.lower() for w in stopwords}
        self._stopwords = _STOPWORDS | extra

    def extract(self, text: str, *, top_n: int = 10) -> list[str]:
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        words = [w for w in _WORD_RE.findall(text.lower()) if w not in self._stopwords]
        counts = Counter(words)
        ranked = sorted(counts, key=lambda w: (-counts[w], words.index(w)))
        return ranked[:top_n]
