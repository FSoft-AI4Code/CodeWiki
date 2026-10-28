"""Shared Mermaid guidance and conservative, parser-independent repair helpers."""

from dataclasses import dataclass, field
import re
from typing import Literal


MERMAID_GUIDELINES = """<MERMAID_SYNTAX>
- Use simple alphanumeric node IDs. Quote flowchart node labels, keeping the shape:
  BAD: C{msg.error()?}       GOOD: C{"msg.error()?"}
  BAD: D[config[key]]        GOOD: D["config[key]"]
- In sequence messages, function calls, () {} [], commas and -- are ordinary text:
  A->>B: call process(data), config[key], {value} -- result
  A literal semicolon must be encoded as #59; (backticks do NOT escape it):
  BAD: A->>B: first; second  GOOD: A->>B: first#59; second
- Double dashes are valid inside node labels, pipe-delimited edge labels and
  sequence messages. Quote inline flowchart edge text that contains --:
  BAD: A -- start -- stop --> B  GOOD: A -- "start -- stop" --> B
  Also valid: A -->|start -- stop| B
- Fix confirmed syntax errors using the returned diagnostics. Validation
  unavailable is a warning, not a request to rewrite a diagram. If a tool repairs
  a label, use its reported saved replacement in subsequent edits.
</MERMAID_SYNTAX>"""


@dataclass(frozen=True)
class DiagramResult:
    status: Literal["valid", "invalid", "unavailable"]
    message: str = ""


@dataclass
class MermaidReport:
    content: str
    diagrams: int = 0
    errors: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)

    def format(self, relative_path: str) -> str:
        parts = []
        if self.errors:
            parts.append(
                f"Mermaid syntax errors found in file: {relative_path}\n" + "\n".join(self.errors)
            )
            parts.append(MERMAID_GUIDELINES)
        if self.unavailable:
            parts.append("Warning: Mermaid validation unavailable:\n" + "\n".join(self.unavailable))
        if self.repairs:
            parts.append("Mermaid labels repaired and validated:\n" + "\n".join(self.repairs))
        if not self.errors and not self.unavailable:
            parts.append(
                f"All mermaid diagrams in file: {relative_path} are syntax correct"
                if self.diagrams
                else "No mermaid diagrams found in the file"
            )
        return "\n".join(parts)


@dataclass(frozen=True)
class MermaidBlock:
    line: int  # Opening fence, one-based; parser line 1 is the following line.
    start: int
    end: int
    content: str
    closed: bool


def mermaid_blocks(content: str) -> list[MermaidBlock]:
    """Locate complete fenced blocks without touching Markdown outside their bodies."""
    lines = content.splitlines(keepends=True)
    blocks = []
    offset = 0
    i = 0
    while i < len(lines):
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", lines[i])
        if not opening:
            offset += len(lines[i])
            i += 1
            continue
        fence, info = opening.groups()
        opening_line = i + 1
        offset += len(lines[i])
        start = offset
        i += 1
        closing = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
        while i < len(lines) and not closing.fullmatch(lines[i]):
            offset += len(lines[i])
            i += 1
        if info.strip().split()[:1] == ["mermaid"]:
            blocks.append(
                MermaidBlock(opening_line, start, offset, content[start:offset], i < len(lines))
            )
        if i < len(lines):
            offset += len(lines[i])
            i += 1
    return blocks


def is_parse_error(message: str) -> bool:
    return bool(
        re.search(r"parse error|lexical error|syntax error|no diagram type detected", message, re.I)
    )


def quote_flowchart_labels(source: str) -> tuple[str, list[tuple[str, str]]]:
    """Quote only unambiguous classic rectangle/diamond labels with delimiters.

    This is deliberately not a general Mermaid normalizer. Walk node and label
    tokens rather than replacing bracket expressions in messages or edge text.
    Unsupported shapes or statements are skipped as whole lines.
    """
    if not re.match(r"\s*(?:flowchart|graph)\s+(?:TD|TB|BT|LR|RL)\b", source):
        return source, []
    changes = []
    output = []
    for line in source.splitlines(keepends=True):
        # Definitions, directives, comments and compound shapes need their
        # own grammars. Never guess at their contents.
        if re.match(
            r"\s*(?:%%|---|subgraph\b|end\b|class\b|classDef\b|style\b|linkStyle\b|click\b)",
            line,
        ) or re.search(r"\[\[|\[\(|\[\||\[/|\[\\|\{\{|@\{", line):
            output.append(line)
            continue
        i = 0
        pieces = []
        first_change = len(changes)
        while i < len(line):
            if line.startswith("%%", i):
                pieces = [line]
                del changes[first_change:]
                break
            # An edge label cannot contain a node definition.
            if line[i] in '|"':
                end = line.find(line[i], i + 1)
                end = len(line) - 1 if end < 0 else end
                pieces.append(line[i : end + 1])
                i = end + 1
                continue
            arrow = re.match(r"-->|---|--x|--o|==>|===|-\.->|-\.-", line[i:])
            if arrow:
                pieces.append(arrow.group())
                i += len(arrow.group())
                continue
            inline = re.match(r"--(?![->ox])|==(?![=>])|-\.(?![-.])", line[i:])
            if inline:
                # Consume the edge text as one token, then resume at the
                # target node. A code-like label is not a node definition.
                closer = {"--": "-->", "==": "==>", "-.": ".->"}[inline.group()]
                end = line.find(closer, i + len(inline.group()))
                label = line[i + len(inline.group()) : end].strip()
                quoted_label = (
                    label.startswith('"') and label.endswith('"') and label.count('"') == 2
                )
                if end < 0 or (re.search(r"--|==|-\.", label) and not quoted_label):
                    pieces = [line]
                    del changes[first_change:]
                    break
                pieces.append(line[i : end + len(closer)])
                i = end + len(closer)
                continue
            if re.match(r"[A-Za-z][A-Za-z0-9_]*(?:\(|>)", line[i:]):
                pieces = [line]
                del changes[first_change:]
                break
            node = re.match(r"[A-Za-z][A-Za-z0-9_]*([\[{])", line[i:])
            if not node or (i and (line[i - 1].isalnum() or line[i - 1] in "_@")):
                pieces.append(line[i])
                i += 1
                continue
            label_start = i + len(node.group())
            delimiter = node.group(1)
            close = "]" if delimiter == "[" else "}"
            j = label_start
            depth = 1
            quoted = False
            while j < len(line) and line[j] not in "\r\n":
                char = line[j]
                if char == '"':
                    quoted = not quoted
                if not quoted:
                    if char == delimiter:
                        depth += 1
                    elif char == close:
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            if depth:
                # An unmatched token makes the rest of this line ambiguous.
                pieces.append(line[i:])
                break
            label = line[label_start:j]
            original = line[i : j + 1]
            if (
                re.search(r"[()\[\]{}]", label)
                and not any(c in label for c in '"`<>|\\')
                and label.strip()
            ):
                replacement = line[i:label_start] + '"' + label + '"' + close
                pieces.append(replacement)
                changes.append((original, replacement))
            else:
                pieces.append(original)
            i = j + 1
        output.append("".join(pieces))
    return "".join(output), changes
