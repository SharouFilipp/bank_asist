import requests
from bs4 import BeautifulSoup
import json
import os
import logging
from datetime import datetime, time, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

MYFIN_URL = "https://myfin.by/currency/torgi-na-bvfb"
CACHE_FILE = "data/trading_cache.json"

def parse_myfin_page() -> Optional[Dict[str, dict]]:
    """Парсит страницу с карточками валют и возвращает словарь {код_валюты: данные}."""
    try:
        resp = requests.get(MYFIN_URL, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Ищем все карточки валют
        cards = soup.select('.currency-detailed-change-card')
        if not cards:
            logger.warning("Карточки валют не найдены")
            return None

        data = {}
        for card in cards:
            # Определяем валюту по ссылке в заголовке
            head_link = card.select_one('.currency-detailed-change-card__currency span')
            if not head_link:
                continue
            currency_text = head_link.get_text(strip=True)
            code = None
            if 'USD' in currency_text:
                code = 'USD'
            elif 'EUR' in currency_text:
                code = 'EUR'
            elif 'RUB' in currency_text:
                code = 'RUB'
            elif 'CNY' in currency_text:
                code = 'CNY'
            else:
                continue

            # Время обновления
            update_time = card.select_one('.currency-detailed-change-card__update-time span')
            date_str = update_time.get_text(strip=True) if update_time else ""

            # Блок изменений: изменение, значение курса, процент
            changes = card.select('.currency-detailed-change-card__changes-cell')
            change = changes[0].get_text(strip=True) if len(changes) > 0 else ""
            rate = changes[1].get_text(strip=True) if len(changes) > 1 else ""
            percent = changes[2].get_text(strip=True) if len(changes) > 2 else ""

            # Детальная информация (ключ-значение)
            info_items = card.select('.currency-detailed-change-card__info-list-item')
            details = {}
            for item in info_items:
                spans = item.find_all('span')
                if len(spans) >= 2:
                    key = spans[0].get_text(strip=True)
                    val = spans[1].get_text(strip=True)
                    details[key] = val

            data[code] = {
                "symbol": currency_text,
                "date": date_str,
                "change": change,
                "rate": rate,
                "percent": percent,
                "start": details.get("Стартовый курс", ""),
                "last": details.get("Последняя сделка", ""),
                "min": details.get("Min курс", ""),
                "max": details.get("Max курс", ""),
                "deals": details.get("Количество сделок", ""),
                "volume": details.get("Оборот в BYN", "")
            }

        return data if data else None
    except Exception as e:
        logger.error(f"Ошибка парсинга myfin: {e}")
        return None

def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def _save_cache(data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_cached_trading_data() -> Optional[Dict[str, dict]]:
    """
    Возвращает кешированные данные торгов, если они свежие (сегодня после 13:00).
    Иначе None.
    """
    cache = _load_cache()
    if not cache or "timestamp" not in cache:
        return None
    cached_time = datetime.fromisoformat(cache["timestamp"])
    now = datetime.now()
    # Данные свежие, если они сегодняшние и время кеша после 13:00
    if cached_time.date() == now.date() and cached_time.time() >= time(13, 0):
        return cache.get("data")
    return None

def fetch_and_cache_trading_data() -> bool:
    """Парсит и сохраняет данные с текущим временем."""
    data = parse_myfin_page()
    if not data:
        return False
    cache = {
        "timestamp": datetime.now().isoformat(),
        "data": data
    }
    _save_cache(cache)
    logger.info("Биржевые данные обновлены")
    return True