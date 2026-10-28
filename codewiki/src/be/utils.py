import asyncio
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Tuple

import tiktoken


logger = logging.getLogger(__name__)


# PythonMonkey (used by mermaid_parser.parse_mermaid_py) is only safe on the
# process main thread. Importing it on another thread segfaults, and using it
# from a non-main thread's loop crashes Python at exit (seen on every version,
# Linux and macOS). The caw backend runs MCP tool calls on a FastMCP daemon
# thread, so it records the main loop here and _try_pythonmonkey_parse hands
# the parse (import included) back to it via asyncio.run_coroutine_threadsafe.
_main_loop: "asyncio.AbstractEventLoop | None" = None
_main_loop_thread_ident: int | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop, _main_loop_thread_ident
    _main_loop = loop
    _main_loop_thread_ident = threading.get_ident()


# ------------------------------------------------------------
# ---------------------- Complexity Check --------------------
# ------------------------------------------------------------


def is_complex_module(components: dict[str, any], core_component_ids: list[str]) -> bool:
    files = set()
    for component_id in core_component_ids:
        if component_id in components:
            files.add(components[component_id].file_path)

    result = len(files) > 1

    return result


# ------------------------------------------------------------
# ---------------------- Token Counting ---------------------
# ------------------------------------------------------------

