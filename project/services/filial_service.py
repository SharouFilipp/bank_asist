"""
services/filial_service.py

Единый сервис работы с отделениями банка.
Обрабатывает все сценарии:
  - список по городу (с подсказкой если геолокация в другом городе)
  - ближайшие N штук (с геолокацией)
  - детали: адрес + часы + курсы в одном ответе
  - рейтинг "лучшее отделение для обмена"
"""

import re
import logging
from datetime import datetime, time as dtime
from typing import Optional, List, Dict, Tuple
from math import radians, cos, sin, asin, sqrt

logger = logging.getLogger(__name__)

# ── Ключи курсов ─────────────────────────────────────────
RATE_LABELS = {
    "USD_in": "USD покупка", "USD_out": "USD продажа",
    "EUR_in": "EUR покупка", "EUR_out": "EUR продажа",
    "RUB_in": "RUB покупка", "RUB_out": "RUB продажа",
    "PLN_in": "PLN покупка", "PLN_out": "PLN продажа",
    "CNY_in": "CNY покупка", "CNY_out": "CNY продажа",
}

CURRENCY_NAMES = {
    "USD": "доллар", "EUR": "евро", "RUB": "российский рубль",
    "PLN": "польский злотый", "CNY": "юань",
}

# ── Расстояние ────────────────────────────────────────────
def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return round(R * 2 * asin(sqrt(a)), 2)

# ── Часы работы ───────────────────────────────────────────
# Реальный формат info_worktime из kursExchange.json:
# "Пн 09 00|Пн 19 00|Вт 09 00|Вт 19 00|Сб 09 00|Сб 14 00|Вс 00 00|Вс 00 00"
# Каждый день — два сегмента: начало и конец (часы и минуты через пробел)

_DAY_RU = {"Пн": 1, "Вт": 2, "Ср": 3, "Чт": 4, "Пт": 5, "Сб": 6, "Вс": 7}
_DAY_NAME = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}


def _parse_worktime(raw: str) -> dict:
    """
    Парсит строку формата "|Пн 10 00 19 00    |Вт 10 00 19 00    |..."
    Каждый сегмент содержит: день откр_ч откр_м закр_ч закр_м.
    Возвращает {isoweekday: ("10:00", "19:00")}.
    """
    if not raw or not raw.strip():
        return {}

    parts = [p.strip() for p in raw.split("|") if p.strip()]
    result = {}
    for part in parts:
        if len(part) < 2:
            continue
        day_code = part[:2]
        day_num = _DAY_RU.get(day_code)
        if day_num is None:
            continue
        nums = re.findall(r'\d{2}', part[2:])
        if len(nums) >= 4:
            result[day_num] = (f"{nums[0]}:{nums[1]}", f"{nums[2]}:{nums[3]}")
    return result


def _is_open_now(worktime_raw: str) -> Tuple[bool, str]:
    """Возвращает (открыто: bool, статус: str)."""
    if not worktime_raw or not worktime_raw.strip():
        return False, "⬜ Часы не указаны"

    now = datetime.now()
    weekday = now.isoweekday()  # 1=пн, 7=вс
    parsed = _parse_worktime(worktime_raw)

    if not parsed:
        return False, "⬜ Часы не указаны"

    if weekday not in parsed:
        return False, "🔴 Закрыто (выходной)"

    open_s, close_s = parsed[weekday]

    # 00:00–00:00 = выходной
    if open_s == "00:00" and close_s == "00:00":
        return False, "🔴 Закрыто (выходной)"

    # Круглосуточно
    if open_s == "00:00" and close_s in ("23:59", "24:00", "00:00"):
        return True, "✅ Круглосуточно"

    try:
        oh, om = map(int, open_s.split(":"))
        ch, cm = map(int, close_s.split(":"))
        open_t = dtime(oh, om)
        close_t = dtime(ch, cm)
        current = now.time().replace(second=0, microsecond=0)
        if open_t <= current <= close_t:
            close_dt = datetime.combine(now.date(), close_t)
            mins_left = int((close_dt - now).total_seconds() / 60)
            if mins_left <= 30:
                return True, f"⚠️ Открыто, закрывается через {mins_left} мин ({close_s})"
            return True, f"✅ Открыто до {close_s}"
        else:
            if current < open_t:
                return False, f"🔴 Закрыто, откроется в {open_s}"
            return False, f"🔴 Закрыто (было до {close_s})"
    except Exception:
        return False, "⬜ Часы не указаны"


