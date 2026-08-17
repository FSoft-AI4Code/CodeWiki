"""PydanticAIBackend — the existing API-key based path.

This backend is a thin adapter over :func:`call_llm` and the pydantic-ai
``Agent`` machinery.  Behaviour is preserved exactly; this file only
repackages it behind the :class:`LLMBackend` interface so the rest of
CodeWiki can be backend-agnostic.
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any

from pydantic_ai import Agent

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.generate_sub_module_documentations import (
    generate_sub_module_documentation_tool,
)
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm, create_fallback_models
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import is_complex_module
from codewiki.src.config import MODULE_TREE_FILENAME, OVERVIEW_FILENAME, Config
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)


class PydanticAIBackend(LLMBackend):
    """API-key based backend using pydantic-ai + openai/litellm clients."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._fallback_models = create_fallback_models(config)
        self._custom_instructions = config.get_prompt_addition()

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
    ) -> str:
        return call_llm(prompt, self._config, model=model)

    async def run_module_agent(
        self,
        module_name: str,
        components: dict[str, Node],
        core_component_ids: list[str],
        module_path: list[str],
        working_dir: str,
    ) -> dict[str, Any]:
        config = self._config
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)

        # overview.md is the root module's own doc (renamed from
        # {repo_name}.md), so its presence only proves the root is done —
        # nested modules must still be checked against their own doc file,
        # or a resume after a partial run silently skips every missing one.
        if not module_path:
            overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
            if os.path.exists(overview_docs_path):
                logger.info("✓ Overview docs already exists at %s", overview_docs_path)
                return module_tree
        docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            logger.info("✓ Module docs already exists at %s", docs_path)
            return module_tree

        if is_complex_module(components, core_component_ids):
            agent = Agent(
                self._fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[
                    read_code_components_tool,
                    str_replace_editor_tool,
                    generate_sub_module_documentation_tool,
                ],
                system_prompt=format_system_prompt(module_name, self._custom_instructions),
            )
        else:
            agent = Agent(
                self._fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[read_code_components_tool, str_replace_editor_tool],
                system_prompt=format_leaf_system_prompt(module_name, self._custom_instructions),
            )

        deps = CodeWikiDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=str(os.path.abspath(config.repo_path)),
            registry={},
            components=components,
            path_to_current_module=module_path,
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=config.max_depth,
            current_depth=1,
            config=config,
            custom_instructions=self._custom_instructions,
        )

        try:
            await agent.run(
                format_user_prompt(
                    module_name=module_name,
                    core_component_ids=core_component_ids,
                    components=components,
                    module_tree=deps.module_tree,
                ),
                deps=deps,
            )
            file_manager.save_json(deps.module_tree, module_tree_path)
            return deps.module_tree
        except Exception as e:
            logger.error("Error processing module %s: %s", module_name, e)
            logger.error("Traceback: %s", traceback.format_exc())
            raise
