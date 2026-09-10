"""Artifact analyzer: build, CI, container, packaging, manifest, config,
schema and script files as first-class dependency-graph nodes.

The language analyzers only see files whose extension is in
``CODE_EXTENSIONS``. Everything that describes how the system is built,
packaged, shipped, configured and tested (Dockerfiles, GitHub workflows,
Makefiles, ``pyproject.toml``, ``package.json``, ``*.proto`` ...) never
became a ``Node`` and was therefore never documented.

This module runs *after* the language analyzers over the same file tree and
emits:

* one ``Node`` per artifact file (``component_type="artifact"``,
  id ``<relative_path>::<file_name>``, ``source_code`` = capped file head),
* one child ``Node`` per unit where a cheap parser exists (CI job,
  Dockerfile stage, Makefile target, ``package.json`` script,
  ``pyproject``/``setup.cfg`` entry point), id ``<relative_path>::<unit>``,
* ``CallRelationship`` edges from artifacts to the code components (or other
  artifacts) they reference. Only fully resolved ids are emitted, because
  ``ast_parser`` falls back to *name* matching for unresolved callees.

Caps keep huge repositories bounded: per-file head, per-class file count and
a total token budget filled in ``CLASS_PRIORITY`` order.

The module deliberately imports nothing from config or prompt code so that
``prompt_template`` can import it without a cycle.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from codewiki.src.be.dependency_analyzer.models.core import CallRelationship, Node
from codewiki.src.be.dependency_analyzer.utils.patterns import ARTIFACT_LOCKFILES
from codewiki.src.be.dependency_analyzer.utils.security import safe_read_head
from codewiki.src.be.utils import count_tokens

try:  # Python >= 3.11
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ARTIFACT_TYPE = "artifact"
ARTIFACT_FILE_NODE_TYPE = "artifact_file"
ARTIFACT_UNIT_NODE_TYPE = "artifact_unit"

# Order in which classes consume the total token budget (and are rendered).
CLASS_PRIORITY = [
    "manifest",
    "build",
    "container",
    "ci",
    "packaging",
    "test_infra",
    "schema",
    "config",
    "script",
    "prose",
]

TRUNCATION_MARKER = "\n# [codewiki: truncated - showing first {shown} of {total} bytes]\n"

# Unit names that should be kept first when a file has more units than the cap.
PRIORITY_UNITS = {
    "all", "build", "test", "tests", "install", "lint", "release", "dev",
    "start", "ci", "docker", "publish", "check", "format", "deploy",
}

_PROSE_EXTS = {".md", ".mdx", ".rst", ".txt"}
_CODE_OR_SCRIPT_EXTS = (
    "py", "sh", "bash", "js", "mjs", "cjs", "ts", "tsx", "jsx", "rb", "ps1",
    "mk", "toml", "yaml", "yml", "json", "cfg", "ini", "java", "kt", "go",
    "rs", "c", "cc", "cpp", "h", "hpp", "cs", "php", "proto", "fbs",
)
PATH_TOKEN_RE = re.compile(
    r"(?<![\w@:/$-])((?:[\w.-]+/)*[\w.-]+\.(?:" + "|".join(_CODE_OR_SCRIPT_EXTS) + r"))\b"
)
PYTHON_MODULE_RE = re.compile(r"\bpython[0-9.]*\s+-m\s+([A-Za-z_][\w.]*)")
MAKE_TARGET_RE = re.compile(r"(?<![\w-])make\s+(?:-[\w-]+\s+)*([A-Za-z0-9][\w./-]*)")
NPM_SCRIPT_RE = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([A-Za-z_][\w:.-]*)")
DOCKER_BUILD_FILE_RE = re.compile(r"docker\s+(?:buildx\s+)?build\b[^\n]*?(?:-f|--file)[=\s]+(\S+)")
_NPM_RESERVED = {
    "install", "ci", "test", "run", "publish", "pack", "audit", "cache", "init",
    "add", "remove", "update", "upgrade", "exec", "link", "login", "version",
    "--frozen-lockfile", "-g", "--global",
}


@dataclass
class ArtifactOptions:
    """Knobs for :func:`analyze_artifacts`."""

    enabled: bool = True
    token_budget: int = 200_000
    with_prose: bool = False
    exclude_patterns: list[str] = field(default_factory=list)
    per_file_bytes: int = 16_384
    per_class_files: int = 40
    per_file_units: int = 25


@dataclass
class ArtifactAnalysis:
    nodes: list[Node]
    relationships: list[CallRelationship]
    index: dict[str, Any]


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #


def artifact_file_node_id(rel_path: str) -> str:
    rel_path = rel_path.replace(os.sep, "/")
    return f"{rel_path}::{posixpath.basename(rel_path)}"


def is_artifact_node(node: Any) -> bool:
    return getattr(node, "component_type", None) == ARTIFACT_TYPE


def is_artifact_file_node(node: Any) -> bool:
    return is_artifact_node(node) and getattr(node, "node_type", None) == ARTIFACT_FILE_NODE_TYPE


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

_DROP_SEGMENTS = {
    "docs", "doc", "node_modules", "vendor", "third_party", "dist", ".git",
    "fixtures", "fixture", "testdata", "test_data", "__snapshots__",
}
_CI_NAMES = {
    ".gitlab-ci.yml", "Jenkinsfile", ".travis.yml", "azure-pipelines.yml",
    "appveyor.yml", ".appveyor.yml", "bitbucket-pipelines.yml", "cloudbuild.yaml",
    "cloudbuild.yml", ".drone.yml",
}
_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml",
    "go.mod", "Gemfile", "pnpm-workspace.yaml", "lerna.json", "nx.json",
    "turbo.json", "composer.json", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "Package.swift", "pubspec.yaml",
    "Pipfile", "environment.yml", "environment.yaml", "MANIFEST.in", "Procfile",
    "conda.yaml", "conda.yml",
}
_MANIFEST_EXTS = {".gemspec", ".csproj", ".fsproj", ".vbproj", ".sln", ".podspec"}
_MANIFEST_RES = [re.compile(r"^requirements[\w.-]*\.txt$"), re.compile(r"^tsconfig[\w.-]*\.json$")]
_PACKAGING_EXTS = {".spec", ".service", ".socket", ".timer", ".plist", ".nuspec", ".wxs", ".desktop"}
_PACKAGING_TOPS = {"debian", "rpm", "installer", "pkg", "packaging"}
_PACKAGING_PACKAGES_NAMES = {
    "control", "rules", "changelog", "postinst", "prerm", "postrm", "preinst", "copyright",
}
_BUILD_NAMES = {
    "Makefile", "GNUmakefile", "makefile", "CMakeLists.txt", "Rakefile", "BUILD",
    "BUILD.gn", "BUILD.bazel", "WORKSPACE", "DEPS", "meson.build", "SConstruct",
    "SConscript", "build.xml", "gulpfile.js", "Gruntfile.js", "Herebyfile.mjs",
}
_BUILD_EXTS = {".gn", ".gni", ".gradle", ".rake", ".mk", ".cmake", ".bzl", ".ninja"}
_BUILD_CONFIG_RE = re.compile(r"^(webpack|rollup|vite|esbuild|tsup|babel)\.config\.[cm]?[jt]s$")
_BUILD_TOPS = {"build", "rakelib", "cmake"}
_TEST_INFRA_NAMES = {
    "pytest.ini", "tox.ini", "conftest.py", ".coveragerc", "codecov.yml",
    "karma.conf.js", ".nycrc", "noxfile.py",
}
_TEST_INFRA_RE = re.compile(r"^(jest|vitest|playwright|cypress|wdio|mocha)\.(config|workspace)\.[\w.]+$")
_SCHEMA_EXTS = {".proto", ".fbs", ".avsc", ".thrift", ".graphql", ".gql", ".capnp", ".xsd", ".wsdl"}
_SCHEMA_RE = re.compile(r"^(openapi|swagger)[\w.-]*\.(ya?ml|json)$")
_CONFIG_EXTS = {
    ".toml", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".options", ".properties",
    ".tf", ".nix", ".editorconfig", ".env",
}
_CONFIG_TOPS = {"config", "configs", "conf", "etc", "settings", ".github"}
_SCRIPT_EXTS = {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"}
_SCRIPT_TOPS = {"bin", "scripts", "script", "tools", "tool", "hack", "ci"}


def classify_artifact(
    rel_path: str,
    name: str,
    size: int,
    opts: ArtifactOptions,
    first_line: str | None = None,
) -> Optional[str]:
    """Return the artifact class of ``rel_path`` or ``None`` when it is not one.

    ``first_line`` is only needed for extension-less files under ``bin/`` or
    ``scripts/`` (shebang check); callers may pass ``None`` elsewhere.
    """
    rel = rel_path.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    segs = rel.split("/")
    depth = len(segs) - 1
    top = segs[0] if depth > 0 else ""
    ext = Path(name).suffix.lower()
    lower = name.lower()

    # ---- hard drops -------------------------------------------------------
    if size <= 0 or name in ARTIFACT_LOCKFILES:
        return None
    if any(seg in _DROP_SEGMENTS for seg in segs[:-1]) and not (opts.with_prose and top in {"docs", "doc"}):
        return None
    for pat in opts.exclude_patterns or []:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return None
        if pat and (rel.startswith(pat.rstrip("/") + "/") or pat.rstrip("/") in segs[:-1]):
            return None
    if rel.startswith(".github/ISSUE_TEMPLATE/") or lower.startswith("pull_request_template"):
        return None
    if name in {"CODEOWNERS", "FUNDING.yml", "funding.yml"}:
        return None

    # ---- prose (opt-in) ----------------------------------------------------
    if ext in _PROSE_EXTS and not any(r.match(name) for r in _MANIFEST_RES):
        if not opts.with_prose:
            return None
        if depth == 0 and (re.match(r"^readme", lower) or re.match(r"^contributing", lower)):
            return "prose"
        if top in {"docs", "doc"} and ext in {".md", ".mdx", ".rst"}:
            return "prose"
        return None

    # ---- ci ------------------------------------------------------------------
    if rel.startswith(".github/workflows/") and ext in {".yml", ".yaml"}:
        return "ci"
    if rel.startswith(".github/actions/") and lower in {"action.yml", "action.yaml"}:
        return "ci"
    if name in _CI_NAMES:
        return "ci"
    if top in {"ci", ".circleci", ".buildkite", ".yamato"} and ext in {".yml", ".yaml", ".sh"}:
        return "ci"

    # ---- container -----------------------------------------------------------
    if fnmatch.fnmatch(name, "Dockerfile*") or lower.endswith(".dockerfile") or lower == "containerfile":
        return "container"
    if re.match(r"^(docker-)?compose[.\w-]*\.ya?ml$", lower):
        return "container"
    if top == "docker" and (
        ext in {".yml", ".yaml", ".sh", ".erb", ".conf", ".env", ".mk"}
        or "makefile" in lower
        or lower.endswith(".conf.py")  # e.g. gunicorn.conf.py; other .py files are code
    ):
        return "container"

    # ---- manifest (before packaging: packages/*/package.json is a manifest) --
    if name in _MANIFEST_NAMES or ext in _MANIFEST_EXTS or any(r.match(name) for r in _MANIFEST_RES):
        return "manifest"

    # Source files are code, not artifacts, unless an explicit name rule above
    # (setup.py, conftest.py, noxfile.py, *.conf.py) already claimed them.
    if ext in {".py", ".js", ".ts", ".java", ".rb", ".go", ".rs", ".c", ".cpp", ".cs", ".php", ".kt"}:
        if name in _TEST_INFRA_NAMES:
            return "test_infra"
        if name in _BUILD_NAMES or _BUILD_CONFIG_RE.match(name) or _TEST_INFRA_RE.match(name):
            return "build" if name in _BUILD_NAMES or _BUILD_CONFIG_RE.match(name) else "test_infra"
        return None

    # ---- packaging -------------------------------------------------------------
    if ext in _PACKAGING_EXTS:
        return "packaging"
    if top in _PACKAGING_TOPS:
        return "packaging"
    if top == "packages" and (ext in {".sh", ".conf", ".spec", ".service"} or name in _PACKAGING_PACKAGES_NAMES):
        return "packaging"

    # ---- build -------------------------------------------------------------------
    if name in _BUILD_NAMES or ext in _BUILD_EXTS or _BUILD_CONFIG_RE.match(name) or lower.startswith(".babelrc"):
        return "build"
    if top in _BUILD_TOPS and ext in _BUILD_EXTS:
        return "build"

    # ---- test infrastructure ---------------------------------------------------
    if name in _TEST_INFRA_NAMES or _TEST_INFRA_RE.match(name) or lower.startswith(".mocharc"):
        return "test_infra"

    # ---- schema ------------------------------------------------------------------
    if ext in _SCHEMA_EXTS or _SCHEMA_RE.match(lower) or lower.endswith(".schema.json"):
        return "schema"
    if top in {"schema", "schemas"} and ext in {".json", ".yml", ".yaml"}:
        return "schema"

    # ---- config ------------------------------------------------------------------
    if ext in _CONFIG_EXTS or (
        name.startswith(".") and ext in {"", ".json", ".yml", ".yaml", ".js", ".cjs"}
    ):
        if depth <= 1 or top in _CONFIG_TOPS:
            return "config"
    if ext in {".json", ".xml"} and (depth == 0 or top in {"config", "configs", "conf", "etc"}):
        return "config"

    # ---- script ------------------------------------------------------------------
    if ext in _SCRIPT_EXTS and (depth <= 2 or top in _SCRIPT_TOPS):
        return "script"
    if ext == "" and top in {"bin", "scripts"} and first_line is not None and first_line.startswith("#!"):
        return "script"

    return None


# --------------------------------------------------------------------------- #
# Unit parsers
# --------------------------------------------------------------------------- #


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _yaml_children(text: str, top_key: str) -> list[tuple[str, int, int]]:
    """Return ``(name, start, end)`` offsets of the direct children of a top-level
    YAML mapping such as ``jobs:`` or ``services:`` without a YAML library."""
    m = re.search(rf"^{re.escape(top_key)}:[ \t]*(?:#.*)?$", text, re.M)
    if not m:
        return []
    body_start = m.end()
    nxt = re.compile(r"^\S", re.M).search(text, body_start + 1)
    body_end = nxt.start() if nxt else len(text)
    body = text[body_start:body_end]
    indent = None
    for line in body.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip(" "))
            break
    if not indent:
        return []
    child_re = re.compile(rf"^ {{{indent}}}([A-Za-z_\"'][\w.\"'-]*):[ \t]*(?:#.*)?$", re.M)
    matches = list(child_re.finditer(body))
    units: list[tuple[str, int, int]] = []
    for i, cm in enumerate(matches):
        start = body_start + cm.start()
        end = body_start + (matches[i + 1].start() if i + 1 < len(matches) else len(body))
        units.append((cm.group(1).strip("\"'"), start, end))
    return units


def _dockerfile_units(text: str) -> list[tuple[str, int, int]]:
    from_re = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M)
    matches = list(from_re.finditer(text))
    if not matches or (len(matches) == 1 and not matches[0].group(2)):
        return []
    units = []
    for i, m in enumerate(matches):
        name = m.group(2) or f"stage_{i}"
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        units.append((name, m.start(), end))
    return units


def _makefile_units(text: str) -> list[tuple[str, int, int]]:
    """Return ``(target, start, end)`` for explicit Makefile targets.

    Pattern rules (``%``), variable expansions in names, dot-targets such as
    ``.PHONY`` and assignments (``VAR := x``) are skipped. A unit spans the
    header line plus the tab-indented recipe lines that follow it.
    """
    target_re = re.compile(r"^([A-Za-z0-9][\w./-]*)\s*:(?![:=])(?P<rest>.*)$")
    units: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    i = 0
    while i < len(lines):
        m = target_re.match(lines[i])
        if not m or "%" in m.group(1) or "$" in m.group(1) or m.group(1).startswith(".") \
                or re.match(r"\s*[?+!]?=", m.group("rest")):
            i += 1
            continue
        name = m.group(1)
        j = i + 1
        while j < len(lines) and (lines[j].startswith("\t") or (lines[j].strip() == "" and j + 1 < len(lines) and lines[j + 1].startswith("\t"))):
            j += 1
        end = offsets[j] - 1 if j < len(lines) else len(text)
        if name not in seen:
            seen.add(name)
            units.append((name, offsets[i], end))
        i = j
    return units


def _package_json_units(text: str) -> tuple[list[tuple[str, int, int, str]], dict | None]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], None
    if not isinstance(data, dict):
        return [], None
    scripts = data.get("scripts")
    units: list[tuple[str, int, int, str]] = []
    if isinstance(scripts, dict):
        for k, v in scripts.items():
            if not isinstance(v, str):
                continue
            unit_text = json.dumps({k: v})
            m = re.search(rf"\"{re.escape(k)}\"\s*:", text)
            start = m.start() if m else 0
            units.append((str(k), start, start + len(unit_text), unit_text))
    return units, data


def _entry_point_units(text: str, kind: str) -> list[tuple[str, int, int, str, str]]:
    """Return ``(name, start, end, module, func)`` for console-script entry points.

    ``kind`` is ``"pyproject"`` or ``"setup_cfg"``.
    """
    results: list[tuple[str, int, int, str, str]] = []
    if kind == "pyproject":
        tables: dict[str, Any] = {}
        if tomllib is not None:
            try:
                data = tomllib.loads(text)
                proj = data.get("project", {}) if isinstance(data, dict) else {}
                for key in ("scripts", "gui-scripts"):
                    val = proj.get(key) if isinstance(proj, dict) else None
                    if isinstance(val, dict):
                        tables.update(val)
                poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data, dict) else {}
                if isinstance(poetry, dict) and isinstance(poetry.get("scripts"), dict):
                    tables.update(poetry["scripts"])
            except Exception:  # noqa: BLE001 - fall back to regex below
                tables = {}
        if not tables:
            for sec in re.finditer(r"^\[(?:project\.(?:gui-)?scripts|tool\.poetry\.scripts)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S):
                for line in sec.group(1).splitlines():
                    lm = re.match(r"^\s*([\w.-]+)\s*=\s*[\"']([\w.]+):([\w.]+)[\"']", line)
                    if lm:
                        tables[lm.group(1)] = f"{lm.group(2)}:{lm.group(3)}"
        for name, target in tables.items():
            if not isinstance(target, str) or ":" not in target:
                continue
            module, func = target.split(":", 1)
            m = re.search(rf"^\s*{re.escape(name)}\s*=", text, re.M)
            start = m.start() if m else 0
            snippet = f'{name} = "{target}"'
            results.append((str(name), start, start + len(snippet), module.strip(), func.strip()))
    else:  # setup.cfg
        m = re.search(r"^console_scripts\s*=\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
        if m:
            for line in m.group(1).splitlines():
                lm = re.match(r"^\s*([\w.-]+)\s*=\s*([\w.]+):([\w.]+)", line)
                if lm:
                    pos = text.find(line)
                    results.append((lm.group(1), pos, pos + len(line), lm.group(2), lm.group(3)))
    return results


def _prioritise_units(units: list, cap: int) -> list:
    if len(units) <= cap:
        return units
    prio = [u for u in units if u[0].lower() in PRIORITY_UNITS]
    rest = [u for u in units if u[0].lower() not in PRIORITY_UNITS]
    return (prio + rest)[:cap]


# --------------------------------------------------------------------------- #
# Reference resolution
# --------------------------------------------------------------------------- #


class _Resolver:
    """Map textual references (paths, ``module:function``) to known node ids."""

    def __init__(self, code_functions: Iterable[dict], tree_files: set[str], tree_dirs: set[str]):
        self.code_ids: set[str] = set()
        self.by_path: dict[str, list[str]] = {}
        for f in code_functions:
            cid = f.get("id") or ""
            if not cid:
                continue
            self.code_ids.add(cid)
            if f.get("class_name"):
                continue  # only top-level definitions represent a file
            rel = (f.get("relative_path") or "").replace(os.sep, "/")
            if rel:
                self.by_path.setdefault(rel, []).append(cid)
        self.tree_files = tree_files
        self.tree_dirs = tree_dirs
        self.artifact_ids: set[str] = set()
        self.artifact_files: set[str] = set()

    def known(self, node_id: str) -> bool:
        return node_id in self.code_ids or node_id in self.artifact_ids

    def resolve_path(self, ref: str, base_dir: str = "") -> list[str]:
        ref = ref.strip().strip("\"'`")
        if not ref or any(ch in ref for ch in "*?$[{}<>|"):
            return []
        if ref.startswith(("http://", "https://", "/", "..", "~")) or "://" in ref:
            return []
        ref = ref[2:] if ref.startswith("./") else ref
        cand = posixpath.normpath(posixpath.join(base_dir, ref)) if base_dir else posixpath.normpath(ref)
        if cand in (".", "") or cand.startswith("../"):
            return []
        if cand in self.tree_dirs:
            return []
        if cand in self.by_path:
            return self.by_path[cand][:10]
        if cand in self.artifact_files:
            return [artifact_file_node_id(cand)]
        # dist/lib path -> source fallback: unique match on the last two segments
        parts = cand.split("/")
        if len(parts) >= 2:
            tail = "/".join(parts[-2:])
            stem_tail = re.sub(r"\.(js|mjs|cjs)$", "", tail)
            hits = [
                p
                for p in self.by_path
                if p == tail
                or p.endswith("/" + tail)
                or re.sub(r"\.(ts|tsx)$", "", p).endswith("/" + stem_tail)
            ]
            if len(hits) == 1:
                return self.by_path[hits[0]][:10]
        return []

    def resolve_module(self, module: str, func: str | None = None) -> list[str]:
        module = module.strip()
        if not module or not re.match(r"^[A-Za-z_][\w.]*$", module):
            return []
        mod_path = module.replace(".", "/")
        candidates = [f"{mod_path}.py", f"{mod_path}/__init__.py", f"src/{mod_path}.py", f"{mod_path}/__main__.py"]
        for p in candidates:
            if func:
                fid = f"{p}::{func}"
                if fid in self.code_ids:
                    return [fid]
        for p in candidates:
            if p in self.by_path:
                return self.by_path[p][:10]
        return []


def _refs_from_shell_text(text: str, resolver: _Resolver, base_dir: str, artifact_units: dict[str, set[str]]) -> set[str]:
    """Collect ids referenced by shell-ish text (CI ``run:`` blocks, RUN lines,
    Makefile recipes, npm script values)."""
    found: set[str] = set()
    for m in PATH_TOKEN_RE.finditer(text):
        found.update(resolver.resolve_path(m.group(1), base_dir))
    for m in PYTHON_MODULE_RE.finditer(text):
        found.update(resolver.resolve_module(m.group(1)))
    for m in MAKE_TARGET_RE.finditer(text):
        target = m.group(1)
        for mk in ("Makefile", "GNUmakefile", "makefile"):
            mk_path = posixpath.normpath(posixpath.join(base_dir, mk)) if base_dir else mk
            if target in artifact_units.get(mk_path, set()):
                found.add(f"{mk_path}::{target}")
    for m in NPM_SCRIPT_RE.finditer(text):
        script = m.group(1)
        if script in _NPM_RESERVED:
            continue
        pj = posixpath.normpath(posixpath.join(base_dir, "package.json")) if base_dir else "package.json"
        if script in artifact_units.get(pj, set()):
            found.add(f"{pj}::{script}")
    for m in DOCKER_BUILD_FILE_RE.finditer(text):
        found.update(resolver.resolve_path(m.group(1), base_dir))
    return found


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #


def _walk_tree(tree: dict | None) -> tuple[list[dict], set[str], set[str]]:
    files: list[dict] = []
    file_paths: set[str] = set()
    dir_paths: set[str] = set()

    def _walk(node: dict | None) -> None:
        if not node:
            return
        if node.get("type") == "file":
            path = (node.get("path") or "").replace(os.sep, "/")
            files.append({**node, "path": path})
            file_paths.add(path)
        elif node.get("type") == "directory":
            path = (node.get("path") or "").replace(os.sep, "/")
            if path not in ("", "."):
                dir_paths.add(path)
            for child in node.get("children", []) or []:
                _walk(child)

    _walk(tree)
    return files, file_paths, dir_paths


def _first_line(base: Path, rel_path: str) -> str | None:
    try:
        text, _, is_binary = safe_read_head(base, base / rel_path, 256)
    except (OSError, PermissionError):
        return None
    if is_binary:
        return None
    return text.split("\n", 1)[0]


def analyze_artifacts(
    file_tree: dict | None,
    repo_dir: str,
    code_functions: list[dict],
    opts: ArtifactOptions | None = None,
) -> ArtifactAnalysis:
    """Turn artifact files in ``file_tree`` into nodes, unit nodes and edges."""
    opts = opts or ArtifactOptions()
    base = Path(repo_dir)
    files, tree_files, tree_dirs = _walk_tree(file_tree)

    # 1. classify (no reads except a shebang sniff)
    candidates: dict[str, list[dict]] = {cls: [] for cls in CLASS_PRIORITY}
    for f in files:
        rel = f["path"]
        name = f.get("name") or posixpath.basename(rel)
        size = int(f.get("_size_bytes") or 0)
        first_line = None
        if Path(name).suffix == "" and rel.split("/")[0] in {"bin", "scripts"} and "/" in rel:
            first_line = _first_line(base, rel)
        cls = classify_artifact(rel, name, size, opts, first_line)
        if cls:
            candidates[cls].append({"path": rel, "name": name, "size": size})

    # 2. per-class cap
    index_classes: dict[str, Any] = {}
    selected: list[tuple[str, dict]] = []
    for cls in CLASS_PRIORITY:
        items = sorted(candidates[cls], key=lambda d: (d["path"].count("/"), d["path"]))
        keep, omitted = items[: opts.per_class_files], items[opts.per_class_files :]
        index_classes[cls] = {
            "files": [],
            "omitted_by_class_cap": [d["path"] for d in omitted[:50]],
            "not_loaded_budget": [],
        }
        selected.extend((cls, d) for d in keep)

    # 3./4. read heads within the budget, in class priority order
    resolver = _Resolver(code_functions, tree_files, tree_dirs)
    loaded: list[tuple[str, dict, str, bool]] = []  # (cls, info, text, truncated)
    tokens_used = 0
    for cls, info in selected:
        rel = info["path"]
        try:
            text, total, is_binary = safe_read_head(base, base / rel, opts.per_file_bytes)
        except (OSError, PermissionError) as e:
            logger.debug("Skipping artifact %s: %s", rel, e)
            continue
        if is_binary or not text.strip():
            continue
        truncated = total > opts.per_file_bytes
        if truncated:
            text = text + TRUNCATION_MARKER.format(shown=len(text.encode("utf-8", "replace")), total=total)
        n_tokens = count_tokens(text)
        if tokens_used + n_tokens > opts.token_budget:
            index_classes[cls]["not_loaded_budget"].append(rel)
            continue
        tokens_used += n_tokens
        loaded.append((cls, info, text, truncated))
        resolver.artifact_files.add(rel)
        resolver.artifact_ids.add(artifact_file_node_id(rel))

    # 5. nodes, units and edges
    nodes: list[Node] = []
    relationships: list[CallRelationship] = []
    artifact_units: dict[str, set[str]] = {}
    unit_specs: list[tuple[str, str, str, str, int, int, dict]] = []  # (cls, rel, unit, text, start_line, end_line, extra)

    def _make_node(rel: str, name: str, cls: str, text: str, node_type: str, start: int, end: int) -> Node:
        return Node(
            id=f"{rel}::{name}",
            name=name,
            component_type=ARTIFACT_TYPE,
            file_path=str(base / rel),
            relative_path=rel,
            source_code=text,
            start_line=start,
            end_line=end,
            has_docstring=False,
            docstring="",
            parameters=[],
            node_type=node_type,
            display_name=rel if node_type == ARTIFACT_FILE_NODE_TYPE else f"{rel}::{name}",
            component_id=f"{rel}::{name}",
            artifact_class=cls,
        )

    # First pass: file nodes + unit discovery (so cross-file unit refs resolve).
    for cls, info, text, truncated in loaded:
        rel, name = info["path"], info["name"]
        line_count = text.count("\n") + 1
        nodes.append(_make_node(rel, name, cls, text, ARTIFACT_FILE_NODE_TYPE, 1, line_count))
        lower = name.lower()
        units: list[tuple[str, int, int]] = []
        extra: dict[str, Any] = {}
        if cls == "ci" and rel.startswith(".github/workflows/"):
            units = _yaml_children(text, "jobs")
        elif cls == "container" and re.match(r"^(docker-)?compose[.\w-]*\.ya?ml$", lower):
            units = _yaml_children(text, "services")
            extra["compose"] = True
        elif cls == "container" and (fnmatch.fnmatch(name, "Dockerfile*") or lower.endswith(".dockerfile")):
            units = _dockerfile_units(text)
            extra["dockerfile"] = True
        elif lower in {"makefile", "gnumakefile"} or lower.endswith(".mk"):
            units = _makefile_units(text)
            extra["makefile"] = True
        elif name == "package.json":
            pj_units, data = _package_json_units(text)
            extra["package_json"] = data
            for uname, start, end, utext in _prioritise_units(pj_units, opts.per_file_units):
                unit_specs.append((cls, rel, uname, utext, _line_of(text, start), _line_of(text, end), {"script": True}))
                artifact_units.setdefault(rel, set()).add(uname)
            units = []
        elif name == "pyproject.toml" or name == "setup.cfg":
            kind = "pyproject" if name == "pyproject.toml" else "setup_cfg"
            for uname, start, end, module, func in _prioritise_units(_entry_point_units(text, kind), opts.per_file_units):
                snippet = f"{uname} = {module}:{func}"
                unit_specs.append((cls, rel, uname, snippet, _line_of(text, start), _line_of(text, end), {"entry": (module, func)}))
                artifact_units.setdefault(rel, set()).add(uname)
            units = []
        for uname, start, end in _prioritise_units(units, opts.per_file_units):
            unit_specs.append((cls, rel, uname, text[start:end].rstrip() + "\n", _line_of(text, start), _line_of(text, max(start, end - 1)), extra))
            artifact_units.setdefault(rel, set()).add(uname)
        index_classes[cls]["files"].append(
            {
                "path": rel,
                "bytes": info["size"],
                "truncated": truncated,
                "loaded": True,
                "units": sorted(artifact_units.get(rel, set())),
            }
        )

    for cls, rel, uname, utext, s_line, e_line, extra in unit_specs:
        uid = f"{rel}::{uname}"
        if resolver.known(uid):
            continue  # unit name collides with the file node name; keep the file node
        nodes.append(_make_node(rel, uname, cls, utext, ARTIFACT_UNIT_NODE_TYPE, s_line, e_line))
        resolver.artifact_ids.add(uid)

    edges: set[tuple[str, str]] = set()

    def _add_edges(caller: str, callees: Iterable[str]) -> None:
        for callee in callees:
            if callee and callee != caller and resolver.known(callee):
                edges.add((caller, callee))

    # Second pass: edges.
    for cls, info, text, _ in loaded:
        rel, name = info["path"], info["name"]
        lower = name.lower()
        file_id = artifact_file_node_id(rel)
        base_dir = posixpath.dirname(rel)
        for uname in artifact_units.get(rel, set()):
            _add_edges(file_id, [f"{rel}::{uname}"])

        if cls == "ci" and rel.startswith(".github/workflows/"):
            for uname, start, end in _yaml_children(text, "jobs"):
                job_text = text[start:end]
                refs: set[str] = set()
                for m in re.finditer(r"uses:\s*\./(\S+)", job_text):
                    for action in ("action.yml", "action.yaml"):
                        refs.update(resolver.resolve_path(posixpath.join(m.group(1), action)))
                wd = re.search(r"working-directory:\s*(\S+)", job_text)
                job_base = posixpath.normpath(wd.group(1).strip("\"'")) if wd else ""
                if job_base in (".", "/"):
                    job_base = ""
                refs.update(_refs_from_shell_text(job_text, resolver, job_base, artifact_units))
                _add_edges(f"{rel}::{uname}" if resolver.known(f"{rel}::{uname}") else file_id, refs)
        elif cls == "container" and re.match(r"^(docker-)?compose[.\w-]*\.ya?ml$", lower):
            for uname, start, end in _yaml_children(text, "services"):
                svc = text[start:end]
                refs = set()
                ctx = re.search(r"^\s+context:\s*(\S+)", svc, re.M)
                dfile = re.search(r"^\s+dockerfile:\s*(\S+)", svc, re.M)
                build_str = re.search(r"^\s+build:\s*(\S+)\s*$", svc, re.M)
                context = (ctx.group(1) if ctx else (build_str.group(1) if build_str else "")).strip("\"'")
                dockerfile = (dfile.group(1) if dfile else "Dockerfile").strip("\"'")
                if ctx or build_str or dfile:
                    refs.update(resolver.resolve_path(posixpath.join(context, dockerfile), base_dir))
                _add_edges(f"{rel}::{uname}" if resolver.known(f"{rel}::{uname}") else file_id, refs)
        elif cls == "container" and (fnmatch.fnmatch(name, "Dockerfile*") or lower.endswith(".dockerfile")):
            units = _dockerfile_units(text) or [(None, 0, len(text))]
            for uname, start, end in units:
                stage = text[start:end]
                refs = set()
                caller = f"{rel}::{uname}" if uname and resolver.known(f"{rel}::{uname}") else file_id
                for m in re.finditer(r"^(?:COPY|ADD)\s+(.*)$", stage, re.I | re.M):
                    args = [a for a in m.group(1).split() if not a.startswith("--")]
                    if m.group(0).find("--from=") != -1:
                        alias = re.search(r"--from=(\S+)", m.group(0)).group(1)
                        _add_edges(caller, [f"{rel}::{alias}"])
                        continue
                    for a in args[:-1]:
                        refs.update(resolver.resolve_path(a, base_dir))
                for m in re.finditer(r"^(?:ENTRYPOINT|CMD|RUN)\s+(.*)$", stage, re.I | re.M):
                    refs.update(_refs_from_shell_text(m.group(1), resolver, base_dir, artifact_units))
                _add_edges(caller, refs)
        elif lower in {"makefile", "gnumakefile"} or lower.endswith(".mk"):
            for m in re.finditer(r"^(?:-?include|sinclude)\s+(\S+)", text, re.M):
                _add_edges(file_id, resolver.resolve_path(m.group(1), base_dir))
            for uname, start, end in _makefile_units(text):
                recipe = text[start:end]
                refs = _refs_from_shell_text(recipe, resolver, base_dir, artifact_units)
                # prerequisites on the header line
                header = recipe.split("\n", 1)[0]
                if ":" in header:
                    for prereq in header.split(":", 1)[1].split():
                        if prereq in artifact_units.get(rel, set()):
                            refs.add(f"{rel}::{prereq}")
                        else:
                            refs.update(resolver.resolve_path(prereq, base_dir))
                _add_edges(f"{rel}::{uname}" if resolver.known(f"{rel}::{uname}") else file_id, refs)
        elif name == "package.json":
            _, data = _package_json_units(text)
            if isinstance(data, dict):
                refs = set()
                for key in ("main", "module", "types", "browser"):
                    if isinstance(data.get(key), str):
                        refs.update(resolver.resolve_path(data[key], base_dir))
                bin_field = data.get("bin")
                for v in ([bin_field] if isinstance(bin_field, str) else list(bin_field.values()) if isinstance(bin_field, dict) else []):
                    if isinstance(v, str):
                        refs.update(resolver.resolve_path(v, base_dir))

                def _walk_exports(val: Any) -> None:
                    if isinstance(val, str):
                        refs.update(resolver.resolve_path(val, base_dir))
                    elif isinstance(val, dict):
                        for v in val.values():
                            _walk_exports(v)
                    elif isinstance(val, list):
                        for v in val:
                            _walk_exports(v)

                _walk_exports(data.get("exports"))
                _add_edges(file_id, refs)
                scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
                for sname, sval in scripts.items():
                    if not isinstance(sval, str):
                        continue
                    caller = f"{rel}::{sname}" if resolver.known(f"{rel}::{sname}") else file_id
                    _add_edges(caller, _refs_from_shell_text(sval, resolver, base_dir, artifact_units))
        elif name in {"pyproject.toml", "setup.cfg"}:
            kind = "pyproject" if name == "pyproject.toml" else "setup_cfg"
            for uname, _s, _e, module, func in _entry_point_units(text, kind):
                caller = f"{rel}::{uname}" if resolver.known(f"{rel}::{uname}") else file_id
                _add_edges(caller, resolver.resolve_module(module, func))
        elif cls in {"script", "ci", "packaging", "test_infra"}:
            _add_edges(file_id, _refs_from_shell_text(text, resolver, base_dir, artifact_units))

    for caller, callee in sorted(edges):
        relationships.append(CallRelationship(caller=caller, callee=callee, is_resolved=True))

    index = {
        "token_budget": opts.token_budget,
        "tokens_used": tokens_used,
        "caps": {
            "per_file_bytes": opts.per_file_bytes,
            "per_class_files": opts.per_class_files,
            "per_file_units": opts.per_file_units,
        },
        "with_prose": opts.with_prose,
        "classes": {cls: v for cls, v in index_classes.items() if v["files"] or v["omitted_by_class_cap"] or v["not_loaded_budget"]},
        "nodes": len(nodes),
        "edges": len(relationships),
    }
    logger.info(
        "Artifact analysis: %d files, %d nodes, %d edges, %d tokens (budget %d)",
        len(loaded), len(nodes), len(relationships), tokens_used, opts.token_budget,
    )
    return ArtifactAnalysis(nodes=nodes, relationships=relationships, index=index)


# --------------------------------------------------------------------------- #
# Index rendering (from Node objects, so prompts never read the JSON)
# --------------------------------------------------------------------------- #


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


_UNIT_LABEL = {
    "ci": "jobs",
    "container": "stages/services",
    "build": "targets",
    "manifest": "scripts/entry points",
}


def build_artifact_index(components: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group artifact file nodes by class with their unit names."""
    units_by_file: dict[str, list[str]] = {}
    files: dict[str, Any] = {}
    for node in components.values():
        if not is_artifact_node(node):
            continue
        if is_artifact_file_node(node):
            files[node.relative_path] = node
        else:
            units_by_file.setdefault(node.relative_path, []).append(node.name)
    index: dict[str, list[dict[str, Any]]] = {}
    for rel, node in sorted(files.items()):
        src = node.source_code or ""
        index.setdefault(node.artifact_class or "config", []).append(
            {
                "path": rel,
                "id": node.id,
                "size": len(src.encode("utf-8", "replace")),
                "truncated": "[codewiki: truncated" in src,
                "units": sorted(units_by_file.get(rel, [])),
            }
        )
    return index


