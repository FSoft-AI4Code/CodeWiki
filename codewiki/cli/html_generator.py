"""
HTML generator for GitHub Pages documentation viewer.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict, Any

from codewiki.cli.utils.errors import FileSystemError
from codewiki.cli.utils.fs import safe_write, safe_read
from codewiki.src.be.search_index import build_search_index
from codewiki.src.config import DEFAULT_QA_PORT, SEARCH_INDEX_FILENAME
from codewiki.src.language import language_tag

logger = logging.getLogger(__name__)

DOCS_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "docs"
MODULE_TREE_OWNERSHIP_VALIDATOR = "validate_module_tree_ownership.py"


class HTMLGenerator:
    """
    Generates static HTML documentation viewer for GitHub Pages.

    Creates a self-contained index.html with embedded styles, scripts,
    and configuration for client-side markdown rendering.
    """

    def __init__(self, template_dir: Optional[Path] = None):
        """
        Initialize HTML generator.

        Args:
            template_dir: Path to template directory (default: package templates)
        """
        if template_dir is None:
            # Use package templates
            template_dir = Path(__file__).parent.parent / "templates" / "github_pages"

        self.template_dir = Path(template_dir)

    def load_module_tree(self, docs_dir: Path) -> Dict[str, Any]:
        """
        Load module tree from documentation directory.

        Args:
            docs_dir: Documentation directory path

        Returns:
            Module tree structure
        """
        module_tree_path = docs_dir / "module_tree.json"
        if not module_tree_path.exists():
            # Fallback to a simple structure
            return {
                "Overview": {"description": "Repository overview", "components": [], "children": {}}
            }

        try:
            content = safe_read(module_tree_path)
            return json.loads(content)
        except Exception as e:
            raise FileSystemError(f"Failed to load module tree: {e}")

    def _write_search_index(self, docs_dir: Path) -> int:
        """Write search_index.json next to module_tree.json for the viewer's Ctrl+K search.

        A standalone file (not embedded in index.html) so the initial page
        load stays small — the viewer fetches it lazily, only when the
        search modal is first opened, the same way it already fetches
        individual .md files.

        Returns:
            Number of heading sections written to the index.
        """
        search_index = build_search_index(str(docs_dir))
        search_index_path = docs_dir / SEARCH_INDEX_FILENAME
        safe_write(search_index_path, json.dumps(search_index, ensure_ascii=False))
        section_count = len(search_index)
        logger.info(
            "Documentation search index: wrote %s (%d sections) for Ctrl+K lazy load",
            search_index_path.name,
            section_count,
        )
        return section_count

    def _copy_module_tree_ownership_validator(self, docs_dir: Path) -> None:
        """Copy ownership validator next to module_tree.json (best-effort)."""
        source = DOCS_TEMPLATES_DIR / MODULE_TREE_OWNERSHIP_VALIDATOR
        if not source.is_file():
            logger.warning(
                "Module tree ownership validator not found at %s; skipping copy",
                source,
            )
            return
        dest = docs_dir / MODULE_TREE_OWNERSHIP_VALIDATOR
        if source.resolve() == dest.resolve():
            return
        shutil.copyfile(source, dest)
        logger.info(
            "Copied %s into docs output for module_tree ownership checks",
            MODULE_TREE_OWNERSHIP_VALIDATOR,
        )

    def load_metadata(self, docs_dir: Path) -> Optional[Dict[str, Any]]:
        """
        Load metadata from documentation directory.

        Args:
            docs_dir: Documentation directory path

        Returns:
            Metadata dictionary or None if not found
        """
        metadata_path = docs_dir / "metadata.json"
        if not metadata_path.exists():
            return None

        try:
            content = safe_read(metadata_path)
            return json.loads(content)
        except Exception:
            # Non-critical, return None
            return None

    def generate(
        self,
        output_path: Path,
        title: str,
        module_tree: Optional[Dict[str, Any]] = None,
        repository_url: Optional[str] = None,
        github_pages_url: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        docs_dir: Optional[Path] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate HTML documentation viewer.

        Args:
            output_path: Output file path (index.html)
            title: Documentation title
            module_tree: Module tree structure (auto-loaded from docs_dir if not provided)
            repository_url: GitHub repository URL
            github_pages_url: Expected GitHub Pages URL
            config: Additional configuration
            docs_dir: Documentation directory (for auto-loading module_tree and metadata)
            metadata: Metadata dictionary (auto-loaded from docs_dir if not provided)

        Returns:
            Summary of generated viewer artifacts (paths and search index stats).
        """
        search_index_sections = 0
        search_index_written = False

        # Auto-load module_tree and metadata from docs_dir if not provided
        if docs_dir:
            if module_tree is None:
                module_tree = self.load_module_tree(docs_dir)
            if metadata is None:
                metadata = self.load_metadata(docs_dir)

        # Build the section-level search index from the .md files already on
        # disk. Best-effort: a failure here must not block index.html itself.
        if docs_dir:
            try:
                search_index_sections = self._write_search_index(docs_dir)
                search_index_written = True
            except Exception as e:  # noqa: BLE001 — search index is optional polish
                logger.warning("Search index generation skipped: %s", e)
            try:
                self._copy_module_tree_ownership_validator(docs_dir)
            except Exception as e:  # noqa: BLE001 — validator script is optional tooling
                logger.warning("Module tree ownership validator copy skipped: %s", e)

        # Default values
        if module_tree is None:
            module_tree = {}
        if config is None:
            config = {}

        # Load template
        template_path = self.template_dir / "viewer_template.html"
        if not template_path.exists():
            raise FileSystemError(f"Template not found: {template_path}")

        template_content = safe_read(template_path)

        # Build info content HTML
        info_content = self._build_info_content(metadata)
        show_info = "block" if info_content else "none"

        # Build repository link
        repo_link = ""
        if repository_url:
            repo_link = f'<a href="{repository_url}" class="repo-link" target="_blank">🔗 View Repository</a>'

        # Determine docs base path
        # For GitHub Pages: relative path to docs folder
        # For local: relative path to docs folder
        docs_base_path = ""
        if docs_dir and output_path.parent != docs_dir:
            # Calculate relative path from output to docs
            try:
                docs_base_path = Path(docs_dir.name).as_posix()
            except Exception:
                docs_base_path = "."

        # The Ask AI window talks to `codewiki serve` on the reader's machine;
        # citations link to the source at the docs' commit when the repo is known.
        commit_id = ((metadata or {}).get("generation_info") or {}).get("commit_id")
        config = {
            "repository_url": repository_url,
            "commit_id": commit_id,
            "chat": {"endpoints": [f"http://127.0.0.1:{DEFAULT_QA_PORT}"]},
            **config,
        }

        # Prepare JSON data for embedding (no "</script>" can close the tag early)
        config_json = json.dumps(config, indent=2).replace("</", "<\\/")
        module_tree_json = json.dumps(module_tree, indent=2)
        metadata_json = json.dumps(metadata, indent=2) if metadata else "null"

        # Docs written in another language: show their translated page titles
        # in the navigation (filenames stay equal to the module names)
        language = ((metadata or {}).get("generation_info") or {}).get("language")
        page_titles = {}
        if language and docs_dir:
            from codewiki.src.be.module_naming import extract_page_titles

            page_titles = extract_page_titles(str(docs_dir), module_tree)
        page_titles_json = json.dumps(page_titles, ensure_ascii=False).replace("</", "<\\/")

        # Where each page lives (nested folders or the docs root)
        doc_paths = {}
        if docs_dir:
            from codewiki.src.be.doc_layout import doc_path_map

            doc_paths = doc_path_map(str(docs_dir), module_tree)
        doc_paths_json = json.dumps(doc_paths, ensure_ascii=False).replace("</", "<\\/")

        # Replace placeholders
        html_content = template_content
        replacements = {
            "{{TITLE}}": self._escape_html(title),
            "{{HTML_LANG}}": language_tag(language),
            "{{PAGE_TITLES_JSON}}": page_titles_json,
            "{{DOC_PATHS_JSON}}": doc_paths_json,
            "{{REPO_LINK}}": repo_link,
            "{{SHOW_INFO}}": show_info,
            "{{INFO_CONTENT}}": info_content,
            "{{CONFIG_JSON}}": config_json,
            "{{MODULE_TREE_JSON}}": module_tree_json,
            "{{METADATA_JSON}}": metadata_json,
            "{{DOCS_BASE_PATH}}": docs_base_path,
        }

        for placeholder, value in replacements.items():
            html_content = html_content.replace(placeholder, value)

        # Write output
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        safe_write(output_path, html_content)

        # Keep the branded viewer self-contained when it is published elsewhere.
        icon_path = self.template_dir / "codewiki-icon.png"
        icon_output_path = output_path.parent / "codewiki-icon.png"
        if icon_path.exists() and icon_path.resolve() != icon_output_path.resolve():
            shutil.copyfile(icon_path, icon_output_path)

        logger.info(
            "GitHub Pages viewer: wrapped %s into %s (search at top of content area)",
            template_path.name,
            output_path.name,
        )

        return {
            "index_html": str(output_path.resolve()),
            "template": str(template_path.resolve()),
            "search_index_written": search_index_written,
            "search_index_sections": search_index_sections,
            "search_index_filename": SEARCH_INDEX_FILENAME if search_index_written else None,
        }

    def _build_info_content(self, metadata: Optional[Dict[str, Any]]) -> str:
        """
        Build HTML content for repo info section.

        Args:
            metadata: Metadata dictionary

        Returns:
            HTML string for info content
        """
        if not metadata or not metadata.get("generation_info"):
            return ""

        info = metadata.get("generation_info", {})
        stats = metadata.get("statistics", {})

        html_parts = []

        if info.get("main_model"):
            html_parts.append(
                f'<div class="info-row"><strong>Model:</strong> {self._escape_html(info["main_model"])}</div>'
            )

        if info.get("timestamp"):
            try:
                from datetime import datetime

                timestamp = info["timestamp"]
                # Parse ISO format timestamp
                if isinstance(timestamp, str):
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    formatted_date = dt.strftime("%Y-%m-%d")
                    html_parts.append(
                        f'<div class="info-row"><strong>Generated:</strong> {formatted_date}</div>'
                    )
            except Exception:
                pass

        if info.get("commit_id"):
            commit_short = info["commit_id"][:8]
            html_parts.append(
                f'<div class="info-row"><strong>Commit:</strong> {commit_short}</div>'
            )

        if stats.get("total_components"):
            components_str = f"{stats['total_components']:,}"
            html_parts.append(
                f'<div class="info-row"><strong>Components:</strong> {components_str}</div>'
            )

        if stats.get("max_depth"):
            html_parts.append(
                f'<div class="info-row"><strong>Max Depth:</strong> {stats["max_depth"]}</div>'
            )

        return "\n                ".join(html_parts)

    def _escape_html(self, text: str) -> str:
        """
        Escape HTML special characters.

        Args:
            text: Text to escape

        Returns:
            Escaped text
        """
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def detect_repository_info(self, repo_path: Path) -> Dict[str, Optional[str]]:
        """
        Detect repository information from git.

        Args:
            repo_path: Repository path

        Returns:
            Dictionary with 'name', 'url', 'github_pages_url'
        """
        info = {
            "name": repo_path.name,
            "url": None,
            "github_pages_url": None,
        }

        try:
            import git

            repo = git.Repo(repo_path)

            # Get repository name
            info["name"] = repo_path.name

            # Get remote URL
            if repo.remotes:
                remote_url = repo.remotes.origin.url

                # Clean URL
                if remote_url.startswith("git@github.com:"):
                    remote_url = remote_url.replace("git@github.com:", "https://github.com/")

                remote_url = remote_url.rstrip("/").replace(".git", "")
                info["url"] = remote_url

                # Compute GitHub Pages URL
                if "github.com" in remote_url:
                    parts = remote_url.split("/")
                    if len(parts) >= 2:
                        owner = parts[-2]
                        repo = parts[-1]
                        info["github_pages_url"] = f"https://{owner}.github.io/{repo}/"

        except Exception:
            pass

        return info
