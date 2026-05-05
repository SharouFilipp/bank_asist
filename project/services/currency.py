import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional


logger = logging.getLogger(__name__)

# Кэш курсов (обновляется раз в сутки)
_rates_cache: Dict[str, float] = {}
_last_update: Optional[datetime] = None

CURRENCIES = {
    "USD": 145, "EUR": 292, "RUB": 298, "BYN": 0
}


# services/currency.py (добавить в начало файла)
import json
import os

# Глобальный кэш курсов банка из JSON
_BANK_RATES = []  # список всех записей из файла kursExchange.json

def load_bank_rates_from_json(filepath: str = "data/kursExchange.json"):
    global _BANK_RATES
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            _BANK_RATES = json.load(f)
        logger.info(f"Загружено {len(_BANK_RATES)} записей о курсах банка из {filepath}")
    else:
        logger.warning(f"Файл {filepath} не найден, курсы банка недоступны")
        _BANK_RATES = []

def get_bank_rates_for_city(city: str) -> dict:
    """
    Возвращает курсы валют для указанного города.
    Если city пустой – возвращает средние курсы по всем филиалам.
    Формат результата: {'USD_in': 2.97, 'USD_out': 3.015, ...}
    """
    if not _BANK_RATES:
        return {}
    # Фильтруем записи по городу (поле 'name')
    if city:
        records = [r for r in _BANK_RATES if r.get("name", "").lower() == city.lower()]
        if not records:
            return {}  # город не найден
    else:
        records = _BANK_RATES

    # Собираем суммы по всем валютам, чтобы усреднить
    from collections import defaultdict
    sum_rates = defaultdict(float)
    count_rates = defaultdict(int)
    for rec in records:
        for key in rec:
            if key.endswith(('_in', '_out')) and rec[key]:
                try:
                    val = float(rec[key])
                    if val > 0:
                        sum_rates[key] += val
                        count_rates[key] += 1
                except ValueError:
                    continue
    # Вычисляем средние
    avg_rates = {}
    for key in sum_rates:
        avg_rates[key] = round(sum_rates[key] / count_rates[key], 4) if count_rates[key] else 0.0
    return avg_rates

def fetch_currency_dynamics(cur_id: int, start_date: str, end_date: str) -> list[dict]:
    """
    Получает динамику курса валюты.
    Сначала пробует диапазонный эндпоинт, если данных нет или период большой – собирает по дням.
    """
    # 1. Пробуем диапазонный эндпоинт (быстрее)
    url = f"https://api.nbrb.by/exrates/rates/dynamics/{cur_id}?startdate={start_date}&enddate={end_date}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return [
                {"date": item["Date"].split("T")[0], "rate": item["Cur_OfficialRate"]}
                for item in data
            ]
    except Exception as e:
        logger.warning(f"Диапазонный запрос не удался: {e}")

    # 2. Если диапазонный не дал результатов – собираем по дням
    logger.info("Сбор динамики по дням...")
    rates = []
    current_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    while current_dt <= end_dt:
        day_str = current_dt.strftime("%Y-%m-%d")
        rate = fetch_currency_rate_on_date(cur_id, day_str)
        if rate is not None:
            rates.append({"date": day_str, "rate": rate})
        current_dt += timedelta(days=1)

    # Пауза между запросами, чтобы не нагружать API (0.1 сек)
    import time
    time.sleep(0.1)

    return rates


def fetch_rates_from_nbrb():
    """Загружает курсы валют с API НБРБ."""
    rates = {}
    for code, cur_id in CURRENCIES.items():
        if cur_id == 0:          # BYN
            rates[code] = 1.0
            continue
        try:
            url = f"https://api.nbrb.by/exrates/rates/{code}?parammode=2"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            rates[code] = data["Cur_OfficialRate"]
        except Exception as e:
            logger.error(f"Ошибка получения курса {code}: {e}")
            # Использовать последнее известное значение
            rates[code] = _rates_cache.get(code, 1.0)
    return rates
import time  # добавьте в начало файла, если ещё нет

def fetch_currency_rate_on_date(cur_id: int, date_str: str) -> Optional[float]:
    # Прямой запрос с повторными попытками
    url = f"https://api.nbrb.by/exrates/rates/{cur_id}?ondate={date_str}&parammode=2"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()["Cur_OfficialRate"]
        except requests.Timeout:
            logger.debug(f"Тайм-аут прямого запроса для {cur_id} на {date_str}, попытка {attempt+1}")
            time.sleep(1)
        except Exception:
            break   # не повторяем другие ошибки

    # Резервный способ через общий список (тоже с повторными попытками)
    for attempt in range(2):
        all_rates = fetch_all_rates_on_date(date_str)
        if all_rates:
            cur_abbr_map = {145: "USD", 292: "EUR", 298: "RUB"}
            abbr = cur_abbr_map.get(cur_id)
            if abbr and abbr in all_rates:
                return all_rates[abbr]
        time.sleep(1)

    logger.warning(f"Курс для {cur_id} на {date_str} не найден после всех попыток")
    return None

def fetch_all_rates_on_date(date_str: str) -> dict:
    url = f"https://api.nbrb.by/exrates/rates?ondate={date_str}&periodicity=0"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            return {item["Cur_Abbreviation"]: item["Cur_OfficialRate"] for item in data}
        except requests.Timeout:
            logger.debug(f"Тайм-аут общего списка на {date_str}, попытка {attempt+1}")
            time.sleep(1)
        except Exception:
            break
    logger.warning(f"Не удалось загрузить все курсы на {date_str}")
    return {}


def get_rates():
    global _rates_cache, _last_update
    now = datetime.now()
    if _last_update is None or (now - _last_update) > timedelta(hours=1):
        _rates_cache = fetch_rates_from_nbrb()
        _last_update = now
    return _rates_cache


def convert(amount: float, from_cur: str, to_cur: str) -> float:
    rates = get_rates()
    from_cur = from_cur.upper()
    to_cur = to_cur.upper()
    if from_cur == to_cur:
        return amount
    # Конвертация через BYN
    byn_amount = amount * rates.get(from_cur, 1.0)
    return round(byn_amount / rates.get(to_cur, 1.0), 2)


def get_supported_currencies():
    return list(CURRENCIES.keys())