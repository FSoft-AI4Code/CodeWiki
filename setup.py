"""
Cython build configuration for CodeWiki.

This compiles every .py under codewiki/ to a native .pyd, EXCEPT:
  - __init__.py / __main__.py  (package markers - must stay as source)
  - anything listed in EXCLUDE  (modules that break when Cython-compiled)

Metadata (name, version, deps, package-data) comes from pyproject.toml;
this file only adds the ext_modules compile step.

Compilation is opt-in: a plain `pip install .` / `pip install -e .` builds
a normal pure-Python package. Set CODEWIKI_CYTHONIZE=1 to compile (the
scripts under packaging/build/ do this).

Build with (from a VS Developer environment, buildenv active):
    set CODEWIKI_CYTHONIZE=1
    set DISTUTILS_USE_SDK=1
    set MSSdk=1
    python setup.py build_ext --inplace
    python -m build --wheel --no-isolation
"""

import os
from setuptools import setup

# Package markers - keep as source so imports work.
SKIP = {"__init__.py", "__main__.py"}

# Modules that must NOT be compiled.
# pydantic v2 inspects model classes at definition time and rejects the
# cyfunction methods Cython produces, so every file defining a BaseModel
# subclass ships as source. These are schema/field definitions, not core
# logic. Add a module here if a fresh `codewiki --help` / generate run
# throws a pydantic (or similar introspection) error pointing at it.
EXCLUDE = {
    "codewiki/src/be/dependency_analyzer/models/analysis.py",
    "codewiki/src/be/dependency_analyzer/models/core.py",
    "codewiki/src/fe/models.py",
}


def py_sources(pkg):
    out = []
    for root, _, names in os.walk(pkg):
        for n in names:
            if n.endswith(".py") and n not in SKIP:
                p = os.path.join(root, n).replace("\\", "/")
                if p not in EXCLUDE:
                    out.append(p)
    return out


if os.environ.get("CODEWIKI_CYTHONIZE") == "1":
    from Cython.Build import cythonize

    setup(
        ext_modules=cythonize(
            py_sources("codewiki"),
            # annotation_typing=False: by default Cython turns annotations
            # like `x: dict = ...` into exact-type checks, so subclasses
            # (defaultdict, OrderedDict, Counter) raise "Expected dict, got
            # collections.defaultdict" in the compiled wheel only.
            compiler_directives={"language_level": "3", "annotation_typing": False},
            build_dir="build_cython",
        ),
    )
else:
    setup()
