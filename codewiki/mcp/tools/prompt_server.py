"""MCP tool: get_prompt — serve CodeWiki's prompt templates to the IDE agent.

CodeWiki ships with carefully designed prompt templates for each stage of
the wiki generation pipeline.  This tool lets the IDE agent retrieve them
(with optional variable substitution) so it can follow the same proven
methodology without needing its own copy of the prompts.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from codewiki.mcp.session import SessionStore
from codewiki.src.be.prompt_template import (
    USER_PROMPT,
    REPO_OVERVIEW_PROMPT,
    MODULE_OVERVIEW_PROMPT,
    format_system_prompt,
    format_leaf_system_prompt,
    format_cluster_prompt,
)


# Prompt catalog: maps prompt_type to (raw_template, usage_hint, variables_doc)
_PROMPT_CATALOG: Dict[str, Dict[str, str]] = {
    "cluster": {
        "description": "Prompt for grouping components into modules. The LLM receives a component list and returns a JSON module tree.",
        "usage_hint": (
            "Use this prompt to cluster components into logical modules. "
            "The response should contain <GROUPED_COMPONENTS> JSON. "
            "Pass the component list from analyze_repo's component_index."
        ),
    },
    "system_complex": {
        "description": "System prompt for documenting a complex (multi-file, parent) module. Includes sub-module delegation instructions.",
        "usage_hint": (
            "Use as the system prompt when generating docs for a parent module. "
            "The agent should create {module_name}.md with architecture overview "
            "and cross-references to sub-module docs."
        ),
    },
    "system_leaf": {
        "description": "System prompt for documenting a leaf (single-file or simple) module.",
        "usage_hint": (
            "Use as the system prompt when generating docs for a leaf module. "
            "The agent should create {module_name}.md with detailed documentation "
            "including Mermaid diagrams."
        ),
    },
    "user": {
        "description": "User prompt template that provides the module tree and core component source code.",
        "usage_hint": (
            "Use as the user/assistant prompt alongside system_leaf or system_complex. "
            "It provides the module tree context and the actual source code of core components."
        ),
    },
    "overview_module": {
        "description": "Prompt for generating a parent module overview from its children's documentation.",
        "usage_hint": (
            "Use this after all child modules are documented. "
            "Provide the module tree with children's docs embedded. "
            "The response should be wrapped in <OVERVIEW> tags."
        ),
    },
    "overview_repo": {
        "description": "Prompt for generating the final repository overview.",
        "usage_hint": (
            "Use this as the LAST step after all modules are documented. "
            "Provide the full module tree with child docs. "
            "Save the result as overview.md."
        ),
    },
}


def handle_get_prompt(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Return a prompt template, optionally with variables filled in."""
    prompt_type = arguments["prompt_type"]
    variables = arguments.get("variables", {})

    if prompt_type not in _PROMPT_CATALOG:
        available = list(_PROMPT_CATALOG.keys())
        return json.dumps({
            "error": f"Unknown prompt_type: {prompt_type}",
            "available_types": available,
        })

    catalog_entry = _PROMPT_CATALOG[prompt_type]

    # Resolve the prompt content
    content = _resolve_prompt(prompt_type, variables)

    result = {
        "prompt_type": prompt_type,
        "description": catalog_entry["description"],
        "usage_hint": catalog_entry["usage_hint"],
        "content": content,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def _resolve_prompt(prompt_type: str, variables: Dict[str, Any]) -> str:
    """Resolve a prompt template with optional variable substitution."""

    if prompt_type == "cluster":
        potential_core_components = variables.get("potential_core_components", "<POTENTIAL_CORE_COMPONENTS placeholder>")
        module_tree = variables.get("module_tree", {})
        module_name = variables.get("module_name", None)
        return format_cluster_prompt(
            potential_core_components=potential_core_components,
            module_tree=module_tree,
            module_name=module_name,
        )

    elif prompt_type == "system_complex":
        module_name = variables.get("module_name", "MODULE_NAME")
        custom_instructions = variables.get("custom_instructions", None)
        return format_system_prompt(module_name, custom_instructions)

    elif prompt_type == "system_leaf":
        module_name = variables.get("module_name", "MODULE_NAME")
        custom_instructions = variables.get("custom_instructions", None)
        return format_leaf_system_prompt(module_name, custom_instructions)

    elif prompt_type == "user":
        module_name = variables.get("module_name", "MODULE_NAME")
        module_tree = variables.get("module_tree", {})

        # Return the template with placeholders filled as possible
        return USER_PROMPT.format(
            module_name=module_name,
            module_tree=json.dumps(module_tree, indent=2) if module_tree else "<MODULE_TREE placeholder>",
            formatted_core_component_codes=variables.get(
                "formatted_core_component_codes",
                "<CORE_COMPONENT_CODES placeholder — use read_code_components to get source code>"
            ),
        )

    elif prompt_type == "overview_module":
        module_name = variables.get("module_name", "MODULE_NAME")
        repo_structure = variables.get("repo_structure", "<REPO_STRUCTURE placeholder>")
        return MODULE_OVERVIEW_PROMPT.format(
            module_name=module_name,
            repo_structure=repo_structure if isinstance(repo_structure, str) else json.dumps(repo_structure, indent=4),
        )

    elif prompt_type == "overview_repo":
        repo_name = variables.get("repo_name", "REPO_NAME")
        repo_structure = variables.get("repo_structure", "<REPO_STRUCTURE placeholder>")
        return REPO_OVERVIEW_PROMPT.format(
            repo_name=repo_name,
            repo_structure=repo_structure if isinstance(repo_structure, str) else json.dumps(repo_structure, indent=4),
        )

    return f"Unknown prompt type: {prompt_type}"
