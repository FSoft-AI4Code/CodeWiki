"""Remove .py sources that have a compiled sibling from built wheels.

Used by every script in packaging/build/ after the wheel is built:

    python packaging/build/strip_wheel.py <wheel or glob> [...]

For each module compiled by Cython (foo.cpython-312-*.so or foo.cp312-*.pyd)
the matching foo.py is dropped. __init__.py / __main__.py always stay, as do
modules setup.py excludes from compilation. The wheel's RECORD is rewritten
so it lists exactly the files left, with fresh hashes, as the wheel spec
requires (and PyPI checks); directory entries are dropped. Standard library only, so it runs in any build environment.
"""

import base64
import csv
import glob
import hashlib
import os
import posixpath
import sys
import zipfile

PACKAGE = "codewiki/"
KEEP = {"__init__.py", "__main__.py"}
COMPILED_SUFFIXES = (".so", ".pyd")


def _module_key(name: str) -> str:
    """'codewiki/cli/main.cpython-312-x86_64-linux-gnu.so' -> 'codewiki/cli/main'."""
    folder, base = posixpath.split(name)
    return posixpath.join(folder, base.split(".", 1)[0])


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def _check_record(path: str, record_name: str) -> None:
    """Fail unless RECORD lists exactly the wheel's files, as PyPI checks
    (warehouse/utils/wheel.py: directories and RECORD signatures exempt)."""
    with zipfile.ZipFile(path) as z:
        rows = list(csv.reader(z.read(record_name).decode().splitlines()))
        record = {row[0] for row in rows if row}
        files = {
            n
            for n in z.namelist()
            if not n.endswith("/") and n not in (record_name + ".jws", record_name + ".p7s")
        }
    if record != files:
        sys.exit(
            f"{path}: RECORD does not match the wheel contents: "
            f"not in RECORD {sorted(files - record)[:5]}, not in wheel {sorted(record - files)[:5]}"
        )


def strip_wheel(path: str) -> None:
    with zipfile.ZipFile(path) as zin:
        entries = [(info, zin.read(info)) for info in zin.infolist()]

    names = [info.filename for info, _ in entries]
    compiled = {
        _module_key(n) for n in names if n.startswith(PACKAGE) and n.endswith(COMPILED_SUFFIXES)
    }
    # A wheel without compiled modules would ship all the source. That happens
    # when setup.py ran without CODEWIKI_CYTHONIZE=1, so fail the build instead.
    if not compiled:
        sys.exit(
            f"{path}: no compiled modules found; was it built with CODEWIKI_CYTHONIZE=1? "
            "Refusing to produce a wheel that ships the full source."
        )
    removed = {
        n
        for n in names
        if n.startswith(PACKAGE)
        and n.endswith(".py")
        and posixpath.basename(n) not in KEEP
        and n[: -len(".py")] in compiled
    }
    records = [n for n in names if n.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        sys.exit(f"{path}: expected one .dist-info/RECORD, found {len(records)}")
    record_name = records[0]

    # Directory entries are dropped too: they are optional in a wheel, and
    # auditwheel/delocate add them, so RECORD would list "codewiki/" etc.,
    # which PyPI's RECORD check reports as files missing from the wheel.
    kept = [
        (info, data) for info, data in entries if info.filename not in removed and not info.is_dir()
    ]
    record = [
        _record_line(info.filename, data) for info, data in kept if info.filename != record_name
    ]
    record.append(f"{record_name},,")
    record_data = ("\n".join(record) + "\n").encode()

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in kept:
            if info.filename == record_name:
                data = record_data
            info.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(info, data)
    os.replace(tmp, path)
    _check_record(path, record_name)

    left = [info.filename for info, _ in kept]
    readable = [
        n
        for n in left
        if n.startswith(PACKAGE) and n.endswith(".py") and posixpath.basename(n) not in KEEP
    ]
    print(os.path.basename(path))
    print(f"  stripped source files : {len(removed)}")
    print(f"  compiled modules      : {sum(n.endswith(COMPILED_SUFFIXES) for n in left)}")
    print(f"  source .py remaining  : {sum(n.endswith('.py') for n in left)}")
    print("  non-marker source still readable:")
    for n in readable:
        print(f"    {n}")


def main(argv: list[str]) -> None:
    wheels = sorted({w for pattern in argv for w in glob.glob(pattern)})
    if not wheels:
        sys.exit(f"no wheels match: {' '.join(argv) or '(no arguments)'}")
    for wheel in wheels:
        strip_wheel(wheel)


if __name__ == "__main__":
    main(sys.argv[1:])
