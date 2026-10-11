# Installing CodeWiki

CodeWiki is published on PyPI as **`nc-codewiki`**. It installs a `codewiki`
command. The published packages are compiled builds (no Python source), so only
the platforms below are supported:

| OS | Architecture | Python |
|---|---|---|
| Windows | x86_64 (64-bit) | 3.12, 3.13, 3.14 |
| Linux (glibc 2.31+, e.g. Ubuntu 20.04+, Debian 11+, RHEL 9+) | x86_64 | 3.12, 3.13, 3.14 |
| macOS | Apple Silicon (arm64) and Intel (x86_64) | 3.12, 3.13, 3.14 |

## Prerequisites

| Requirement | Why | Where |
|---|---|---|
| **Python 3.12, 3.13 or 3.14 (64-bit)** | The compiled packages exist only for these versions. | https://www.python.org/downloads/ |
| **Node.js with npm** | Needed **during install**: a dependency (PythonMonkey's `pminit`, used to parse Mermaid diagrams) runs `npm` while it installs. | https://nodejs.org (LTS) |
| **git** | Needed **to run** CodeWiki: it reads repositories through git. | https://git-scm.com/downloads |

On Windows, tick **"Add python.exe to PATH"** in the Python installer. After
installing any of these, open a **new** terminal so PATH updates take effect.

The install downloads dependencies from PyPI, so you need internet access.

## Install

Pick one:

```bash
# uv (recommended): installs the codewiki command in its own environment
uv tool install nc-codewiki

# pip, into the current Python environment or virtualenv
pip install nc-codewiki
```

pip and uv choose the right build for your OS and Python version automatically.
On recent Linux distributions, system-wide `pip install` is blocked (PEP 668);
use `uv tool install`, or install into a virtualenv.

Check it works:

```bash
codewiki --version
codewiki --help
```

Next: configure a model and generate docs, see `QUICKSTART.md`.

## Upgrade and uninstall

```bash
uv tool upgrade nc-codewiki        # or: pip install --upgrade nc-codewiki
uv tool uninstall nc-codewiki      # or: pip uninstall nc-codewiki
```

## Install from a wheel file

To install a specific build without PyPI (for example a wheel attached to a
release or taken from a CI run), use the file that matches your system. The
filename says which one:

```
nc_codewiki-2.0.2-cp313-cp313-win_amd64.whl
                  ^^^^^        ^^^^^^^^^
                  Python 3.13  Windows x86_64
```

| Your system | Look for |
|---|---|
| Python 3.12 / 3.13 / 3.14 | `cp312` / `cp313` / `cp314` |
| Windows x86_64 | `win_amd64` |
| Linux x86_64 | `manylinux_..._x86_64` |
| macOS (Apple Silicon or Intel) | `macosx_..._universal2` |

```bash
pip install nc_codewiki-2.0.2-cp313-cp313-win_amd64.whl
```

Dependencies still come from PyPI, and the prerequisites above still apply.

### Installer scripts

`install_codewiki.bat` (Windows), `install_codewiki_linux.sh` (Linux) and
`install_codewiki_mac.sh` (macOS) do the install above for you:

1. Check that git and Node.js/npm are installed, and say how to get them if not.
2. If uv is installed, or you let the script install it (with uv's official
   installer), run `uv tool install nc-codewiki`. uv picks (or downloads) a
   Python version that has a matching build.
3. Otherwise, fall back to pip with an installed Python 3.12, 3.13 or 3.14:
   - Linux / macOS: into a dedicated venv at `~/.codewiki-venv`, with
     `codewiki` linked into `~/.local/bin`. Uninstall with
     `rm -rf ~/.codewiki-venv ~/.local/bin/codewiki`.
   - Windows: into that Python itself (`py -3.x -m pip install nc-codewiki`).
4. Verify that `codewiki` runs, and print how to upgrade and uninstall.

Any `nc_codewiki-*.whl` files in the same folder as the script are used as well,
so the scripts can also install a build that isn't on PyPI yet. Include the
wheel for each Python version you want to support: uv picks the newest
suitable Python on the machine and needs a wheel for it.

## Troubleshooting

- **`No matching distribution found for nc-codewiki`** or **"not a supported
  wheel on this platform"**: your OS, CPU or Python version isn't in the table
  above (for example Python 3.11, Windows on ARM, or Linux on ARM). Check with
  `python --version`.
- **Install fails mentioning `npm`, `pminit`, PythonMonkey or mermaid**: Node.js
  isn't installed or isn't on PATH. Install it, open a new terminal, re-run.
- **On Linux, pip tries to build PythonMonkey from source and fails**: your
  glibc is older than 2.31 (`ldd --version`). Use a newer distribution.
- **Errors about git when running CodeWiki**: install git, open a new terminal.
- **`codewiki` not recognized after install**: open a new terminal. With uv, run
  `uv tool update-shell` once so its tool directory is on PATH.
- **Wrong `codewiki` runs, or imports fail oddly**: an unrelated PyPI package
  named `codewiki` also installs a `codewiki` module. Don't install both in the
  same environment; `pip uninstall codewiki` if it's there.
