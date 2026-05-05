# services/llm_classifier.py
import json
import logging
from services.llm_utils import ask_llm   # перенесённая функция

logger = logging.getLogger(__name__)

def llm_classify_and_extract(query: str) -> dict:
    """
    Классифицирует запрос и извлекает параметры с помощью LLM.
    Поддерживает intent'ы:
        currency_rate, expense, currency_dynamics, conversion,
        filial, filial_detail, best_exchange, rag
    """
    prompt = f"""
Ты — помощник классификации запросов банковского ассистента.
Определи намерение пользователя и извлеки параметры.
Верни ТОЛЬКО JSON без лишнего текста:
{{
  "intent": "currency_rate" | "expense" | "currency_dynamics" | "conversion" | "filial" | "filial_detail" | "expense_period" | "saving_advice" | "best_exchange" | "rag" | "item_search",
  "parameters": {{
    "currency": "USD" | "EUR" | "RUB" | null,
    "city": "Минск" | "Брест" | "Гродно" | ... | null,
    "amount": число | null,
    "from_currency": "USD" | null,
    "to_currency": "BYN" | null,
    "category": "продукты" | "транспорт" | null,
    "is_trading_advice": true | false,
    "operation": "buy" | "sell" | null,
    "filial_ref": "номер/адрес отделения или порядковый номер" | null,
    "detail_type": "hours" | "rates" | "address" | "all" | null,
    "period": "прошлая неделя" | "этот месяц" | "январь 2026" | "12.04.2025" | null
  }},
  "confidence": число от 0 до 1
}}


filial_detail — запрос о конкретном отделении: часы, курсы, адрес, полная инфа.
filial_ref — ссылка: номер ("400/4001"), адрес ("Горького 91"), порядковый номер ("1", "первое").
detail_type: "hours" — режим работы, "rates" — курсы в отделении, "address" — адрес, "all" — всё.

Примеры filial_detail:
Запрос: "часы работы отделения 400/4001"
Ответ: {{"intent": "filial_detail", "parameters": {{"filial_ref": "400/4001", "detail_type": "hours"}}, "confidence": 0.97}}

Запрос: "какой курс в первом отделении"
Ответ: {{"intent": "filial_detail", "parameters": {{"filial_ref": "1", "detail_type": "rates"}}, "confidence": 0.95}}

Запрос: "курсы на горького 91"
Ответ: {{"intent": "filial_detail", "parameters": {{"filial_ref": "Горького 91", "detail_type": "rates"}}, "confidence": 0.96}}

Запрос: "когда работает отделение на советских пограничников"
Ответ: {{"intent": "filial_detail", "parameters": {{"filial_ref": "советских пограничников", "detail_type": "hours"}}, "confidence": 0.95}}

Запрос: "часы работы отделений в гродно"
Ответ: {{"intent": "filial_detail", "parameters": {{"city": "Гродно", "filial_ref": null, "detail_type": "hours"}}, "confidence": 0.92}}

Запрос: "расскажи подробнее про второе"
Ответ: {{"intent": "filial_detail", "parameters": {{"filial_ref": "2", "detail_type": "all"}}, "confidence": 0.93}}


expense_period — запрос о расходах за конкретный период (неделя, месяц, дата, год).
saving_advice — запрос о советах по экономии, анализ трат, программы лояльности.

Примеры expense_period:
Запрос: "Сколько я потратил на прошлой неделе?"
Ответ: {{"intent": "expense_period", "parameters": {{"period": "прошлая неделя"}}, "confidence": 0.97}}

Запрос: "Расходы в январе 2026"
Ответ: {{"intent": "expense_period", "parameters": {{"period": "январь 2026"}}, "confidence": 0.96}}

Запрос: "Что я покупал 15.03.2025?"
Ответ: {{"intent": "expense_period", "parameters": {{"period": "15.03.2025"}}, "confidence": 0.95}}

Запрос: "Траты за прошлый месяц"
Ответ: {{"intent": "expense_period", "parameters": {{"period": "прошлый месяц"}}, "confidence": 0.96}}

Примеры saving_advice:
Запрос: "Как мне сэкономить?"
Ответ: {{"intent": "saving_advice", "parameters": {{}}, "confidence": 0.95}}

Запрос: "Проанализируй мои расходы и дай советы"
Ответ: {{"intent": "saving_advice", "parameters": {{}}, "confidence": 0.96}}

Запрос: "Какие программы лояльности есть в моих магазинах?"
Ответ: {{"intent": "saving_advice", "parameters": {{}}, "confidence": 0.94}}

best_exchange – запрос, где пользователь хочет найти **лучшее**, **выгодное** или **самое удобное** отделение для обмена валюты (купить или продать).
operation=buy – купить валюту, operation=sell – продать валюту.
Если валюта не названа, поле currency должно быть null.
Если город не назван, поле city должно быть null.

Примеры best_exchange:
Запрос: "где лучше всего купить доллары в Минске?"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": "USD", "city": "Минск", "operation": "buy"}}, "confidence": 0.96}}

Запрос: "выгодный обмен евро в Бресте"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": "EUR", "city": "Брест", "operation": "buy"}}, "confidence": 0.93}}

Запрос: "где продать рубли дороже в Гродно?"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": "RUB", "city": "Гродно", "operation": "sell"}}, "confidence": 0.94}}

Запрос: "в каком отделении самый выгодный обмен?"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": null, "city": null, "operation": "buy"}}, "confidence": 0.9}}

Запрос: "самый выгодный курс доллара"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": "USD", "city": null, "operation": "buy"}}, "confidence": 0.92}}

Запрос: "в каком отделении в Гродно самый выгодный обмен?"
Ответ: {{"intent": "best_exchange", "parameters": {{"currency": null, "city": "Гродно", "operation": "buy"}}, "confidence": 0.95}}

Примеры item_search:
Запрос: "Сколько я потратил на торт?"
Ответ: {{"intent": "item_search", "parameters": {{"query": "торт"}}, "confidence": 0.94}}

Запрос: "Расходы на кофе за месяц"
Ответ: {{"intent": "item_search", "parameters": {{"query": "кофе"}}, "confidence": 0.93}}

Примеры expense:
Запрос: "Сколько я потратил на транспорт?"
Ответ: {{"intent": "expense", "parameters": {{"category": "транспорт"}}, "confidence": 0.95}}

Запрос: "Расходы на здоровье"
Ответ: {{"intent": "expense", "parameters": {{"category": "здоровье"}}, "confidence": 0.94}}

Запрос: "Сколько я потратил на продукты и на какие товары?"
Ответ: {{"intent": "expense", "parameters": {{"category": "продукты"}}, "confidence": 0.95}}

Запрос: "Сколько на кафе и что именно я там брал?"
Ответ: {{"intent": "expense", "parameters": {{"category": "кафе"}}, "confidence": 0.93}}

Запрос: "Покажи расходы на услуги"
Ответ: {{"intent": "expense", "parameters": {{"category": "услуги"}}, "confidence": 0.94}}

Запрос: "Сколько я потратил на пончики?"
Ответ: {{"intent": "item_search", "parameters": {{"query": "пончик"}}, "confidence": 0.93}}

is_trading_advice должно быть true, если пользователь просит совет, рекомендацию, стоит ли покупать/продавать валюту, или спрашивает о торгах, объёмах, бирже. В остальных случаях false.
Если намерение не связано с валютой, is_trading_advice всегда false.

Примеры trading_advice:

Запрос: "Курсы валют на завтра"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": true, "currency": null}}, "confidence": 0.95}}

Запрос: "Какой курс доллара будет через неделю?"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": true, "currency": "USD"}}, "confidence": 0.94}}

Запрос: "Дай рекомендации когда лучше купить валюту?"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": true, "currency": null}}, "confidence": 0.96}}

Запрос: "Стоит ли покупать доллары сейчас?"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": true, "currency": "USD"}}, "confidence": 0.97}}

Запрос: "Какие объемы торгов по евро?"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": true, "currency": "EUR"}}, "confidence": 0.96}}

Запрос: "Курс доллара в Минске"
Ответ: {{"intent": "currency_rate", "parameters": {{"is_trading_advice": false, "currency": "USD", "city": "Минск"}}, "confidence": 0.98}}

Если запрос про филиалы или отделения, обязательно укажи город в поле city.
Если запрос про курсы валют в конкретном городе, тоже заполняй city.

Запрос: "{query}"
Ответ:"""

    try:
        raw = ask_llm(prompt, temperature=0.0, max_tokens=400)
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end > start:
            result = json.loads(raw[start:end])
            result.setdefault('parameters', {})
            result.setdefault('confidence', 0.5)
            print(result)
            return result
    except Exception as e:
        logger.warning(f"LLM классификация не удалась: {e}")
    return {"intent": "rag", "parameters": {}, "confidence": 0.0}