def _format_worktime_full(raw: str) -> str:
    """Полное расписание по дням недели."""
    parsed = _parse_worktime(raw)
    if not parsed:
        return "Часы работы не указаны"
    parts = []
    for day in range(1, 8):
        name = _DAY_NAME[day]
        if day in parsed:
            o, c = parsed[day]
            if o == "00:00" and c == "00:00":
                parts.append(f"{name}: закрыто")
            elif o == "00:00" and c in ("23:59", "24:00"):
                parts.append(f"{name}: круглосуточно")
            else:
                parts.append(f"{name}: {o}–{c}")
        else:
            parts.append(f"{name}: закрыто")
    return " | ".join(parts)

# ── Адрес ─────────────────────────────────────────────────
def _format_address(b: dict) -> str:
    st = b.get("street_type", "")
    s = b.get("street", "")
    h = b.get("home_number", "")
    city = b.get("name", "")
    addr = f"{st} {s}, {h}".strip(", ").strip()
    return f"{city}, {addr}" if city else addr

# ── Курсы ─────────────────────────────────────────────────
def _format_rates(b: dict, currency: str = None) -> str:
    """Форматирует курсы — все или для конкретной валюты."""
    lines = []
    for key, label in RATE_LABELS.items():
        if currency and not key.startswith(currency):
            continue
        val = b.get(key)
        if val and str(val) not in ("0", "0.0", "0.0000", ""):
            lines.append(f"  {label}: {val} BYN")
    return "\n".join(lines) if lines else "Курсы не указаны"

def _has_rates(b: dict) -> bool:
    return any(
        b.get(k) and str(b.get(k)) not in ("0","0.0","0.0000","")
        for k in RATE_LABELS
    )

# ── Извлечение числа из запроса ───────────────────────────
def _extract_count(query: str) -> int:
    """Извлекает сколько отделений показать. По умолчанию 5."""
    m = re.search(r'\b(\d+)\b', query)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 50:
            return n
    words = {"один": 1, "одно": 1, "два": 2, "две": 2, "три": 3,
             "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
             "восемь": 8, "девять": 9, "десять": 10}
    for w, n in words.items():
        if w in query.lower():
            return n
    return 5

# ── Форматирование одного отделения ───────────────────────
def format_branch_card(b: dict, idx: int = None,
                       dist_km: float = None,
                       show_rates: bool = False,
                       show_hours: bool = False,
                       show_status: bool = True,
                       currency: str = None,
                       rating: dict = None) -> str:
    """
    Полная карточка отделения.
    idx — порядковый номер, dist_km — расстояние.
    """
    name = b.get("filials_text", "Отделение")
    addr = _format_address(b)
    lines = []

    # Заголовок
    prefix = f"{idx}. " if idx else ""
    dist_str = f" — {dist_km} км" if (dist_km is not None and dist_km < 900) else ""
    lines.append(f"**{prefix}{name}**{dist_str}")
    lines.append(f"📍 {addr}")

    # Статус открыто/закрыто
    if show_status:
        wt_raw = b.get("info_worktime", "")
        is_open, status_str = _is_open_now(wt_raw)
        lines.append(f"🕐 {status_str}")

    # Полные часы
    if show_hours:
        wt_raw = b.get("info_worktime", "")
        lines.append(f"📅 {_format_worktime_full(wt_raw)}")

    # Курсы
    if show_rates:
        rates_str = _format_rates(b, currency)
        if rates_str != "Курсы не указаны":
            cur_label = f" ({CURRENCY_NAMES.get(currency, currency)})" if currency else ""
            lines.append(f"💱 **Курсы{cur_label}:**\n{rates_str}")
        else:
            lines.append("💱 Курсы не указаны")

    # Рейтинг (для best_exchange)
    if rating:
        score = rating.get("score", 0)
        stars = "⭐" * min(5, max(1, round(score)))
        lines.append(f"🏆 Оценка: {stars} ({score:.1f}/5)")
        if rating.get("reason"):
            lines.append(f"   {rating['reason']}")

    return "\n".join(lines)

