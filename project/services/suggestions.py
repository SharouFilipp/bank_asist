import datetime
import re
import os
import logging
from typing import List

logger = logging.getLogger(__name__)

# Тот же ask_llm, что и в api.py (можно импортировать, но для автономности продублируем)
def ask_llm(prompt, model=None, temperature=0.3, max_tokens=500):
    OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
    if OPENROUTER_KEY:
        try:
            import openai
            client = openai.OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_KEY,
                default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "Banking RAG"}
            )
            model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-3.5-turbo")
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"OpenRouter error: {e}")
    try:
        import ollama
        resp = ollama.chat(model=model or "mistral:7b", messages=[{"role": "user", "content": prompt}])
        return resp["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return None

AVAILABLE_FUNCTIONS = [
    "курс валют (доллар, евро, рубль и др.)",
    "адреса и часы работы филиалов банка",
    "загрузка и распознавание чеков (сумма, категория, валюта)",
    "просмотр статистики расходов (диаграмма)",
    "конвертер валют (например, 100 USD в BYN)",
    "общие вопросы о кредитах, вкладах, банковских услугах",
    "помощь в редактировании суммы чека"
]

STARTER_SUGGESTIONS = [
    "Какой курс доллара?",
    "Где ближайший филиал в Минске?",
    "Конвертировать 100 USD в BYN",
    "Покажи мои расходы за месяц",
    "Как сэкономить на продуктах?",
]

def get_suggestions(user_id: int, q: str = "") -> List[str]:
    """Возвращает стартовые подсказки (все или отфильтрованные по q)."""
    if not q:
        return STARTER_SUGGESTIONS[:]
    q_lower = q.lower()
    # Простой фильтр по вхождению строки
    filtered = [s for s in STARTER_SUGGESTIONS if q_lower in s.lower()]
    if not filtered:
        # Если ничего не найдено, можно вернуть пару общих
        filtered = ["Конвертировать валюту", "Найти отделение", "Показать расходы"]
    return filtered

def generate_follow_ups(question: str, answer: str, max_count: int = 3) -> List[str]:
    """Генерирует контекстные продолжения диалога."""
    func_list = "\n".join(f"- {f}" for f in AVAILABLE_FUNCTIONS)
    prompt = f"""Ты — банковский ассистент. Пользователь спросил: «{question}»
Ты ответил: «{answer[:500]}...»

Твои возможности:
{func_list}

Предложи ровно {max_count} варианта того, что пользователь может спросить дальше, **используя только перечисленные возможности**. Короткие фразы (до 20 слов), на русском, каждая с новой строки, без нумерации.
Варианты:"""
    try:
        response = ask_llm(prompt, temperature=0.7, max_tokens=300)
        if not response:
            return ["Можете уточнить?", "Приведите пример.", "Покажите по шагам."]
        lines = [line.strip() for line in response.split('\n') if line.strip()]
        filtered = [l for l in lines if not re.search(r'\b(pdf|видео|сравни(ть)?\s+с\s+другими|отчёт\s+в\s+excel)\b', l, re.IGNORECASE)]
        return filtered[:max_count] if filtered else ["Можете уточнить?", "Приведите пример.", "Покажите по шагам."]
    except:
        return ["Можете уточнить?", "Приведите пример.", "Покажите по шагам."]