"""
CLI adapter for documentation generator backend.

This adapter wraps the existing backend documentation_generator.py
and provides CLI-specific functionality like progress reporting.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from codewiki.cli.models.job import DocumentationJob, LLMConfig
from codewiki.cli.utils.errors import APIError, IncompleteGenerationError
from codewiki.cli.utils.progress import ProgressTracker
from codewiki.src.be.doc_layout import list_doc_files

# Import backend modules
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_LAYOUT,
    DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
    set_cli_context,
)
from codewiki.src.config import Config as BackendConfig
from codewiki.src.utils import portable_repo_path


def _generated_files(working_dir: str) -> list[str]:
    """Docs-relative paths of the pages and the JSON files in the docs root."""
    pages = sorted(list_doc_files(working_dir).values())
    jsons = sorted(f for f in os.listdir(working_dir) if f.endswith(".json"))
    return pages + jsons


def _committed_and_clean(path: Path) -> bool:
    """True when git tracks files under ``path`` and none of them (nor untracked ones) changed."""
    import subprocess

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if not top or Path(top.strip()).resolve() == path.resolve():
        return False  # docs written into the repo root: never delete that
    tracked = git("ls-files", "--", ".")
    status = git("status", "--porcelain", "--untracked-files=all", "--", ".")
    return bool(tracked and tracked.strip()) and status is not None and not status.strip()


def _is_repo_root(path: Path) -> bool:
    """True for a git checkout's top folder (never cleared or moved wholesale)."""
    if (path / ".git").exists():
        return True
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and Path(out.stdout.strip()).resolve() == path.resolve()


def _module_file_count(module_info: dict) -> int:
    """Distinct source files in a module and its sub-modules.

    Component ids are ``<file path>::<name>``; a super-group keeps its
    components in its children, so they are counted too.
    """
    files: set[str] = set()
    stack = [module_info]
    while stack:
        info = stack.pop()
        if not isinstance(info, dict):
            continue
        files.update(str(cid).split("::", 1)[0] for cid in info.get("components") or [])
        children = info.get("children")
        if isinstance(children, dict):
            stack.extend(children.values())
    return len(files)


