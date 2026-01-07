from pydantic_ai import RunContext, Tool
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
import logging

logger = logging.getLogger(__name__)


async def read_code_components(ctx: RunContext[CodeWikiDeps], component_ids: list[str]) -> str:
    """Read the code of a given component id

    Args:
        component_ids: The ids of the components to read, e.g. ["sweagent.types.AgentRunResult", "sweagent.types.AgentRunResult"] where sweagent.types part is the path to the component and AgentRunResult is the name of the component
    """
    
    logger.debug(f"   📚 Reading {len(component_ids)} components: {component_ids[:3]}{'...' if len(component_ids) > 3 else ''}")

    results = []
    found_count = 0
    not_found_count = 0

    for component_id in component_ids:
        if component_id not in ctx.deps.components:
            results.append(f"# Component {component_id} not found")
            not_found_count += 1
        else:
            results.append(f"# Component {component_id}:\n{ctx.deps.components[component_id].source_code.strip()}\n\n")
            found_count += 1
    
    logger.info(f"   ✅ Read components - Found: {found_count}, Not found: {not_found_count}")

    return "\n".join(results)

read_code_components_tool = Tool(function=read_code_components, name="read_code_components", description="Read the code of a given list of component ids", takes_ctx=True,
    strict=False,)