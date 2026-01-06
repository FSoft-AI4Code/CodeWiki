from pydantic_ai import RunContext, Tool, Agent

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.prompt_template import SYSTEM_PROMPT, LEAF_SYSTEM_PROMPT, format_user_prompt
from codewiki.src.be.utils import is_complex_module, count_tokens
from codewiki.src.be.cluster_modules import format_potential_core_components
from codewiki.src.config import MAX_TOKEN_PER_LEAF_MODULE

import logging
import traceback
logger = logging.getLogger(__name__)



async def generate_sub_module_documentation(
    ctx: RunContext[CodeWikiDeps],
    sub_module_specs: dict[str, list[str]]
) -> str:
    """Generate detailed description of a given sub-module specs to the sub-agents

    Args:
        sub_module_specs: The specs of the sub-modules to generate documentation for. E.g. {"sub_module_1": ["core_component_1.1", "core_component_1.2"], "sub_module_2": ["core_component_2.1", "core_component_2.2"], ...}
    """

    deps = ctx.deps
    previous_module_name = deps.current_module_name
    
    # Create fallback models from config
    fallback_models = create_fallback_models(deps.config)

    # add the sub-module to the module tree
    value = deps.module_tree
    for key in deps.path_to_current_module:
        value = value[key]["children"]
    for sub_module_name, core_component_ids in sub_module_specs.items():
        value[sub_module_name] = {"components": core_component_ids, "children": {}}
    
    for sub_module_name, core_component_ids in sub_module_specs.items():

        # Create visual indentation for nested modules
        indent = "  " * deps.current_depth
        arrow = "└─" if deps.current_depth > 0 else "→"

        logger.info(f"{indent}{arrow} 📝 Starting sub-module: {sub_module_name} ({len(core_component_ids)} components)")

        num_tokens = count_tokens(format_potential_core_components(core_component_ids, ctx.deps.components)[-1])
        logger.info(f"{indent}   📊 Token count: {num_tokens}")
        
        if is_complex_module(ctx.deps.components, core_component_ids) and ctx.deps.current_depth < ctx.deps.max_depth and num_tokens >= MAX_TOKEN_PER_LEAF_MODULE:
            logger.info(f"{indent}   🌳 Creating complex agent (with recursion capability)")
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=SYSTEM_PROMPT.format(module_name=sub_module_name, language=deps.config.language),
                tools=[read_code_components_tool, str_replace_editor_tool, generate_sub_module_documentation_tool],
            )
        else:
            logger.info(f"{indent}   🍃 Creating leaf agent (simple module)")
            sub_agent = Agent(
                model=fallback_models,
                name=sub_module_name,
                deps_type=CodeWikiDeps,
                system_prompt=LEAF_SYSTEM_PROMPT.format(module_name=sub_module_name, language=deps.config.language),
                tools=[read_code_components_tool, str_replace_editor_tool],
            )

        deps.current_module_name = sub_module_name
        deps.path_to_current_module.append(sub_module_name)
        deps.current_depth += 1

        try:
            # Use iter() with node.stream() to enable streaming API (stream: true)
            logger.info(f"{indent}   🚀 Running sub-agent with iter() + node.stream(): {sub_module_name}")
            
            async with sub_agent.iter(
                format_user_prompt(
                    module_name=deps.current_module_name,
                    core_component_ids=core_component_ids,
                    components=ctx.deps.components,
                    module_tree=ctx.deps.module_tree,
                    language=deps.config.language
                ),
                deps=ctx.deps,
                usage=ctx.usage  # Pass parent usage to delegate agent for token tracking
            ) as sub_agent_run:
                # Iterate through nodes and stream model requests
                node_count = 0
                async for node in sub_agent_run:
                    node_count += 1
                    node_type = type(node).__name__
                    logger.info(f"{indent}      📍 Sub-node #{node_count}: {node_type}")
                    
                    # Log detailed information for each node type
                    if node_type == 'UserPromptNode':
                        logger.debug(f"{indent}         💬 User prompt initialized")
                    elif node_type == 'ModelRequestNode':
                        logger.debug(f"{indent}         🤖 Model request sent")
                    elif node_type == 'CallToolsNode':
                        # Extract model response details
                        if hasattr(node, 'model_response'):
                            model_response = node.model_response
                            if hasattr(model_response, 'usage'):
                                usage = model_response.usage
                                logger.info(f"{indent}         📊 Model response - Input: {usage.input_tokens}, Output: {usage.output_tokens}, Total: {usage.total_tokens} tokens")
                            # Log which tools were called
                            if hasattr(model_response, 'parts'):
                                for part in model_response.parts:
                                    part_type = type(part).__name__
                                    if part_type == 'ToolCallPart':
                                        logger.info(f"{indent}         🔧 Tool called: {part.tool_name}")
                                        logger.debug(f"{indent}            Args: {part.args}")
                    elif node_type == 'ToolReturnNode':
                        logger.debug(f"{indent}         ✅ Tool execution completed")
                    elif node_type == 'End':
                        logger.info(f"{indent}         🏁 Sub-agent execution finished")
                    
                    # Stream model request nodes to enable streaming API
                    from pydantic_ai import Agent as PydanticAgent
                    if PydanticAgent.is_model_request_node(node):
                        async with node.stream(sub_agent_run.ctx) as stream:
                            async for event in stream:
                                # Just consume the stream to enable streaming
                                pass
                
                # Get final result from sub_agent_run.result
                if sub_agent_run.result:
                    result = sub_agent_run.result
                    
                    # Note: Token usage is automatically aggregated via usage=ctx.usage parameter
                    # Log the token usage for this sub-module for visibility
                    if hasattr(sub_agent_run, 'usage') and callable(sub_agent_run.usage):
                        usage_stats = sub_agent_run.usage()
                        if usage_stats and hasattr(usage_stats, 'total_tokens'):
                            logger.info(f"{indent}   📊 Sub-module token usage: {usage_stats.total_tokens} tokens")
            
            logger.info(f"{indent}   ✅ Sub-agent completed")
            
            logger.info(f"{indent}   ✅ Completed sub-module: {sub_module_name}")
        except Exception as e:
            logger.error(f"{indent}   ❌ Failed sub-module {sub_module_name}: {str(e)}")
            logger.error(f"{indent}   Traceback: {traceback.format_exc()}")
            logger.info(f"{indent}   ⏩ Continuing with next sub-module...")
        finally:
            deps.path_to_current_module.pop()
            deps.current_depth -= 1

    # restore the previous module name
    deps.current_module_name = previous_module_name

    return f"Generate successfully. Documentations: {', '.join([key + '.md' for key in sub_module_specs.keys()])} are saved in the working directory."


generate_sub_module_documentation_tool = Tool(function=generate_sub_module_documentation, name="generate_sub_module_documentation", description="Generate detailed description of a given sub-module specs to the sub-agents", takes_ctx=True)