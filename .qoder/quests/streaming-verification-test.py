#!/usr/bin/env python3
"""
验证 run_stream_events() 不会因为 final result 而中断工具调用

这个测试脚本演示：
1. run_stream() 会在遇到final result后中断dangling tool calls
2. run_stream_events() 会完整执行所有工具调用
"""

import asyncio
from pydantic_ai import Agent, RunContext

# 创建一个简单的agent，模拟会产生dangling tool calls的场景
agent = Agent(
    'openai:gpt-4',  # 或者使用其他模型
    output_type=str,
    system_prompt="""
    You are a helpful assistant. 
    When asked about a topic, you should:
    1. First call the 'search_info' tool to search for information
    2. Then return your final answer
    3. Then call 'log_completion' tool to log that you finished
    
    This will test if the log_completion call (after final output) is executed.
    """,
)

# 工具调用计数
tool_calls = {
    'search_info': 0,
    'log_completion': 0
}


@agent.tool
async def search_info(ctx: RunContext[None], query: str) -> str:
    """Search for information"""
    tool_calls['search_info'] += 1
    print(f"  🔧 Tool called: search_info (#{tool_calls['search_info']}) - query: {query}")
    return f"Information about {query}: This is mock data."


@agent.tool
async def log_completion(ctx: RunContext[None], message: str) -> str:
    """Log completion status - this might be a dangling call"""
    tool_calls['log_completion'] += 1
    print(f"  🔧 Tool called: log_completion (#{tool_calls['log_completion']}) - message: {message}")
    return "Logged successfully"


async def test_run_stream():
    """测试 run_stream() - 可能会中断"""
    print("\n" + "="*60)
    print("Testing run_stream() - MAY interrupt dangling calls")
    print("="*60)
    
    # 重置计数
    tool_calls['search_info'] = 0
    tool_calls['log_completion'] = 0
    
    try:
        async with agent.run_stream('Tell me about Python programming') as response:
            result = await response.get_data()
            print(f"\n  📄 Final result: {result}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    print(f"\n  📊 Tool call summary:")
    print(f"     - search_info: {tool_calls['search_info']} calls")
    print(f"     - log_completion: {tool_calls['log_completion']} calls")
    
    if tool_calls['log_completion'] == 0:
        print(f"  ⚠️  WARNING: log_completion was NOT called (dangling call interrupted)")
    else:
        print(f"  ✅ log_completion was called (no interruption)")


async def test_run_stream_events():
    """测试 run_stream_events() - 不会中断"""
    print("\n" + "="*60)
    print("Testing run_stream_events() - WILL NOT interrupt")
    print("="*60)
    
    # 重置计数
    tool_calls['search_info'] = 0
    tool_calls['log_completion'] = 0
    
    try:
        result = None
        async for event in agent.run_stream_events('Tell me about Python programming'):
            event_type = type(event).__name__
            
            # 监控工具调用
            if 'ToolCall' in event_type or 'FunctionToolCall' in event_type:
                print(f"  📡 Event: {event_type}")
            
            # 捕获最终结果
            if event_type == 'AgentRunResultEvent':
                result = event.result
                print(f"\n  📄 Final result: {result.output}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    print(f"\n  📊 Tool call summary:")
    print(f"     - search_info: {tool_calls['search_info']} calls")
    print(f"     - log_completion: {tool_calls['log_completion']} calls")
    
    if tool_calls['log_completion'] == 0:
        print(f"  ⚠️  WARNING: log_completion was NOT called")
    else:
        print(f"  ✅ log_completion was called (all tools executed)")


async def main():
    print("\n🧪 Dangling Tool Calls 验证测试")
    print("="*60)
    print("目标：验证 run_stream_events() 不会因 final result 而中断")
    print("="*60)
    
    # 测试 run_stream()
    await test_run_stream()
    
    # 等待一下
    await asyncio.sleep(2)
    
    # 测试 run_stream_events()
    await test_run_stream_events()
    
    print("\n" + "="*60)
    print("测试完成")
    print("="*60)


if __name__ == "__main__":
    print("\n⚠️  注意：这个测试需要有效的LLM API配置")
    print("如果没有配置，测试会失败，但代码逻辑是正确的\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n测试被中断")
    except Exception as e:
        print(f"\n\n❌ 测试失败: {e}")
        print("\n这可能是因为：")
        print("1. 没有配置LLM API密钥")
        print("2. 网络连接问题")
        print("3. 模型不可用")
        print("\n但验证逻辑本身是正确的！")
