"""
Native OpenAI Agent Implementation
Replaces pydantic_ai with direct OpenAI API calls
"""

import json
import logging
from typing import Dict, List, Any, Callable, Optional
from dataclasses import dataclass
from openai import OpenAI

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Definition of a tool that can be called by the agent"""
    name: str
    description: str
    function: Callable
    parameters: Dict[str, Any]
    takes_ctx: bool = False


class AgentContext:
    """Context object passed to tool functions"""
    def __init__(self, deps: Any):
        self.deps = deps


class NativeAgent:
    """
    Native OpenAI agent implementation using function calling.
    Replaces pydantic_ai Agent class.
    """
    
    def __init__(
        self,
        client: OpenAI,
        model: str,
        fallback_model: Optional[str] = None,
        name: str = "agent",
        tools: List[ToolDefinition] = None,
        system_prompt: str = "",
        temperature: float = 0.0,
        max_tokens: int = 32768,
        max_iterations: int = 50
    ):
        self.client = client
        self.model = model
        self.fallback_model = fallback_model
        self.name = name
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        
        # Create tool map for quick lookup
        self.tool_map = {tool.name: tool for tool in self.tools}
    
    def _get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Convert tool definitions to OpenAI function calling format"""
        schemas = []
        for tool in self.tools:
            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
            }
            schemas.append(schema)
        return schemas
    
    async def _execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        ctx: AgentContext
    ) -> str:
        """Execute a tool function and return its result"""
        if tool_name not in self.tool_map:
            return f"Error: Tool {tool_name} not found"
        
        tool = self.tool_map[tool_name]
        
        try:
            # If tool takes context, pass it as first argument
            if tool.takes_ctx:
                result = await tool.function(ctx, **arguments)
            else:
                result = await tool.function(**arguments)
            
            # Convert result to string if it isn't already
            if not isinstance(result, str):
                result = json.dumps(result)
            
            return result
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {str(e)}")
            return f"Error executing {tool_name}: {str(e)}"
    
    def _call_llm(self, messages: List[Dict[str, Any]], use_tools: bool = True) -> Dict[str, Any]:
        """Make a call to the LLM with optional tool support"""
        try:
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens
            }
            
            if use_tools and self.tools:
                kwargs["tools"] = self._get_tool_schemas()
                kwargs["tool_choice"] = "auto"
            
            response = self.client.chat.completions.create(**kwargs)
            return response
            
        except Exception as e:
            # Try fallback model if available
            if self.fallback_model:
                logger.warning(f"Primary model failed: {e}. Trying fallback model: {self.fallback_model}")
                try:
                    kwargs["model"] = self.fallback_model
                    response = self.client.chat.completions.create(**kwargs)
                    return response
                except Exception as fallback_error:
                    logger.error(f"Fallback model also failed: {fallback_error}")
                    raise
            raise
    
    async def run(self, user_message: str, deps: Any) -> str:
        """
        Run the agent with a user message.
        
        Args:
            user_message: The user's input message
            deps: Dependencies to pass to tool functions
            
        Returns:
            The final response from the agent
        """
        # Initialize context
        ctx = AgentContext(deps=deps)
        
        # Initialize conversation with system prompt
        messages = []
        if self.system_prompt:
            messages.append({
                "role": "system",
                "content": self.system_prompt
            })
        messages.append({
            "role": "user",
            "content": user_message
        })
        
        # Agent loop
        iteration = 0
        while iteration < self.max_iterations:
            iteration += 1
            logger.info(f"Agent iteration {iteration}/{self.max_iterations}")
            
            # Call LLM
            response = self._call_llm(messages, use_tools=True)
            assistant_message = response.choices[0].message
            
            # Add assistant message to conversation
            messages.append({
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": assistant_message.tool_calls if hasattr(assistant_message, 'tool_calls') and assistant_message.tool_calls else None
            })
            
            # Check if we're done (no tool calls)
            if not hasattr(assistant_message, 'tool_calls') or not assistant_message.tool_calls:
                # Return final response
                return assistant_message.content if assistant_message.content else "Task completed."
            
            # Execute tool calls
            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse tool arguments: {e}")
                    arguments = {}
                
                logger.info(f"Executing tool: {tool_name}")
                logger.debug(f"Tool arguments: {arguments}")
                
                # Execute the tool
                tool_result = await self._execute_tool(tool_name, arguments, ctx)
                
                # Add tool result to conversation
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": tool_result
                })
        
        # If we hit max iterations, return what we have
        logger.warning(f"Agent reached max iterations ({self.max_iterations})")
        return "Task execution reached maximum iterations. Please check the results."


def create_tool_definition(
    name: str,
    description: str,
    function: Callable,
    parameters: Dict[str, Any],
    takes_ctx: bool = False
) -> ToolDefinition:
    """
    Helper function to create a tool definition.
    
    Args:
        name: Name of the tool
        description: Description of what the tool does
        function: The function to call
        parameters: JSON schema for the function parameters
        takes_ctx: Whether the function takes a context as first argument
    """
    return ToolDefinition(
        name=name,
        description=description,
        function=function,
        parameters=parameters,
        takes_ctx=takes_ctx
    )