enc = tiktoken.encoding_for_model("gpt-4")


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in a text.
    """
    # source code may contain literal special tokens such as "<|endoftext|>"
    # (e.g. CLIP tokenizers); count them as plain text instead of raising
    length = len(enc.encode(text, disallowed_special=()))
    # logger.debug(f"Number of tokens: {length}")
    return length


# ------------------------------------------------------------
# ---------------------- Mermaid Validation -----------------
# ------------------------------------------------------------


async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """
    Validate all Mermaid diagrams in a markdown file.

    Args:
        md_file_path: Path to the markdown file to check
        relative_path: Relative path to the markdown file
    Returns:
        "All mermaid diagrams are syntax correct" if all diagrams are valid,
        otherwise returns error message with details about invalid diagrams
    """

    try:
        # Read the markdown file
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"

        content = file_path.read_text(encoding="utf-8")

        # Extract all mermaid code blocks
        mermaid_blocks = extract_mermaid_blocks(content)

        if not mermaid_blocks:
            return "No mermaid diagrams found in the file"

        # Validate each mermaid diagram sequentially to avoid segfaults
        errors = []
        for i, (line_start, diagram_content) in enumerate(mermaid_blocks, 1):
            error_msg = await validate_single_diagram(diagram_content, i, line_start)
            if error_msg:
                errors.append("\n")
                errors.append(error_msg)

        # if errors:
        #     logger.debug(f"Mermaid syntax errors found in file: {md_file_path}: {errors}")

        if errors:
            return (
                "Mermaid syntax errors found in file: " + relative_path + "\n" + "\n".join(errors)
            )
        else:
            return "All mermaid diagrams in file: " + relative_path + " are syntax correct"

    except Exception as e:
        return f"Error processing file: {str(e)}"


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """
    Extract all mermaid code blocks from markdown content.

    Returns:
        List of tuples containing (line_number, diagram_content)
    """
    mermaid_blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Look for mermaid code block start
        if line == "```mermaid" or line.startswith("```mermaid"):
            start_line = i + 1
            diagram_lines = []
            i += 1

            # Collect lines until we find the closing ```
            while i < len(lines):
                if lines[i].strip() == "```":
                    break
                diagram_lines.append(lines[i])
                i += 1

            if diagram_lines:  # Only add non-empty diagrams
                diagram_content = "\n".join(diagram_lines)
                mermaid_blocks.append((start_line, diagram_content))

        i += 1

    return mermaid_blocks


# Set once PythonMonkey fails to import or loses its event loop; validation
# then falls back to system Node.js running the same parser bundle.
_PYTHONMONKEY_BROKEN = False
_NODE_TIMEOUT_SECONDS = 15.0

# Disabled: remote fallback through mermaid-py, which sends diagram content to
# https://mermaid.ink (or MERMAID_INK_SERVER). Kept commented out so it can be
# brought back: uncomment this, _parse_via_mermaid_py, the block at the end of
# validate_single_diagram's fallbacks, and mermaid-py in pyproject/requirements.
# _MERMAID_PY_BROKEN = os.environ.get("MERMAID_VALIDATE", "0").strip() != "1"

# Validation never touches the network: PythonMonkey, then Node, then skip.
_pm_unavailable_warned = False
_skip_warned = False


def _warn_pythonmonkey_unavailable_once() -> None:
    global _pm_unavailable_warned
    if not _pm_unavailable_warned:
        _pm_unavailable_warned = True
        logger.warning("PythonMonkey Mermaid validation is unavailable; trying system Node.js.")


def _warn_validation_skipped_once(reason: str) -> None:
    global _skip_warned
    if not _skip_warned:
        _skip_warned = True
        logger.warning("Mermaid validation skipped: %s", reason)


# PythonMonkey prints JS noise to stderr, so validation points sys.stderr at
# devnull. Concurrent agents validate at the same time; a plain save/restore
# per call interleaves and can leave stderr on a closed devnull, so the swap
# is reference-counted: the first caller swaps, the last one restores.
_stderr_lock = threading.Lock()
_stderr_users = 0
_saved_stderr = None


def _quiet_stderr_enter() -> None:
    global _stderr_users, _saved_stderr
    with _stderr_lock:
        if _stderr_users == 0:
            _saved_stderr = sys.stderr
            sys.stderr = open(os.devnull, "w")  # noqa: SIM115 — closed in _quiet_stderr_exit
        _stderr_users += 1


def _quiet_stderr_exit() -> None:
    global _stderr_users, _saved_stderr
    with _stderr_lock:
        _stderr_users -= 1
        if _stderr_users == 0:
            sys.stderr.close()
            sys.stderr = _saved_stderr
            _saved_stderr = None


async def _try_pythonmonkey_parse(diagram_content: str) -> str | None:
    """Attempt to parse via PythonMonkey/mermaid-parser-py.

    Returns the extracted parse-error message, "" on success, or None when
    PythonMonkey itself is unusable (failed import or lost event loop) so the
    caller can fall back to system Node.js.
    """
    global _PYTHONMONKEY_BROKEN
    if _PYTHONMONKEY_BROKEN:
        _warn_pythonmonkey_unavailable_once()
        return None

    on_main_thread = threading.current_thread() is threading.main_thread()
    via_main_loop = (
        not on_main_thread
        and _main_loop is not None
        and _main_loop.is_running()
        and _main_loop_thread_ident == threading.main_thread().ident
    )
    if not (on_main_thread or via_main_loop):
        # e.g. the web app's worker thread: no main-thread loop to hand off to.
        _warn_pythonmonkey_unavailable_once()
        return None

    _quiet_stderr_enter()
    try:
        if via_main_loop:
            fut = asyncio.run_coroutine_threadsafe(
                _parse_with_pythonmonkey(diagram_content), _main_loop
            )
            await asyncio.wrap_future(fut)
        else:
            await _parse_with_pythonmonkey(diagram_content)
        return ""
    except ImportError:
        _PYTHONMONKEY_BROKEN = True
        _warn_pythonmonkey_unavailable_once()
        return None
    except Exception as e:
        error_str = str(e)
        # Raised when called off PythonMonkey's home loop. Latch the failure
        # once so later diagrams skip straight to Node.
        if "cannot find a running Python event-loop" in error_str:
            _PYTHONMONKEY_BROKEN = True
            _warn_pythonmonkey_unavailable_once()
            return None
        match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
        if match:
            return match.group(0).strip()
        # Unknown error from the JS parser — fall back rather than surface it.
        return None
    finally:
        _quiet_stderr_exit()


async def _parse_with_pythonmonkey(diagram_content: str) -> None:
    """Parse on the main thread; importing here keeps the import there too."""
    try:
        from mermaid_parser.parser import parse_mermaid_py
    except Exception as e:
        raise ImportError("PythonMonkey is unavailable") from e
    await parse_mermaid_py(diagram_content)


def _resolve_node_executable() -> str | None:
    override = os.environ.get("CODEWIKI_NODE_PATH", "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("node")


# Reads the diagram on stdin, exits 0 if it parses, else 1 with the error on stderr.
_NODE_PARSE_SCRIPT = (
    'const parse = require(process.argv[1]); let s = "";'
    'process.stdin.on("data", (d) => (s += d)).on("end", () => parse(s).then('
    "() => process.exit(0),"
    "(e) => { process.stderr.write(String((e && e.message) || e)); process.exit(1); }));"
)


def _parse_via_node(diagram_content: str) -> str | None:
    """Validate with system Node.js running mermaid-parser-py's own bundle.

    Returns the parse-error message, "" on success, or None (after one
    warning) when Node or the bundle is missing or the run fails.
    """
    node = _resolve_node_executable()
    if node is None:
        _warn_validation_skipped_once(
            "Node.js not found on PATH (set CODEWIKI_NODE_PATH to point at it)."
        )
        return None
    # find_spec does not run mermaid_parser/__init__, which imports PythonMonkey.
    spec = importlib.util.find_spec("mermaid_parser")
    bundle = Path(spec.origin).parent / "js" / "parser.bundle.js" if spec and spec.origin else None
    if bundle is None or not bundle.is_file():
        _warn_validation_skipped_once("mermaid-parser-py's parser.bundle.js was not found.")
        return None
    try:
        result = subprocess.run(
            [node, "-e", _NODE_PARSE_SCRIPT, str(bundle)],
            input=diagram_content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _warn_validation_skipped_once(f"Node.js validator failed to run ({e}).")
        return None
    if result.returncode == 0:
        return ""
    return result.stderr.strip() or "Mermaid parse failed (no message from Node.js)"


# def _parse_via_mermaid_py(diagram_content: str) -> str:
#     """Validate via mermaid-py. Returns parse-error text, or "" if valid.
#
#     mermaid-py raises MermaidError on parse failure and returns an SVG body
#     on success — we must drive the result off the exception, not the body
#     text, otherwise a successful SVG gets reported as a parse error.
#     """
#     import mermaid as md
#
#     try:
#         md.Mermaid(diagram_content)
#         return ""
#     except Exception as e:
#         return str(e)


