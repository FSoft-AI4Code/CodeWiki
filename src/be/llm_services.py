from openai import OpenAI
from config import MAIN_MODEL, FALLBACK_MODEL_1, LLM_BASE_URL, LLM_API_KEY

# Native OpenAI client configuration

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