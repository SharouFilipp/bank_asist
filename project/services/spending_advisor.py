# services/spending_advisor.py — полная версия с парсингом периода, анализом магазинов и советами
import logging
import re
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import List, Optional, Tuple, Dict
from database import SessionLocal, Receipt
from services.currency import convert

logger = logging.getLogger(__name__)

from services.loyalty_parser import get_loyalty_info, format_loyalty_block

MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

MONTH_LABELS = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
                "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
MONTH_LABELS_GEN = ["","январе","феврале","марте","апреле","мае","июне",
                    "июле","августе","сентябре","октябре","ноябре","декабре"]
DAYS = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]


def parse_period(query: str) -> Tuple[Optional[date], Optional[date], str]:
    """Парсит период из запроса. Возвращает (date_from, date_to, label)."""
    q = query.lower()
    today = date.today()

    # Конкретная дата ДД.ММ.ГГГГ
    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{4})', q)
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return d, d, d.strftime("%d.%m.%Y")
        except ValueError:
            pass

    # Прошлая неделя
    if "прошл" in q and "недел" in q:
        mon = today - timedelta(days=today.weekday() + 7)
        sun = mon + timedelta(days=6)
        return mon, sun, "прошлую неделю"

    # Эта/текущая неделя
    if ("эт" in q or "текущ" in q) and "недел" in q:
        mon = today - timedelta(days=today.weekday())
        return mon, today, "эту неделю"

    # Вчера
    if "вчера" in q:
        y = today - timedelta(days=1)
        return y, y, "вчера"

    # Сегодня
    if "сегодня" in q:
        return today, today, "сегодня"

    # Последние N дней
    m = re.search(r'последни[ехй]\s+(\d+)\s+дн', q)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), today, f"последние {n} дней"

    # Прошлый месяц
    if "прошл" in q and "месяц" in q:
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev, last_prev, f"{MONTH_LABELS[last_prev.month]} {last_prev.year}"

    # Этот/текущий месяц
    if ("эт" in q or "текущ" in q) and "месяц" in q:
        return today.replace(day=1), today, f"{MONTH_LABELS[today.month]} {today.year}"

    # Конкретный месяц: "январь 2026", "в феврале"
    for prefix, month_num in MONTHS_RU.items():
        if prefix in q:
            year_m = re.search(r'(\d{4})', q)
            year = int(year_m.group(1)) if year_m else today.year
            try:
                first = date(year, month_num, 1)
                last = date(year + 1, 1, 1) - timedelta(days=1) if month_num == 12 \
                       else date(year, month_num + 1, 1) - timedelta(days=1)
                return first, last, f"{MONTH_LABELS[month_num]} {year}"
            except ValueError:
                pass

    # Год: "в 2025 году" / "прошлый год"
    year_m = re.search(r'\b(20\d{2})\b', q)
    if year_m:
        year = int(year_m.group(1))
        return date(year, 1, 1), date(year, 12, 31), f"{year} год"
    if "прошл" in q and "год" in q:
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), f"{y} год"

    return None, None, ""


def get_receipts_for_period(user_id: int, date_from: date, date_to: date) -> List[Receipt]:
    db = SessionLocal()
    try:
        return (db.query(Receipt)
                .filter(Receipt.user_id == user_id,
                        Receipt.receipt_date >= date_from,
                        Receipt.receipt_date <= date_to)
                .order_by(Receipt.receipt_date.desc())
                .all())
    finally:
        db.close()


