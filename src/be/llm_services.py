from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIModelSettings
from pydantic_ai.models.fallback import FallbackModel

from config import MAIN_MODEL, FALLBACK_MODEL_1, LLM_BASE_URL, LLM_API_KEY


main_model = OpenAIModel(
    model_name=MAIN_MODEL,
    provider=OpenAIProvider(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY
    ),
    settings=OpenAIModelSettings(
        temperature=0.0,
        max_tokens=32768
    )
)

fallback_model_1 = OpenAIModel(
    model_name=FALLBACK_MODEL_1,
    provider=OpenAIProvider(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY
    ),
    settings=OpenAIModelSettings(
        temperature=0.0,
        max_tokens=32768
    )
)

fallback_models = FallbackModel(main_model, fallback_model_1)

# ------------------------------------------------------------
from openai import OpenAI

client = OpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY
)

def call_llm(prompt: str, model: str = MAIN_MODEL, temperature: float = 0.0):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=32768
    )
    return response.choices[0].message.content