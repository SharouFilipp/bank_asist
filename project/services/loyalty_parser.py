"""
services/loyalty_parser.py

Парсер программ лояльности через веб-поиск.

Схема работы:
  1. Кэш (7 дней)
  2. Поиск через LLM с web search (Perplexity/Sonar через OpenRouter
     или Tavily если задан TAVILY_API_KEY)
  3. Fallback — базовые данные

Запуск вручную: python -m services.loyalty_parser [магазин ...]
Обновление кэша: POST /loyalty/refresh
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

CACHE_FILE = "data/loyalty_cache.json"
CACHE_TTL_DAYS = 7

# ─── Базовые данные (fallback) ─────────────────────────────
FALLBACK_DATA: Dict[str, dict] = {
    "евроопт":    {"card": "Е-Плюс",            "cashback": "до 3% кешбэка баллами",  "tip": "Акции каждую пятницу. 1 балл = 1 BYN скидки.",                        "app": "Евроопт: Скидки и Акции"},
    "гиппо":      {"card": "Клуб Гиппо",         "cashback": "1–5% кешбэк баллами",   "tip": "Двойные баллы по воскресеньям. Скидки пенсионерам до 12:00.",         "app": "Гиппо"},
    "корона":     {"card": "Клубная карта",       "cashback": "3–7% скидка",           "tip": "Персональные скидки в приложении обновляются еженедельно.",           "app": "Корона"},
    "prostore":   {"card": "ProCard",             "cashback": "до 5% кешбэк",          "tip": "Товары дня со скидкой до 30%.",                                       "app": "ProStore"},
    "простор":    {"card": "ProCard",             "cashback": "до 5%",                 "tip": "Товары дня со скидкой до 30%.",                                       "app": "ProStore"},
    "белмаркет":  {"card": "Белмаркет Бонус",    "cashback": "2% на все покупки",     "tip": "Акционные товары обновляются по четвергам.",                          "app": "Белмаркет"},
    "соседи":     {"card": "Карта соседа",        "cashback": "1% кешбэк",             "tip": "Скидки на собственные торговые марки магазина.",                      "app": "Соседи"},
    "рублёвский": {"card": "Рублёвский Бонус",   "cashback": "3% баллами",            "tip": "Уточняйте дни повышенного кешбэка в приложении.",                    "app": "Рублёвский"},
    "алми":       {"card": "Almi Club",           "cashback": "до 3%",                 "tip": "Скидка растёт при увеличении суммы покупок.",                         "app": "Almi"},
    "доброном":   {"card": "Карта Доброном",      "cashback": "1–2% бонусами",         "tip": "Дополнительные скидки в категории «Товары дня».",                     "app": "Доброном"},
    "mart":       {"card": "MART Card",           "cashback": "2%",                    "tip": "Свежие продукты дешевле за 2 часа до закрытия.",                      "app": "MART"},
    "bigzz":      {"card": "BigZZ Card",          "cashback": "до 5%",                 "tip": "Распродажи каждый вторник и четверг.",                                "app": "BigZZ"},
}

STORE_SITES = {
    "евроопт": "euroopt.by",
    "гиппо": "gippo.by",
    "корона": "korona.by",
    "prostore": "prostore.by",
    "простор": "prostore.by",
    "белмаркет": "belmarket.by",
    "соседи": "sosedi.by",
    "рублёвский": "rubliovskiy.by",
    "алми": "almi.by",
    "доброном": "dobronom.by",
    "mart": "mart.by",
    "bigzz": "bigzz.by",
}

# ─── Кэш ──────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def _cache_get(key: str) -> Optional[dict]:
    entry = _load_cache().get(key)
    if not entry:
        return None
    cached_at = datetime.fromisoformat(entry.get("cached_at", "2000-01-01"))
    if datetime.now() - cached_at > timedelta(days=CACHE_TTL_DAYS):
        logger.info(f"[loyalty] кэш для {key} устарел")
        return None
    return entry["data"]

def _cache_set(key: str, data: dict):
    cache = _load_cache()
    cache[key] = {"cached_at": datetime.now().isoformat(), "data": data}
    _save_cache(cache)

# ─── Поиск через LLM с web search ─────────────────────────

def _search_via_perplexity(store_name: str, site: str = "") -> Optional[dict]:
    """
    Ищет программу лояльности и акции через Perplexity/sonar.
    Полностью гибкий — работает с ЛЮБЫМ магазином.
    site — подсказка, Perplexity сам находит нужные страницы.
    """
    OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
    if not OPENROUTER_KEY:
        logger.warning("[loyalty] OPENROUTER_API_KEY не задан")
        return None

    try:
        import openai
        client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
            default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "Banking RAG"},
        )
        from datetime import date
        today = date.today().strftime("%d.%m.%Y")
        site_hint = f" (сайт: {site})" if site else " (магазин Беларуси)"

        prompt = f"""Найди актуальную информацию о программе лояльности и акциях магазина «{store_name}»{site_hint}.
Дата: {today}. Только актуальные данные, не выдумывай.