def render_artifact_index(components: dict[str, Any], max_files_per_class: int = 40) -> str:
    """Render the artifact index as a compact text block, or ``""`` if none."""
    index = build_artifact_index(components)
    if not index:
        return ""
    lines = [
        "<REPOSITORY_ARTIFACTS>",
        "Build, CI, container, packaging, manifest, config, schema and script files in this "
        "repository, grouped by class. Component ids are `<path>::<name>`; read a file with "
        "`str_replace_editor view` (working_dir=`repo`).",
    ]
    for cls in CLASS_PRIORITY:
        entries = index.get(cls)
        if not entries:
            continue
        n_trunc = sum(1 for e in entries if e["truncated"])
        header = f"## {cls} ({len(entries)} files)"
        if n_trunc:
            header += f" [{n_trunc} truncated]"
        lines.append(header)
        for e in entries[:max_files_per_class]:
            line = f"- {e['path']} ({_human_size(e['size'])})"
            if e["units"]:
                label = _UNIT_LABEL.get(cls, "units")
                shown = e["units"][:8]
                more = len(e["units"]) - len(shown)
                line += f" - {label}: {', '.join(shown)}" + (f", +{more} more" if more > 0 else "")
            lines.append(line)
        if len(entries) > max_files_per_class:
            lines.append(f"- ... {len(entries) - max_files_per_class} more {cls} files")
    lines.append("</REPOSITORY_ARTIFACTS>")
    return "\n".join(lines)
