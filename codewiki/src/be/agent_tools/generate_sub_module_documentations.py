import os

from pydantic_ai import RunContext, Tool, Agent

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.module_naming import plan_sub_module_specs
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.prompt_template import SYSTEM_PROMPT, LEAF_SYSTEM_PROMPT, format_user_prompt
from codewiki.src.be.utils import is_complex_module, count_tokens
from codewiki.src.be.cluster_modules import format_potential_core_components

import logging

logger = logging.getLogger(__name__)


async def generate_sub_module_documentation(
    ctx: RunContext[CodeWikiDeps], sub_module_specs: dict[str, list[str]]
) -> str:
    """Delegate documentation generation of sub-modules to sub-agents. Each sub-module will be documented separately.

    Args:
        sub_module_specs: A dictionary mapping sub-module names to their core component IDs.
            Example: {"authentication": ["auth_handler.py::AuthHandler", "auth_middleware.py::verify_token"], "database": ["db_client.py::DBClient", "models.py::UserModel"]}
            Each key is a descriptive sub-module name, and the value is a list of component IDs from the current module's core components that belong to that sub-module.
            Sub-module names must be unique across the whole wiki. A name already used by another module is
            prefixed with the current module name; a name that is already documented (plain or prefixed)
            is SKIPPED, not regenerated. The tool result reports the final file names actually saved and
            every skipped request.
    """

    deps = ctx.deps
    previous_module_name = deps.current_module_name

    # Create fallback models from config
    fallback_models = create_fallback_models(deps.config)

    # Resolve name collisions against the module tree and files already on disk
    # before touching the tree (issue #76): docs live in one flat directory.
    # A request that is already documented (plain or parent-prefixed name) is
    # skipped rather than renamed x_2, x_3, ... (issue #113).
    plan = plan_sub_module_specs(
        sub_module_specs,
        previous_module_name,
        deps.module_tree,
        deps.absolute_docs_path,
    )
    name_map = plan.name_map
    if not name_map:
        return _skipped_report(plan.skipped, deps.current_module_name)
    final_specs = {
        name_map[requested_name]: core_component_ids
        for requested_name, core_component_ids in sub_module_specs.items()
        if requested_name in name_map
    }

    # add the sub-module to the module tree
    value = deps.module_tree
    for key in deps.path_to_current_module:
        value = value[key]["children"]
    for sub_module_name, core_component_ids in final_specs.items():
        value[sub_module_name] = {"components": core_component_ids, "children": {}}

    for sub_module_name, core_component_ids in final_specs.items():
        # Create visual indentation for nested modules
        indent = "  " * deps.current_depth
        arrow = "└─" if deps.current_depth > 0 else "→"

        logger.info(f"{indent}{arrow} Generating documentation for sub-module: {sub_module_name}")

        num_tokens = count_tokens(
            format_potential_core_components(core_component_ids, ctx.deps.components)[-1]
        )

        if (
            is_complex_module(ctx.deps.components, core_component_ids)
            and ctx.deps.current_depth < ctx.deps.max_depth
            and num_tokens >= ctx.deps.config.max_token_per_leaf_module * ctx.deps.current_depth
        ):
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=SYSTEM_PROMPT.format(
                    module_name=sub_module_name, custom_instructions=ctx.deps.custom_instructions
                ),
                tools=[
                    read_code_components_tool,
                    str_replace_editor_tool,
                    generate_sub_module_documentation_tool,
                ],
                retries=ctx.deps.config.max_retries,
            )
        else:
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=LEAF_SYSTEM_PROMPT.format(
                    module_name=sub_module_name, custom_instructions=ctx.deps.custom_instructions
                ),
                tools=[read_code_components_tool, str_replace_editor_tool],
                retries=ctx.deps.config.max_retries,
            )

        deps.current_module_name = sub_module_name
        deps.path_to_current_module.append(sub_module_name)
        deps.current_depth += 1
        # log the current module tree
        # print(f"Current module tree: {json.dumps(deps.module_tree, indent=4)}")

        await sub_agent.run(
            format_user_prompt(
                module_name=deps.current_module_name,
                core_component_ids=core_component_ids,
                components=ctx.deps.components,
                module_tree=ctx.deps.module_tree,
            ),
            deps=ctx.deps,
        )

        # remove the sub-module name from the path to current module and the module tree
        deps.path_to_current_module.pop()
        deps.current_depth -= 1

    # restore the previous module name
    deps.current_module_name = previous_module_name

    # Report what actually landed on disk so the parent agent links real filenames.
    saved = []
    missing = []
    for requested_name, final_name in name_map.items():
        entry = f"{final_name}.md"
        if final_name != requested_name:
            entry += f" (requested '{requested_name}', renamed to avoid a collision)"
        if os.path.exists(os.path.join(deps.absolute_docs_path, f"{final_name}.md")):
            saved.append(entry)
        else:
            missing.append(entry)

    report = f"Saved documentations: {', '.join(saved) if saved else 'none'}."
    if missing:
        report += f" MISSING (generation did not produce these files): {', '.join(missing)}."
        logger.warning("Sub-module documentation missing after generation: %s", ", ".join(missing))
    if plan.skipped:
        report += " " + _skipped_report(plan.skipped, deps.current_module_name)
    return report


def _skipped_report(skipped: dict[str, str], current_module_name: str) -> str:
    """Tell the parent agent, unambiguously, not to retry skipped sub-modules."""
    if not skipped:
        return "No sub-modules were generated."
    items = ", ".join(f"'{name}' ({reason})" for name, reason in skipped.items())
    return (
        f"Skipped sub-modules: {items}. Do NOT call generate_sub_module_documentation again "
        f"for these; link the existing pages from `{current_module_name}.md` instead."
    )


generate_sub_module_documentation_tool = Tool(
    function=generate_sub_module_documentation,
    name="generate_sub_module_documentation",
    takes_ctx=True,
)
