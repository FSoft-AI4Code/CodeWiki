import asyncio
import os
import re
import sys
import threading
from pathlib import Path
from typing import List, Tuple
import logging

from codewiki.src.be.mermaid_support import (
    DiagramResult,
    MermaidReport,
    is_parse_error,
    mermaid_blocks,
    quote_flowchart_labels,
)
import tiktoken


logger = logging.getLogger(__name__)


# PythonMonkey (used by mermaid_parser.parse_mermaid_py) binds its JS engine
# to the thread that first imported it — typically the main thread at module
# load time. The caw backend dispatches MCP tool calls on a FastMCP
# daemon-thread event loop, where parse_mermaid_py raises
# "cannot find a running Python event-loop". Recording the main loop here
# lets validate_single_diagram marshal the call back via
# asyncio.run_coroutine_threadsafe so PythonMonkey finds its home loop.
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


def _diagram_message(message: str, diagram_num: int, line_start: int) -> str:
    # Translate parser-relative line numbers without dropping useful error text.
    message = re.sub(
        r"line (\d+)",
        lambda match: f"line {line_start + int(match.group(1))}",
        message,
    )
    return f"Diagram {diagram_num}: {message}"


async def validate_mermaid_content(content: str, *, repair: bool = False) -> MermaidReport:
    """Validate Markdown and optionally return only parser-confirmed repairs."""
    report = MermaidReport(content)
    replacements = []
    for number, block in enumerate(mermaid_blocks(content), 1):
        report.diagrams += 1
        result = await _validate_diagram(block.content)
        if (
            repair
            and block.closed
            and result.status == "invalid"
            and re.search(
                r"got ['\"]?(?:PS|PE|SQS|SQE|DIAMOND_START|DIAMOND_STOP)\b", result.message
            )
        ):
            candidate, changes = quote_flowchart_labels(block.content)
            if changes:
                checked = await _validate_diagram(candidate)
                if checked.status == "valid":
                    replacements.append((block.start, block.end, candidate))
                    report.repairs.extend(
                        f"Diagram {number} (fence line {block.line}): {old} -> {new}"
                        for old, new in changes
                    )
                    result = checked
                elif checked.status == "unavailable":
                    report.unavailable.append(
                        _diagram_message(
                            "repair not saved; revalidation unavailable: " + checked.message,
                            number,
                            block.line,
                        )
                    )
        if result.status == "invalid":
            report.errors.append(_diagram_message(result.message, number, block.line))
        elif result.status == "unavailable":
            report.unavailable.append(_diagram_message(result.message, number, block.line))
    for start, end, replacement in reversed(replacements):
        report.content = report.content[:start] + replacement + report.content[end:]
    return report


async def _validate_mermaid_file(md_file_path: str, relative_path: str, *, repair: bool) -> str:
    try:
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"
        original = file_path.read_bytes()
        report = await validate_mermaid_content(original.decode("utf-8"), repair=repair)
        if report.content.encode("utf-8") != original:
            # Validation may await a remote renderer. Never overwrite an edit
            # made while it was running, or claim an unsaved repair succeeded.
            if file_path.read_bytes() != original:
                return "Warning: File changed during Mermaid validation; re-run validation."
            file_path.write_bytes(report.content.encode("utf-8"))
        return report.format(relative_path)
    except Exception as e:
        return f"Warning: Mermaid validation unavailable for {relative_path}: {e}"


async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """Validate without changing the file, including after an undo operation."""
    return await _validate_mermaid_file(md_file_path, relative_path, repair=False)


async def validate_and_repair_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """Validate a successful write and persist only verified flowchart repairs."""
    return await _validate_mermaid_file(md_file_path, relative_path, repair=True)


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """Return opening-fence line numbers and bodies (legacy read-only API)."""
    return [
        (block.line, block.content.removesuffix("\n").removesuffix("\r"))
        for block in mermaid_blocks(content)
    ]


# PythonMonkey 1.3.1 only supports Python 3.8–3.11. On 3.12+ it still imports,
# but its SpiderMonkey cleanup runs on the wrong thread during _Py_Finalize and
# segfaults at interpreter shutdown (macOS crash dialog after a successful run).
# Skip it proactively so SpiderMonkey is never loaded into the process.
_PYTHONMONKEY_BROKEN = sys.version_info >= (3, 12)

# mermaid-py validates diagrams by sending them to a remote rendering service
# (https://mermaid.ink by default, overridable via the MERMAID_INK_SERVER env
# var understood by mermaid-py itself) rather than spawning a local Node.js
# subprocess. Note that this sends diagram content to that third-party service.
# It can still hang or fail when the service is unreachable (no network egress,
# DNS/firewall issues, or an outage). Enabled by default; set MERMAID_VALIDATE=0
# to disable.
_MERMAID_PY_BROKEN = os.environ.get("MERMAID_VALIDATE", "1") == "0"
_MERMAID_PY_PROBED = True  # Skip probing — rely on env var


