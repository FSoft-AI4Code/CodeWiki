"""
TechDocs source generator for Backstage.

Turns CodeWiki output into an MkDocs project that ``techdocs-cli generate``
builds as is::

    <out>/
    ├── mkdocs.yml      # site name, repo link, nav from the module tree
    └── docs/
        ├── index.md    # the repository overview
        └── ...         # module pages, in the same layout as the source

Only pages and images are copied: ``module_tree.json``, ``metadata.json``,
``update_record.json`` and ``temp/`` (the dependency graph) stay out of the
published site. Each page gets front matter, a provenance banner and a
collapsible list of the components it documents, linked to the source at the
documented commit. The source docs are not modified, so the export can be
re-run after every ``--update``.

Based on the generator proposed in
https://github.com/FSoft-AI4Code/CodeWiki/issues/145.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml

from codewiki.src.be.doc_layout import (
    METADATA_FILENAME,
    OVERVIEW_STEM,
    TEMP_DIR,
    list_doc_files,
    load_module_tree,
    rewrite_page_links,
)
from codewiki.src.be.module_naming import _first_h1, extract_page_titles

INDEX_FILENAME = "index.md"
DOCS_SUBDIR = "docs"
MKDOCS_FILENAME = "mkdocs.yml"
# Written into the generated docs folder; only a folder carrying it is wiped on re-runs
MARKER_FILENAME = ".codewiki-techdocs"
ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
MAX_LISTED_COMPONENTS = 50
MAX_DESCRIPTION_CHARS = 160

# Source links per host; {repo} is the https repository URL
SOURCE_URL_TEMPLATES = {
    "github": "{repo}/blob/{ref}/{path}",
    "gitlab": "{repo}/-/blob/{ref}/{path}",
    "bitbucket.org": "{repo}/src/{ref}/{path}",
}
COMMIT_URL_TEMPLATES = {
    "github": "{repo}/commit/{ref}",
    "gitlab": "{repo}/-/commit/{ref}",
    "bitbucket.org": "{repo}/commits/{ref}",
}

# Fenced ```mermaid blocks keep a "mermaid" class, so the TechDocs Mermaid
# addon detects every diagram (including ones with front matter)
MERMAID_FENCES = """markdown_extensions:
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
"""

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINK_TEXT_RE = re.compile(r"\[([^\]\n]+)\]\(\s*<?([^)\s<>#]+\.md)")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Lines that are not prose: headings, tables, quotes, list items, images, HTML, rules
_NON_PROSE_RE = re.compile(r"^(#|\||>|[-*+]\s|\d+[.)]\s|!\[|<|---|\*\*\*|___)")


@dataclass
class TechDocsResult:
    out_dir: Path
    pages: int
    commit: Optional[str]


def _load_metadata(docs_dir: Path) -> dict[str, Any]:
    try:
        with open(docs_dir / METADATA_FILENAME, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _humanize(name: str) -> str:
    return name.replace("_", " ")


def _nav_titles(titles: dict[str, str]) -> dict[str, str]:
    """Page titles without inline code marks, for the navigation."""
    return {stem: title.replace("`", "") for stem, title in titles.items()}


def _out_relpath(stem: str, rel: str) -> str:
    return INDEX_FILENAME if stem == OVERVIEW_STEM else rel


def normalize_repo_url(url: Optional[str]) -> Optional[str]:
    """https form of a git remote URL (SSH converted, credentials dropped), or None."""
    if not url:
        return None
    url = url.strip()
    # git@host:owner/repo.git -> https://host/owner/repo
    scp = re.match(r"^[\w.-]+@([\w.-]+):(?!//)(.+)$", url)
    if scp:
        url = f"https://{scp.group(1)}/{scp.group(2)}"
    # ssh://git@host[:port]/owner/repo.git -> https://host/owner/repo
    ssh = re.match(r"^ssh://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$", url)
    if ssh:
        url = f"https://{ssh.group(1)}/{ssh.group(2)}"
    if not url.startswith("https://"):
        return None
    url = re.sub(r"^https://[^@/]+@", "https://", url)  # drop credentials
    return re.sub(r"\.git$", "", url.rstrip("/"))


def detect_repo_url(repo_path: Optional[str]) -> Optional[str]:
    """https URL of the ``origin`` remote of ``repo_path``, or None."""
    if not repo_path or not os.path.isdir(repo_path):
        return None
    try:
        import git

        url = git.Repo(repo_path, search_parent_directories=True).remotes.origin.url
    except Exception:  # noqa: BLE001 — the remote URL is optional
        return None
    return normalize_repo_url(url)


def _host_key(repo_url: Optional[str], templates: dict[str, str]) -> Optional[str]:
    if not repo_url:
        return None
    return next((host for host in templates if host in repo_url), None)


def source_url_template(repo_url: Optional[str]) -> Optional[str]:
    host = _host_key(repo_url, SOURCE_URL_TEMPLATES)
    return SOURCE_URL_TEMPLATES[host] if host else None


def build_nav(level: dict[str, Any], pages: dict[str, str], titles: dict[str, str]) -> list[Any]:
    """MkDocs nav for one level of the module tree.

    A module with children becomes a section whose first entry is the
    module's own page, so ``auth.md`` and ``auth/`` show up as one item.
    """
    nav: list[Any] = []
    for name, info in level.items():
        title = titles.get(name) or _humanize(name)
        rel = pages.get(name)
        children = info.get("children") if isinstance(info, dict) else None
        child_nav = build_nav(children, pages, titles) if isinstance(children, dict) else []
        if child_nav:
            nav.append({title: ([{title: rel}] if rel else []) + child_nav})
        elif rel:
            nav.append({title: rel})
    return nav


def _nav_pages(nav: Any) -> set[str]:
    """Every page path referenced by a nav."""
    if isinstance(nav, str):
        return {nav}
    if isinstance(nav, list):
        return set().union(*(_nav_pages(item) for item in nav)) if nav else set()
    if isinstance(nav, dict):
        return set().union(*(_nav_pages(value) for value in nav.values())) if nav else set()
    return set()


def _module_components(module_tree: dict[str, Any]) -> dict[str, list[str]]:
    components: dict[str, list[str]] = {}
    stack = [module_tree]
    while stack:
        level = stack.pop()
        for name, info in level.items():
            if not isinstance(info, dict):
                continue
            components[name] = [c for c in info.get("components") or [] if isinstance(c, str)]
            if isinstance(info.get("children"), dict):
                stack.append(info["children"])
    return components


def _titled_link_text(text: str, titles: dict[str, str]) -> str:
    """Show page titles instead of module names in links (``[CLI_Utilities](...)``)."""

    def fix(m: re.Match) -> str:
        stem = os.path.basename(m.group(2))[: -len(".md")]
        label = m.group(1).strip("` ")
        if label in (stem, f"{stem}.md") and stem in titles:
            return m.group(0).replace(f"[{m.group(1)}]", f"[{titles[stem]}]", 1)
        return m.group(0)

    out = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        out.append(line if in_fence else _LINK_TEXT_RE.sub(fix, line))
    return "".join(out)


def _description(text: str) -> Optional[str]:
    """First prose paragraph after the H1, as plain text."""
    seen_h1 = False
    in_fence = False
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if paragraph:
                break
            continue
        if in_fence:
            continue
        if not seen_h1:
            seen_h1 = stripped.startswith("# ")
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if _NON_PROSE_RE.match(stripped):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    if not paragraph:
        return None
    plain = _MD_LINK_RE.sub(r"\1", " ".join(paragraph))
    plain = re.sub(r"[*_`]", "", plain)
    if len(plain) > MAX_DESCRIPTION_CHARS:
        plain = plain[: MAX_DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return plain


def _banner(info: dict[str, Any], repo_url: Optional[str]) -> str:
    commit = info.get("commit_id")
    date = str(info.get("timestamp") or "")[:10]
    model = info.get("main_model")
    parts = ["This page was written by CodeWiki from the source code"]
    if commit:
        short = f"`{commit[:8]}`"
        host = _host_key(repo_url, COMMIT_URL_TEMPLATES)
        if host:
            short = f"[{short}]({COMMIT_URL_TEMPLATES[host].format(repo=repo_url, ref=commit)})"
        parts.append(f"at commit {short}")
    if date:
        parts.append(f"on {date}")
    if model:
        parts.append(f"with `{model}`")
    return (
        '!!! info "Generated documentation"\n'
        f"    {' '.join(parts)}. Check the code before relying on details.\n"
    )


def _components_block(
    components: list[str], template: Optional[str], repo_url: Optional[str], commit: Optional[str]
) -> str:
    if not components:
        return ""
    lines = [f'??? abstract "Documented components ({len(components)})"']
    for component_id in components[:MAX_LISTED_COMPONENTS]:
        path, _, symbol = component_id.partition("::")
        label = f"`{symbol or os.path.basename(path)}`"
        if template and repo_url and commit:
            url = template.format(repo=repo_url, ref=commit, path=quote(path))
            lines.append(f"    - [{label}]({url}) — `{path}`")
        else:
            lines.append(f"    - {label} — `{path}`")
    if len(components) > MAX_LISTED_COMPONENTS:
        lines.append(f"    - … and {len(components) - MAX_LISTED_COMPONENTS} more")
    return "\n\n" + "\n".join(lines) + "\n"


def _insert_after_h1(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            return "".join(lines[: i + 1]) + "\n" + block + "".join(lines[i + 1 :])
    return block + "\n" + text


def _front_matter(title: str, description: Optional[str]) -> str:
    data = {"title": title}
    if description:
        data["description"] = description
    return (
        "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000) + "---\n\n"
    )


def check_output_dir(out_dir: Path, docs_dir: Path) -> None:
    """Raise ValueError unless an export to ``out_dir`` is safe; changes nothing.

    The output must not overlap the CodeWiki docs, and an existing
    ``<out_dir>/docs`` must carry the marker of a previous export.
    """
    out_dir, docs_dir = Path(out_dir), Path(docs_dir)
    out, source = out_dir.resolve(), docs_dir.resolve()
    if out.is_relative_to(source) or source.is_relative_to(out):
        raise ValueError(
            f"TechDocs output {out_dir} must be outside the CodeWiki docs {docs_dir} "
            "(and must not contain them)"
        )
    out_docs = out_dir / DOCS_SUBDIR
    if out_docs.exists() and not (out_docs / MARKER_FILENAME).exists():
        raise ValueError(
            f"{out_docs} exists and was not generated by CodeWiki; refusing to replace it"
        )


def _prepare_out_docs(out_dir: Path, docs_dir: Path) -> Path:
    """Create an empty ``<out_dir>/docs``, refusing to touch anything CodeWiki didn't write."""
    check_output_dir(out_dir, docs_dir)
    out_docs = out_dir / DOCS_SUBDIR
    if out_docs.exists():
        shutil.rmtree(out_docs)
    out_docs.mkdir(parents=True)
    (out_docs / MARKER_FILENAME).write_text("Generated by codewiki; replaced on every run.\n")
    return out_docs


