"""Parsers from ``tree-sitter-language-pack``.

Pack 1.x does not ship its grammars in the wheel. It downloads each grammar
the first time it is loaded and caches it under ``cache_dir()``. Without
network access that first load fails with a ``DownloadError`` that names a
URL but not the cause or the fix. The per-file ``except`` blocks of the
analyzers would also swallow it, and the language would silently drop out of
the graph. So the analysis checks the grammars it needs once, before it
parses any file, and stops with one clear message.
"""

from functools import lru_cache

import tree_sitter_language_pack as tslp

# Pack grammars each analyzed language loads. ``docker/Dockerfile`` downloads
# the same set at build time (``tests/test_pack_grammars.py`` checks this).
PACK_GRAMMARS_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "dart": ("dart",),
    "go": ("go",),
    "lean": ("lean",),
    "vue": ("vue", "javascript", "typescript"),
    "zig": ("zig",),
}

PACK_GRAMMARS: tuple[str, ...] = tuple(
    sorted({g for grammars in PACK_GRAMMARS_BY_LANGUAGE.values() for g in grammars})
)


class GrammarUnavailableError(RuntimeError):
    """A pack grammar is not cached and could not be downloaded."""

    def __init__(self, grammar: str, cause: Exception):
        try:
            cache = tslp.cache_dir()
        except Exception:  # noqa: BLE001 — the path only improves the message
            cache = "the tree-sitter-language-pack cache"
        names = ", ".join(repr(g) for g in PACK_GRAMMARS)
        super().__init__(
            f"The tree-sitter grammar '{grammar}' is not in {cache} and could not be "
            f"downloaded ({cause}). tree-sitter-language-pack downloads each grammar on "
            "first use, so the first analysis of these files needs network access. "
            "To prepare a machine without network access, run this once on a machine "
            "with access and copy the cache folder: "
            f'python -c "import tree_sitter_language_pack as t; t.download([{names}])"'
        )
        self.grammar = grammar


@lru_cache(maxsize=None)
def get_pack_parser(grammar: str):
    """Return the pack parser for ``grammar``, with a clear error when it is unavailable."""
    try:
        return tslp.get_parser(grammar)
    except tslp.DownloadError as e:
        raise GrammarUnavailableError(grammar, e) from e


def ensure_pack_grammars(languages) -> None:
    """Load every pack grammar that ``languages`` need, or raise ``GrammarUnavailableError``."""
    for language in sorted(set(languages)):
        for grammar in PACK_GRAMMARS_BY_LANGUAGE.get(language, ()):
            get_pack_parser(grammar)
