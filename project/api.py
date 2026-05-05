from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional, List
import os
import uuid
import pytesseract
import logging
from datetime import timedelta,datetime
from collections import defaultdict
import re
import json
from sqlalchemy.orm import Session
from database import SessionLocal, User, FilialLocation, ReceiptItem
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, get_db, ACCESS_TOKEN_EXPIRE_MINUTES
)
from services.expenses import create_receipt, get_receipts_by_user, update_receipt
from services.charts import build_expense_chart
from services.currency import convert, get_supported_currencies
import time
import asyncio
from services.currency import load_bank_rates_from_json
from currency_updater import fetch_and_save_kurs_exchange, rebuild_chroma
from services.llm_utils import ask_llm
from services.currency_parser import get_cached_trading_data, fetch_and_cache_trading_data
import traceback
from rag_crew import DocumentSearchTool
# CrewAI (оставляем, но используем аккуратно)
from rag_crew import banking_crew

from services.suggestions import get_suggestions, generate_follow_ups
from langchain_community.vectorstores import Chroma
from services.ocr_services import ocr_image, extract_receipt_data_with_llm



last_filial_context = {}
last_conversion_context = {}
exchange_context = {}
pending_exchange_context = {}  # user_id: {"currency": ..., "operation": ...}
last_expense_context = {}      # user_id: {"category": str, "total": float, "receipt_ids": list}

# ===== Единая история диалога =====
# user_id -> deque из {"role": "user"|"assistant", "text": str, "category": str, "data": dict}
from collections import deque
_conversation_history: dict = {}  # user_id -> deque(maxlen=10)

def _get_history(user_id: int) -> deque:
    if user_id not in _conversation_history:
        _conversation_history[user_id] = deque(maxlen=10)
    return _conversation_history[user_id]

def _push_history(user_id: int, role: str, text: str, category: str = "", data: dict = None):
    _get_history(user_id).append({
        "role": role, "text": text,
        "category": category, "data": data or {}
    })

def _history_as_text(user_id: int, max_turns: int = 6) -> str:
    hist = list(_get_history(user_id))[-max_turns:]
    lines = []
    for h in hist:
        prefix = "Пользователь" if h["role"] == "user" else "Ассистент"
        lines.append(f"{prefix}: {h['text'][:300]}")
    return "\n".join(lines)

def _resolve_with_context(user_id: int, query: str, raw_params: dict) -> dict:
    """
    Если LLM не смогла извлечь ключевые параметры — пробует достать их из истории.
    Возвращает обогащённый params.
    """
    params = dict(raw_params)
    hist = list(_get_history(user_id))

    # Восстанавливаем город из последнего filial/best_exchange ответа
    if not params.get("city"):
        for h in reversed(hist):
            city = h.get("data", {}).get("city")
            if city:
                params["city"] = city
                break

    # Восстанавливаем список отделений из контекста
    if not params.get("branches"):
        for h in reversed(hist):
            branches = h.get("data", {}).get("branches")
            if branches:
                params["branches"] = branches
                break

    # Восстанавливаем категорию расходов
    if not params.get("category"):
        for h in reversed(hist):
            cat = h.get("data", {}).get("expense_category")
            if cat:
                params["category"] = cat
                break

    return params
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Banking RAG Assistant", version="2.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/static/maps", StaticFiles(directory="static/maps"), name="maps")

# Путь к тессеракту (при необходимости)
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'


# ---------- Модели ----------
class QueryRequest(BaseModel):
    user_query: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # history передаётся с клиента (web/bot) для дополнительного контекста
    client_history: Optional[list] = None  # [{role, text}] — необязательно

class ReceiptUpdate(BaseModel):
    amount: Optional[float] = None
    currency: Optional[str] = None
    category: Optional[str] = None
    comment: Optional[str] = None

class Token(BaseModel):
    access_token: str
    token_type: str

from services.expenses import get_monthly_expenses_converted
from services.currency import load_bank_rates_from_json

with open('kursExchange.json', encoding='utf-8') as f:
    data = json.load(f)