def summarize_period(user_id: int, query: str) -> Optional[str]:
    """Если запрос содержит период — возвращает детальный отчёт, иначе None."""
    date_from, date_to, label = parse_period(query)
    if not date_from:
        return None

    receipts = get_receipts_for_period(user_id, date_from, date_to)
    if not receipts:
        return f"За {label} чеков не найдено. Сфотографируйте чеки, чтобы видеть статистику."

    total = sum(convert(r.amount, r.currency, "BYN") for r in receipts)

    cats: Dict[str, float] = defaultdict(float)
    stores: Dict[str, float] = defaultdict(float)
    for r in receipts:
        cats[r.category or "другое"] += convert(r.amount, r.currency, "BYN")
        if r.store:
            stores[r.store.strip()] += convert(r.amount, r.currency, "BYN")

    sorted_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
    sorted_stores = sorted(stores.items(), key=lambda x: x[1], reverse=True)

    lines = [f"**Расходы за {label}:**\n",
             f"💳 Всего: **{total:.2f} BYN** ({len(receipts)} чеков)\n",
             "**По категориям:**"]
    for cat, amt in sorted_cats:
        pct = amt / total * 100
        bar = "█" * max(1, int(pct / 10))
        lines.append(f"  {bar} {cat}: {amt:.2f} BYN ({pct:.0f}%)")

    if sorted_stores:
        lines.append("\n**Где тратили:**")
        for store, amt in sorted_stores[:5]:
            lines.append(f"  • {store}: {amt:.2f} BYN")

    return "\n".join(lines)


def _collect_stats(receipts: list, months: int) -> dict:
    """Собирает всю статистику из чеков в один словарь для LLM."""
    total = sum(convert(r.amount, r.currency, "BYN") for r in receipts)

    cats: Dict[str, list] = defaultdict(list)
    stores: Dict[str, list] = defaultdict(list)
    by_weekday: Dict[int, float] = defaultdict(float)
    monthly: Dict[tuple, float] = defaultdict(float)

    for r in receipts:
        amt = convert(r.amount, r.currency, "BYN")
        cats[r.category or "другое"].append(amt)
        if r.store:
            stores[r.store.strip()].append(amt)
        if r.receipt_date:
            by_weekday[r.receipt_date.weekday()] += amt
            monthly[(r.receipt_date.year, r.receipt_date.month)] += amt

    cat_totals = {c: round(sum(v), 2) for c, v in cats.items()}
    store_totals = {s: round(sum(v), 2) for s, v in stores.items()}
    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    sorted_stores = sorted(store_totals.items(), key=lambda x: x[1], reverse=True)

    avg_check = total / len(receipts)
    freq = len(receipts) / months

    # Тренд месяц к месяцу
    sorted_months = sorted(monthly.items())
    trend_pct = None
    monthly_breakdown = []
    for (y, m), amt in sorted_months:
        monthly_breakdown.append(f"{MONTH_LABELS[m]} {y}: {amt:.2f} BYN")
    if len(sorted_months) >= 2:
        prev_amt = sorted_months[-2][1]
        cur_amt = sorted_months[-1][1]
        if prev_amt > 0:
            trend_pct = round((cur_amt - prev_amt) / prev_amt * 100, 1)

    # Пик по дням
    peak_day = DAYS[max(by_weekday, key=by_weekday.get)] if by_weekday else None

    # Программы лояльности для топ-магазинов
    loyalty_data = []
    for store_name, amt in sorted_stores[:5]:
        info = get_loyalty_info(store_name)
        if info:
            loyalty_data.append({
                "store": store_name,
                "spent": amt,
                "card": info.get("card"),
                "cashback": info.get("cashback"),
                "conditions": info.get("conditions"),
                "current_promos": info.get("current_promos") or [],
                "special_offers": info.get("special_offers") or [],
                "app": info.get("app"),
                "app_features": info.get("app_features"),
                "tip": info.get("tip"),
                "source": info.get("source"),
                "fetched_at": info.get("fetched_at"),
            })

    return {
        "total": round(total, 2),
        "avg_per_month": round(total / months, 2),
        "months": months,
        "receipts_count": len(receipts),
        "avg_check": round(avg_check, 2),
        "freq_per_month": round(freq, 1),
        "peak_day": peak_day,
        "trend_pct": trend_pct,
        "categories": sorted_cats[:8],
        "stores": sorted_stores[:5],
        "monthly_breakdown": monthly_breakdown,
        "loyalty": loyalty_data,
    }