def _run_in_daemon_thread(fn, *args) -> "asyncio.Future":
    """Run fn in a daemon thread and return an awaitable for its result.

    Unlike asyncio.to_thread, a thread left hanging after wait_for times out
    does not block interpreter exit.
    """
    import threading

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _set(setter, value):
        if not fut.done():
            setter(value)

    def _worker():
        try:
            setter, value = fut.set_result, fn(*args)
        except BaseException as e:
            setter, value = fut.set_exception, e
        try:
            loop.call_soon_threadsafe(_set, setter, value)
        except RuntimeError:
            pass  # loop already closed; nobody is waiting any more

    threading.Thread(target=_worker, daemon=True).start()
    return fut


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """
    Validate a single mermaid diagram.

    Args:
        diagram_content: The mermaid diagram content
        diagram_num: Diagram number for error reporting
        line_start: Starting line number in the file

    Returns:
        Error message if invalid, empty string if valid
    """
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is None:
        try:
            core_error = await asyncio.wait_for(
                _run_in_daemon_thread(_parse_via_node, diagram_content),
                timeout=_NODE_TIMEOUT_SECONDS + 5.0,
            )
        except asyncio.TimeoutError:
            core_error = None
            _warn_validation_skipped_once("Node.js validator timed out.")
    # Disabled remote fallback (see _MERMAID_PY_BROKEN above); needs
    # `global _MERMAID_PY_BROKEN` at the top of this function.
    # if core_error is None and not _MERMAID_PY_BROKEN:
    #     try:
    #         core_error = await asyncio.wait_for(
    #             _run_in_daemon_thread(_parse_via_mermaid_py, diagram_content),
    #             timeout=_NODE_TIMEOUT_SECONDS,
    #         )
    #     except asyncio.TimeoutError:
    #         # Latch so later diagrams don't each block 15s on the same service.
    #         _MERMAID_PY_BROKEN = True
    #         core_error = None
    #     except Exception as e:
    #         return f"  Diagram {diagram_num}: Exception during validation - {str(e)}"
    # Validation unavailable is not a syntax error: a non-empty return here would
    # make callers report valid diagrams as broken and send agents into fix loops.
    if not core_error:
        return ""

    line_match = re.search(r"line (\d+)", core_error)
    if line_match:
        error_line_in_diagram = int(line_match.group(1))
        actual_line_in_file = line_start + error_line_in_diagram
        newline = "\n"
        return f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:{newline}{newline.join(core_error.split(newline)[1:])}"
    return f"Diagram {diagram_num}: {core_error}"


if __name__ == "__main__":
    # Test with the provided file
    import asyncio

    test_file = "output/docs/SWE_agent-docs/agent_hooks.md"
    result = asyncio.run(validate_mermaid_diagrams(test_file, "agent_hooks.md"))
    print(result)