# ── Рейтинг для "лучший филиал" ───────────────────────────
def _score_branch(b: dict, lat: float, lon: float,
                  currency: str = "USD", operation: str = "buy",
                  coords_map: dict = None) -> dict:
    """
    Оценивает отделение по трём критериям:
    - курс (40%)
    - расстояние (40%)
    - открыто прямо сейчас (20%)
    coords_map: {filial_id: (lat, lon)} — предзагруженные координаты из БД
    """
    fid = str(b.get("filial_id", ""))
    if coords_map and fid in coords_map:
        b_lat, b_lon = coords_map[fid]
    else:
        b_lat = b.get("latitude") or b.get("lat")
        b_lon = b.get("longitude") or b.get("lon")

    # Расстояние
    if b_lat and b_lon:
        dist = _haversine(lat, lon, float(b_lat), float(b_lon))
    else:
        dist = 999

    # Курс
    rate_key = f"{currency}_{'in' if operation == 'buy' else 'out'}"
    rate_val = b.get(rate_key)
    try:
        rate_val = float(rate_val)
    except (TypeError, ValueError):
        rate_val = None

    # Открыто?
    wt_raw = b.get("info_worktime", "")
    is_open, status_str = _is_open_now(wt_raw)

    # Нормализация оценок
    dist_score = max(0, 5 - dist)               # 0–5 км → 0–5 баллов
    dist_score = min(5, dist_score)

    if rate_val:
        # Для покупки — чем выше курс тем лучше (банк даёт больше BYN)
        # Для продажи — чем ниже курс тем лучше (банк берёт меньше BYN)
        rate_score = 3.0  # базовая оценка
    else:
        rate_score = 1.0

    open_score = 5.0 if is_open else 0.0

    total = dist_score * 0.4 + rate_score * 0.4 + open_score * 0.2

    reasons = []
    has_real_coords = (lat != 0.0 or lon != 0.0)
    if not has_real_coords or dist >= 900:
        reasons.append("расстояние неизвестно")
    elif dist < 1:
        reasons.append(f"очень близко ({dist} км)")
    elif dist < 3:
        reasons.append(f"рядом ({dist} км)")
    else:
        reasons.append(f"{dist} км")
    if is_open:
        reasons.append(status_str.replace("✅ ", ""))
    else:
        reasons.append("сейчас закрыто")
    if rate_val:
        op_label = "покупка" if operation == "buy" else "продажа"
        reasons.append(f"{currency} {op_label}: {rate_val} BYN")

    return {
        "score": round(total, 2),
        "dist_km": dist,
        "rate_val": rate_val,
        "is_open": is_open,
        "status": status_str,
        "reason": " • ".join(reasons),
    }

# ── Главные публичные функции ─────────────────────────────

def get_branches_list(branches: list, city: str, show_n: int = 5,
                      show_rates: bool = False,
                      show_hours: bool = False,
                      currency: str = None) -> str:
    """Список отделений города."""
    total = len(branches)
    lines = [f"**Отделения в {city} ({total} шт.):**\n"]
    for i, b in enumerate(branches[:show_n], 1):
        card = format_branch_card(
            b, idx=i,
            show_rates=show_rates,
            show_hours=show_hours,
            show_status=True,
            currency=currency,
        )
        lines.append(card)
        if i < min(show_n, total):
            lines.append("")
    if total > show_n:
        lines.append(f"\n_...и ещё {total - show_n} отделений. Напишите «все отделения в {city}» чтобы увидеть полный список._")
    return "\n".join(lines)


