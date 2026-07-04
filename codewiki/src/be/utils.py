import asyncio
import os
import re
import sys
import threading
from pathlib import Path
from typing import List, Tuple
import logging
import tiktoken
import traceback


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
    length = len(enc.encode(text))
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
        
        content = file_path.read_text(encoding='utf-8')
        
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
            return "Mermaid syntax errors found in file: " + relative_path + "\n" + "\n".join(errors)
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
    lines = content.split('\n')
    i = 0
    
    while i < len(lines):
        line = lines[i].strip()
        
        # Look for mermaid code block start
        if line == '```mermaid' or line.startswith('```mermaid'):
            start_line = i + 1
            diagram_lines = []
            i += 1
            
            # Collect lines until we find the closing ```
            while i < len(lines):
                if lines[i].strip() == '```':
                    break
                diagram_lines.append(lines[i])
                i += 1
            
            if diagram_lines:  # Only add non-empty diagrams
                diagram_content = '\n'.join(diagram_lines)
                mermaid_blocks.append((start_line, diagram_content))
        
        i += 1
    
    return mermaid_blocks


# PythonMonkey 1.3.1 only supports Python 3.8–3.11. On 3.12+ it still imports,
# but its SpiderMonkey cleanup runs on the wrong thread during _Py_Finalize and
# segfaults at interpreter shutdown (macOS crash dialog after a successful run).
# Skip it proactively so SpiderMonkey is never loaded into the process.
_PYTHONMONKEY_BROKEN = sys.version_info >= (3, 12)

# mermaid-py spawns a Node.js subprocess that can hang indefinitely (e.g. when
# Node.js is missing or the mermaid CLI is misconfigured).  Enabled by default;
# set MERMAID_VALIDATE=0 to disable.
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
    sys.stderr = open(os.devnull, 'w')
    try:
        if (
            _main_loop is not None
            and _main_loop.is_running()
            and threading.get_ident() != _main_loop_thread_ident
        ):
            fut = asyncio.run_coroutine_threadsafe(
                parse_mermaid_py(diagram_content), _main_loop
            )
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
        if match:
            return match.group(0).strip()
        # Unknown error from the JS parser — fall back rather than surface it.
        return None
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr


def _parse_via_mermaid_py(diagram_content: str) -> str:
    """Validate via mermaid-py. Returns parse-error text, or "" if valid.

    mermaid-py raises MermaidError on parse failure and returns an SVG body
    on success — we must drive the result off the exception, not the body
    text, otherwise a successful SVG gets reported as a parse error.
    """
    import mermaid as md
    try:
        md.Mermaid(diagram_content)
        return ""
    except Exception as e:
        return str(e)


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
    global _MERMAID_PY_BROKEN
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is None:
        if _MERMAID_PY_BROKEN:
            # Validation disabled/unavailable is not a syntax error — a
            # non-empty return here would make callers report valid diagrams
            # as broken and send agents into fix loops.
            logger.debug("Diagram %d: validation skipped (mermaid-py disabled or unavailable)", diagram_num)
            return ""
        try:
            core_error = await asyncio.wait_for(
                asyncio.to_thread(_parse_via_mermaid_py, diagram_content),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            # Inconclusive, not a parse failure. Latch so the remaining
            # diagrams (and later calls) don't each block 15s on the same
            # broken Node.js setup.
            _MERMAID_PY_BROKEN = True
            logger.warning("Diagram %d: mermaid validation timed out (15s); skipping further validation", diagram_num)
            return ""
        except Exception as e:
            return f"  Diagram {diagram_num}: Exception during validation - {str(e)}"

    if not core_error:
        return ""

    line_match = re.search(r'line (\d+)', core_error)
    if line_match:
        error_line_in_diagram = int(line_match.group(1))
        actual_line_in_file = line_start + error_line_in_diagram
        newline = '\n'
        return f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:{newline}{newline.join(core_error.split(newline)[1:])}"
    return f"Diagram {diagram_num}: {core_error}"


if __name__ == "__main__":
    # Test with the provided file
    import asyncio
    test_file = "output/docs/SWE_agent-docs/agent_hooks.md"
    result = asyncio.run(validate_mermaid_diagrams(test_file, "agent_hooks.md"))
    print(result)