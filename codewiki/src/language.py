"""
Output-language support for generated documentation.

Only page prose follows the chosen language; filenames, module-tree keys and
link targets stay stable ASCII identifiers (see module_naming.py), so the
viewer, resume and `--update` keep working.
"""

# code -> (display name, BCP-47 tag for <html lang>)
_KNOWN_LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "en"),
    "ja": ("Japanese", "ja"),
    "zh": ("Chinese (Simplified)", "zh-CN"),
    "zh-cn": ("Chinese (Simplified)", "zh-CN"),
    "zh-hans": ("Chinese (Simplified)", "zh-CN"),
    "zh-tw": ("Chinese (Traditional)", "zh-TW"),
    "zh-hant": ("Chinese (Traditional)", "zh-TW"),
    "ko": ("Korean", "ko"),
    "vi": ("Vietnamese", "vi"),
    "th": ("Thai", "th"),
    "id": ("Indonesian", "id"),
    "fr": ("French", "fr"),
    "de": ("German", "de"),
    "es": ("Spanish", "es"),
    "pt": ("Portuguese", "pt"),
    "pt-br": ("Portuguese (Brazil)", "pt-BR"),
    "it": ("Italian", "it"),
    "nl": ("Dutch", "nl"),
    "pl": ("Polish", "pl"),
    "ru": ("Russian", "ru"),
    "uk": ("Ukrainian", "uk"),
    "tr": ("Turkish", "tr"),
    "ar": ("Arabic", "ar"),
    "hi": ("Hindi", "hi"),
}

_NAME_TO_CODE = {name.lower(): code for code, (name, _) in _KNOWN_LANGUAGES.items()}
_NAME_TO_CODE.update({"chinese": "zh", "portuguese": "pt"})


def _lookup(language: str) -> tuple[str, str] | None:
    key = language.strip().lower().replace("_", "-")
    if key in _KNOWN_LANGUAGES:
        return _KNOWN_LANGUAGES[key]
    if key in _NAME_TO_CODE:
        return _KNOWN_LANGUAGES[_NAME_TO_CODE[key]]
    return None


def normalize_language(language: str | None) -> str | None:
    """
    Map a language code or name to the name used in prompts and metadata.

    Known codes/names become a canonical name ("ja" -> "Japanese"); any other
    text passes through unchanged. English (or nothing) returns None, so the
    default output is exactly what CodeWiki produced before this option.
    """
    if not language or not language.strip():
        return None
    known = _lookup(language)
    name = known[0] if known else language.strip()
    return None if name == "English" else name


def language_tag(language: str | None) -> str:
    """BCP-47 tag for <html lang>; "en" when unset or unknown."""
    if not language:
        return "en"
    known = _lookup(language)
    return known[1] if known else "en"


LANGUAGE_DIRECTIVE = """<OUTPUT_LANGUAGE>
Write ALL documentation prose in {language}: page titles, headings, paragraphs, lists, table text, Mermaid node/edge labels and captions.
Keep code, identifiers, file paths, CLI commands and API names exactly as they are.
Filenames, module names and link targets are fixed identifiers: never translate or rename them. Save the page under exactly the `.md` filename named above, even when it differs from a package or directory name; keep sub-module names as given; link to other pages as `[text](<exact module name>.md)`. Only the visible link text may be translated.
Start each page with a single `# ` heading that is the page title in {language}.
</OUTPUT_LANGUAGE>"""


def format_language_directive(language: str | None) -> str:
    """The prompt block for `language`, or "" when no language is set."""
    name = normalize_language(language)
    return LANGUAGE_DIRECTIVE.format(language=name) if name else ""


def resolve_update_language(stored: str | None, requested: str | None) -> str | None:
    """
    Language for an incremental update of docs generated in `stored`.

    The stored language wins over any saved default, so updated pages match
    the existing ones. An explicit `requested` language that differs raises
    ValueError: switching language needs a full regeneration.
    """
    stored_name = normalize_language(stored)
    if requested is not None and normalize_language(requested) != stored_name:
        raise ValueError(
            f"These docs were generated in {stored_name or 'English'}, but --language asks for "
            f"{normalize_language(requested) or 'English'}. An incremental update cannot switch "
            "language; rerun without --update to regenerate the docs in the new language."
        )
    return stored_name