def get_saving_advice(user_id: int, months: int = 3) -> str:
    """
    Собирает все данные о расходах и передаёт в LLM.
    LLM сама пишет персональный анализ и советы живым языком.
    """
    db = SessionLocal()
    try:
        cutoff = date.today() - timedelta(days=30 * months)
        receipts = (db.query(Receipt)
                    .filter(Receipt.user_id == user_id, Receipt.receipt_date >= cutoff)
                    .all())
    finally:
        db.close()

    if not receipts:
        return ("Пока нет данных для анализа. Загрузите несколько чеков — "
                "я разберу куда уходят деньги и подскажу как сэкономить.")

    # Собираем всю статистику
    stats = _collect_stats(receipts, months)

    # Форматируем для LLM — структурированный контекст
    cats_text = "\n".join(
        f"  • {cat}: {amt} BYN ({amt/stats['total']*100:.0f}%)"
        for cat, amt in stats["categories"]
    )
    stores_text = "\n".join(
        f"  • {s}: {a} BYN" for s, a in stats["stores"]
    ) if stats["stores"] else "  Магазины не указаны (нет данных)"

    monthly_text = "\n".join(f"  {m}" for m in stats["monthly_breakdown"]) or "  Нет данных"

    trend_text = ""
    if stats["trend_pct"] is not None:
        sign = "+" if stats["trend_pct"] > 0 else ""
        trend_text = f"Тренд последних двух месяцев: {sign}{stats['trend_pct']}%"

    # Формируем блок лояльности с акциями и спецпредложениями
    loyalty_text = ""
    if stats["loyalty"]:
        parts = []
        for l in stats["loyalty"]:
            is_real = l["source"] in ("web_search", "tavily", "cache")
            if is_real:
                src_label = "АКТУАЛЬНЫЕ ДАННЫЕ (получены через веб-поиск — используй смело)"
            else:
                src_label = "БАЗОВЫЕ ДАННЫЕ (общая информация — не выдумывай конкретные акции)"

            block = [
                f"  Магазин: {l['store']} | Потрачено: {l['spent']} BYN",
                f"  Источник: {src_label}",
            ]
            if l.get("card"):
                block.append(f"  Карта: {l['card']}")
            if l.get("cashback"):
                block.append(f"  Кешбэк/скидка: {l['cashback']}")
            if l.get("conditions"):
                block.append(f"  Условия: {l['conditions']}")
            if l.get("current_promos"):
                block.append(f"  Текущие акции: {'; '.join(l['current_promos'][:3])}")
            if l.get("special_offers"):
                block.append(f"  Спецпредложения: {'; '.join(l['special_offers'][:3])}")
            if l.get("tip"):
                block.append(f"  Главный совет: {l['tip']}")
            if l.get("app"):
                block.append(f"  Приложение: {l['app']}")
            if l.get("fetched_at"):
                block.append(f"  Дата данных: {l['fetched_at']}")

            parts.append("\n".join(block))

        loyalty_text = "ДАННЫЕ О МАГАЗИНАХ (лояльность + акции):\n\n" + "\n\n".join(parts)
    else:
        loyalty_text = "ДАННЫЕ О ПРОГРАММАХ ЛОЯЛЬНОСТИ: не найдены. Не упоминай конкретные программы."

    prompt = f"""Ты — персональный финансовый советник. Твоя задача — проанализировать реальные данные о расходах и дать полезные советы.

⚠️ СТРОГИЕ ПРАВИЛА — нарушение недопустимо:
1. Используй ТОЛЬКО цифры и факты из блока «ДАННЫЕ». Не придумывай суммы, проценты, акции или условия которых нет в данных.
2. Если источник лояльности помечен «БАЗОВЫЕ ДАННЫЕ» — упоминай только карту и кешбэк из данных. НЕ выдумывай конкретные акции («2 по 1», «скидка в пятницу», «двойные баллы» и т.п.) — ты их не знаешь.
3. Если источник «ДАННЫЕ С САЙТА» — можно детально рассказать про условия из поля «Официальный совет».
4. Не давай советы сравнивать цены в других магазинах если они не упомянуты в данных.
5. Если данных мало (1–2 чека) — честно скажи что для глубокого анализа нужно больше чеков.

=== ДАННЫЕ ===

Период анализа: {months} мес.
Итого потрачено: {stats['total']} BYN (~{stats['avg_per_month']} BYN/мес)
Количество чеков: {stats['receipts_count']} ({stats['freq_per_month']} покупок/мес)
Средний чек: {stats['avg_check']} BYN
Пик трат по дням недели: {stats['peak_day'] or 'нет данных'}
{trend_text}

Расходы по месяцам:
{monthly_text}

Расходы по категориям:
{cats_text}

Топ-магазины:
{stores_text}

{loyalty_text}

=== ЗАДАЧА ===

Напиши персональный анализ на русском языке. Структура:

**1. Общая картина**
Что реально происходит с финансами на основе данных. Если данных мало — скажи об этом честно.

**2. Где можно сэкономить**
Конкретные советы только на основе того что есть в данных. Считай экономию в BYN.

**3. Программы лояльности**
Только то что есть в данных о лояльности. Если источник базовый — говори об этом осторожно, без выдуманных деталей.

**4. Следующий шаг**
2–3 конкретных действия которые можно сделать прямо сейчас.

Пиши живым языком. Форматирование: **жирный** для важного, эмодзи умеренно. Объём: 200–350 слов."""

    from services.llm_utils import ask_llm

    # Статичная шапка с цифрами — всегда показываем пользователю
    header_lines = [
        f"**📊 Анализ расходов за {months} мес.**\n",
        f"💰 Итого: **{stats['total']} BYN** (~{stats['avg_per_month']} BYN/мес)\n",
        "**По категориям:**",
    ]
    for cat, amt in stats["categories"][:6]:
        pct = amt / stats["total"] * 100
        bar = "█" * max(1, int(pct / 10))
        header_lines.append(f"  {bar} {cat}: {amt} BYN ({pct:.0f}%)")

    if stats["stores"]:
        header_lines.append("\n**Топ-магазины:**")
        for s, a in stats["stores"]:
            # Показываем источник данных лояльности рядом с магазином
            loyalty_source = next(
                (f" ({'✅ данные с сайта' if l['source'] in ('parsed','cache') else '📋 базовые данные'})"
                 for l in stats["loyalty"] if l["store"] == s),
                ""
            )
            header_lines.append(f"  • {s}: {a} BYN{loyalty_source}")

    header = "\n".join(header_lines)

    # LLM-анализ — низкая температура для точности
    llm_analysis = ask_llm(prompt, temperature=0.1, max_tokens=700)

    if llm_analysis and len(llm_analysis) > 100:
        return header + "\n\n---\n\n" + llm_analysis
    else:
        # Fallback если LLM недоступна
        logger.warning("LLM unavailable, using rule-based tips")
        tips = _rule_based_tips(stats)
        return header + "\n\n**✅ Советы по экономии:**\n" + "\n".join(f"  • {t}" for t in tips)


