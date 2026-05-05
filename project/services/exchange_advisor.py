# services/exchange_advisor.py
import os
import json
import logging
from datetime import datetime
from typing import List, Optional, Dict
from math import radians, sin, cos, sqrt, atan2
import requests as http_requests
import re
from datetime import datetime

from database import SessionLocal, FilialLocation
from services.currency import _BANK_RATES, load_bank_rates_from_json

logger = logging.getLogger(__name__)

# Убедимся, что банковские курсы загружены в глобальную переменную
if not _BANK_RATES:
    load_bank_rates_from_json("data/kursExchange.json")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в километрах между двумя точками."""
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def parse_worktime(worktime_str: str):
    """
    Извлекает время открытия/закрытия для сегодняшнего дня.
    Понимает форматы:
      - ЧЧ:ММ–ЧЧ:ММ (например, 09:00–18:00)
      - ЧЧ ММ ЧЧ ММ (например, 00 00 23 59)
    Возвращает (open_minutes, close_minutes) или дефолт (9*60, 18*60).
    """
    try:
        today = datetime.now().weekday()  # 0 = понедельник
        days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        target_day = days_ru[today]
        clean = worktime_str.lower().replace(" ", "")
        parts = clean.split("|")
        for part in parts:
            if target_day in part:
                # Ищем времена в двух форматах
                # 1) ЧЧ:ММ
                times = re.findall(r'\d{2}:\d{2}', part)
                if len(times) >= 2:
                    h1, m1 = map(int, times[0].split(':'))
                    h2, m2 = map(int, times[1].split(':'))
                    return (h1*60 + m1, h2*60 + m2)
                # 2) ЧЧ ММ (без двоеточия) – ищем четыре подряд идущие двузначные числа
                # Но в части после дня могут быть ещё числа, поэтому аккуратно:
                nums = re.findall(r'\d{2}', part)
                # nums будет содержать все двузначные числа: часы и минуты
                if len(nums) >= 4:
                    h1, m1, h2, m2 = map(int, nums[:4])
                    return (h1*60 + m1, h2*60 + m2)
    except Exception as e:
        logger.debug(f"Ошибка парсинга времени '{worktime_str}': {e}")
    return (540, 1080)


def calc_time_score(worktime_str: str) -> float:
    """
    Оценка удобства времени:
      - 1.0, если отделение открыто круглосуточно (0:00–23:59),
      - 1.0, если сейчас открыто и до закрытия >= 30 минут,
      - 0.0, если закрыто,
      - линейно между 0 и 1 при оставшемся времени < 30 минут.
    """
    open_min, close_min = parse_worktime(worktime_str)
    # Круглосуточное? (0:00–23:59 или 0:00–24:00)
    if open_min == 0 and (close_min == 1439 or close_min == 1440):
        return 1.0

    now = datetime.now()
    current_min = now.hour * 60 + now.minute
    if current_min < open_min or current_min >= close_min:
        return 0.0
    remaining = close_min - current_min
    return min(1.0, remaining / 30.0)

def time_status(worktime_str: str) -> str:
    """Возвращает читаемый статус работы отделения на данный момент."""
    open_min, close_min = parse_worktime(worktime_str)
    if open_min == 0 and close_min in (1439, 1440):
        return "круглосуточно"
    now = datetime.now()
    current_min = now.hour * 60 + now.minute
    if current_min < open_min:
        h, m = divmod(open_min, 60)
        return f"откроется в {h:02d}:{m:02d}"
    if current_min >= close_min:
        return "закрыто"
    remaining = close_min - current_min
    if remaining < 30:
        return "скоро закроется"
    return "открыто"


def geocode_city(city: str) -> Optional[tuple]:
    """Получить координаты центра города через Nominatim (OpenStreetMap)."""
    try:
        resp = http_requests.get(
            f"https://nominatim.openstreetmap.org/search?q={city}+Беларусь&format=json&limit=1",
            headers={"User-Agent": "BankingRAG/1.0"},
            timeout=5
        )
        data = resp.json()
        if data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            logger.info(f"Геокодирован город {city}: {lat}, {lon}")
            return lat, lon
    except Exception as e:
        logger.warning(f"Ошибка геокодирования города {city}: {e}")
    return None
from services.geo_service import find_nearest_filials

def find_best_exchange(
    city: str,
    currency: str,
    operation: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    w_course: float = 0.6,
    w_distance: float = 0.3,
    w_time: float = 0.1
) -> List[Dict]:
    # 1. Фильтр кандидатов по городу и наличию нужного курса
    candidates = [b for b in _BANK_RATES if b.get("name") == city]
    if not candidates:
        return []

    rate_key = f"{currency}_out" if operation == "buy" else f"{currency}_in"
    valid = []
    for c in candidates:
        val = c.get(rate_key)
        if val and val != "0.0000":
            try:
                c["_rate"] = float(val)
                valid.append(c)
            except ValueError:
                continue
    if not valid:
        return []

    # 2. Координаты пользователя или центра города (если не переданы)
    user_lat, user_lon = lat, lon
    if user_lat is None or user_lon is None:
        geocoded = geocode_city(city)
        if geocoded:
            user_lat, user_lon = geocoded
        else:
            # Если геокодирование не удалось — используем центр Минска как fallback
            user_lat, user_lon = 53.9, 27.56
            logger.warning(f"Не удалось получить координаты города {city}, используется Минск")

    # 3. Получаем все отделения с расстояниями через проверенную geo_service
    db = SessionLocal()
    try:
        # Запрашиваем достаточно много ближайших, чтобы охватить весь город
        nearest = find_nearest_filials(db, user_lat, user_lon, top_n=500)
    finally:
        db.close()
    print(nearest)

    # Строим словарь расстоянией по filial_id (строка)
    dist_map = {}
    for f in nearest:
        fid = str(f['filial_id'])
        dist_map[fid] = f['distance_km']
    print(dist_map )

    # 4. Собираем курсы и вычисляем рейтинг
    rates = [c["_rate"] for c in valid]
    min_rate, max_rate = min(rates), max(rates)
    rate_range = max_rate - min_rate if max_rate != min_rate else 1.0

    # Сначала соберём все расстояния, чтобы вычислить min/max
    results_raw = []
    for c in valid:
        fid_str = str(c.get("filial_id"))
        distance_km = dist_map.get(fid_str)        # может быть None, если не найдено
        c["_distance"] = distance_km
        time_score = calc_time_score(c.get("info_worktime", ""))
        c["_time_score"] = time_score
        results_raw.append(c)
    

    # Извлекаем только существующие расстояния
    distances = [c["_distance"] for c in results_raw if c["_distance"] is not None]
    use_distance = len(distances) > 0
    
    if use_distance:
        min_dist = min(distances)
        max_dist = max(distances)
        dist_range = max_dist - min_dist if max_dist != min_dist else 1.0
    else:
        # Перераспределяем веса, если расстояний нет
        w_course += w_distance * 0.7
        w_time += w_distance * 0.3
        w_distance = 0.0
        min_dist = max_dist = dist_range = 0  # не используются

    # Финальный расчёт рейтинга
    final = []
    for c in results_raw:
        rate = c["_rate"]
        profit = (max_rate - rate) / rate_range if operation == "buy" else (rate - min_rate) / rate_range

        dist = c.get("_distance")
        if use_distance and dist is not None:
            dist_score = 1 - (dist - min_dist) / dist_range
        else:
            dist_score = 0.0

        total = w_course * profit + w_distance * dist_score + w_time * c["_time_score"]

        final.append({
            "filial_id": c.get("filial_id"),
            "filial_name": c.get("filials_text", "Отделение"),
            "address": f"{c.get('street_type','')} {c.get('street','')}, {c.get('home_number','')}",
            "rate": rate,
            "profit": round(profit, 3),
            "distance_km": round(dist, 2) if dist else None,
            "distance_score": round(dist_score, 3) if use_distance else None,
            "time_score": round(time_score, 3),
            "time_status": time_status(c.get("info_worktime", "")),
            "total_score": round(total, 4)
        })
    print(final)

    final.sort(key=lambda x: x["total_score"], reverse=True)
    return final[:5]