def generate_techdocs(
    docs_dir: Path,
    out_dir: Path,
    site_name: Optional[str] = None,
    repo_url: Optional[str] = None,
    source_template: Optional[str] = None,
) -> TechDocsResult:
    """Write an MkDocs/TechDocs project for the CodeWiki docs in ``docs_dir``.

    Raises:
        FileNotFoundError: ``docs_dir`` holds no CodeWiki pages.
        ValueError: ``out_dir`` overlaps ``docs_dir``, or its ``docs/`` folder
            was not written by a previous export.
    """
    docs_dir, out_dir = Path(docs_dir), Path(out_dir)
    module_tree = load_module_tree(str(docs_dir))
    pages = list_doc_files(str(docs_dir), module_tree)
    if not pages:
        raise FileNotFoundError(f"No CodeWiki pages found in {docs_dir}")

    metadata = _load_metadata(docs_dir)
    info = metadata.get("generation_info") or {}
    commit = info.get("commit_id")
    repo_url = (
        normalize_repo_url(repo_url)
        or repo_url
        or detect_repo_url(info.get("repo_path"))
        or detect_repo_url(str(docs_dir))  # docs checked in to the repository
    )
    source_template = source_template or source_url_template(repo_url)
    site_name = (
        site_name or os.path.basename(str(info.get("repo_path") or "").rstrip("/")) or "Code Wiki"
    )

    out_pages = {stem: _out_relpath(stem, rel) for stem, rel in pages.items()}
    titles = extract_page_titles(str(docs_dir), module_tree)
    for stem, rel in pages.items():  # pages outside the module tree
        if stem not in titles and (title := _first_h1(str(docs_dir / rel))):
            titles[stem] = title
    components = _module_components(module_tree)
    banner = _banner(info, repo_url)

    out_docs = _prepare_out_docs(out_dir, docs_dir)

    overview_description = None
    for stem, rel in pages.items():
        out_rel = out_pages[stem]
        text = (docs_dir / rel).read_text(encoding="utf-8")
        text = _titled_link_text(text, titles)
        text = rewrite_page_links(text, out_rel, out_pages)
        description = _description(text)
        if stem == OVERVIEW_STEM:
            overview_description = description
        text = _insert_after_h1(text, banner)
        text = text.rstrip("\n") + _components_block(
            components.get(stem, []), source_template, repo_url, commit
        )
        title = titles.get(stem) or _humanize(stem)
        target = out_docs / out_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_front_matter(title, description) + text + "\n", encoding="utf-8")

    for root, dirs, files in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if d != TEMP_DIR and not d.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() in ASSET_EXTENSIONS:
                source = Path(root) / name
                target = out_docs / source.relative_to(docs_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    nav_titles = _nav_titles(titles)
    nav: list[Any] = []
    if OVERVIEW_STEM in out_pages:
        nav.append({nav_titles.get(OVERVIEW_STEM) or "Overview": INDEX_FILENAME})
    nav += build_nav(module_tree, out_pages, nav_titles)
    in_nav = _nav_pages(nav)
    for stem, rel in sorted(out_pages.items()):
        if rel not in in_nav:
            nav.append({nav_titles.get(stem) or _humanize(stem): rel})

    config: dict[str, Any] = {"site_name": site_name}
    if overview_description:
        config["site_description"] = overview_description
    if repo_url:
        config["repo_url"] = repo_url
    config["edit_uri"] = ""  # generated pages are not edited by hand
    config["docs_dir"] = DOCS_SUBDIR
    config["nav"] = nav
    config["plugins"] = ["techdocs-core"]
    mkdocs_yml = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=1000)
    (out_dir / MKDOCS_FILENAME).write_text(mkdocs_yml + MERMAID_FENCES, encoding="utf-8")

    return TechDocsResult(out_dir=out_dir, pages=len(pages), commit=commit)