def get_nearest_branches(all_branches: list, lat: float, lon: float,
                         n: int = 5,
                         city_filter: str = None,
                         show_rates: bool = False,
                         show_hours: bool = False,
                         currency: str = None,
                         only_open: bool = False,
                         db=None) -> str:
    """
    N ближайших отделений с расстоянием, статусом, часами и курсами.
    Координаты берёт из БД (FilialLocation) через db, либо из полей branch dict.
    """
    # Строим словарь filial_id -> (lat, lon) из БД
    coords_map = {}
    if db is not None:
        try:
            from database import FilialLocation
            locs = db.query(FilialLocation).all()
            for loc in locs:
                coords_map[str(loc.filial_id)] = (loc.latitude, loc.longitude)
        except Exception as e:
            logger.warning(f"Не удалось загрузить координаты из БД: {e}")

    scored = []
    for b in all_branches:
        if city_filter and b.get("name") != city_filter:
            continue

        fid = str(b.get("filial_id", ""))
        if fid in coords_map:
            b_lat, b_lon = coords_map[fid]
        else:
            b_lat = b.get("latitude") or b.get("lat")
            b_lon = b.get("longitude") or b.get("lon")

        if not b_lat or not b_lon:
            continue

        dist = _haversine(lat, lon, float(b_lat), float(b_lon))
        scored.append((dist, b))

    scored.sort(key=lambda x: x[0])

    # Если просят «открытые» — фильтруем, иначе показываем все (открытые первыми)
    if only_open:
        open_scored = [(d, b) for d, b in scored if _is_open_now(b.get("info_worktime", ""))[0]]
        top = open_scored[:n]
        if not top:
            # Нет открытых — сообщаем
            city_label = f" в {city_filter}" if city_filter else ""
            return f"😔 Сейчас все ближайшие отделения{city_label} закрыты.\n\n💡 Напишите «ближайшие отделения» чтобы увидеть полный список с часами."
    else:
        # Открытые вперёд, закрытые — по расстоянию после
        open_b  = [(d, b) for d, b in scored if _is_open_now(b.get("info_worktime", ""))[0]]
        closed_b = [(d, b) for d, b in scored if not _is_open_now(b.get("info_worktime", ""))[0]]
        top = (open_b + closed_b)[:n]

    # ── Фолбэк: если координат нет — показываем список города без расстояний ──
    if not top:
        if city_filter:
            city_branches = [b for b in all_branches if b.get("name") == city_filter]
        else:
            city_branches = all_branches
        if not city_branches:
            return "Не найдено отделений поблизости."
        city_label = f" в {city_filter}" if city_filter else ""
        lines = [
            f"**Отделения{city_label}** (координаты ещё заполняются, расстояние недоступно):\n"
        ]
        for i, b in enumerate(city_branches[:n], 1):
            card = format_branch_card(
                b, idx=i, dist_km=None,
                show_rates=show_rates,
                show_hours=show_hours,
                show_status=True,
                currency=currency,
            )
            lines.append(card)
            if i < min(n, len(city_branches)):
                lines.append("")
        total = len(city_branches)
        if total > n:
            lines.append(f"\n_...и ещё {total-n} отделений._")
        lines.append("\n_💡 Для точного поиска по расстоянию запустите геокодирование: `python -m services.geo_service`_")
        return "\n".join(lines)

    city_label = f" в {city_filter}" if city_filter else ""
    lines = [f"**{n} ближайших отделений{city_label}:**\n"]
    for i, (dist, b) in enumerate(top, 1):
        card = format_branch_card(
            b, idx=i, dist_km=round(dist, 2),
            show_rates=show_rates,
            show_hours=show_hours,
            show_status=True,
            currency=currency,
        )
        lines.append(card)
        if i < len(top):
            lines.append("")
    return "\n".join(lines)


