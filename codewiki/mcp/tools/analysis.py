"""MCP tool: analyze_repo — parse a repository and build the dependency graph.

This is the entry-point tool for the IDE-driven wiki generation pipeline.
It runs CodeWiki's Tree-sitter-based dependency analyzer (no LLM needed),
caches the results in a new session, and writes the full component index,
leaf nodes, and other analysis data to files on disk.  The IDE agent reads
those files directly instead of receiving large payloads over stdio.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.mcp.workspace import SessionWorkspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Incremental update: detect changes since last generation
# ---------------------------------------------------------------------------

def _detect_changes(
    repo_path: Path,
    output_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Detect changes since last documentation generation.

    Returns a changes dict with affected modules, or None if no previous
    generation exists (first run).

    Detection strategy:
      1. Git-based: compare stored commit_id with current HEAD, plus check
         uncommitted changes via ``git status``.
      2. Fallback: compare file mtime with stored ``timestamp`` in metadata.
    """
    metadata_path = output_dir / "metadata.json"
    module_tree_path = output_dir / "module_tree.json"

    if not metadata_path.exists() or not module_tree_path.exists():
        return None

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        module_tree = json.loads(module_tree_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None

    # Try git-based detection first
    changes = _detect_via_git(repo_path, metadata, output_dir)

    # Fallback to mtime-based detection
    if changes is None:
        changes = _detect_via_mtime(repo_path, metadata)

    if changes is None:
        return None

    changed_files = changes["changed_files"]
    if not changed_files:
        return {
            "has_previous": True,
            "no_changes": True,
            "method": changes.get("method", "unknown"),
            "message": "No changes detected since last generation. Documentation is up to date.",
        }

    affected, cascade = _find_affected_modules(module_tree, changed_files)

    return {
        "has_previous": True,
        "no_changes": False,
        "method": changes.get("method", "unknown"),
        "changed_files": changed_files,
        "affected_modules": sorted(affected),
        "cascade_modules": sorted(cascade),
        "hint": (
            f"Only {len(affected)} module(s) need updating: {sorted(affected)}. "
            f"Parent modules to refresh: {sorted(cascade)}. "
            "Use edit_doc_file for targeted updates, write_doc_file for new modules."
        ),
    }


def _detect_via_git(
    repo_path: Path,
    metadata: Dict[str, Any],
    output_dir: Path | None = None,
) -> Optional[Dict[str, Any]]:
    """Detect changes via git. Returns None if not in a git repo or if no
    previous commit is recorded (so the caller can fall through to mtime).

    Checks committed changes (diff against stored commit_id), staged changes
    (``index.diff('HEAD')``), and unstaged/untracked changes.
    """
    try:
        import git
        repo = git.Repo(repo_path, search_parent_directories=True)
    except Exception:
        return None

    prev_commit = metadata.get("generation_info", {}).get("commit_id")
    if not prev_commit:
        return None  # No baseline to compare; let mtime fallback handle it

    try:
        current_commit = repo.head.commit.hexsha
    except Exception:
        return None

    # Compute subpath prefix for monorepo support.
    # Git diff returns paths relative to the git root, but component IDs
    # use paths relative to repo_path.  Strip the prefix so they align.
    git_root = Path(repo.working_dir).resolve()
    repo_root = repo_path.resolve()
    try:
        subpath = repo_root.relative_to(git_root).as_posix()
    except ValueError:
        subpath = ""
    if subpath == ".":
        subpath = ""

    # Output-dir prefix (relative to repo_path) so generated docs, metadata
    # and session workspace files never count as source changes.
    output_dir_rel = ""
    if output_dir is not None:
        try:
            output_dir_rel = Path(output_dir).resolve().relative_to(repo_root).as_posix()
            if output_dir_rel == ".":
                output_dir_rel = ""
        except (ValueError, TypeError):
            pass

    def _normalize(p: str) -> Optional[str]:
        """Strip the monorepo subpath and drop generated/non-source paths."""
        if subpath:
            if not p.startswith(subpath + "/"):
                return None  # outside target subdirectory
            p = p[len(subpath) + 1:]
        if p.startswith(".codewiki/"):
            return None
        if output_dir_rel and (p == output_dir_rel or p.startswith(output_dir_rel + "/")):
            return None
        return p

    changed: list[str] = []
    seen: set[str] = set()

    def _add(raw: Optional[str]) -> None:
        if raw:
            p = _normalize(raw)
            if p and p not in seen:
                changed.append(p)
                seen.add(p)

    # 1) Committed changes since last generation
    if prev_commit != current_commit:
        try:
            diff_index = repo.commit(prev_commit).diff(current_commit)
        except Exception:
            # Baseline commit unreachable (shallow clone, rebase, gc).
            # Committed changes can't be enumerated, and returning an empty
            # list here would falsely report "up to date" on a clean tree —
            # fall back to mtime detection instead.
            logger.warning(
                "Stored commit %s is unreachable in %s; falling back to mtime detection",
                prev_commit, repo_path,
            )
            return None
        for diff in diff_index:
            for p in (diff.a_path, diff.b_path):
                _add(p)

    # 2) Uncommitted changes: staged (index vs HEAD) + unstaged (working tree vs index) + untracked
    try:
        for d in list(repo.index.diff("HEAD")) + list(repo.index.diff(None)):
            _add(d.a_path)
            _add(d.b_path)
        for item in repo.untracked_files:
            _add(item)
    except Exception:
        pass

    return {"changed_files": changed, "method": "git"}


def _detect_via_mtime(
    repo_path: Path,
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Fallback: detect changed files by comparing mtime with generation timestamp."""
    timestamp_str = metadata.get("generation_info", {}).get("timestamp")
    if not timestamp_str:
        return None

    try:
        from datetime import datetime
        prev_time = datetime.fromisoformat(timestamp_str).timestamp()
    except (ValueError, TypeError):
        return None

    # Language extensions recognized by CodeWiki
    source_extensions = {
        ".py", ".java", ".js", ".jsx", ".ts", ".tsx",
        ".c", ".h", ".cpp", ".hpp", ".cc", ".hh",
        ".cs", ".kt", ".kts",
    }

    changed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        # Skip hidden dirs and common non-source dirs
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".venv")
        ]
        for filename in filenames:
            filepath = Path(dirpath) / filename
            if filepath.suffix.lower() not in source_extensions:
                continue
            try:
                if filepath.stat().st_mtime > prev_time:
                    rel_path = filepath.relative_to(repo_path).as_posix()
                    changed.append(rel_path)
            except OSError:
                continue

    return {"changed_files": changed, "method": "mtime"}


def _find_affected_modules(
    module_tree: Dict[str, Any],
    changed_files: List[str],
) -> Tuple[set, set]:
    """Map changed files to affected modules using module_tree.json.

    Uses substring matching (same as the CLI ``_invalidate_affected_modules``).
    Returns (affected_modules, cascade_parent_modules).
    """
    affected: set[str] = set()
    cascade: set[str] = set()

    def _walk(tree: Dict, parents: list[str] | None = None):
        if parents is None:
            parents = []
        for mod_name, mod_info in tree.items():
            components = mod_info.get("components", [])
            hit = False
            for comp in components:
                comp_file = comp.split("::")[0]
                for cf in changed_files:
                    if comp_file == cf or comp_file.endswith("/" + cf) or cf.endswith("/" + comp_file):
                        hit = True
                        break
                    # Changed dir contains the component file, or vice versa
                    if cf.startswith(comp_file + "/") or comp_file.startswith(cf + "/"):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                affected.add(mod_name)
                cascade.update(parents)

            children = mod_info.get("children", {})
            if isinstance(children, dict) and children:
                _walk(children, parents + [mod_name])

    _walk(module_tree)

    # overview.md depends on all child docs, always refresh if anything changed
    if affected:
        cascade.add("overview")

    return affected, cascade


def handle_analyze_repo(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Run the dependency analysis, write results to workspace files,
    and return a compact summary with file paths."""
    repo_path = Path(arguments["repo_path"]).expanduser().resolve()
    if not repo_path.exists():
        return json.dumps({"error": f"Repository not found: {repo_path}"})

    output_dir = Path(arguments.get("output_dir", str(repo_path / "docs"))).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build a minimal Config for the dependency analyzer (no LLM fields used)
    from codewiki.src.config import Config
    config = Config(
        repo_path=str(repo_path),
        output_dir=str(output_dir / "temp"),
        dependency_graph_dir=str(output_dir / "temp" / "dependency_graphs"),
        docs_dir=str(output_dir),
        max_depth=2,
        llm_base_url="not-needed",
        llm_api_key="not-needed",
        main_model="unused",
        cluster_model="unused",
    )

    # Apply optional include/exclude patterns
    include = arguments.get("include_patterns")
    exclude = arguments.get("exclude_patterns")
    if include or exclude:
        agent_instructions: Dict[str, Any] = {}
        if include:
            agent_instructions["include_patterns"] = [p.strip() for p in include.split(",")]
        if exclude:
            agent_instructions["exclude_patterns"] = [p.strip() for p in exclude.split(",")]
        config.agent_instructions = agent_instructions

    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
    builder = DependencyGraphBuilder(config)
    components, leaf_nodes = builder.build_dependency_graph()

    # Create the session (generates session_id)
    session = store.create(
        repo_path=str(repo_path),
        output_dir=str(output_dir),
        components=components,
        leaf_nodes=leaf_nodes,
    )

    # Record the analyzed commit now — close_session uses it as the
    # incremental-update baseline in metadata.json.
    from codewiki.cli.utils.repo_validator import get_git_commit_hash
    session.analyzed_commit = get_git_commit_hash(repo_path) or None

    # Create the workspace with the real session_id
    workspace = SessionWorkspace(repo_path, session.session_id)
    session.workspace = workspace

    # -- Write full data to workspace files --

    # 1. Full component index (no pagination)
    component_index: list[dict] = []
    for comp_id, node in components.items():
        component_index.append({
            "id": comp_id,
            "type": getattr(node, "component_type", "unknown"),
            "file": getattr(node, "relative_path", ""),
        })
    workspace.write_json("component_index.json", component_index)

    # 2. Full leaf nodes list
    workspace.write_json("leaf_nodes.json", leaf_nodes)

    # 3. Language stats
    languages: Dict[str, int] = {}
    for node in components.values():
        lang = getattr(node, "language", "unknown")
        languages[lang] = languages.get(lang, 0) + 1
    workspace.write_json("languages.json", languages)

    # 4. Incremental update: detect changes since last generation
    changes = _detect_changes(repo_path, output_dir)
    if changes is not None:
        workspace.write_json("changes.json", changes)

    # 5. Summary with preview for quick reference
    summary = {
        "session_id": session.session_id,
        "repo_name": repo_path.name,
        "repo_path": str(repo_path),
        "output_dir": str(output_dir),
        "total_components": len(components),
        "total_leaf_nodes": len(leaf_nodes),
        "languages": languages,
        "leaf_nodes_preview": leaf_nodes[:20],
    }
    workspace.write_json("summary.json", summary)

    # -- Return compact MCP response --
    result = {
        "session_id": session.session_id,
        "workspace_dir": str(workspace.root),
        "repo_name": repo_path.name,
        "output_dir": str(output_dir),
        "stats": {
            "total_components": len(components),
            "total_leaf_nodes": len(leaf_nodes),
            "languages": languages,
        },
        "files": {
            "component_index": str(workspace.root / "component_index.json"),
            "leaf_nodes": str(workspace.root / "leaf_nodes.json"),
            "languages": str(workspace.root / "languages.json"),
            "summary": str(workspace.root / "summary.json"),
        },
        "changes": changes,
        "hint": (
            "Read the files above for full data. "
            "Use read_code_components(session_id, component_ids) to read source code. "
            "Use save_module_tree(session_id, module_tree) after clustering. "
            "Call get_prompt('cluster') for clustering rules."
        ),
    }
    if changes and not changes.get("no_changes"):
        result["hint"] = (
            "Incremental update detected. Only update affected modules listed in "
            "'changes.affected_modules'. Use edit_doc_file for targeted updates. "
            "Refresh cascade parent modules in 'changes.cascade_modules'."
        )
    return json.dumps(result, indent=2, ensure_ascii=False)