async def _try_pythonmonkey_parse(diagram_content: str) -> str | None:
    """Attempt to parse via PythonMonkey/mermaid-parser-py.

    Returns the extracted parse-error message, "" on success, or None when
    PythonMonkey itself is unusable (broken JS event loop binding on
    Python 3.13+) so the caller can fall back to mermaid-py.
    """
    global _PYTHONMONKEY_BROKEN
    if _PYTHONMONKEY_BROKEN:
        return None

    import os

    try:
        from mermaid_parser.parser import parse_mermaid_py
    except Exception:
        _PYTHONMONKEY_BROKEN = True
        return None

    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        if (
            _main_loop is not None
            and _main_loop.is_running()
            and threading.get_ident() != _main_loop_thread_ident
        ):
            fut = asyncio.run_coroutine_threadsafe(parse_mermaid_py(diagram_content), _main_loop)
            await asyncio.wrap_future(fut)
        else:
            await parse_mermaid_py(diagram_content)
        return ""
    except Exception as e:
        error_str = str(e)
        # PythonMonkey 1.3.1 only supports Python 3.8-3.11; on newer Pythons
        # every JS call raises this. Latch the failure once so subsequent
        # diagrams skip the broken path and go straight to mermaid-py.
        if "cannot find a running Python event-loop" in error_str:
            _PYTHONMONKEY_BROKEN = True
            return None
        match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
        if match and is_parse_error(match.group(0)):
            return match.group(0).strip()
        # Unknown error from the JS parser — fall back rather than surface it.
        return None
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr


def _remote_result(status_code: int, body: str) -> DiagramResult:
    if 200 <= status_code < 300:
        return DiagramResult("valid")
    if status_code in (400, 422) and is_parse_error(body):
        # Newer mermaid-py truncates str(MermaidError), losing the final
        # lexer token. Use the response body for actionable diagnostics.
        return DiagramResult("invalid", body)
    return DiagramResult("unavailable", f"HTTP {status_code}: {body[:500]}")


def _parse_via_mermaid_py(diagram_content: str) -> DiagramResult:
    """Validate HTTP responses explicitly, including mermaid-py 0.8.0.

    0.8.0 returns error responses without raising. Later versions raise
    MermaidError; both must distinguish service failures from parse errors.
    """
    import mermaid as md

    try:
        diagram = md.Mermaid(diagram_content)
    except Exception as e:
        if isinstance(e, getattr(md, "MermaidError", ())):
            status_code = getattr(e, "status_code", None)
            if isinstance(status_code, int):
                return _remote_result(status_code, getattr(e, "response_text", str(e)))
        return DiagramResult("unavailable", str(e))
    for name in ("svg_response", "img_response"):
        response = getattr(diagram, name, None)
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            return DiagramResult("unavailable", "renderer returned no HTTP response")
        result = _remote_result(status_code, response.text)
        if result.status != "valid":
            return result
    return DiagramResult("valid")


def _run_in_daemon_thread(fn, *args) -> "asyncio.Future":
    """Run fn in a daemon thread and return an awaitable for its result.

    Unlike asyncio.to_thread, a thread left hanging after wait_for times out
    (mermaid-py's requests.get has no timeout) does not block interpreter exit.
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


async def _validate_diagram(diagram_content: str) -> DiagramResult:
    global _MERMAID_PY_BROKEN
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is not None:
        if not core_error:
            return DiagramResult("valid")
        if is_parse_error(core_error):
            return DiagramResult("invalid", core_error)
    if _MERMAID_PY_BROKEN:
        return DiagramResult("unavailable", "remote validation disabled or previously timed out")
    try:
        return await asyncio.wait_for(
            _run_in_daemon_thread(_parse_via_mermaid_py, diagram_content), timeout=15.0
        )
    except asyncio.TimeoutError:
        _MERMAID_PY_BROKEN = True
        logger.warning("Mermaid validation timed out (15s); skipping further remote validation")
        return DiagramResult("unavailable", "remote renderer timed out after 15s")
    except Exception as e:
        return DiagramResult("unavailable", str(e))


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """Legacy string API: empty only on success; unavailable is an explicit warning."""
    result = await _validate_diagram(diagram_content)
    if result.status == "valid":
        return ""
    message = _diagram_message(result.message, diagram_num, line_start)
    if result.status == "unavailable":
        return "Warning: Mermaid validation unavailable: " + message
    return message