def format_filial_text(raw_text: str, city: str) -> str:
    """Пытается отформатировать список филиалов через LLM, если он доступен.
    В случае неудачи возвращает исходный текст."""
    OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
    if not OPENROUTER_KEY:
        # LLM не настроен – просто причешем текст
        clean = raw_text.replace("\n\n\n", "\n\n")
        # выделим дни работы
        clean = clean.replace("\n--- Метаданные ---", "\n⏰ Часы работы:")
        return clean

    try:
        import openai
        client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
            default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "Banking RAG"}
        )
        prompt = (
            f"Данные о филиалах банка в городе {city}:\n{raw_text}\n\n"
            "Оформи эту информацию в красивом, читаемом виде. Выдели адрес и часы работы. "
            "Не добавляй ничего от себя. Если список большой, сгруппируй."
        )
        resp = client.chat.completions.create(
            model=os.getenv("OPENROUTER_MODEL", "openai/gpt-3.5-turbo"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        result = resp.choices[0].message.content.strip()
        return result if result else raw_text
    except Exception as e:
        logger.warning(f"LLM форматирование не удалось: {e}")
        # fallback
        clean = raw_text.replace("\n\n\n", "\n\n")
        clean = clean.replace("\n--- Метаданные ---", "\n⏰ Часы работы:")
        return clean

async def scheduled_trading_update():
    """Ждёт до 13:05 каждый день и обновляет данные."""
    while True:
        now = datetime.now()
        target = now.replace(hour=13, minute=5, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        fetch_and_cache_trading_data()
    

async def periodic_currency_update():
    while True:
        await asyncio.sleep(86400)  # 24 часа
        success = fetch_and_save_kurs_exchange()
        if success:
            rebuild_chroma()
            load_bank_rates_from_json("data/kursExchange.json")
            logger.info("Данные и Chroma обновлены")


@app.on_event("startup")
async def startup_event():
    # 1. Скачиваем kursExchange.json, если его нет
    if not os.path.exists("data/kursExchange.json"):
        fetch_and_save_kurs_exchange()
    load_bank_rates_from_json("data/kursExchange.json")

    # 2. Перестраиваем Chroma (создаст retriever и установит его в rag_crew)
    rebuild_chroma()

    # 3. Обновление торгов БВФБ
    if not get_cached_trading_data():
        fetch_and_cache_trading_data()
    asyncio.create_task(scheduled_trading_update())
    asyncio.create_task(periodic_currency_update())   # ежедневное обновление курсов и Chroma
    # Заполняем координаты филиалов, если таблица пуста
       # Заполнять координаты только если таблица пуста и нет файла с точными координатами
    db = SessionLocal()
    if db.query(FilialLocation).count() == 0 and not os.path.exists("data/filial_exact_coords.json"):
        from services.geo_service import populate_filial_locations
        populate_filial_locations(db)
    db.close()
# ---------- Аутентификация ----------
@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if user:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed = get_password_hash(password)
    new_user = User(username=username, password_hash=hashed)
    db.add(new_user)
    db.commit()
    return {"msg": "User created successfully"}


@app.post("/token", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/users/me")
def read_current_user(current_user: User = Depends(get_current_user)):
    return {"username": current_user.username, "id": current_user.id}

# ---------- Основной RAG-эндпоинт ----------
@app.post("/ask")
async def ask_assistant(request: QueryRequest, current_user: User = Depends(get_current_user)):
    query = request.user_query.strip()
    trading_data = None
    if not query:
        raise HTTPException(status_code=400, detail="Empty query")
    

    # lat = request.latitude
    # lon = request.longitude
    # if lat is not None and lon is not None and any(w in query for w in ["ближайший", "рядом", "возле", "около", "филиал", "отделение"]):
    #     from services.geo_service import find_nearest_filial, find_nearest_filials, create_map_html

    #     # Определяем, хочет ли пользователь топ-5 или просто ближайший
    #     if any(w in query for w in ["топ", "список", "несколько", "5", "пять", "ближайшие", "все"]):
    #         top_n = 5
    #         # Ищем топ-5
    #         db = SessionLocal()
    #         nearest_filials = find_nearest_filials(db, lat, lon, top_n=top_n)
    #         db.close()
    #         if not nearest_filials:
    #             return JSONResponse(content={"assistant_answer": "Поблизости филиалов не найдено."})

    #         # Формируем текстовый ответ
    #         lines = [f"**{len(nearest_filials)} ближайших филиалов:**"]
    #         for i, f in enumerate(nearest_filials, 1):
    #             lines.append(f"{i}. {f['address']} — {f['distance_km']} км")
    #         answer = "\n".join(lines)

    #         # Генерируем карту
    #         map_filename = f"nearest_map_{current_user.id}_{int(time.time())}.html"
    #         map_path = os.path.join("static/maps", map_filename)
    #         create_map_html(lat, lon, nearest_filials, map_path)
            

    #         from services.geo_service import create_map_image
    #         img_filename = f"nearest_map_{current_user.id}_{int(time.time())}.png"
    #         img_path = os.path.join("static/maps", img_filename)
    #         create_map_image(lat, lon, nearest_filials, img_path)
    #         map_image_url = f"/static/maps/{img_filename}"

    #         follow_ups = generate_follow_ups(query, answer)
    #         return JSONResponse(content={
    #             "assistant_answer": answer,
    #             "map_url": f"/static/maps/{map_filename}",
    #             "map_image_url": map_image_url,
    #             "follow_ups": follow_ups
    #         })
    #     else:
    #         # Обычный поиск одного ближайшего (уже существующий код)
    #         db = SessionLocal()
    #         nearest = find_nearest_filial(db, lat, lon, city=None)
    #         db.close()
    #         if not nearest:
    #             return JSONResponse(content={"assistant_answer": "К сожалению, поблизости нет филиалов."})
    #         answer = (
    #             f"Ближайший филиал: {nearest['address']}\n"
    #             f"Расстояние: {nearest['distance_km']} км\n"
    #             f"Координаты: {nearest['latitude']}, {nearest['longitude']}"
    #         )
    #         follow_ups = generate_follow_ups(query, answer)
    #         return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})
    # # Классификация запроса

    from services.query_classifier import classify_query, get_confidence, add_training_example
    from services.llm_classifier import llm_classify_and_extract

    ML_CONFIDENCE_THRESHOLD = 0.65

    # === Записываем запрос пользователя в историю ===
    _push_history(current_user.id, "user", query)

    # === Умное разрешение контекста: если запрос короткий/неполный — обогащаем историей ===
    history_text = _history_as_text(current_user.id)
    query_lower = query.lower().strip()

    # Признаки что пользователь уточняет предыдущий ответ
    CONTEXT_SIGNALS = [
        "все", "все отделения", "и их курс", "и курсы", "с курсами",
        "расскажи подробнее", "подробнее", "а курсы", "а часы",
        "первое", "второе", "третье", "это отделение",
        "а можно", "покажи все", "и что", "а что",
        "ещё", "еще", "больше", "остальные", "все 27", "полный список",
    ]
    is_context_query = (
        len(query.split()) <= 8 and
        any(sig in query_lower for sig in CONTEXT_SIGNALS)
    )

    # Если явный запрос с уточнением — используем LLM с историей для понимания намерения
    if is_context_query and history_text:
        context_prompt = f"""История диалога:
{history_text}

Новый запрос пользователя: "{query}"

Определи что именно хочет пользователь с учётом контекста. Верни ТОЛЬКО JSON:
{{
  "intent": "filial_detail" | "filial" | "currency_rate" | "expense" | "conversion" | "rag",
  "city": "город из контекста или null",
  "filial_ref": "ссылка на отделение (номер/адрес/порядковый номер) или null",
  "detail_type": "hours" | "rates" | "address" | "all" | null,
  "wants_all": true | false,
  "explanation": "одна фраза что хочет пользователь"
}}

Примеры:
- "А можно все отделения и их курсы?" после списка отделений Гродно → {{"intent":"filial_detail","city":"Гродно","filial_ref":null,"detail_type":"rates","wants_all":true,"explanation":"хочет курсы во всех отделениях Гродно"}}
- "Курс в первом?" после списка отделений → {{"intent":"filial_detail","city":"Гродно","filial_ref":"1","detail_type":"rates","wants_all":false,"explanation":"курс в первом отделении"}}
- "А часы?" после ответа про курсы → {{"intent":"filial_detail","city":"Гродно","filial_ref":null,"detail_type":"hours","wants_all":false,"explanation":"часы работы отделения"}}
"""
        try:
            
            ctx_raw = ask_llm(context_prompt, temperature=0.0, max_tokens=200)
            import json as _json
            s = ctx_raw.find('{'); e = ctx_raw.rfind('}') + 1
            if s != -1 and e > s:
                ctx_result = _json.loads(ctx_raw[s:e])
                logger.info(f"Context resolver: {ctx_result}")
                # Применяем результат
                resolved_intent = ctx_result.get("intent", "")
                resolved_city = ctx_result.get("city")
                resolved_ref = ctx_result.get("filial_ref")
                resolved_detail = ctx_result.get("detail_type")
                resolved_all = ctx_result.get("wants_all", False)

                if resolved_intent in ("filial_detail", "filial"):
                    # Собираем branches из истории
                    branches_from_hist = []
                    for h in reversed(list(_get_history(current_user.id))):
                        b = h.get("data", {}).get("branches")
                        if b:
                            branches_from_hist = b
                            break

                    if resolved_intent == "filial_detail" and not resolved_ref and resolved_all and resolved_city:
                        # Пользователь хочет курсы/часы ВСЕХ отделений города
                        from services.currency import _BANK_RATES
                        from index_data_new import format_worktime
                        city_branches = [b for b in _BANK_RATES if b.get("name") == resolved_city]
                        if city_branches:
                            RATE_LABELS = {
                                "USD_in":"USD покупка","USD_out":"USD продажа",
                                "EUR_in":"EUR покупка","EUR_out":"EUR продажа",
                                "RUB_in":"RUB покупка","RUB_out":"RUB продажа",
                            }
                            lines = [f"**{'Курсы валют' if resolved_detail=='rates' else 'Режим работы'} по всем отделениям {resolved_city}:**\n"]
                            for b in city_branches:
                                name = b.get("filials_text","Отделение")
                                addr = f"{b.get('street_type','')} {b.get('street','')}, {b.get('home_number','')}".strip(", ")
                                lines.append(f"**{name}** — {addr}")
                                if resolved_detail == "rates":
                                    rates_found = []
                                    for key, label in RATE_LABELS.items():
                                        val = b.get(key)
                                        if val and str(val) not in ("0","0.0","0.0000",""):
                                            rates_found.append(f"  {label}: {val} BYN")
                                    if rates_found:
                                        lines.extend(rates_found)
                                    else:
                                        lines.append("  Курсы не указаны")
                                elif resolved_detail == "hours":
                                    wt = format_worktime(b.get("info_worktime",""))
                                    lines.append(f"  🕐 {wt}")
                                elif resolved_detail == "all":
                                    wt = format_worktime(b.get("info_worktime",""))
                                    lines.append(f"  🕐 {wt}")
                                    for key, label in RATE_LABELS.items():
                                        val = b.get(key)
                                        if val and str(val) not in ("0","0.0","0.0000",""):
                                            lines.append(f"  {label}: {val} BYN")
                                lines.append("")

                            answer = "\n".join(lines)
                            _push_history(current_user.id, "assistant", answer,
                                         category="filial_detail",
                                         data={"city": resolved_city, "branches": city_branches})
                            follow_ups = generate_follow_ups(query, answer)
                            return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

                    # Обычный filial_detail с контекстным city
                    if resolved_city and not params.get("city"):
                        params["city"] = resolved_city
                    category = resolved_intent
                    params["filial_ref"] = resolved_ref
                    params["detail_type"] = resolved_detail or "all"
                    params["branches"] = branches_from_hist
                    context_activated = True
        except Exception as _e:
            logger.warning(f"Context resolver failed: {_e}")

    # === Контекстная обработка незавершённого запроса best_exchange ===
    pending = pending_exchange_context.get(current_user.id)
    context_activated = False
    if pending:
        # Любой короткий запрос после pending считаем городом если LLM не нашла другой intent
        new_params = llm_classify_and_extract(query).get("parameters", {})
        detected_city = new_params.get("city")

        # Если LLM не распознала город — пробуем сам запрос как название города
        if not detected_city:
            q_stripped = query.strip().strip("?!.,")
            if 1 <= len(q_stripped.split()) <= 3:  # короткий запрос — скорее всего город
                detected_city = q_stripped

        if detected_city:
            category = "best_exchange"
            params = {**pending, "city": detected_city}
            del pending_exchange_context[current_user.id]
            context_activated = True
        elif new_params.get("currency"):
            pending["currency"] = new_params["currency"]
            pending_exchange_context[current_user.id] = pending
            return JSONResponse(content={
                "assistant_answer": "Для поиска лучшего обменного курса уточните город.",
                "follow_ups": ["Минск", "Гродно", "Брест", "Гомель", "Витебск", "Могилёв"]
            })

    # Если контекст не активирован, проводим обычную классификацию
    if not context_activated:
        confidence = get_confidence(query)
        category = classify_query(query)
        params = {}

        if confidence < ML_CONFIDENCE_THRESHOLD:
            logger.info(f"ML confidence {confidence:.2f} ниже порога, запрашиваем LLM")
            llm_result = llm_classify_and_extract(query)
            llm_intent = llm_result.get('intent', 'rag')
            llm_conf = llm_result.get('confidence', 0.0)
            params = llm_result.get('parameters', {})

            if llm_intent in ("currency_rate", "expense", "currency_dynamics", "conversion",
                              "filial", "filial_detail", "expense_period", "saving_advice", "rag", "best_exchange", "item_search"):
                category = llm_intent
                logger.info(f"LLM определила категорию {category} с уверенностью {llm_conf}")
            else:
                category = "rag"

            if llm_conf > 0.8:
                add_training_example(query, category, retrain=True)
        else:
            logger.info(f"ML модель уверена (conf={confidence:.2f}), категория {category}")

    # ---------- Обработка расходов ----------
    
    if category == "expense":
        db = SessionLocal()
        receipts = get_receipts_by_user(db, current_user.id)
        db.close()

        if not receipts:
            return JSONResponse(content={"assistant_answer": "У вас пока нет чеков."})

        # --- Определяем, спрашивает ли пользователь про конкретные товары ---
        wants_items = any(w in query.lower() for w in [
            "какие товары", "какие покупки", "что купил", "что покупал",
            "что именно", "какие продукты", "список товаров", "что входит"
        ])

        target_category = params.get("category")

        # --- Контекстный fallback: если категория не распознана, берём из предыдущего запроса ---
        if not target_category and last_expense_context.get(current_user.id):
            prev = last_expense_context[current_user.id]
            # Пользователь уточняет предыдущий запрос
            if wants_items or any(w in query.lower() for w in ["подробнее", "детали", "расскажи"]):
                target_category = prev.get("category")

        if target_category:
            from services.expenses import get_category_spending
            spending = get_category_spending(current_user.id, target_category)
            if spending["count"] == 0:
                # Пробуем item_search как запасной вариант
                category = "item_search"
                params["query"] = target_category
            else:
                # Сохраняем контекст
                last_expense_context[current_user.id] = {
                    "category": target_category,
                    "total": spending["total"],
                    "receipt_ids": [r.id for r in spending.get("receipts", [])]
                }

                answer_lines = [
                    f"**Расходы на {target_category}:**",
                    f"• Сумма: {spending['total']:.2f} BYN",
                    f"• Количество чеков: {spending['count']}",
                    f"• Период: {spending.get('period', 'неизвестен')}",
                ]

                # Если запрос содержит "и какие товары" — добавляем поиск по item_index
                if wants_items:
                    try:
                        from services.item_index import CHROMA_ITEMS_PATH, embeddings as item_embeddings
                        from langchain_community.vectorstores import Chroma as ChromaStore
                        store = ChromaStore(
                            persist_directory=CHROMA_ITEMS_PATH,
                            embedding_function=item_embeddings,
                            collection_name=f"user_items_{current_user.id}"
                        )
                        raw_results = store.similarity_search_with_score(target_category, k=20)
                        items_docs = [doc for doc, score in raw_results if score > 0.4]
                        if items_docs:
                            answer_lines.append(f"\n**Товары из категории «{target_category}»:**")
                            seen = set()
                            for doc in items_docs[:10]:
                                name = doc.metadata.get("name", "")
                                price = doc.metadata.get("total_price", 0)
                                date_str = doc.metadata.get("date", "")
                                key = (name, price)
                                if key not in seen:
                                    seen.add(key)
                                    line = f"  • {name} — {price} BYN"
                                    if date_str:
                                        line += f" ({date_str})"
                                    answer_lines.append(line)
                    except Exception as e:
                        logger.warning(f"Item search при expense не удался: {e}")

                answer = "\n".join(answer_lines)
                follow_ups = generate_follow_ups(query, answer)
                # Добавляем контекстные follow-up
                if not wants_items:
                    follow_ups.insert(0, f"Какие именно товары я купил в категории «{target_category}»?")
                _push_history(current_user.id, "assistant", answer, category="expense",
                     data={"expense_category": target_category, "total": spending["total"]})
                return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

        if "по месяцам" in query or "помесячно" in query:
            db2 = SessionLocal()
            monthly = get_monthly_expenses_converted(db2, current_user.id, target_currency="BYN")
            db2.close()
            if not monthly:
                return JSONResponse(content={"assistant_answer": "Нет данных для помесячной разбивки."})

            months_names = [
                "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
            ]
            lines = ["**Расходы по месяцам (в BYN):**"]
            for item in monthly:
                month_name = months_names[item['month'] - 1]
                lines.append(f"\n• **{month_name} {item['year']}**: {item['total']} BYN ({item['count']} чеков)")
                if item.get('categories'):
                    sorted_cats = sorted(item['categories'].items(), key=lambda x: x[1], reverse=True)
                    for cat, total in sorted_cats:
                        lines.append(f"    {cat}: {total} BYN")
            answer = "\n".join(lines)
            follow_ups = generate_follow_ups(query, answer)
            return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

        # Общая статистика с диаграммой
        chart_path = build_expense_chart(receipts, current_user.id)
        total_byn = sum(convert(r.amount, r.currency, "BYN") for r in receipts)
        # Разбивка по категориям
        from collections import defaultdict
        cats = defaultdict(float)
        for r in receipts:
            cats[r.category or "другое"] += convert(r.amount, r.currency, "BYN")
        sorted_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
        cat_lines = "\n".join(f"  • {cat}: {amt:.2f} BYN" for cat, amt in sorted_cats[:5])
        answer = f"**Всего потрачено:** {total_byn:.2f} BYN\n\n**Топ категорий:**\n{cat_lines}"
        follow_ups = generate_follow_ups(query, answer)
        return JSONResponse(content={
            "assistant_answer": answer,
            "image_url": f"/static/{os.path.basename(chart_path)}",
            "follow_ups": follow_ups
        })

    # ---------- Курсы валют ----------
    elif category == "currency_rate":
        # 1. Город из параметров, полученных классификатором (ML+LLM)
        city = params.get("city") if params else None

        # 2. Базовый контекст: курсы НБРБ
        from services.currency import get_rates
        nbrb_rates = get_rates()
        context_parts = [
            "Официальные курсы НБРБ:\n" +
            "\n".join([f"{cur}: {rate} BYN" for cur, rate in nbrb_rates.items()])
        ]

        # 3. Если город указан – ищем информацию о филиалах и локальных курсах в Chroma
        chroma_context = ""
        if city:
            try:
                
                tool = DocumentSearchTool()
                raw = tool._run(query=query, metadata={
                    "source": "belarusbank_api",
                    "type": "filial_info",
                    "city": city
                })
                if raw and "Информация не найдена" not in raw:
                    chroma_context = f"Информация о филиалах и курсах в городе {city}:\n{raw}"
            except Exception as e:
                logger.warning(f"Ошибка при поиске в Chroma для города {city}: {e}")

        if chroma_context:
            context_parts.append(chroma_context)
        else:
            # Если город не указан или данных нет – показываем список доступных городов
            try:
                from services.currency import _BANK_RATES
                if _BANK_RATES:
                    cities = sorted(set(r.get("name") for r in _BANK_RATES if r.get("name")))
                    context_parts.append(f"Доступные города с отделениями Беларусбанка: {', '.join(cities)}.")
            except Exception as e:
                logger.warning(f"Не удалось получить список городов: {e}")

        # 4. Контекст предыдущих запросов пользователя
        last_filial = last_filial_context.get(current_user.id)
        if last_filial:
            context_parts.append(
                f"Предыдущий поиск филиалов: город {last_filial['city']}, найдено {last_filial.get('count', '?')} отделений."
            )
        last_conv = last_conversion_context.get(current_user.id)
        if last_conv:
            context_parts.append(
                f"Последняя конвертация: {last_conv['amount']} {last_conv['from_cur']} = {last_conv['result']} {last_conv['to_cur']} (курс {last_conv['rate']})."
            )

        
        # 5. Биржевые данные (если запрос действительно про рекомендации/торги)
        # Перехватываем кросс/арбитраж запросы — они обрабатываются в currency_dynamics
        _q_low_cr = query.lower()
        if any(w in _q_low_cr for w in ["арбитраж","кросс-курс","кросс курс","заработ на курс","как заработ"]):
            # Редиректим в currency_dynamics с флагом арбитража
            from services.currency_analyst import find_arbitrage_opportunities
            from services.currency import get_rates, get_bank_rates_for_city as _gbrc
            _nbrb = get_rates()
            _city = next((c for c in ["минск","гродно","брест","гомель","витебск","могилёв"] if c in _q_low_cr), "минск")
            _bank = _gbrc(_city.capitalize()) or {}
            _answer = find_arbitrage_opportunities(_nbrb, _bank, _city.capitalize())
            _push_history(current_user.id, "assistant", _answer, category="currency_dynamics")
            return JSONResponse(content={
                "assistant_answer": _answer,
                "follow_ups": ["Динамика USD за месяц", "Динамика EUR за месяц",
                               "Лучшее отделение для обмена USD"]
            })

        trading_data = None
        if params.get("is_trading_advice"):
            trading_data = get_cached_trading_data()
            if not trading_data:
                return JSONResponse(content={
                    "assistant_answer": "Извините, сегодня торговые данные еще не получены. Попробуйте после 13:00.",
                    "follow_ups": ["Курсы НБРБ", "Курсы банка", "Конвертировать валюту"]
                })
            lines = []
            for code, td in trading_data.items():
                lines.append(
                    f"{td['symbol']}: курс {td['rate']}, изм. {td['change']} ({td['percent']}), "
                    f"сделок {td['deals']}, оборот {td['volume']} BYN, "
                    f"мин {td['min']}, макс {td['max']}"
                )
            trading_context = "Данные торгов на БВФБ (сегодня):\n" + "\n".join(lines)
            context_parts.append(trading_context)

        full_context = "\n\n".join(context_parts)

        # 6. Промпт для LLM
        current_date = datetime.now().strftime("%d.%m.%Y")
        base_prompt = (
            f"Ты — русскоязычный банковский ассистент. Ответь на вопрос пользователя о курсах валют, "
            f"используя только предоставленный контекст. Данные актуальны на {current_date}.\n\n"
            f"Контекст:\n{full_context}\n\n"
            f"Вопрос пользователя: {query}\n\n"
            "Ответ (на русском, с указанием даты актуальности):"
        )

        if trading_data:
            base_prompt += " Дай рекомендацию, стоит ли сейчас покупать или продавать валюту, учитывая динамику и объёмы торгов."

        answer = ask_llm(base_prompt)

        if answer:
            follow_ups = generate_follow_ups(query, answer)
            return JSONResponse(content={
                "assistant_answer": answer,
                "follow_ups": follow_ups
            })

        # fallback
        fallback_answer = context_parts[0]
        follow_ups = generate_follow_ups(query, fallback_answer)
        return JSONResponse(content={
            "assistant_answer": fallback_answer,
            "follow_ups": follow_ups
        })
    # ---------- Динамика курсов ----------
    elif category == "currency_dynamics":
        from services.currency_analyst import (
            analyze_dynamics, build_advanced_chart,
            format_analysis_text, find_arbitrage_opportunities
        )
        from services.currency import fetch_currency_dynamics, get_rates, get_bank_rates_for_city

        q_low = query.lower()

        # Определяем валюту
        cur_map = {
            "USD": ["usd","доллар","бакс"],
            "EUR": ["eur","евро"],
            "RUB": ["rub","рубл","российск"],
            "PLN": ["pln","злот","польск"],
            "CNY": ["cny","юан","китайск"],
        }
        cur_nbrb_ids = {"USD":145,"EUR":292,"RUB":298,"PLN":293,"CNY":462}
        cur_name, cur_id = None, None
        for cur, aliases in cur_map.items():
            if any(a in q_low for a in aliases):
                cur_name = cur
                cur_id = cur_nbrb_ids.get(cur)
                break

        # Режим: арбитраж / кросс-курсы
        wants_arbitrage = any(w in q_low for w in [
            "арбитраж","кросс","заработ","выгод","стратег","как заработ"
        ])

        if wants_arbitrage:
            nbrb = get_rates()
            # Убеждаемся что банковские данные загружены
            from services.currency import _BANK_RATES, load_bank_rates_from_json
            if not _BANK_RATES:
                load_bank_rates_from_json()
            city_q = next(
                (w for w in ["минск","гродно","брест","гомель","витебск","могилёв"] if w in q_low),
                "минск"
            )
            bank = get_bank_rates_for_city(city_q.capitalize()) or {}
            if not bank:
                bank = get_bank_rates_for_city("Минск") or {}
            if not bank:
                return JSONResponse(content={
                    "assistant_answer": (
                        "Банковские курсы ещё не загружены — кросс-курсовой анализ недоступен.\n"
                        "Попробуйте спросить про динамику валют или курс НБРБ."
                    )
                })
            answer = find_arbitrage_opportunities(nbrb, bank, city_q.capitalize())
            _push_history(current_user.id, "assistant", answer, category="currency_dynamics")
            return JSONResponse(content={
                "assistant_answer": answer,
                "follow_ups": [
                    "Динамика USD за месяц",
                    "Динамика EUR за месяц",
                    "Лучшее отделение для обмена USD",
                ]
            })

        if not cur_id:
            return JSONResponse(content={
                "assistant_answer": "Укажите валюту: USD, EUR, RUB, PLN или CNY.",
                "follow_ups": ["Динамика USD за месяц", "Динамика EUR за месяц",
                               "Как заработать на кросс-курсах?"]
            })

        # Определяем период
        dates_found = re.findall(r'(\d{1,2}[./-]\d{1,2}[./-]\d{4})', query)
        if len(dates_found) >= 2:
            start_str, end_str = dates_found[0], dates_found[1]
        else:
            days = 90 if any(w in q_low for w in ["3 месяц","квартал","90"]) else \
                   180 if any(w in q_low for w in ["полгода","6 месяц","180"]) else \
                   365 if any(w in q_low for w in ["год","365"]) else \
                   30
            end_dt   = datetime.now()
            start_dt = end_dt - timedelta(days=days)
            start_str = start_dt.strftime("%d.%m.%Y")
            end_str   = end_dt.strftime("%d.%m.%Y")

        def _parse_date(s):
            for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try: return datetime.strptime(s, fmt)
                except: continue
            return None

        sd = _parse_date(start_str)
        ed = _parse_date(end_str)
        if not sd or not ed:
            return JSONResponse(content={"assistant_answer": "Неверный формат даты."})

        # Загружаем данные
        data = fetch_currency_dynamics(cur_id, sd.strftime("%Y-%m-%d"), ed.strftime("%Y-%m-%d"))
        if not data:
            return JSONResponse(content={"assistant_answer": f"Нет данных о динамике {cur_name}."})

        # Полный анализ
        forecast_days = 14 if len(data) >= 30 else 7
        analysis = analyze_dynamics(data, cur_name, forecast_days=forecast_days)

        # Строим продвинутый график
        chart_name = f"dyn_{cur_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"
        chart_path = os.path.join("static", chart_name)

        # Статистика — детерминированная часть
        stat_text = format_analysis_text(analysis)

        # LLM — живой комментарий аналитика
        llm_commentary = ""
        try:
            fc = analysis.get("forecast", [])
            fc_last = fc[-1]["rate"] if fc else analysis["last"]
            direction = "рост" if fc_last > analysis["last"] else ("снижение" if fc_last < analysis["last"] else "стабильность")
            llm_prompt = (
                f"Ты — валютный аналитик Беларусбанка. Дай короткий (3-4 предложения) "
                f"профессиональный комментарий к динамике {cur_name}/BYN за {analysis['n_days']} дней.\n\n"
                f"Контекст: изменение {analysis['change_abs']:+.4f} BYN ({analysis['change_pct']:+.2f}%), "
                f"тренд {analysis['trend']}, волатильность {'высокая' if analysis['volatility'] > 0.01 else 'умеренная' if analysis['volatility'] > 0.003 else 'низкая'}, "
                f"RSI={analysis.get('rsi','н/д')} ({analysis.get('rsi_signal','')}), "
                f"прогноз: {direction} до ~{fc_last} BYN.\n\n"
                f"Объясни простым языком что происходит и стоит ли сейчас покупать/продавать {cur_name}. "
                f"Не дублируй числа из отчёта — добавляй смысл. Пиши по-русски."
            )
            llm_commentary = ask_llm(llm_prompt, temperature=0.3, max_tokens=200)
            if llm_commentary:
                llm_commentary = f"\n\n**💬 Комментарий аналитика:**\n{llm_commentary}"
        except Exception as ex:
            logger.warning(f"LLM commentary failed: {ex}")

        text_analysis = stat_text + llm_commentary

        # Биржевые данные БВФБ из trading_cache.json
        bvfb = None
        bvfb_note = ""
        try:
            from services.currency_analyst import get_latest_bvfb_rate, format_bvfb_block
            bvfb = get_latest_bvfb_rate(cur_name)
            if bvfb:
                bvfb_note = format_bvfb_block(bvfb)

                # Сравниваем НБРБ vs БВФБ
                nbrb_rate = float(get_rates().get(cur_name, 0))
                mult = bvfb.get("multiplier", 1)
                bvfb_rate_norm = bvfb["rate"] / mult if bvfb.get("rate") else None
                if nbrb_rate and bvfb_rate_norm:
                    diff = round(bvfb_rate_norm - nbrb_rate, 4)
                    diff_pct = round(diff / nbrb_rate * 100, 3)
                    sign = "+" if diff >= 0 else ""
                    bvfb_note += (
                        f"\n  **vs НБРБ {nbrb_rate} BYN:** {sign}{diff} BYN ({sign}{diff_pct}%)"
                        f"\n  _{'Биржа выше НБРБ — завтра НБРБ может повысить курс' if diff > 0 else 'Биржа ниже НБРБ — возможное снижение курса'}_"
                    )
        except Exception as ex:
            logger.warning(f"БВФБ: {ex}")

        # Строим график с биржевой линией
        build_advanced_chart(analysis, chart_path, bvfb=bvfb)

        answer = text_analysis + bvfb_note
        _push_history(current_user.id, "assistant", answer, category="currency_dynamics",
                     data={"cur": cur_name, "analysis": analysis})

        follow_ups = [
            f"Как заработать на кросс-курсах {cur_name}?",
            f"Конвертировать 100 {cur_name} в BYN",
            f"Лучшее отделение для обмена {cur_name}",
            f"Динамика {cur_name} за 3 месяца" if days <= 30 else f"Динамика {cur_name} за месяц",
        ]
        return JSONResponse(content={
            "assistant_answer": answer,
            "image_url": f"/static/{chart_name}",
            "follow_ups": follow_ups
        })

    # ---------- Конвертация валют ----------
    elif category == "item_search":
        query_item = params.get("query") or query
        from services.item_index import CHROMA_ITEMS_PATH, embeddings as item_embeddings
        store = Chroma(
            persist_directory=CHROMA_ITEMS_PATH,
            embedding_function=item_embeddings,
            collection_name=f"user_items_{current_user.id}"
        )
        raw_results = store.similarity_search_with_score(query_item, k=50)
        docs = [doc for doc, score in raw_results if score > 0.5]

        if not docs:
            return JSONResponse(content={
                "assistant_answer": f"Не найдено покупок, связанных с '{query_item}'."
            })

        items_list = []
        for doc in docs:
            name = doc.metadata["name"]
            price = doc.metadata["total_price"]
            date_str = doc.metadata.get("date", "")
            store_name = doc.metadata.get("store", "")
            line = f"{name} - {price} BYN"
            if date_str:
                line += f", дата: {date_str}"
            if store_name:
                line += f", магазин: {store_name}"
            items_list.append(line)
        items_text = "\n".join(items_list)

        prompt = f"""
Пользователь спросил: "Сколько я потратил на {query_item}?"
В базе найдены следующие товары (с ценами, датами и магазинами), которые могут относиться к этому запросу:
{items_text}

Выбери только те позиции, которые действительно являются "{query_item}" или явно связаны с этим продуктом.
Исключи всё, что не относится к запросу.
Верни ответ в формате:
- Список подходящих товаров с названием, ценой, датой и магазином (если известны).
- Общая сумма, потраченная именно на {query_item}.
- Количество таких покупок.
Если подходящих товаров нет, напиши "Не найдено".
"""
        llm_response = ask_llm(prompt, temperature=0.1, max_tokens=500)

        if llm_response:
            return JSONResponse(content={"assistant_answer": llm_response})
        else:
            total = sum(doc.metadata["total_price"] for doc in docs)
            count = len(docs)
            unique_names = list(set(doc.metadata["name"] for doc in docs))[:5]
            summary = (f"По запросу '{query_item}' найдено {count} покупок "
                       f"на сумму {total:.2f} BYN. Примеры товаров: {', '.join(unique_names)}")
            return JSONResponse(content={"assistant_answer": summary})

    # ---------- Конвертация валют ----------
    elif category == "conversion":
        amount    = params.get("amount")
        from_cur  = (params.get("from_currency") or params.get("currency") or "").upper()
        to_cur    = (params.get("to_currency") or "").upper()
        city      = params.get("city")

        # Определяем режим явно из текста запроса
        q_low = query.lower()
        wants_nbrb = any(w in q_low for w in ["нбрб", "нацбанк", "официальн", "нац. банк"])
        wants_bank = city or any(w in q_low for w in ["курс", "отделени", "филиал", "банк"])

        # Если параметры не извлечены — просим LLM
        if not (amount and from_cur):
            extract_prompt = (
                f"Извлеки из запроса: '{query}'\n"
                "Верни строго JSON без пояснений:\n"
                "{\"amount\": число или null, \"from_cur\": \"USD\" или null, "
                "\"to_cur\": \"BYN\" или null, \"city\": \"город\" или null, "
                "\"wants_nbrb\": true если явно просит НБРБ иначе false}"
            )
            try:
                raw = ask_llm(extract_prompt, temperature=0.0, max_tokens=150)
                clean = raw.strip().strip('`').replace("```json","").replace("```","")
                extracted = json.loads(clean)
                amount      = amount   or extracted.get("amount")
                from_cur    = from_cur or (extracted.get("from_cur") or "").upper()
                to_cur      = to_cur   or (extracted.get("to_cur")   or "").upper()
                city        = city     or extracted.get("city")
                if extracted.get("wants_nbrb"):
                    wants_nbrb = True
            except Exception:
                pass

        if not (amount and from_cur):
            return JSONResponse(content={
                "assistant_answer": (
                    "Уточните сумму и валюту. Например:\n"
                    "• «100 USD в BYN» — по курсу НБРБ\n"
                    "• «100 USD в BYN по курсу Гродно» — по курсу отделений\n"
                    "• «100 USD в EUR по НБРБ» — официальный кросс-курс"
                )
            })

        from_cur = from_cur.upper()
        to_cur   = (to_cur or "BYN").upper()

        from services.currency import get_rates, get_bank_rates_for_city
        nbrb = get_rates()

        # ── Приоритеты: НБРБ > город > по умолчанию НБРБ ──────────
        # Явно НБРБ или не указан город — используем НБРБ
        use_bank_rates = bool(city) and not wants_nbrb

        rate_buy = rate_sell = None
        rate_label = "официальный курс НБРБ"

        if use_bank_rates:
            bank = get_bank_rates_for_city(city)
            if bank:
                b = float(bank.get(f"{from_cur}_in") or 0)
                s = float(bank.get(f"{from_cur}_out") or 0)
                if b > 0:
                    rate_buy  = b
                    rate_sell = s if s > 0 else b
                    rate_label = f"курс отделений {city}"
                else:
                    # Банковских данных нет — fallback на НБРБ с предупреждением
                    rate_label = f"курс НБРБ (отделения {city} не имеют данных по {from_cur})"
                    use_bank_rates = False

        def _calc(amount_val, f_cur, t_cur, buy: bool = True):
            """Считает конвертацию через BYN как промежуточную."""
            # f_cur → BYN
            if f_cur == "BYN":
                to_byn_rate = 1.0
            elif use_bank_rates and rate_buy:
                to_byn_rate = rate_buy if buy else (rate_sell or rate_buy)
            else:
                to_byn_rate = float(nbrb.get(f_cur, 0)) or None
            if not to_byn_rate:
                return None, None

            # BYN → t_cur
            if t_cur == "BYN":
                effective_rate = to_byn_rate
                result = round(amount_val * to_byn_rate, 2)
            else:
                if use_bank_rates:
                    bank_t = get_bank_rates_for_city(city) or {}
                    # Покупаем t_cur у банка → берём курс продажи банком
                    from_byn_rate = float(bank_t.get(f"{t_cur}_out") or 0)
                else:
                    from_byn_rate = float(nbrb.get(t_cur, 0))
                if not from_byn_rate:
                    return None, None
                effective_rate = round(to_byn_rate / from_byn_rate, 6)
                result = round(amount_val * to_byn_rate / from_byn_rate, 2)

            return result, effective_rate

        result, rate_used = _calc(amount, from_cur, to_cur, buy=True)
        if result is None:
            return JSONResponse(content={
                "assistant_answer": f"Не удалось найти курс {from_cur}→{to_cur}. Проверьте код валюты."
            })

        
        current_date = datetime.now().strftime("%d.%m.%Y")

        lines = [
            f"**{amount} {from_cur} → {to_cur}**\n",
            f"💱 Итого: **{result} {to_cur}**",
            f"📊 Курс: 1 {from_cur} = {rate_used} {to_cur}",
            f"📋 {rate_label} на {current_date}",
        ]

        # Если банковский курс — показываем оба направления
        if use_bank_rates and rate_sell:
            result_sell, rate_sell_used = _calc(amount, from_cur, to_cur, buy=False)
            lines.append(f"\n🏦 **Детали курса отделений {city}:**")
            lines.append(f"  Вы продаёте {from_cur} банку (покупка банком): {rate_buy:.4f} BYN → вы получите **{result} {to_cur}**")
            if result_sell:
                lines.append(f"  Вы покупаете {from_cur} у банка (продажа банком): {rate_sell:.4f} BYN → вы заплатите **{result_sell} {to_cur}**")

        answer = "\n".join(lines)

        last_conversion_context[current_user.id] = {
            "amount": amount, "from_cur": from_cur, "to_cur": to_cur,
            "result": result, "rate": rate_used, "city": city,
        }
        _push_history(current_user.id, "assistant", answer, category="conversion")

        follow_ups = generate_follow_ups(query, answer)
        if city and not wants_nbrb:
            follow_ups.insert(0, f"По официальному курсу НБРБ")
            follow_ups.insert(1, f"Лучшее отделение в {city} для обмена {from_cur}")
        elif wants_nbrb or not city:
            follow_ups.insert(0, f"По курсу отделений Минска")
            follow_ups.insert(1, f"По курсу отделений Гродно")

        return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

    elif category in ("filial", "filial_detail", "best_exchange"):
        db = SessionLocal()
        from services.filial_service import (
            get_branches_list, get_nearest_branches, get_best_exchange_branch,
            suggest_other_city, detect_user_city, _extract_count,
            format_branch_card, _format_rates, _format_worktime_full,
            _is_open_now, _format_address, RATE_LABELS
        )
        from services.currency import _BANK_RATES
        from index_data_new import format_worktime

        user_id = current_user.id
        q = query.lower()
        lat = request.latitude
        lon = request.longitude
        last = last_filial_context.get(user_id) or {}

        # ── Параметры из классификатора ──────────────────────
        city        = (params or {}).get("city")
        filial_ref  = (params or {}).get("filial_ref")
        detail_type = (params or {}).get("detail_type", "all")
        currency    = (params or {}).get("currency", "") or None
        operation   = (params or {}).get("operation", "buy")

        # Распознаём валюту из текста если не пришла из классификатора
        if not currency:
            for cur, aliases in {
                "USD": ["доллар","usd","бакс"],
                "EUR": ["евро","eur"],
                "RUB": ["рубл","rub","российск"],
                "PLN": ["злот","pln","польск"],
                "CNY": ["юан","cny","китайск"],
            }.items():
                if any(a in q for a in aliases):
                    currency = cur
                    break

        # Определяем операцию
        if any(w in q for w in ["продать","продаю","сдать","sell"]):
            operation = "sell"
        elif any(w in q for w in ["купить","куплю","buy","обменять","поменять"]):
            operation = "buy"

        # Флаги из текста
        wants_nearest   = any(w in q for w in ["ближайш","рядом","возле","около","недалеко"])
        wants_best      = (
            any(w in q for w in ["лучш","выгодн","оптимальн","рекоменд","где поменять","где купить","где продать"])
            or (params or {}).get("_wants_best")
            or category == "best_exchange"
        )
        show_rates      = any(w in q for w in ["курс","обмен","валют","rate"]) or currency is not None
        show_hours      = any(w in q for w in ["час","время","режим","расписани","работает","открыт","закрыт"])
        show_detail     = filial_ref or any(w in q for w in ["подробн","расскажи","информаци","детали"])
        only_open       = any(w in q for w in ["открыт","работает сейчас","сейчас открыт","не закрыт","которые работают"])

        # "все валюты в этом отделении" / "все курсы в отделении" — это детали, НЕ список всех отделений
        # show_all срабатывает только если нет контекста конкретного отделения
        has_branch_context = last.get("branch") or last.get("branches") or filial_ref
        detail_words = ["этом","этого","данном","указанном","выше","отделении","филиале","пункте"]
        is_detail_query = show_rates and any(w in q for w in detail_words)
        if is_detail_query:
            show_detail = True
            show_all = False
        else:
            show_all = any(w in q for w in ["все отделения","всех отделений","полный список","все филиалы"]) or \
                       (any(w in q for w in ["все ","всех","полный","весь","целиком","полностью"]) and not show_rates)

        # Если спрашивают об открытых без города — берём из контекста
        if only_open and not city and last.get("city"):
            city = last["city"]
        n_branches      = _extract_count(query) if wants_nearest or show_all else 5

        # Восстанавливаем город из контекста ТОЛЬКО для уточняющих запросов
        # НЕ восстанавливаем если пользователь спрашивает «ближайшие» — тогда ищем по координатам
        # НЕ восстанавливаем если предыдущий запрос был «ближайшие» без явного города
        prev_was_nearest_no_city = (last.get("mode") == "nearest" and not last.get("explicit_city"))
        if not city and last.get("city") and not wants_nearest and not prev_was_nearest_no_city:
            CLARIFY = ["часы","режим","адрес","курс","отделени","ещё","еще","больше","подробн","расскажи","лучш","оцен","рекоменд","открыт"]
            if any(w in q for w in CLARIFY):
                city = last["city"]

        # ════════════════════════════════════════════════════
        # СЦЕНАРИЙ 1: Лучшее отделение для обмена
        # ════════════════════════════════════════════════════
        if wants_best or (category == "best_exchange"):
            # Приоритет: 1) город из запроса  2) координаты  3) контекст (только без координат)
            if lat is not None and lon is not None:
                # Есть координаты — определяем город по ним, игнорируем контекст
                city_filter = city or detect_user_city(lat, lon, _BANK_RATES, db=db)
            else:
                # Нет координат — берём город из запроса или контекста
                city_filter = city or last.get("city")

            if not city_filter and lat is None:
                # Нет ни города ни геолокации — спрашиваем город
                pending_exchange_context[user_id] = {
                    "currency": currency or "USD",
                    "operation": operation,
                }
                return JSONResponse(content={
                    "assistant_answer": "В каком городе ищем лучший курс обмена?",
                    "follow_ups": ["Минск", "Гродно", "Брест", "Гомель", "Витебск", "Могилёв"]
                })

            answer = get_best_exchange_branch(
                _BANK_RATES,
                lat if lat is not None else 0.0,
                lon if lon is not None else 0.0,
                currency=currency or "USD",
                operation=operation,
                city_filter=city_filter,
                top_n=3,
                db=db,
                skip_distance_filter=bool(city),  # если город указан явно — не фильтруем по км
            )

            # Если нет координат — убираем из ответа расстояния (они будут 999)
            if lat is None:
                answer = answer.replace("999 км от вас", "расстояние неизвестно")

            follow_ups = [
                "Ближайшие отделения",
                f"Все отделения {'в ' + city_filter if city_filter else ''}",
                "Курсы EUR в лучшем отделении",
            ]
            last_filial_context[user_id] = {**last, "city": city_filter or "", "mode": "best"}
            _push_history(user_id, "assistant", answer, category="filial",
                         data={"city": city_filter, "mode": "best"})
            return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

        # ════════════════════════════════════════════════════
        # СЦЕНАРИЙ 2: Ближайшие N отделений
        # ════════════════════════════════════════════════════
        if wants_nearest:
            if lat is not None and lon is not None:
                # Есть координаты — если город НЕ указан явно в запросе,
                # НЕ берём из контекста — ищем глобально по координатам
                city_for_nearest = city  # только явно указанный в запросе
                user_city = detect_user_city(lat, lon, _BANK_RATES, db=db)

                answer = get_nearest_branches(
                    _BANK_RATES, lat, lon,
                    n=n_branches,
                    city_filter=city_for_nearest,
                    show_rates=show_rates,
                    show_hours=show_hours,
                    currency=currency,
                    only_open=only_open,
                    db=db,
                )
                # Подсказка если явно указал другой город
                if city_for_nearest and user_city and user_city != city_for_nearest:
                    answer += f"\n\n💡 Вы сейчас в **{user_city}**. Напишите «ближайшие отделения» чтобы найти рядом с вами."

                follow_ups = []
                if city_for_nearest:
                    total = len([b for b in _BANK_RATES if b.get("name") == city_for_nearest])
                    if total > n_branches:
                        follow_ups.append(f"Показать все {total} отделений в {city_for_nearest}")
                follow_ups += ["Курсы валют в ближайшем отделении", "Лучшее отделение для обмена USD"]
                last_filial_context[user_id] = {
                    **last,
                    # Если город не был явно указан — сохраняем город пользователя по координатам
                    "city": city_for_nearest or user_city or "",
                    "mode": "nearest",
                    "n": n_branches,
                    "explicit_city": city_for_nearest,  # запоминаем был ли город явным
                }
                _push_history(user_id, "assistant", answer, category="filial",
                             data={"city": city_for_nearest or user_city or "", "mode": "nearest"})
                return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})
            else:
                # Нет геолокации — просим разрешить или показываем список города
                if city:
                    city_branches = [b for b in _BANK_RATES if b.get("name") == city]
                    answer = get_branches_list(city_branches, city, show_n=n_branches,
                                              show_rates=show_rates, show_hours=show_hours,
                                              currency=currency)
                    answer = f"📍 Геолокация недоступна — показываю все отделения в {city}:\n\n" + answer
                    answer += "\n\n_Разрешите доступ к геолокации чтобы увидеть расстояния._"
                else:
                    answer = "📍 Для поиска ближайших отделений разрешите доступ к геолокации в браузере или укажите город.\n\nНапример: «ближайшие отделения в Минске»"
                return JSONResponse(content={
                    "assistant_answer": answer,
                    "follow_ups": ["Минск", "Гродно", "Брест", "Гомель"]
                })

        # ════════════════════════════════════════════════════
        # СЦЕНАРИЙ 3: Детали конкретного отделения
        # ════════════════════════════════════════════════════
        # Триггеры: явная ссылка на отделение, или уточнение про текущее (этот, данный, выше)
        last_branch = last.get("branch")  # одиночное отделение из предыдущего ответа
        if filial_ref or show_detail or (show_rates and last_branch and not city and not wants_nearest):
            ORDINALS = {
                "1": 0, "первое": 0, "первый": 0,
                "2": 1, "второе": 1, "второй": 1,
                "3": 2, "третье": 2, "третий": 2,
                "4": 3, "четвёртое": 3, "четвертое": 3,
                "5": 4, "пятое": 4, "пятый": 4,
            }
            def find_branch(ref, pool):
                if not ref:
                    return None
                ref_l = ref.lower().strip()
                if ref_l in ORDINALS and pool:
                    idx = ORDINALS[ref_l]
                    if idx < len(pool):
                        b = pool[idx]
                        fid = str(b.get("filial_id",""))
                        return next((r for r in _BANK_RATES if str(r.get("filial_id","")) == fid), b)
                for b in _BANK_RATES:
                    ft = str(b.get("filials_text","")).lower()
                    if ref_l.replace(" ","") in ft.replace(" ",""):
                        return b
                for b in _BANK_RATES:
                    street = str(b.get("street","")).lower()
                    if any(w in street for w in ref_l.split() if len(w) > 3):
                        return b
                return None

            pool = last.get("branches", [])
            ref = filial_ref or (ORDINALS and next((w for w in q.split() if w in ORDINALS), None))
            branch = find_branch(ref, pool)
            # Если ссылки нет — берём последнее показанное отделение
            if not branch and last_branch:
                branch = last_branch
            if not branch and city:
                city_pool = [b for b in _BANK_RATES if b.get("name") == city]
                branch = find_branch(ref, city_pool)

            if branch:
                name = branch.get("filials_text", "Отделение")
                addr = _format_address(branch)
                is_open, status = _is_open_now(branch.get("info_worktime",""))
                hours = _format_worktime_full(branch.get("info_worktime",""))
                rates = _format_rates(branch, currency)

                lines = [f"**{name}**", f"📍 {addr}", f"🕐 {status}",
                         f"\n📅 **Режим работы:**\n{hours}"]
                if rates != "Курсы не указаны":
                    cur_label = f" ({currency})" if currency else ""
                    lines.append(f"\n💱 **Курсы{cur_label}:**\n{rates}")
                answer = "\n".join(lines)

                follow_ups = ["Курсы всех валют в этом отделении",
                              f"Лучшее отделение для обмена USD"]
                _push_history(user_id, "assistant", answer, category="filial_detail",
                             data={"city": branch.get("name",""), "branch": branch})
                last_filial_context[user_id] = {
                    **last,
                    "city": branch.get("name", last.get("city", "")),
                    "branch": branch,   # сохраняем для следующего уточнения
                    "mode": "detail",
                }
                return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

        # ════════════════════════════════════════════════════
        # СЦЕНАРИЙ 4: Нужен город
        # ════════════════════════════════════════════════════
        if not city:
            if lat is not None and lon is not None:
                # Показываем ближайшие без фильтра по городу
                answer = get_nearest_branches(_BANK_RATES, lat, lon, n=5,
                                              show_rates=show_rates,
                                              show_hours=show_hours,
                                              db=db)
                last_filial_context[user_id] = {**last, "mode": "nearest"}
                _push_history(user_id, "assistant", answer, category="filial", data={"mode": "nearest"})
                return JSONResponse(content={"assistant_answer": answer,
                                             "follow_ups": ["Курсы в ближайшем", "Лучшее для обмена USD"]})
            return JSONResponse(content={
                "assistant_answer": "В каком городе вас интересуют отделения?",
                "follow_ups": ["Минск", "Гродно", "Брест", "Гомель", "Витебск", "Могилёв"]
            })

        # ════════════════════════════════════════════════════
        # СЦЕНАРИЙ 5: Список по городу
        # ════════════════════════════════════════════════════
        city_branches = [b for b in _BANK_RATES if b.get("name") == city]
        if not city_branches:
            return JSONResponse(content={"assistant_answer": f"Отделения в {city} не найдены."})

        # Если пользователь в другом городе — добавляем подсказку
        user_city = detect_user_city(lat, lon, _BANK_RATES, db=db) if (lat and lon) else None
        total = len(city_branches)

        if show_all:
            answer = get_branches_list(city_branches, city, show_n=total,
                                       show_rates=show_rates, show_hours=show_hours,
                                       currency=currency)
        else:
            answer = get_branches_list(city_branches, city, show_n=n_branches,
                                       show_rates=show_rates, show_hours=show_hours,
                                       currency=currency)

        # Подсказка если геолокация в другом городе
        if user_city and user_city != city and lat and lon:
            answer += f"\n\n💡 Вы сейчас в **{user_city}**. Напишите «ближайшие отделения» чтобы найти рядом с вами."

        follow_ups = []
        if total > n_branches and not show_all:
            follow_ups.append(f"Показать все {total} отделений в {city}")
        follow_ups += [
            f"Часы работы отделений в {city}",
            f"Курсы валют в отделениях {city}",
            f"Лучшее отделение в {city} для обмена USD",
        ]

        last_filial_context[user_id] = {
            "city": city, "count": total, "mode": "list",
            "branches": city_branches[:n_branches]
        }
        _push_history(user_id, "assistant", answer, category="filial",
                     data={"city": city, "count": total, "branches": city_branches[:n_branches]})
        db.close()
        return JSONResponse(content={"assistant_answer": answer, "follow_ups": follow_ups})

    # ---------- Расходы за период ----------
    elif category == "expense_period":
        from services.spending_advisor import summarize_period, parse_period
        # Пробуем извлечь период из запроса
        result = summarize_period(current_user.id, query)
        if result:
            _push_history(current_user.id, "assistant", result, category="expense_period")
            follow_ups = generate_follow_ups(query, result)
            follow_ups.insert(0, "Дай советы по экономии")
            follow_ups.insert(1, "Расходы за прошлый месяц")
            return JSONResponse(content={"assistant_answer": result, "follow_ups": follow_ups})
        else:
            # Период не распознан — просим уточнить
            return JSONResponse(content={
                "assistant_answer": (
                    "Уточните период. Например:\n"
                    "• «прошлая неделя»\n"
                    "• «январь 2026»\n"
                    "• «12.04.2025»\n"
                    "• «прошлый месяц»"
                ),
                "follow_ups": [
                    "Расходы на прошлой неделе",
                    "Расходы за прошлый месяц",
                    f"Расходы за {__import__('datetime').date.today().strftime('%B %Y')}",
                ]
            })

    # ---------- Советы по экономии ----------
    elif category == "saving_advice":
        from services.spending_advisor import get_saving_advice
        # Определяем глубину анализа из запроса
        months = 3
        if "год" in query.lower() or "полный" in query.lower():
            months = 12
        elif "полгода" in query.lower() or "6 месяц" in query.lower():
            months = 6
        elif "месяц" in query.lower() and "прошл" not in query.lower():
            months = 1
        result = get_saving_advice(current_user.id, months=months)
        _push_history(current_user.id, "assistant", result, category="saving_advice")
        follow_ups = generate_follow_ups(query, result)
        follow_ups.insert(0, "Расходы за прошлый месяц")
        follow_ups.insert(1, "Расходы на прошлой неделе")
        return JSONResponse(content={"assistant_answer": result, "follow_ups": follow_ups})


    # ---------- Остальное – через CrewAI ----------
    try:
        result = banking_crew.kickoff(inputs={'user_query': query})
        answer = str(result)
        follow_ups = generate_follow_ups(query, answer)
        return JSONResponse(content={
            "assistant_answer": answer,
            "follow_ups": follow_ups
        })
    except Exception as e:
        logger.error(f"CrewAI error: {e}")
        error_msg = "Произошла ошибка при обработке запроса."
        return JSONResponse(content={
            "assistant_answer": error_msg,
            "follow_ups": []
        })

# ---------- Работа с чеками ----------
@app.post("/upload-image")
async def upload_image(
    image: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Файл должен быть изображением")

    os.makedirs("images", exist_ok=True)
    file_ext = os.path.splitext(image.filename)[1] or ".png"
    filename = f"{uuid.uuid4()}{file_ext}"
    image_path = os.path.join("images", filename)

    contents = await image.read()
    with open(image_path, "wb") as f:
        f.write(contents)

    try:
        from PIL import Image as PILImage
        PILImage.open(image_path).verify()
    except Exception:
        os.remove(image_path)
        raise HTTPException(status_code=400, detail="Файл повреждён или не является изображением")

    ocr_text = ocr_image(image_path)
    llm_data = extract_receipt_data_with_llm(ocr_text)

    from index_data_new import extract_text_from_image
    expense_data = extract_text_from_image(image_path)
    if llm_data.get("total_amount") is not None:
        expense_data["amount"] = float(llm_data["total_amount"])
    if llm_data.get("date"):
        expense_data["date"] = llm_data["date"]
    items = llm_data.get("items", [])
    expense_data["items"] = items

    if not expense_data.get("amount") and not items:
        os.remove(image_path)
        raise HTTPException(status_code=400, detail="Не удалось распознать чек")

    db = SessionLocal()
    try:
        receipt = create_receipt(
            db=db,
            user_id=current_user.id,
            amount=expense_data.get("amount") or 0.0,
            currency=expense_data.get("currency", "BYN"),
            category=llm_data.get("receipt_category") or expense_data.get("category", "другое"),
            receipt_date=expense_data.get("date"),
            image_path=image_path
        )
        receipt.store = llm_data.get("store")

        for item in items:
            db_item = ReceiptItem(
                receipt_id=receipt.id,
                name=item.get("name", "Неизвестный товар"),
                quantity=item.get("quantity", 1),
                unit_price=item.get("unit_price", item.get("total_price", 0)),
                total_price=item.get("total_price", 0),
                category=item.get("category"),
                raw_text=item.get("name")
            )
            db.add(db_item)

        receipt_id = receipt.id
        receipt_amount = receipt.amount
        receipt_currency = receipt.currency
        receipt_category = receipt.category
        receipt_date = receipt.receipt_date
        receipt_store = receipt.store

        db.commit()
    finally:
        db.close()

    from services.item_index import index_receipt_items
    index_receipt_items(
        current_user.id, items,
        receipt_date=str(receipt_date),
        store=receipt_store,
        receipt_category=receipt_category
    )

    db2 = SessionLocal()
    receipts = get_receipts_by_user(db2, current_user.id)
    chart_path = build_expense_chart(receipts, current_user.id)
    db2.close()

    return JSONResponse(content={
        "message": "Чек обработан",
        "receipt_id": receipt_id,
        "amount": receipt_amount,
        "currency": receipt_currency,
        "category": receipt_category,
        "date": str(receipt_date) if receipt_date else None,
        "store": receipt_store,
        "image_url": f"/images/{filename}",
        "chart_url": f"/static/{os.path.basename(chart_path)}",
        "extracted": llm_data
    })
# @app.get("/receipts/search")
# def search_items(query: str, current_user: User = Depends(get_current_user)):
#     """Поиск расходов по названию товара."""
#     try:
#         store = Chroma(
#             persist_directory=CHROMA_ITEMS_PATH,
#             embedding_function=NomicEmbeddings(model="nomic-embed-text-v1"),
#             collection_name=f"user_items_{current_user.id}"
#         )
#         docs = store.similarity_search(query, k=50)
#         results = []
#         total = 0.0
#         for doc in docs:
#             results.append({
#                 "name": doc.metadata["name"],
#                 "total_price": doc.metadata["total_price"],
#                 "quantity": doc.metadata["quantity"],
#                 "date": doc.metadata.get("date", "")
#             })
#             total += doc.metadata["total_price"]
#         return {"total_spent": round(total, 2), "count": len(results), "items": results}
#     except Exception as e:
#         logger.error(f"Поиск по товарам: {e}")
#         return {"total_spent": 0, "count": 0, "items": []}

@app.put("/receipts/{receipt_id}")
async def edit_receipt(receipt_id: int, data: ReceiptUpdate,
                       current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    receipt = update_receipt(db, receipt_id, current_user.id,
                             **data.dict(exclude_unset=True))
    if not receipt:
        db.close()
        raise HTTPException(status_code=404, detail="Чек не найден")
    db.close()

    # Перестраиваем график
    db = SessionLocal()
    receipts = get_receipts_by_user(db, current_user.id)
    db.close()
    build_expense_chart(receipts, current_user.id)

    return {"status": "updated"}


@app.get("/receipts")
def list_receipts(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    receipts = get_receipts_by_user(db, current_user.id)
    db.close()
    return [{
        "id": r.id,
        "amount": r.amount,
        "currency": r.currency,
        "category": r.category,
        "date": str(r.receipt_date) if r.receipt_date else None,
        "comment": r.comment,
        "image_url": f"/images/{os.path.basename(r.image_path)}" if r.image_path else None
    } for r in receipts]


# ---------- Диаграмма ----------
@app.get("/expenses/chart")
def get_expense_chart(
    chart_type: str = "pie",
    period: str = "all",   # all | today | week | month | last_month | year | YYYY-MM
    current_user: User = Depends(get_current_user)
):
    from datetime import date, timedelta
    import calendar

    db = SessionLocal()
    all_receipts = get_receipts_by_user(db, current_user.id)
    db.close()

    today = date.today()
    receipts = all_receipts

    if period == "today":
        receipts = [r for r in all_receipts if r.receipt_date == today]
    elif period == "week":
        monday = today - timedelta(days=today.weekday())
        receipts = [r for r in all_receipts if r.receipt_date and r.receipt_date >= monday]
    elif period == "month":
        receipts = [r for r in all_receipts
                    if r.receipt_date and r.receipt_date.year == today.year
                    and r.receipt_date.month == today.month]
    elif period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        receipts = [r for r in all_receipts
                    if r.receipt_date and first_prev <= r.receipt_date <= last_prev]
    elif period == "year":
        receipts = [r for r in all_receipts
                    if r.receipt_date and r.receipt_date.year == today.year]
    elif len(period) == 7 and period[4] == "-":  # YYYY-MM
        try:
            y, m = int(period[:4]), int(period[5:])
            last_day = calendar.monthrange(y, m)[1]
            d_from = date(y, m, 1)
            d_to   = date(y, m, last_day)
            receipts = [r for r in all_receipts
                        if r.receipt_date and d_from <= r.receipt_date <= d_to]
        except Exception:
            pass

    chart_path = build_expense_chart(receipts, current_user.id, chart_type)
    period_labels = {
        "all": "Все время", "today": "Сегодня", "week": "Эта неделя",
        "month": "Этот месяц", "last_month": "Прошлый месяц", "year": f"{today.year} год"
    }
    return {
        "chart_url": f"/static/{os.path.basename(chart_path)}",
        "period_label": period_labels.get(period, period),
        "count": len(receipts),
    }


# ---------- Конвертация валют ----------
@app.get("/convert")
def convert_currency(amount: float, from_cur: str, to_cur: str):
    result = convert(amount, from_cur.upper(), to_cur.upper())
    return {"result": result, "from": from_cur.upper(), "to": to_cur.upper()}


# ---------- Подсказки ----------
@app.get("/suggestions")
def suggestions(q: str = "", current_user: User = Depends(get_current_user)):
    return {"suggestions": get_suggestions(current_user.id, q)}


# ---------- Статические файлы и корень ----------
@app.get("/")
async def read_index():
    return FileResponse('static/index.html')



@app.post("/loyalty/refresh")
async def refresh_loyalty_cache(current_user: User = Depends(get_current_user)):
    """Принудительно обновляет кэш программ лояльности с сайтов магазинов."""
    from services.loyalty_parser import refresh_cache_for_stores, STORE_CONFIGS
    stores = list(STORE_CONFIGS.keys())
    import asyncio
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, refresh_cache_for_stores, stores)
    return {"status": "done", "results": results}


@app.get("/health")
async def health_check():
    return {"status": "ok"}


# ---------- Запуск ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)