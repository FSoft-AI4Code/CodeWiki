"""BM25F ranking over multi-field units (doc sections and code components).

A Python port of the scorer the docs viewer runs for Ctrl+K search
(``viewer_template.html``), extended for code: identifiers are split on
camelCase / snake_case and also kept whole, so ``TimestampSigner`` matches
"timestamp signer", "timestampsigner" and "TimestampSigner".
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from collections.abc import Iterable

_WORD_RE = re.compile(r"\w+")  # Unicode: pages may be written in any output language
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

STOPWORDS = frozenset(
    "a an and are as at be by does do for from how i if in is it its of on or "
    "the this that to was what when where which who why will with you your can "
    "we our me my there their them they then than so not no use used using".split()
)

K1 = 1.2
B = 0.75
PREFIX_WEIGHT = 0.5
MAX_PREFIX_EXPANSIONS = 6


def stem(term: str) -> str:
    """Light suffix stripping so "timestamps"/"signing"/"signed" meet their roots.

    Words also lose a final "e" and a doubled final consonant, roots and
    inflections alike, so "merge"/"merging"/"merged" and "stem"/"stemming" meet.
    """
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        term = term[:-1]
    if len(term) > 5 and term.endswith("ing"):
        term = term[:-3]
    elif len(term) > 4 and term.endswith("ed") and not term.endswith("eed"):
        term = term[:-2]
    if not (term.isascii() and term.isalpha()) or len(term) <= 3:
        return term  # identifiers, numbers and non-English words keep their spelling
    if term.endswith("e") and not term.endswith("ee"):
        term = term[:-1]
    if term[-1] == term[-2] and term[-1] not in "aeioulsfz":
        term = term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """Lowercase, stemmed terms: whole identifiers plus their camel/snake parts."""
    terms: list[str] = []
    for word in _WORD_RE.findall(text or ""):
        lower = word.lower()
        parts = [
            p.lower()
            for chunk in word.split("_")
            for p in (_CAMEL_RE.findall(chunk) if chunk.isascii() else [chunk])
        ]
        if lower not in STOPWORDS and len(lower) > 1:
            terms.append(stem(lower))
        if parts != [lower]:  # also "_evict" -> "evict"
            terms.extend(stem(p) for p in parts if p not in STOPWORDS and len(p) > 1)
    return terms


class BM25F:
    """Index of documents with named, weighted fields."""

    def __init__(self, weights: dict[str, float]) -> None:
        self.weights = weights
        self._docs: list[dict[str, Counter]] = []
        self._lengths: list[dict[str, int]] = []
        self._df: Counter = Counter()
        self._postings: dict[str, list[int]] = {}
        self._avg: dict[str, float] = {}
        self._vocab: list[str] = []

    def add(self, fields: dict[str, str]) -> int:
        counters = {name: Counter(tokenize(fields.get(name, ""))) for name in self.weights}
        self._docs.append(counters)
        self._lengths.append({name: sum(c.values()) for name, c in counters.items()})
        doc = len(self._docs) - 1
        for term in set().union(*(c.keys() for c in counters.values())):
            self._df[term] += 1
            self._postings.setdefault(term, []).append(doc)
        return doc

    def finalize(self) -> None:
        n = max(1, len(self._docs))
        self._avg = {
            name: max(1.0, sum(lengths[name] for lengths in self._lengths) / n)
            for name in self.weights
        }
        self._vocab = sorted(self._df)

    def __len__(self) -> int:
        return len(self._docs)

    def _expand(self, term: str) -> list[tuple[str, float]]:
        if term in self._df:
            return [(term, 1.0)]
        if len(term) < 3:
            return []
        start = bisect.bisect_left(self._vocab, term)
        out = []
        for word in self._vocab[start : start + MAX_PREFIX_EXPANSIONS]:
            if not word.startswith(term):
                break
            out.append((word, PREFIX_WEIGHT))
        return out

    def query_terms(self, query: str | Iterable[str]) -> list[tuple[str, float]]:
        terms = tokenize(query) if isinstance(query, str) else list(query)
        seen: dict[str, float] = {}
        for term in terms:
            for word, weight in self._expand(term):
                seen[word] = max(seen.get(word, 0.0), weight)
        return list(seen.items())

    def score(self, doc: int, terms: list[tuple[str, float]]) -> float:
        n = len(self._docs)
        counters = self._docs[doc]
        lengths = self._lengths[doc]
        total = 0.0
        for term, qweight in terms:
            tf = 0.0
            for name, weight in self.weights.items():
                freq = counters[name].get(term, 0)
                if freq:
                    norm = 1 - B + B * lengths[name] / self._avg[name]
                    tf += weight * freq / norm
            if not tf:
                continue
            df = self._df[term]
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            total += qweight * idf * tf / (K1 + tf)
        return total

    def search(
        self, query: str | Iterable[str], k: int | None = 20, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        terms = self.query_terms(query)
        if not terms:
            return []
        candidates: set[int] = set()
        for term, _ in terms:
            candidates.update(self._postings.get(term, ()))
        if allowed is not None:
            candidates &= allowed
        scored = [(doc, self.score(doc, terms)) for doc in candidates]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda s: s[1], reverse=True)
        return scored[:k]
