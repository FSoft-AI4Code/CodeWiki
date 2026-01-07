"""
LLM service factory for creating configured LLM clients.
"""
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.models.fallback import FallbackModel
from openai import OpenAI

from codewiki.src.config import Config


def create_main_model(config: Config) -> OpenAIChatModel:
    """Create the main LLM model from configuration."""
    return OpenAIChatModel(
        model_name=config.main_model,
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key
        ),
        settings=OpenAIChatModelSettings(
            temperature=0.0,
            max_tokens=32768
        )
    )


def create_fallback_model(config: Config) -> OpenAIChatModel:
    """Create the fallback LLM model from configuration."""
    return OpenAIChatModel(
        model_name=config.fallback_model,
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key
        ),
        settings=OpenAIChatModelSettings(
            temperature=0.0,
            max_tokens=32768
        )
    )


def create_fallback_models(config: Config) -> FallbackModel:
    """Create fallback models chain from configuration."""
    main = create_main_model(config)
    fallback = create_fallback_model(config)
    return FallbackModel(main, fallback)


def create_openai_client(config: Config) -> OpenAI:
    """Create OpenAI client from configuration."""
    return OpenAI(
        base_url=config.llm_base_url,
        api_key=config.llm_api_key
    )


def call_llm(
    prompt: str,
    config: Config,
    model: str = None,
    temperature: float = 0.0,
    return_usage: bool = False,
    stream: bool = True
) -> str | tuple[str, dict]:
    """
    Call LLM with the given prompt.
    
    Args:
        prompt: The prompt to send
        config: Configuration containing LLM settings
        model: Model name (defaults to config.main_model)
        temperature: Temperature setting
        return_usage: If True, return (text, usage_dict) tuple
        stream: Enable streaming mode (default: True)
        
    Returns:
        LLM response text, or (text, usage_dict) if return_usage=True
        usage_dict contains: {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    """
    if model is None:
        model = config.main_model
    
    client = create_openai_client(config)
    
    if stream:
        # Use streaming mode with usage stats if requested
        stream_options = {"include_usage": True} if return_usage else None
        
        response_stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=32768,
            stream=True,
            stream_options=stream_options
        )
        
        # Collect streamed chunks and usage from final chunk
        content_chunks = []
        usage_data = None
        
        for chunk in response_stream:
            if chunk.choices and chunk.choices[0].delta.content:
                content_chunks.append(chunk.choices[0].delta.content)
            
            # Usage info is in the final chunk when stream_options={"include_usage": True}
            if return_usage and hasattr(chunk, 'usage') and chunk.usage:
                usage_data = chunk.usage
        
        content = ''.join(content_chunks)
        
        if return_usage:
            usage = {
                "prompt_tokens": usage_data.prompt_tokens if usage_data else 0,
                "completion_tokens": usage_data.completion_tokens if usage_data else 0,
                "total_tokens": usage_data.total_tokens if usage_data else 0,
            }
            return content, usage
    else:
        # Use non-streaming mode
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=32768,
            stream=False
        )
        
        content = response.choices[0].message.content
        
        if return_usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            return content, usage
    
    return content