"""A tiny repository + generated-docs directory for the Q&A tests.

The dependency graph is produced by CodeWiki's own analyzer, so the index and
the stale-file overlay are tested against real parser output.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

SIGNER = '''"""Signing helpers."""

DEFAULT_SEP = "."


class Signer:
    """Signs values with a secret key."""

    def __init__(self, secret_key, sep=DEFAULT_SEP):
        self.secret_key = secret_key
        self.sep = sep

    def get_signature(self, value):
        """Return the HMAC signature of value."""
        return str(hash((self.secret_key, value)))

    def sign(self, value):
        return value + self.sep + self.get_signature(value)

    def unsign(self, signed_value):
        """Verify the signature and return the original value."""
        value, _, sig = signed_value.rpartition(self.sep)
        if sig != self.get_signature(value):
            raise ValueError("bad signature")
        return value
'''

TIMED = '''"""Signatures that expire."""

import time

from pkg.signer import Signer

MAX_AGE = 30


class TimestampSigner(Signer):
    """A signer that also records when the value was signed."""

    def get_timestamp(self):
        return int(time.time())

    def unsign(self, signed_value, max_age=MAX_AGE):
        """Check the signature, then reject values older than max_age seconds."""
        value = super().unsign(signed_value)
        value, _, stamp = value.rpartition(self.sep)
        if self.get_timestamp() - int(stamp) > max_age:
            raise ValueError("signature expired")
        return value


def legacy_window():
    MAX_AGE = 5  # a local of the same name, not the module constant
    return MAX_AGE


def configured_age(settings):
    return getattr(settings, "MAX_AGE", 0) or default_age()


def default_age():
    # MAX_AGE is the fallback
    return MAX_AGE
'''

SIGNING_MD = """# Signing

The Signing module turns a secret key into tamper-proof values.

## How unsign verifies a value

`Signer.unsign` splits off the signature and compares it with a fresh HMAC.

## Configuration

The separator defaults to `DEFAULT_SEP`.
"""

TIMED_MD = """# Timed

Timed signatures expire after a maximum age.

## Expiry check

`TimestampSigner.unsign` rejects values older than `MAX_AGE` seconds.
"""

OVERVIEW_MD = """# demo

demo is a toy signing library used by the tests.

## Architecture

Signing produces signatures; Timed adds expiry on top.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def build(tmp_path: Path, use_git: bool = True) -> tuple[Path, Path]:
    """Create ``repo/`` with ``repo/docs`` generated at its first commit."""
    from codewiki.src.be.dependency_analyzer.ast_parser import DependencyParser
    from codewiki.src.be.updater.graph_store import save_graph

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text(
        'from pkg.timed import MAX_AGE\n\n__all__ = ["MAX_AGE"]\n'
    )
    (repo / "config").mkdir()
    (repo / "config" / "settings.yaml").write_text("MAX_AGE: 30\n")
    (repo / "pkg" / "signer.py").write_text(SIGNER)
    (repo / "pkg" / "timed.py").write_text(TIMED)
    (repo / "tests" / "test_timed.py").write_text(
        "from pkg.timed import MAX_AGE\n\n\ndef test_default():\n    assert MAX_AGE == 30\n"
    )
    commit = None
    if use_git:
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")
        commit = git(repo, "rev-parse", "HEAD")

    graph = DependencyParser(str(repo), include_patterns=["pkg/*.py"]).parse_repository()
    docs = repo / "docs"
    (docs / "temp" / "dependency_graphs").mkdir(parents=True)
    save_graph(graph, str(docs / "temp" / "dependency_graphs" / "repo_dependency_graph.json"))

    signer_ids = sorted(c for c in graph if c.startswith("pkg/signer.py"))
    timed_ids = sorted(c for c in graph if c.startswith("pkg/timed.py"))
    tree = {
        "Signing": {"path": "pkg", "components": signer_ids, "children": {}},
        "Timed": {"path": "pkg", "components": timed_ids, "children": {}},
    }
    (docs / "module_tree.json").write_text(json.dumps(tree))
    (docs / "Signing.md").write_text(SIGNING_MD)
    (docs / "Timed.md").write_text(TIMED_MD)
    (docs / "overview.md").write_text(OVERVIEW_MD)
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": commit,
                    "repo_path": str(repo),
                    "timestamp": datetime.now().isoformat(),
                    "layout": "flat",
                }
            }
        )
    )
    if use_git:
        # The docs are committed with the code, like a real repository.
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "docs")
    return repo, docs