class CLIDocumentationGenerator:
    """
    CLI adapter for documentation generation with progress reporting.

    This class wraps the backend documentation generator and adds
    CLI-specific features like progress tracking and error handling.
    """

    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        config: dict[str, Any],
        verbose: bool = False,
        generate_html: bool = False,
        commit_id: str | None = None,
        techdocs_dir: Path | None = None,
    ):
        """
        Initialize the CLI documentation generator.

        Args:
            repo_path: Repository path
            output_dir: Output directory
            config: LLM configuration
            verbose: Enable verbose output
            generate_html: Whether to generate HTML viewer
            techdocs_dir: Export a Backstage TechDocs project here (None: don't)
            commit_id: Git commit SHA for incremental update tracking
        """
        self.repo_path = repo_path
        self.output_dir = output_dir
        self.config = config
        self.verbose = verbose
        self.generate_html = generate_html
        self.techdocs_dir = techdocs_dir
        self.techdocs_result = None
        self.commit_id = commit_id
        self.progress_tracker = ProgressTracker(total_stages=5, verbose=verbose)
        self.job = DocumentationJob()
        self._usage_ledger = None

        # Setup job metadata
        self.job.repository_path = str(repo_path)
        self.job.repository_name = repo_path.name
        self.job.output_directory = str(output_dir)
        self.job.llm_config = LLMConfig(
            main_model=config.get("main_model", ""),
            cluster_model=config.get("cluster_model", ""),
            base_url=config.get("base_url", ""),
        )

        # Configure backend logging
        self._configure_backend_logging()

    def _configure_backend_logging(self):
        """Configure backend logger for CLI use with colored output."""
        from codewiki.src.be.dependency_analyzer.utils.logging_config import ColoredFormatter

        # Get backend logger (parent of all backend modules)
        backend_logger = logging.getLogger("codewiki.src.be")

        # Remove existing handlers to avoid duplicates
        backend_logger.handlers.clear()

        if self.verbose:
            # In verbose mode, show INFO and above
            backend_logger.setLevel(logging.INFO)

            # Create console handler with formatting
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)

            # Use colored formatter for better readability
            colored_formatter = ColoredFormatter()
            console_handler.setFormatter(colored_formatter)

            # Add handler to logger
            backend_logger.addHandler(console_handler)
        else:
            # In non-verbose mode, suppress backend logs (use WARNING level to hide INFO/DEBUG)
            backend_logger.setLevel(logging.WARNING)

            # Create console handler for warnings and errors only
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.WARNING)

            # Use colored formatter even for warnings/errors
            colored_formatter = ColoredFormatter()
            console_handler.setFormatter(colored_formatter)

            backend_logger.addHandler(console_handler)

            # Keep the progress lines readable: without this, every claude /
            # codex session prints its transcript (caw's default display) and
            # FastMCP logs each MCP session at INFO, interleaved across the
            # concurrent agents. CAW_LOG=short|full still opts back in.
            logging.getLogger("mcp").setLevel(logging.WARNING)
            if "CAW_LOG" not in os.environ:
                from codewiki._vendor.caw import set_global_display

                set_global_display(None)

        # Prevent propagation to root logger to avoid duplicate messages
        backend_logger.propagate = False

    def generate(self) -> DocumentationJob:
        """
        Generate documentation with progress tracking.

        Returns:
            Completed DocumentationJob

        Raises:
            APIError: If LLM API call fails
        """
        self.job.start()

        try:
            # Set CLI context for backend
            set_cli_context(True)

            # Create backend config with CLI settings
            backend_config = BackendConfig.from_cli(
                repo_path=str(self.repo_path),
                output_dir=str(self.output_dir),
                llm_base_url=self.config.get("base_url"),
                llm_api_key=self.config.get("api_key"),
                main_model=self.config.get("main_model"),
                cluster_model=self.config.get("cluster_model"),
                fallback_model=self.config.get("fallback_model"),
                provider=self.config.get("provider", "openai-compatible"),
                aws_region=self.config.get("aws_region", "us-east-1"),
                google_project=self.config.get("google_project", ""),
                google_location=self.config.get("google_location", ""),
                max_tokens=self.config.get("max_tokens", 32768),
                min_token_per_module=self.config.get("min_token_per_module", 1000),
                max_token_per_module=self.config.get("max_token_per_module", 36369),
                max_token_per_leaf_module=self.config.get(
                    "max_token_per_leaf_module", DEFAULT_MAX_TOKEN_PER_LEAF_MODULE
                ),
                max_leaf_nodes_per_cluster=self.config.get("max_leaf_nodes_per_cluster", 600),
                max_depth=self.config.get("max_depth", 2),
                request_limit=self.config.get("request_limit", 100),
                agent_retries=self.config.get("agent_retries", 3),
                concurrency=self.config.get("concurrency", DEFAULT_CONCURRENCY),
                max_context_tokens=self.config.get("max_context_tokens", 0),
                agent_instructions=self.config.get("agent_instructions"),
                use_gitignore=self.config.get("use_gitignore", True),
                prompt_caching=self.config.get("prompt_caching", True),
                pricing=self.config.get("pricing", {}),
                artifacts_enabled=self.config.get("artifacts_enabled", True),
                artifact_token_budget=self.config.get("artifact_token_budget", 200_000),
                with_prose=self.config.get("with_prose", False),
                layout=self.config.get("layout", DEFAULT_LAYOUT),
                crosslinks_enabled=self.config.get("crosslinks_enabled", True),
            )

            # Run backend documentation generation
            asyncio.run(self._run_backend_generation(backend_config))
            self._record_usage_statistics()

            # Stage 4: HTML Generation (optional)
            if self.generate_html:
                self._run_html_generation()

            if self.techdocs_dir is not None:
                self._run_techdocs_generation()

            # Stage 5: Finalization (metadata already created by backend)
            self._finalize_job()

            # Complete job
            self._finish_usage()
            self.job.complete()

            return self.job

        except APIError as e:
            self.job.fail(str(e))
            self._finish_usage(incomplete=True)
            raise
        except Exception as e:
            self.job.fail(str(e))
            self._finish_usage(incomplete=True)
            raise

    def _docs_commit(self) -> str | None:
        """The commit the existing docs were generated from (metadata.json), if known."""
        import json

        try:
            with open(self.output_dir / "metadata.json", encoding="utf-8") as f:
                return (json.load(f).get("generation_info") or {}).get("commit_id")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    async def _run_backend_generation(self, backend_config: BackendConfig):
        """Run the backend documentation generation with progress tracking."""

        # Stage 1: Dependency Analysis
        self.progress_tracker.start_stage(1, "Dependency Analysis")
        if self.verbose:
            self.progress_tracker.update_stage(0.2, "Initializing dependency analyzer...")

        # Create documentation generator
        doc_generator = DocumentationGenerator(backend_config, commit_id=self.commit_id)
        doc_generator.progress_emit = self.progress_tracker.note
        self._usage_ledger = doc_generator.usage_ledger

        # Incremental update (component-level): keep the previous graph before
        # the builder overwrites it, so the updater can diff old against new.
        update_opts = self._update_options()
        prev_graph_path = None
        if self.config.get("no_cache") and update_opts is None:
            self._start_fresh()
        if update_opts is not None:
            from codewiki.src.be.updater.graph_store import (
                rebuild_graph_at_commit,
                snapshot_old_graph,
            )

            # Decide before anything below writes into docs/: if they are committed and
            # unmodified now, git holds exactly them, so a full build needs no backup copy.
            self._docs_clean_at_start = _committed_and_clean(self.output_dir)
            if not self._docs_clean_at_start:
                # Not in git: back them up now, while they are still the previous docs (the
                # update deletes and rewrites pages before it may fall back to a full build).
                self._backup_docs()

            prev_graph_path = snapshot_old_graph(
                backend_config.dependency_graph_dir, str(self.repo_path)
            )
            # docs/temp/ is git-ignored, so a fresh clone has no previous graph: rebuild it
            # from the docs' commit (no LLM) instead of falling back to a full rebuild.
            docs_commit = self._docs_commit()
            if prev_graph_path is None and docs_commit:
                if self.verbose:
                    self.progress_tracker.update_stage(
                        0.1, "Rebuilding the previous dependency graph from the docs commit..."
                    )
                prev_graph_path = rebuild_graph_at_commit(backend_config, docs_commit)

        if self.verbose:
            self.progress_tracker.update_stage(0.5, "Parsing source files...")

        # Build dependency graph
        try:
            components, leaf_nodes = doc_generator.graph_builder.build_dependency_graph()
            # Trees saved by an older CodeWiki may not cover every file the analysis finds now.
            from codewiki.src.be.module_scope import fit_saved_trees

            fit_saved_trees(str(self.output_dir.absolute()), components)
            self.job.statistics.total_files_analyzed = len(components)
            self.job.statistics.leaf_nodes = len(leaf_nodes)

            if self.verbose:
                self.progress_tracker.update_stage(
                    0.8, f"Analyzed {len(components)} files, found {len(leaf_nodes)} leaf nodes"
                )
                # Log individual files analyzed
                for comp_name in sorted(components.keys())[:20]:
                    self.progress_tracker.update_stage(0.9, f"  File: {comp_name}")
                if len(components) > 20:
                    self.progress_tracker.update_stage(
                        0.9, f"  ... and {len(components) - 20} more files"
                    )
        except Exception as e:  # noqa: BLE001 — surfaced to the user as an APIError
            raise APIError(f"Dependency analysis failed: {e}")

        self.progress_tracker.complete_stage()

        if update_opts is not None:
            outcome = await self._run_incremental_update(
                backend_config, doc_generator, update_opts, prev_graph_path, components, leaf_nodes
            )
            if outcome in ("incremental", "no_change"):
                # The builder wrote the new graph under this checkout's name. Drop graphs
                # left by earlier checkouts so the next update has exactly one to pick.
                from codewiki.src.be.updater.graph_store import (
                    graph_file_path,
                    prune_superseded_graphs,
                )

                prune_superseded_graphs(
                    backend_config.dependency_graph_dir,
                    keep=graph_file_path(backend_config.dependency_graph_dir, str(self.repo_path)),
                )
                self._drop_docs_backup()  # updated in place: the backup is not needed
                return
            if outcome == "resume":
                # Resume below: the cached module tree is reused and existing pages kept.
                self._drop_docs_backup()
            else:
                # full_fallback / detector_failure: preserve the old docs, rebuild from scratch
                self._move_docs_aside()
                components, leaf_nodes = doc_generator.graph_builder.build_dependency_graph()

        # Stage 2: Module Clustering
        self.progress_tracker.start_stage(2, "Module Clustering")
        if self.verbose:
            self.progress_tracker.update_stage(0.5, "Clustering modules with LLM...")

        # Import clustering function
        from codewiki.src.be.cluster_modules import (
            cluster_modules,
            ensure_artifact_module,
            get_clustering_input_token_count,
            super_group_modules,
        )
        from codewiki.src.be.module_scope import (
            get_module_token_count,
            validate_module_file_coverage,
        )
        from codewiki.src.config import FIRST_MODULE_TREE_FILENAME, MODULE_TREE_FILENAME
        from codewiki.src.utils import file_manager

        working_dir = str(self.output_dir.absolute())
        file_manager.ensure_directory(working_dir)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)

        try:
            if os.path.exists(first_module_tree_path):
                module_tree = file_manager.load_json(first_module_tree_path)
                # Never clobber an existing module_tree.json with the cached
                # first tree: it carries sub-modules inserted by agents in
                # earlier (resumed) runs, and those modules short-circuit on
                # their existing .md without re-inserting their children.
                if not os.path.exists(module_tree_path):
                    file_manager.save_json(module_tree, module_tree_path)
                if self.verbose:
                    self.progress_tracker.update_stage(0.5, "Loaded cached module tree")
            else:
                if self.verbose:
                    clustering_tokens = get_module_token_count(components)
                    self.progress_tracker.update_stage(
                        0.3,
                        (
                            f"Preparing {len(leaf_nodes)} leaf nodes for clustering "
                            f"(code_tokens={clustering_tokens}, leaf_tokens="
                            f"{get_clustering_input_token_count(leaf_nodes, components)}, threshold "
                            f"{backend_config.max_token_per_module})"
                        ),
                    )
                    if clustering_tokens <= backend_config.max_token_per_module:
                        self.progress_tracker.update_stage(
                            0.4,
                            "Skipping LLM clustering; code size fits within the module token threshold",
                        )
                    else:
                        self.progress_tracker.update_stage(
                            0.4,
                            "Clustering modules with LLM...",
                        )
                cluster_model = backend_config.cluster_model or None
                with doc_generator.usage_ledger.in_phase("clustering"):
                    module_tree = cluster_modules(
                        leaf_nodes,
                        components,
                        backend_config,
                        completer=lambda p: doc_generator.backend.complete(p, model=cluster_model),
                    )
                    if module_tree:
                        module_tree = super_group_modules(
                            module_tree,
                            backend_config,
                            completer=lambda p: doc_generator.backend.complete(
                                p, model=cluster_model
                            ),
                        )
                # Artifact nodes the clustering LLM dropped get a fixed module
                # so build/CI/config coverage does not depend on the LLM.
                if getattr(backend_config, "artifacts_enabled", True):
                    module_tree = ensure_artifact_module(
                        module_tree,
                        leaf_nodes,
                        components,
                        min_tokens=backend_config.min_token_per_module,
                    )
                # Only freshly clustered trees are deduped: renaming a cached
                # key whose .md already exists would orphan the doc.
                from codewiki.src.be.module_naming import dedupe_module_tree_names

                module_tree = dedupe_module_tree_names(module_tree)
                validate_module_file_coverage(module_tree, components)
                file_manager.save_json(module_tree, first_module_tree_path)
                file_manager.save_json(module_tree, module_tree_path)
                from codewiki.src.be.cluster_batch_cache import delete_clustering_batch_cache

                delete_clustering_batch_cache(working_dir)

            self.job.module_count = len(module_tree)

            if self.verbose:
                if len(module_tree) == 0:
                    self.progress_tracker.update_stage(
                        1.0,
                        "Created 0 modules; continuing in whole-repository documentation mode",
                    )
                else:
                    self.progress_tracker.update_stage(
                        1.0,
                        f"Created {len(module_tree)} modules",
                    )
                for mod_name in sorted(module_tree.keys()):
                    file_count = _module_file_count(module_tree[mod_name])
                    self.progress_tracker.update_stage(
                        1.0, f"  Module: {mod_name} ({file_count} files)"
                    )
        except Exception as e:  # noqa: BLE001 — surfaced to the user as an APIError
            raise APIError(f"Module clustering failed: {e}")

        self.progress_tracker.complete_stage()

        # Stage 3: Documentation Generation
        self.progress_tracker.start_stage(3, "Documentation Generation")
        if self.verbose:
            self.progress_tracker.update_stage(0.1, "Generating module documentation...")

        try:
            if self.verbose:
                self.progress_tracker.update_stage(
                    0.2, f"Generating documentation for {self.job.module_count} modules..."
                )

            # Run the actual documentation generation
            await doc_generator.generate_module_documentation(components, leaf_nodes)
            self._crosslink(doc_generator, working_dir, components)

            if self.verbose:
                self.progress_tracker.update_stage(0.9, "Creating repository overview...")

            # Create metadata
            doc_generator.create_documentation_metadata(working_dir, components, len(leaf_nodes))
            self._merge_update_summary(working_dir)

            # Collect generated files
            self.job.files_generated.extend(_generated_files(working_dir))

        except Exception as e:  # noqa: BLE001 — surfaced to the user as an APIError
            raise APIError(f"Documentation generation failed: {e}")

        # Reconcile the final module tree against the docs on disk so name
        # collisions or failed sub-agents can't pass as success (issue #76).
        # Outside the try/except above so it is not reported as an API error.
        missing_docs = doc_generator.validate_generated_docs(working_dir)
        if missing_docs:
            raise IncompleteGenerationError(
                "Documentation generation finished but these module docs are missing: "
                + ", ".join(f"{name}.md" for name in missing_docs),
                missing_modules=missing_docs,
            )

        self.progress_tracker.complete_stage()

    def _crosslink(self, doc_generator, working_dir, components) -> None:
        report = doc_generator.crosslink_documentation(working_dir, components)
        if report is not None and self.verbose:
            self.progress_tracker.update_stage(0.95, f"Cross-links: {report.summary()}")

    # ------------------------------------------------------------------
    # Incremental update helpers
    # ------------------------------------------------------------------

    def _update_options(self):
        """Return ``UpdateOptions`` when this run is a component-level update, else None."""
        if not self.config.get("update"):
            return None
        raw = dict(self.config.get("update_options") or {})
        rung = str(raw.pop("rung", "3") or "3")
        if rung == "0":
            return None  # legacy path handled in generate.py
        if not (self.output_dir / "module_tree.json").exists():
            return None  # nothing to update against: normal generation
        from codewiki.src.be.updater.options import UpdateOptions

        return UpdateOptions.from_rung(rung, **raw)

    async def _run_incremental_update(
        self, backend_config, doc_generator, update_opts, prev_graph_path, components, leaf_nodes
    ) -> str:
        import json

        from codewiki.src.be.updater.orchestrator import IncrementalUpdater

        self.progress_tracker.start_stage(2, "Incremental Update")
        revision = {
            "old_commit": self._docs_commit(),
            "new_commit": self.commit_id,
            "repo_path": portable_repo_path(str(self.repo_path), str(self.output_dir)),
        }
        updater = IncrementalUpdater(
            backend_config, doc_generator.backend, doc_generator, update_opts
        )
        self._last_update_record = updater.record
        with doc_generator.usage_ledger.in_phase("update"):
            record = await updater.run(prev_graph_path, components, leaf_nodes, revision)
        # The diff is done (or a full build follows), so drop the scratch copy: left in
        # docs/temp it doubled what gets committed (35 MB of a user's 85 MB docs folder).
        if prev_graph_path and os.path.exists(prev_graph_path):
            os.remove(prev_graph_path)
        self._last_update_record = record
        summary = record.summary()
        if self.verbose:
            self.progress_tracker.update_stage(
                0.9,
                f"Update outcome: {record.outcome} (diff {summary['diff_counts']}, "
                f"{summary['n_active']} active leaves, {summary['n_calls']} LLM calls)",
            )
        if record.errors:
            # The run goes on past a failed leaf; say so, or a partial update reads as complete.
            self.progress_tracker.note(
                f"⚠️  The update had {len(record.errors)} error(s); the pages involved may be "
                "out of date (details in update_record.json). Rerun --update to retry:\n  "
                + "\n  ".join(e[:300] for e in record.errors)
            )
        working_dir = str(self.output_dir.absolute())
        if record.outcome == "no_change":
            missing = doc_generator.validate_generated_docs(working_dir)
            if missing:
                # The code matches the saved graph but pages are missing: an earlier run
                # stopped before writing them. Generate those, keeping the pages that exist.
                self.progress_tracker.note(
                    f"No code changes, but {len(missing)} module page(s) are missing "
                    "(an earlier run did not finish); generating them."
                )
                self.progress_tracker.complete_stage()
                return "resume"
        if record.outcome in ("incremental", "no_change"):
            self._crosslink(doc_generator, working_dir, components)
            # create_documentation_metadata rewrites metadata.json from scratch;
            # keep the history of earlier updates so a chain of updates accumulates.
            prior_history = self._read_update_history(working_dir)
            doc_generator.create_documentation_metadata(working_dir, components, len(leaf_nodes))
            self._merge_update_summary(working_dir, prior_history)
            self.job.files_generated.extend(_generated_files(working_dir))
            tree_path = os.path.join(working_dir, "module_tree.json")
            if os.path.exists(tree_path):
                with open(tree_path, encoding="utf-8") as f:
                    self.job.module_count = len(json.load(f))
            missing_docs = doc_generator.validate_generated_docs(working_dir)
            if missing_docs:
                raise IncompleteGenerationError(
                    "Incremental update finished but these module docs are missing: "
                    + ", ".join(f"{name}.md" for name in missing_docs),
                    missing_modules=missing_docs,
                )
        self.progress_tracker.complete_stage()
        return record.outcome

    def _backup_docs(self) -> None:
        """Copy the docs to ``<docs>.prev-<commit>`` beside them, before the update edits them.

        Git-ignored (outside docs/, `git add .` would otherwise commit a full copy); only the
        latest backup is kept. temp/ is left out: it is the bulk of the size, and the graph
        is rebuilt from the docs' commit when missing.
        """
        import shutil

        if not self.output_dir.is_dir() or _is_repo_root(self.output_dir):
            return
        if not any(self.output_dir.iterdir()):
            return
        name = self.output_dir.name
        for old in self.output_dir.parent.glob(f"{name}.prev-*"):
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        target = self.output_dir.parent / f"{name}.prev-{(self.commit_id or 'unknown')[:8]}"
        shutil.copytree(self.output_dir, target, ignore=shutil.ignore_patterns("temp"))
        (target / ".gitignore").write_text(
            "# CodeWiki backup of the previous docs. Do not commit; delete when done.\n*\n",
            encoding="utf-8",
        )
        self._preserved_docs_dir = str(target)

    def _start_fresh(self) -> None:
        """--no-cache: a plain run resumes (it reuses the module tree and every existing
        page); clear the docs instead, keeping the old ones recoverable."""
        if self.output_dir.is_dir() and any(self.output_dir.iterdir()):
            self._docs_clean_at_start = _committed_and_clean(self.output_dir)
            self._move_docs_aside()

    def _drop_docs_backup(self) -> None:
        import shutil

        backup = getattr(self, "_preserved_docs_dir", None)
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
            self._preserved_docs_dir = None

    def _move_docs_aside(self) -> None:
        """Clear the output dir for a full rebuild, keeping the previous docs recoverable.

        Docs that were committed and unmodified when the update started are safe in git;
        others were backed up by ``_backup_docs`` before the update touched them. A repo
        root is never cleared.
        """
        import shutil

        # metadata.json goes with the docs: keep its update history for the rebuilt docs.
        self._prior_update_history = self._read_update_history(str(self.output_dir))
        record = getattr(self, "_last_update_record", None)
        name = self.output_dir.name
        if _is_repo_root(self.output_dir):
            note = "docs are written to the repo root, which is never cleared"
        else:
            if getattr(self, "_docs_clean_at_start", False):
                note = f"previous docs are in git; restore them with: git restore --source=HEAD {name}/"
            else:
                if not getattr(self, "_preserved_docs_dir", None):
                    self._backup_docs()
                # Relative: the note is saved in update_record.json and metadata.json.
                backup = portable_repo_path(self._preserved_docs_dir, str(self.output_dir))
                note = f"previous docs saved to {backup} (not committed; delete it when done)"
            shutil.rmtree(self.output_dir)
            self.output_dir.mkdir(parents=True, exist_ok=True)
        if record is not None:
            record.detector_notes.append(note)
        self.progress_tracker.note(f"Full rebuild: {note}")

    @staticmethod
    def _read_update_history(working_dir: str) -> list:
        path = os.path.join(working_dir, "metadata.json")
        try:
            with open(path, encoding="utf-8") as f:
                history = (json.load(f) or {}).get("update_history")
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(history, list):
            return []
        for entry in history:  # entries written before absolute paths stopped being published
            rev = entry.get("revision") if isinstance(entry, dict) else None
            if isinstance(rev, dict) and os.path.isabs(str(rev.get("repo_path") or "")):
                rev["repo_path"] = portable_repo_path(rev["repo_path"], working_dir)
        return history

    def _merge_update_summary(self, working_dir: str, prior_history: list | None = None) -> None:
        record = getattr(self, "_last_update_record", None)
        if record is None:
            return
        from codewiki.src.be.updater.record import merge_into_metadata

        preserved = getattr(self, "_preserved_docs_dir", None)
        if self._usage_ledger is not None:
            record.usage_report = self._usage_ledger.summary()
        summary = record.summary()
        if preserved:
            summary["previous_docs"] = portable_repo_path(preserved, working_dir)
        merge_into_metadata(
            working_dir,
            summary,
            prior_history=prior_history or getattr(self, "_prior_update_history", None),
        )
        try:
            record.save(working_dir)
        except OSError:
            pass

    def _run_html_generation(self):
        """Run HTML generation stage."""
        self.progress_tracker.start_stage(4, "HTML Generation")

        from codewiki.cli.html_generator import HTMLGenerator

        # Generate HTML
        html_generator = HTMLGenerator()

        if self.verbose:
            self.progress_tracker.update_stage(0.3, "Loading module tree and metadata...")

        repo_info = html_generator.detect_repository_info(self.repo_path)

        # Generate HTML with auto-loading of module_tree and metadata from docs_dir
        output_path = self.output_dir / "index.html"
        if self.verbose:
            self.progress_tracker.update_stage(
                0.5, "Building search_index.json for Ctrl+K search..."
            )
        viewer_summary = html_generator.generate(
            output_path=output_path,
            title=repo_info["name"],
            repository_url=repo_info["url"],
            github_pages_url=repo_info["github_pages_url"],
            docs_dir=self.output_dir,  # Auto-load module_tree and metadata from here
        )
        if self.verbose:
            self.progress_tracker.update_stage(
                0.8,
                "Wrapping viewer_template.html into index.html (search at top of content)...",
            )

        self.job.files_generated.append("index.html")
        if viewer_summary.get("search_index_written"):
            self.job.files_generated.append(viewer_summary["search_index_filename"])

        click.echo(
            "  GitHub Pages viewer: wrapped viewer_template.html → index.html "
            "(search at top of content area)"
        )
        if viewer_summary.get("search_index_written"):
            click.echo(
                f"  Documentation search: wrote {viewer_summary['search_index_filename']} "
                f"({viewer_summary['search_index_sections']} sections, lazy-loaded on Ctrl+K)"
            )
        else:
            click.echo("  Documentation search: search index was not written (see warnings above)")

        if self.verbose:
            self.progress_tracker.update_stage(1.0, "Generated index.html")

        self.progress_tracker.complete_stage()

    def _run_techdocs_generation(self):
        """Export the docs as a Backstage TechDocs project (no LLM calls)."""
        self.progress_tracker.start_stage(4, "TechDocs Export")

        from codewiki.cli.techdocs_generator import detect_repo_url, generate_techdocs

        try:
            self.techdocs_result = generate_techdocs(
                docs_dir=self.output_dir,
                out_dir=self.techdocs_dir,
                site_name=self.repo_path.name,
                repo_url=detect_repo_url(str(self.repo_path)),
            )
        except (OSError, ValueError) as e:
            # The docs are already written; a failed export must not fail the run
            import click

            click.secho(
                f"⚠ TechDocs export failed: {e}\n"
                f"  Retry with: codewiki techdocs {self.output_dir} {self.techdocs_dir}",
                fg="yellow",
                err=True,
            )
            self.progress_tracker.complete_stage()
            return

        if self.verbose:
            self.progress_tracker.update_stage(
                1.0, f"Wrote {self.techdocs_result.pages} pages to {self.techdocs_dir}"
            )

        self.progress_tracker.complete_stage()

    def _record_usage_statistics(self):
        """Copy the run's token and cost totals from the usage ledger into the job."""
        if self._usage_ledger is None:
            return
        summary = self._usage_ledger.summary()
        totals = summary["totals"]
        stats = self.job.statistics
        stats.usage_report = summary
        stats.usage_totals = totals
        stats.total_tokens_used = int(
            sum(
                totals.get(k, 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                )
            )
            or totals.get("total_tokens", 0)
        )
        stats.total_cost_usd = totals.get("cost_usd")
        stats.unpriced_models = summary["unpriced_models"]

    def _finish_usage(self, incomplete: bool = False):
        """Persist the final report even when validation or a later stage fails.

        Reporting errors must never hide the original generation failure.
        """
        if self._usage_ledger is None:
            return
        self._usage_ledger.incomplete = incomplete
        self._record_usage_statistics()
        report = self.job.statistics.usage_report
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.output_dir / "metadata.json"
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["usage"] = report
            record = getattr(self, "_last_update_record", None)
            if record is not None:
                record.usage_report = report
                record.save(str(self.output_dir))
                summary = record.summary()
                if getattr(self, "_preserved_docs_dir", None):
                    # Relative, like _merge_update_summary: metadata is published with the docs.
                    summary["previous_docs"] = portable_repo_path(
                        self._preserved_docs_dir, str(self.output_dir)
                    )
                history = metadata.get("update_history", [])
                if not isinstance(history, list):
                    history = []
                if history and history[-1].get("run_id") == record.run_id:
                    history[-1] = summary
                else:
                    history.append(summary)
                metadata.update(last_update=summary, update_history=history)
            from codewiki.cli.utils.fs import safe_write

            safe_write(path, json.dumps(metadata, indent=2, ensure_ascii=False))
        except Exception:
            logging.getLogger(__name__).warning("Could not save the run usage report")
        if incomplete:
            from codewiki.cli.utils.instructions import _echo_usage

            _echo_usage({"usage_report": report, "provider": self.config.get("provider", "")})

    def _finalize_job(self):
        """Finalize the job (metadata already created by backend)."""
        # Just verify metadata exists
        metadata_path = self.output_dir / "metadata.json"
        if not metadata_path.exists():
            # Create our own if backend didn't
            with open(metadata_path, "w") as f:
                f.write(self.job.to_json())
