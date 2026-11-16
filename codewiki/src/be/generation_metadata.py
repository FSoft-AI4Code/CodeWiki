"""Generation context must describe artifacts produced by the current run."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from codewiki.src.be.doc_layout import config_layout, list_doc_files
from codewiki.src.config import MODULE_TREE_FILENAME


class GenerationArtifacts:
    """Remember existing pages so a failed attempt cannot relabel untouched docs."""

    def __init__(self, docs_dir: str | Path):
        self.docs_dir = Path(docs_dir)
        self.pages = self._pages()
        self.had_tree = (self.docs_dir / MODULE_TREE_FILENAME).exists()

    def _pages(self) -> dict[str, bytes | None]:
        pages = {}
        for rel in list_doc_files(str(self.docs_dir)).values():
            try:
                pages[rel] = hashlib.sha256((self.docs_dir / rel).read_bytes()).digest()
            except OSError:
                # An unreadable page is not evidence of a write by this run.
                pages[rel] = None
        return pages

    def produced_artifacts(self) -> bool:
        if not self.pages and not self.had_tree and (self.docs_dir / MODULE_TREE_FILENAME).exists():
            return True
        return any(
            digest is not None
            and (
                rel not in self.pages or (self.pages[rel] is not None and self.pages[rel] != digest)
            )
            for rel, digest in self._pages().items()
        )

    def add_generation_info(self, metadata: dict[str, Any], config: Any) -> None:
        if not self.produced_artifacts():
            return
        info = metadata.get("generation_info")
        if not isinstance(info, dict):
            info = {}
        # A recorded null language means English; only absent keys are unknown.
        info.setdefault("language", getattr(config, "language", None))
        info.setdefault("layout", config_layout(config))
        metadata["generation_info"] = info


def save_metadata(path: str | Path, metadata: dict[str, Any]) -> None:
    """Replace metadata atomically, retaining the previous file on write failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".metadata-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(metadata, stream, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
