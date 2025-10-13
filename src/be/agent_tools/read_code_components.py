from ..native_agent import AgentContext, create_tool_definition


async def read_code_components(ctx: AgentContext, component_ids: list[str]) -> str:
    """Read the code of a given component id

    Args:
        component_ids: The ids of the components to read, e.g. ["sweagent.types.AgentRunResult", "sweagent.types.AgentRunResult"] where sweagent.types part is the path to the component and AgentRunResult is the name of the component
    """

    results = []

    for component_id in component_ids:
        if component_id not in ctx.deps.components:
            results.append(f"# Component {component_id} not found")
        else:
            results.append(f"# Component {component_id}:\n{ctx.deps.components[component_id].source_code.strip()}\n\n")

    return "\n".join(results)


# Tool definition for native OpenAI function calling
read_code_components_tool = create_tool_definition(
    name="read_code_components",
    description="Read the code of a given list of component ids",
    function=read_code_components,
    parameters={
        "type": "object",
        "properties": {
            "component_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The ids of the components to read, e.g. ['sweagent.types.AgentRunResult']"
            }
        },
        "required": ["component_ids"]
    },
    takes_ctx=True
)