Верни ТОЛЬКО JSON:
{{
  "card": "название карты или null",
  "cashback": "кешбэк/скидка или null",
  "conditions": "условия накопления или null",
  "current_promos": ["текущая акция 1", "акция 2"],
  "special_offers": ["постоянное спецпредложение"],
  "app": "приложение или null",
  "app_features": "функции приложения или null",
  "tip": "главный совет по экономии или null",
  "official_site": "найденный сайт или null"
}}"""

        resp = client.chat.completions.create(
            model="perplexity/sonar",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content.strip()
        logger.info(f"[loyalty] Perplexity → {store_name}: {raw[:150]}")

        s, e = raw.find('{'), raw.rfind('}') + 1
        if s == -1 or e <= s:
            return None

        data = json.loads(raw[s:e])
        if not any(data.get(k) for k in ("card", "cashback", "current_promos")):
            logger.info(f"[loyalty] нет данных для {store_name}")
            return None

        data["current_promos"] = [p for p in (data.get("current_promos") or []) if p]
        data["special_offers"] = [p for p in (data.get("special_offers") or []) if p]
        data["source"] = "web_search"
        data["fetched_at"] = today
        # Обновляем site если Perplexity нашёл официальный сайт
        if data.get("official_site") and not site:
            key = _match_key(store_name)
            if key and key in STORE_SITES:
                pass  # не перезаписываем известные
            # Сохраняем найденный сайт в кэш для будущих запросов
            _update_discovered_site(store_name, data["official_site"])
        return data

    except Exception as ex:
        logger.warning(f"[loyalty] Perplexity ошибка {store_name}: {ex}")
        return None


def _update_discovered_site(store_name: str, site: str):
    """Сохраняет найденный Perplexity сайт в кэш для будущих запросов."""
    try:
        cache = _load_cache()
        if "_discovered_sites" not in cache:
            cache["_discovered_sites"] = {}
        cache["_discovered_sites"][store_name.lower()] = site
        _save_cache(cache)
        logger.info(f"[loyalty] discovered site for {store_name}: {site}")
    except Exception:
        pass



def _search_via_tavily(store_name: str, site: str) -> Optional[dict]:
    """
    Альтернатива: поиск через Tavily API.
    Tavily сам ищет + парсит страницы и возвращает чистый текст.
    Free tier: 1000 запросов/мес. Ключ: https://app.tavily.com
    """
    TAVILY_KEY = os.getenv("TAVILY_API_KEY")
    if not TAVILY_KEY:
        return None

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_KEY)

        # Поиск с фокусом на конкретный сайт
        result = client.search(
            query=f"программа лояльности карта {store_name} кешбэк условия site:{site}",
            search_depth="advanced",    # парсит полный контент страниц
            include_domains=[site],     # только официальный сайт
            max_results=3,
        )

        # Собираем текст из результатов
        texts = []
        for r in result.get("results", []):
            content = r.get("content", "")
            if content and len(content) > 100:
                texts.append(f"[{r.get('url', '')}]\n{content}")

        if not texts:
            logger.info(f"[loyalty] Tavily не нашёл результатов для {store_name}")
            return None

        combined = "\n\n".join(texts[:2])
        logger.info(f"[loyalty] Tavily нашёл {len(texts)} результатов для {store_name}")

        # Структурируем через LLM
        from services.llm_utils import ask_llm
        struct_prompt = f"""Из текста о программе лояльности магазина «{store_name}» извлеки точную информацию.
Используй ТОЛЬКО то что есть в тексте.

Верни ТОЛЬКО JSON:
{{
  "card": "название карты или null",
  "cashback": "размер кешбэка/скидки или null",
  "tip": "главное условие одним предложением или null",
  "app": "название приложения или null"
}}

