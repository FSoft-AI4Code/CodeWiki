#!/usr/bin/env bash
# Run the test suite against an installed (Cython-compiled) wheel.
#
# Usage: test_wheel.sh <venv-python> <repo-dir>
#
# The tests are copied out of the repo and run from there, so `import codewiki`
# resolves to the installed wheel, not the source tree. Compiled-only bugs
# (e.g. Cython's strict type checks on annotations) only show up this way;
# `codewiki --help` alone never reaches the analyzers.
set -euo pipefail

py="$1"
repo="$2"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

"$py" -m pip install --quiet pytest pytest-asyncio
cp -r "$repo/tests" "$work/tests"
cd "$work"

# Fail if codewiki would be imported from source instead of the wheel.
"$py" - <<'PY'
import codewiki.src.be.dependency_analyzer.analysis.call_graph_analyzer as m
assert not m.__file__.endswith(".py"), f"not compiled: {m.__file__}"
print("compiled:", m.__file__)
PY

# test_every_package_is_listed_in_pyproject reads the repo's pyproject.toml,
# which is not part of the wheel. smoke_test_mcp.py needs a live server.
"$py" -m pytest -p no:cacheprovider -q tests \
  --ignore=tests/smoke_test_mcp.py \
  --deselect tests/test_agent_limits.py::test_every_package_is_listed_in_pyproject
