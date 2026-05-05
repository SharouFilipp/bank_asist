# services/llm_utils.py
import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

def ask_llm(prompt: str, model: str = None, temperature: float = 0.3, max_tokens: int = 5000) -> str:
    """Отправляет запрос к LLM (OpenRouter или Ollama) и возвращает ответ."""
    OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
    if OPENROUTER_KEY:
        try:
            import openai
            client = openai.OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_KEY,
                default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "Banking RAG"}
            )
            model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free") #openai/gpt-3.5-turbo
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"Ошибка OpenRouter: {e}")
    # Fallback: локальный Ollama
    try:
        import ollama
        resp = ollama.chat(model=model or "mistral:7b", messages=[{"role": "user", "content": prompt}])
        return resp["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка Ollama: {e}")
        return None