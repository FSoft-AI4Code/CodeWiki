from pydantic_ai import Agent
# import logfire
import logging
import os
import traceback
from typing import Dict, List, Any

# Configure logging and monitoring

logger = logging.getLogger(__name__)

# try:
#     # Configure logfire with environment variables for Docker compatibility
#     logfire_token = os.getenv('LOGFIRE_TOKEN')
#     logfire_project = os.getenv('LOGFIRE_PROJECT_NAME', 'default')
#     logfire_service = os.getenv('LOGFIRE_SERVICE_NAME', 'default')
    
#     if logfire_token:
#         # Configure with explicit token (for Docker)
#         logfire.configure(
#             token=logfire_token,
#             project_name=logfire_project,
#             service_name=logfire_service,
#         )
#     else:
#         # Use default configuration (for local development with logfire auth)
#         logfire.configure(
#             project_name=logfire_project,
#             service_name=logfire_service,
#         )
    
#     logfire.instrument_pydantic_ai()
#     logger.debug(f"Logfire configured successfully for project: {logfire_project}")
    
# except Exception as e:
#     logger.warning(f"Failed to configure logfire: {e}")

# Local imports
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.agent_tools.generate_sub_module_documentations import generate_sub_module_documentation_tool
from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.prompt_template import (
    SYSTEM_PROMPT,
    LEAF_SYSTEM_PROMPT,
    format_user_prompt,
)
from codewiki.src.be.utils import is_complex_module
from codewiki.src.config import (
    Config,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME,
)
from codewiki.src.utils import file_manager
from codewiki.src.be.dependency_analyzer.models.core import Node


class AgentOrchestrator:
    """Orchestrates the AI agents for documentation generation."""
    
    def __init__(self, config: Config):
        self.config = config
        self.fallback_models = create_fallback_models(config)
        self.progress_callback = None
    
    def set_progress_callback(self, callback):
        """Set a callback function for progress updates."""
        self.progress_callback = callback
    
    def create_agent(self, module_name: str, components: Dict[str, Any], 
                    core_component_ids: List[str]) -> Agent:
        """Create an appropriate agent based on module complexity."""
        if is_complex_module(components, core_component_ids):
            return Agent(
                self.fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[
                    read_code_components_tool, 
                    str_replace_editor_tool, 
                    generate_sub_module_documentation_tool
                ],
                system_prompt=SYSTEM_PROMPT.format(module_name=module_name, language=self.config.language),
            )
        else:
            return Agent(
                self.fallback_models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[read_code_components_tool, str_replace_editor_tool],
                system_prompt=LEAF_SYSTEM_PROMPT.format(module_name=module_name, language=self.config.language),
            )
    
    async def process_module(self, module_name: str, components: Dict[str, Node], 
                           core_component_ids: List[str], module_path: List[str], working_dir: str) -> tuple[Dict[str, Any], dict]:
        """Process a single module and generate its documentation.
        
        Returns:
            Tuple of (module_tree, token_usage)
        """
        logger.info(f"🔄 Starting to process module: {module_name}")
        logger.info(f"   📦 Core components: {len(core_component_ids)}")
        
        # Send progress update for component processing
        if self.progress_callback:
            self.progress_callback(
                progress=f"Processing module: {module_name} ({len(core_component_ids)} components)",
                current_component=f"{module_name} (starting)",
                total_components=len(core_component_ids),
                total_tokens=0  # Will be updated after agent run
            )
        
        # Load or create module tree
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)
        
        # Create agent
        logger.info(f"   🤖 Creating agent for module: {module_name}")
        agent = self.create_agent(module_name, components, core_component_ids)
        
        # Create dependencies
        deps = CodeWikiDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=str(os.path.abspath(self.config.repo_path)),
            registry={},
            components=components,
            path_to_current_module=module_path,
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=self.config.max_depth,
            current_depth=1,
            config=self.config
        )

        # check if overview docs already exists
        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            logger.info(f"   ✓ Overview docs already exists, skipping")
            return module_tree, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # check if module docs already exists
        docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            logger.info(f"   ✓ Module docs already exists, skipping")
            return module_tree, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        
        # Run agent with iter() and stream nodes for true streaming API
        try:
            logger.info(f"   🚀 Running agent for module: {module_name} (streaming mode)")
            
            # Send progress update
            if self.progress_callback:
                self.progress_callback(
                    progress=f"Generating documentation for module: {module_name}",
                    current_component=f"{module_name} (generating docs)",
                    total_tokens=0  # Will be updated after agent run
                )
            
            # Use iter() with node.stream() to enable streaming API (stream: true)
            logger.info(f"   🚀 Running agent with iter() + node.stream() for module: {module_name}")
            
            async with agent.iter(
                format_user_prompt(
                    module_name=module_name,
                    core_component_ids=core_component_ids,
                    components=components,
                    module_tree=deps.module_tree,
                    language=self.config.language
                ),
                deps=deps
            ) as agent_run:
                # Iterate through nodes and stream model requests
                async for node in agent_run:
                    node_type = type(node).__name__
                    logger.debug(f"   📍 Node: {node_type}")
                    
                    # Stream model request nodes to enable streaming API
                    if Agent.is_model_request_node(node):
                        async with node.stream(agent_run.ctx) as stream:
                            async for event in stream:
                                # Just consume the stream to enable streaming
                                pass
                
                # Get final result from agent_run.result
                if agent_run.result:
                    result = agent_run.result
                    
                    # Extract token usage from result
                    if hasattr(result, 'usage') and result.usage:
                        usage_data = {
                            "prompt_tokens": result.usage.request_tokens if hasattr(result.usage, 'request_tokens') else 0,
                            "completion_tokens": result.usage.response_tokens if hasattr(result.usage, 'response_tokens') else 0,
                            "total_tokens": result.usage.total_tokens if hasattr(result.usage, 'total_tokens') else 0,
                        }
                        deps.add_token_usage(usage_data)
            
            logger.info(f"   ✅ Agent completed")
            
            # Send progress update with token usage
            if self.progress_callback:
                self.progress_callback(
                    progress=f"Completed module: {module_name}",
                    current_component=f"{module_name} (done)",
                    total_tokens=deps.token_usage["total_tokens"]
                )
            
            # Save updated module tree
            logger.info(f"   💾 Saving module tree for: {module_name}")
            file_manager.save_json(deps.module_tree, module_tree_path)
            logger.info(f"   ✅ Successfully completed module: {module_name}")
            
            return deps.module_tree, deps.token_usage
            
        except Exception as e:
            logger.error(f"   ❌ Error processing module {module_name}: {str(e)}")
            logger.error(f"   Traceback: {traceback.format_exc()}")
            raise