Текст:
{combined[:1200]}"""

        raw = ask_llm(struct_prompt, temperature=0.0, max_tokens=250)
        if not raw:
            return None

        s, e = raw.find('{'), raw.rfind('}') + 1
        if s == -1 or e <= s:
            return None

        data = json.loads(raw[s:e])
        if not any(data.get(k) for k in ("card", "cashback")):
            return None

        data["source"] = "tavily"
        return data

    except Exception as e:
        logger.warning(f"[loyalty] Tavily ошибка для {store_name}: {e}")
        return None

# ─── Главные функции ──────────────────────────────────────

def _match_key(store_name: str) -> Optional[str]:
    name = store_name.lower().strip()
    for key in FALLBACK_DATA:
        if key in name:
            return key
    return None


def parse_store_loyalty(store_name: str) -> Optional[dict]:
    """
    Получает актуальные данные о программе лояльности через веб-поиск.
    Пробует: Perplexity → Tavily → None
    """
    key = _match_key(store_name)
    if not key:
        logger.info(f"[loyalty] нет конфига для «{store_name}»")
        return None

    site = STORE_SITES.get(key, "")

    # 1. Perplexity через OpenRouter (ключ уже есть в проекте)
    logger.info(f"[loyalty] поиск через Perplexity для «{store_name}»")
    result = _search_via_perplexity(store_name, site)
    if result and any(result.get(k) for k in ("card", "cashback")):
        _cache_set(key, result)
        logger.info(f"[loyalty] ✅ {store_name} — данные получены через Perplexity")
        return result

    # 2. Tavily (если задан ключ)
    if os.getenv("TAVILY_API_KEY"):
        logger.info(f"[loyalty] поиск через Tavily для «{store_name}»")
        result = _search_via_tavily(store_name, site)
        if result and any(result.get(k) for k in ("card", "cashback")):
            _cache_set(key, result)
            logger.info(f"[loyalty] ✅ {store_name} — данные получены через Tavily")
            return result

    logger.info(f"[loyalty] веб-поиск не дал результатов для «{store_name}»")
    return None


def get_loyalty_info(store_name: str) -> Optional[dict]:
    """
    Главная функция: кэш → веб-поиск (Perplexity) → fallback.
    Работает с ЛЮБЫМ магазином — не только из FALLBACK_DATA/STORE_SITES.
    source = 'cache' | 'web_search' | 'fallback'
    """
    if not store_name or not store_name.strip():
        return None

    key = _match_key(store_name)
    cache_key = key or store_name.lower().strip()

    # 1. Кэш (по ключу из конфига или по названию)
    cached = _cache_get(cache_key)
    if cached:
        logger.info(f"[loyalty] {store_name} — из кэша")
        return {**cached, "source": "cache"}

    # 2. Веб-поиск через Perplexity
    # Для известных магазинов передаём site-подсказку
    site = STORE_SITES.get(key, "") if key else ""
    # Проверяем ранее найденные сайты
    try:
        disc = _load_cache().get("_discovered_sites", {})
        site = site or disc.get(store_name.lower(), "")
    except Exception:
        pass

    found = _search_via_perplexity(store_name, site)
    if found and any(found.get(k) for k in ("card", "cashback", "current_promos")):
        # Кэшируем под правильным ключом
        _cache_set(cache_key, found)
        return found

    # 3. Fallback только для известных магазинов
    if key:
        fb = FALLBACK_DATA.get(key)
        if fb:
            logger.info(f"[loyalty] {store_name} — fallback данные")
            return {**fb, "source": "fallback"}

    # 4. Для незнакомого магазина — None (не возвращаем выдуманные данные)
    logger.info(f"[loyalty] данные для «{store_name}» не найдены")
    return None


def format_loyalty_block(store_name: str, info: dict) -> str:
    """
    Форматирует полный блок о магазине:
    карта лояльности + акции + спецпредложения.
    """
    source_icons = {
        "cache":      "✅ актуально (кэш)",
        "web_search": "✅ актуально (веб-поиск)",
        "tavily":     "✅ актуально (веб-поиск)",
        "fallback":   "📋 базовая информация",
    }
    src = info.get("source", "")
    icon = source_icons.get(src, "")
    fetched = info.get("fetched_at", "")

    lines = [f"\n🏪 **{store_name}**"]

    # Карта лояльности
    if info.get("card"):
        lines.append(f"  🃏 Карта: **{info['card']}**")
    if info.get("cashback"):
        lines.append(f"  💰 Выгода: {info['cashback']}")
    if info.get("conditions"):
        lines.append(f"  📋 Условия: {info['conditions']}")

    # Текущие акции
    promos = info.get("current_promos") or []
    if promos:
        lines.append("  \n  🔥 **Текущие акции:**")
        for p in promos[:4]:
            lines.append(f"    • {p}")

    # Спецпредложения
    specials = info.get("special_offers") or []
    if specials:
        lines.append("  \n  ⭐ **Специальные предложения:**")
        for s in specials[:3]:
            lines.append(f"    • {s}")

    # Приложение
    if info.get("app"):
        app_line = f"  📱 {info['app']}"
        if info.get("app_features"):
            app_line += f" — {info['app_features']}"
        lines.append(app_line)

    # Главный совет
    if info.get("tip"):
        lines.append(f"  \n  💡 {info['tip']}")

    # Источник
    suffix = f" • {fetched}" if fetched else ""
    if icon:
        lines.append(f"  _{icon}{suffix}_")

    return "\n".join(lines)


def refresh_cache_for_stores(store_names: list) -> dict:
    """Принудительно обновляет кэш. Запускать по расписанию раз в неделю."""
    results = {}
    for store in store_names:
        found = parse_store_loyalty(store)
        if found and any(found.get(k) for k in ("card", "cashback")):
            results[store] = f"✅ обновлён ({found.get('source')})"
        else:
            results[store] = "⚠️ не удалось (используется fallback)"
        time.sleep(1)
    return results


# ─── CLI ──────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    stores = sys.argv[1:] if len(sys.argv) > 1 else list(FALLBACK_DATA.keys())
    print(f"Поиск данных для {len(stores)} магазинов...\n")

    for store in stores:
        print(f"{'─'*40}\nМагазин: {store}")
        info = get_loyalty_info(store)
        if info:
            for k in ("card", "cashback", "tip", "app", "source"):
                if info.get(k):
                    print(f"  {k:12}: {info[k]}")
        else:
            print("  ❌ данные не найдены")
        print()