def get_best_exchange_branch(all_branches: list, lat: float, lon: float,
                              currency: str = "USD",
                              operation: str = "buy",
                              city_filter: str = None,
                              top_n: int = 3,
                              db=None,
                              skip_distance_filter: bool = False) -> str:
    """
    Выбирает лучшие отделения для обмена валюты.
    skip_distance_filter=True — когда город указан явно, не отсеиваем по расстоянию.
    """
    # Загружаем координаты из БД
    coords_map = {}
    if db is not None:
        try:
            from database import FilialLocation
            for loc in db.query(FilialLocation).all():
                coords_map[str(loc.filial_id)] = (loc.latitude, loc.longitude)
        except Exception as e:
            logger.warning(f"Координаты из БД недоступны: {e}")

    scored = []
    for b in all_branches:
        if city_filter and b.get("name") != city_filter:
            continue
        if not _has_rates(b):
            continue
        sc = _score_branch(b, lat, lon, currency, operation, coords_map=coords_map)
        # Фильтруем по расстоянию только если координаты реальные И не задан явный город
        has_real_coords = (lat != 0.0 or lon != 0.0)
        if has_real_coords and not skip_distance_filter and sc["dist_km"] > 50:
            continue
        scored.append((sc, b))

    # Сортируем: сначала открытые по убыванию score, потом закрытые
    open_branches = [(sc, b) for sc, b in scored if sc["is_open"]]
    closed_branches = [(sc, b) for sc, b in scored if not sc["is_open"]]
    open_branches.sort(key=lambda x: -x[0]["score"])
    closed_branches.sort(key=lambda x: -x[0]["score"])
    result = (open_branches + closed_branches)[:top_n]

    if not result:
        return f"Не найдено отделений с курсом {currency} поблизости."

    op_label = "купить" if operation == "buy" else "продать"
    cur_name = CURRENCY_NAMES.get(currency, currency)
    lines = [f"**🏆 Лучшие отделения чтобы {op_label} {cur_name} ({currency}):**\n"]

    for rank, (sc, b) in enumerate(result, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
        name = b.get("filials_text", "Отделение")
        addr = _format_address(b)

        lines.append(f"{medal} **{name}**")
        lines.append(f"📍 {addr}")
        lines.append(f"📏 {sc['dist_km']} км от вас")
        lines.append(f"🕐 {sc['status']}")

        rate_key = f"{currency}_{'in' if operation == 'buy' else 'out'}"
        rate_val = b.get(rate_key)
        if rate_val and str(rate_val) not in ("0", "0.0", "0.0000"):
            op_str = "покупка" if operation == "buy" else "продажа"
            lines.append(f"💱 {currency} {op_str}: **{rate_val} BYN**")

        # Все доступные курсы этого отделения
        other_rates = _format_rates(b)
        if other_rates != "Курсы не указаны":
            lines.append(f"💱 Все курсы:\n{other_rates}")

        # Звёзды
        stars = "⭐" * min(5, max(1, round(sc["score"])))
        lines.append(f"🏆 Оценка: {stars}")

        if rank < len(result):
            lines.append("")

    return "\n".join(lines)


def suggest_other_city(user_city: str, asked_city: str,
                       branches_in_asked: list) -> str:
    """
    Когда геолокация в user_city, а спрашивают про asked_city —
    показывает список asked_city + подсказку.
    """
    total = len(branches_in_asked)
    lines = [
        f"📍 Вы находитесь в **{user_city}**, но я покажу отделения в **{asked_city}**:\n"
    ]
    for i, b in enumerate(branches_in_asked[:5], 1):
        name = b.get("filials_text", "Отделение")
        addr = _format_address(b)
        _, status = _is_open_now(b.get("info_worktime", ""))
        lines.append(f"{i}. **{name}**\n   📍 {addr}\n   🕐 {status}")
        if i < min(5, total):
            lines.append("")

    if total > 5:
        lines.append(f"\n_...и ещё {total-5} отделений. Напишите «все отделения в {asked_city}»._")

    lines.append(f"\n💡 Хотите ближайшие отделения в **{user_city}**? Напишите «ближайшие отделения».")
    return "\n".join(lines)


def detect_user_city(lat: float, lon: float, all_branches: list, db=None) -> Optional[str]:
    """
    Определяет город пользователя по геолокации —
    берёт город ближайшего отделения.
    Использует координаты из БД (FilialLocation).
    """
    # Строим coords_map из БД
    coords_map = {}
    if db is not None:
        try:
            from database import FilialLocation
            for loc in db.query(FilialLocation).all():
                coords_map[str(loc.filial_id)] = (loc.latitude, loc.longitude, loc.city)
        except Exception as e:
            logger.warning(f"detect_user_city: БД недоступна: {e}")

    best = None
    best_dist = float("inf")
    for b in all_branches:
        fid = str(b.get("filial_id", ""))
        if fid in coords_map:
            b_lat, b_lon, _ = coords_map[fid]
        else:
            b_lat = b.get("latitude") or b.get("lat")
            b_lon = b.get("longitude") or b.get("lon")
        if not b_lat or not b_lon:
            continue
        d = _haversine(lat, lon, float(b_lat), float(b_lon))
        if d < best_dist:
            best_dist = d
            best = b
    return best.get("name") if best and best_dist < 100 else None