def _rule_based_tips(stats: dict) -> List[str]:
    """Резервные советы если LLM недоступна."""
    tips = []
    total = stats["total"]
    cats = dict(stats["categories"])
    avg_check = stats["avg_check"]
    freq = stats["freq_per_month"]
    months = stats["months"]

    food = sum(v for k, v in cats.items() if any(w in k.lower() for w in ["продукт","еда","food"]))
    cafe = sum(v for k, v in cats.items() if any(w in k.lower() for w in ["кафе","ресторан","общепит"]))

    if food and food / total > 0.45:
        tips.append("40%+ бюджета — еда. Список покупок заранее снижает расходы на 20–25%.")
    if cafe and cafe > 80:
        tips.append(f"Кафе/рестораны: {cafe:.0f} BYN. Готовьте обед дома 3 раза в неделю — экономия ~{cafe*0.4:.0f} BYN.")
    if freq > 20 and avg_check < 15:
        tips.append("Много мелких покупок. Укрупните: 1 большая закупка вместо 5 мелких.")
    if stats.get("trend_pct") and stats["trend_pct"] > 15:
        tips.append(f"Расходы выросли на {stats['trend_pct']}%. Пересмотрите крупнейшую статью трат.")
    if stats["loyalty"]:
        l = stats["loyalty"][0]
        tips.append(f"Оформите карту «{l['card']}» в {l['store']} — {l['cashback']}.")
    if not tips:
        tips.append("Расходы стабильны. Добавляйте больше чеков для точного анализа.")

    return tips[:4]