import re
from pathlib import Path
from typing import List, Tuple
import logging
import tiktoken


logger = logging.getLogger(__name__)

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
    import sys
    import os
    from io import StringIO

    core_error = ""
    
    try:
        from mermaid_parser.parser import parse_mermaid_py
        # logger.debug("Using mermaid-parser-py to validate mermaid diagrams")
    
        try:
            # Redirect stderr to suppress mermaid parser JavaScript errors
            old_stderr = sys.stderr
            sys.stderr = open(os.devnull, 'w')
            
            try:
                json_output = await parse_mermaid_py(diagram_content)
            finally:
                # Restore stderr
                sys.stderr.close()
                sys.stderr = old_stderr
        except Exception as e:
            error_str = str(e)
            
            # Extract the core error information from the exception message
            # Look for the pattern that contains "Parse error on line X:"
            error_pattern = r"Error:(.*?)(?=Stack Trace:|$)"
            match = re.search(error_pattern, error_str, re.DOTALL)
            
            if match:
                core_error = match.group(0).strip()
                core_error = core_error
            else:
                logger.error(f"No match found for error pattern, fallback to mermaid-py\n{error_str}")
                raise Exception(error_str)

    except Exception as e:
        logger.warning("Using mermaid-py to validate mermaid diagrams")
        try:
            import mermaid as md
            # Create Mermaid object and check response
            render = md.Mermaid(diagram_content)
            core_error = render.svg_response.text
            
        except Exception as e:
            return f"  Diagram {diagram_num}: Exception during validation - {str(e)}"

    # Check if response indicates a parse error
    if core_error:
        # Extract line number from parse error and calculate actual line in markdown file
        line_match = re.search(r'line (\d+)', core_error)
        if line_match:
            error_line_in_diagram = int(line_match.group(1))
            actual_line_in_file = line_start + error_line_in_diagram
            newline = '\n'
            return f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:{newline}{newline.join(core_error.split(newline)[1:])}"
        else:
            return f"Diagram {diagram_num}: {core_error}"
    
    return ""  # No error


if __name__ == "__main__":
    # Test with the provided file
    import asyncio
    test_file = "output/docs/SWE_agent-docs/agent_hooks.md"
    result = asyncio.run(validate_mermaid_diagrams(test_file, "agent_hooks.md"))
    print(result)