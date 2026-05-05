"""
services/currency_analyst.py

Технический анализ курсов валют:
- Тренд (линейная регрессия + скользящие средние)
- Волатильность
- Прогноз на N дней
- Кросс-курсовые арбитражные возможности
- Автономный поиск акций через Perplexity
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

logger = logging.getLogger(__name__)

CURRENCY_NBRB_IDS = {
    "USD": 145, "EUR": 292, "RUB": 298,
    "PLN": 293, "CNY": 462, "GBP": 429,
}


# ─── Технический анализ ───────────────────────────────────

def _linear_regression(y: list) -> tuple:
    """Возвращает (slope, intercept, r2)."""
    n = len(y)
    if n < 2:
        return 0, y[0] if y else 0, 0
    x = list(range(n))
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    ss_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    ss_xx = sum((xi - x_mean) ** 2 for xi in x)
    if ss_xx == 0:
        return 0, y_mean, 0
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    y_pred = [slope * xi + intercept for xi in x]
    ss_res = sum((yi - yp) ** 2 for yi, yp in zip(y, y_pred))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return slope, intercept, r2


def _moving_average(values: list, window: int) -> list:
    result = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        result.append(sum(values[start:i+1]) / (i - start + 1))
    return result


def _volatility(values: list) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return round(math.sqrt(variance), 6)


def _rsi(values: list, period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def analyze_dynamics(data: list, cur_name: str, forecast_days: int = 7) -> dict:
    """
    Полный технический анализ данных курса.
    data: [{date, rate}, ...]
    Возвращает dict с метриками и текстовым выводом.
    """
    if not data:
        return {}

    dates = [d["date"] for d in data]
    rates = [float(d["rate"]) for d in data]
    n = len(rates)

    slope, intercept, r2 = _linear_regression(rates)
    ma7  = _moving_average(rates, 7)
    ma20 = _moving_average(rates, 20)
    vol  = _volatility(rates)
    rsi_val = _rsi(rates)

    first, last = rates[0], rates[-1]
    change_abs = round(last - first, 4)
    change_pct = round((last - first) / first * 100, 2) if first else 0

    # Прогноз: продолжаем линейный тренд
    forecast = []
    last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
    for i in range(1, forecast_days + 1):
        pred_rate = slope * (n + i - 1) + intercept
        pred_date = (last_date + timedelta(days=i)).strftime("%Y-%m-%d")
        forecast.append({"date": pred_date, "rate": round(max(pred_rate, 0), 4)})

    # Тренд-сигнал
    if abs(slope) < 0.0001:
        trend = "боковой"
        trend_emoji = "➡️"
    elif slope > 0:
        trend = "восходящий"
        trend_emoji = "📈"
    else:
        trend = "нисходящий"
        trend_emoji = "📉"

    # RSI-сигнал
    rsi_signal = ""
    if rsi_val:
        if rsi_val > 70:
            rsi_signal = "перекуплен — возможна коррекция вниз"
        elif rsi_val < 30:
            rsi_signal = "перепродан — возможен отскок вверх"
        else:
            rsi_signal = "нейтральная зона"

    # Уровни поддержки/сопротивления
    support    = round(min(rates[-20:]) if n >= 20 else min(rates), 4)
    resistance = round(max(rates[-20:]) if n >= 20 else max(rates), 4)

    return {
        "cur": cur_name,
        "dates": dates,
        "rates": rates,
        "ma7": ma7,
        "ma20": ma20,
        "forecast": forecast,
        "slope": slope,
        "r2": r2,
        "trend": trend,
        "trend_emoji": trend_emoji,
        "change_abs": change_abs,
        "change_pct": change_pct,
        "volatility": vol,
        "rsi": rsi_val,
        "rsi_signal": rsi_signal,
        "support": support,
        "resistance": resistance,
        "first": first,
        "last": last,
        "n_days": n,
        "forecast_days": forecast_days,
    }


def build_advanced_chart(analysis: dict, output_path: str, bvfb: dict = None) -> str:
    """
    Строит продвинутый график в стиле крипто-трейдинга:
    - Цена + MA7 + MA20
    - Зоны поддержки/сопротивления
    - Прогноз с доверительным интервалом
    - RSI панель снизу
    """
    cur = analysis["cur"]
    dates_str = analysis["dates"]
    rates = analysis["rates"]
    ma7   = analysis["ma7"]
    ma20  = analysis["ma20"]
    forecast = analysis["forecast"]
    rsi_val  = analysis.get("rsi")

    # Парсим даты
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates_str]
    fc_dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in forecast]
    fc_rates = [d["rate"] for d in forecast]

    fig = plt.figure(figsize=(13, 8), facecolor="#0d1117")

    # Два подграфика: цена + RSI
    if rsi_val:
        ax1 = plt.subplot2grid((4, 1), (0, 0), rowspan=3, fig=fig)
        ax2 = plt.subplot2grid((4, 1), (3, 0), rowspan=1, fig=fig, sharex=ax1)
    else:
        ax1 = fig.add_subplot(111)
        ax2 = None

    ax1.set_facecolor("#0d1117")
    ax1.tick_params(colors="#8b949e")
    ax1.spines[:].set_color("#30363d")

    # Основная линия цены — градиент цвет по тренду
    trend = analysis["trend"]
    price_color = "#3fb950" if trend == "восходящий" else ("#f85149" if trend == "нисходящий" else "#8b949e")

    ax1.fill_between(dates, rates, min(rates), alpha=0.15, color=price_color)
    ax1.plot(dates, rates, color=price_color, linewidth=2, label=f"{cur}/BYN", zorder=3)

    # Скользящие средние
    ax1.plot(dates, ma7,  color="#e3b341", linewidth=1.2, linestyle="--", alpha=0.85, label="MA7")
    ax1.plot(dates, ma20, color="#58a6ff", linewidth=1.2, linestyle="--", alpha=0.85, label="MA20")

    # Уровни поддержки/сопротивления
    ax1.axhline(analysis["support"],    color="#3fb950", linewidth=0.8, linestyle=":", alpha=0.6)
    ax1.axhline(analysis["resistance"], color="#f85149", linewidth=0.8, linestyle=":", alpha=0.6)
    ax1.text(dates[0], analysis["support"] + 0.001,    "Поддержка", color="#3fb950", fontsize=7, alpha=0.8)
    ax1.text(dates[0], analysis["resistance"] + 0.001, "Сопротивление", color="#f85149", fontsize=7, alpha=0.8)

    # Прогноз с доверительным интервалом
    vol = analysis["volatility"]
    ax1.plot(fc_dates, fc_rates, color="#a5d6ff", linewidth=1.5,
             linestyle="--", label=f"Прогноз {analysis['forecast_days']} дн.")
    upper = [r + 2 * vol for r in fc_rates]
    lower = [max(r - 2 * vol, 0) for r in fc_rates]
    ax1.fill_between(fc_dates, lower, upper, alpha=0.12, color="#a5d6ff", label="Доверит. интервал")

    # Вертикальная линия — граница факт/прогноз
    ax1.axvline(dates[-1], color="#8b949e", linewidth=0.8, linestyle=":")

    ax1.set_title(f"Динамика {cur}/BYN  {trend} тренд {analysis['trend_emoji']}",
                  color="#c9d1d9", fontsize=13, pad=10)
    ax1.set_ylabel("Курс BYN", color="#8b949e")
    ax1.legend(loc="upper left", framealpha=0.2, labelcolor="#c9d1d9", fontsize=8)
    ax1.grid(True, color="#21262d", linewidth=0.5)

    # RSI панель
    if ax2 and rsi_val:
        # Рисуем RSI как горизонтальную линию (у нас одно значение)
        ax2.set_facecolor("#0d1117")
        ax2.tick_params(colors="#8b949e")
        ax2.spines[:].set_color("#30363d")
        ax2.axhline(rsi_val, color="#e3b341", linewidth=1.5)
        ax2.axhline(70, color="#f85149", linewidth=0.8, linestyle="--", alpha=0.6)
        ax2.axhline(30, color="#3fb950", linewidth=0.8, linestyle="--", alpha=0.6)
        ax2.fill_between([dates[0], dates[-1]], 70, 100, alpha=0.05, color="#f85149")
        ax2.fill_between([dates[0], dates[-1]], 0, 30, alpha=0.05, color="#3fb950")
        ax2.set_ylim(0, 100)
        ax2.set_ylabel("RSI", color="#8b949e", fontsize=8)
        ax2.text(dates[-1], rsi_val + 2, f"RSI: {rsi_val}", color="#e3b341", fontsize=8)
        ax2.grid(True, color="#21262d", linewidth=0.5)

    # Добавляем биржевую линию БВФБ если есть данные
    if bvfb:
        add_bvfb_to_chart(ax1, bvfb, dates)
        # Обновляем легенду
        ax1.legend(loc="upper left", framealpha=0.2, labelcolor="#c9d1d9", fontsize=8)

    plt.tight_layout(pad=1.5)
    plt.savefig(output_path, dpi=130, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    return output_path


def format_analysis_text(a: dict) -> str:
    """Форматирует текстовый анализ для пользователя."""
    lines = [
        f"**📊 Анализ {a['cur']}/BYN за {a['n_days']} дней**\n",
        f"**Курс:** {a['first']} → {a['last']} BYN",
        f"**Изменение:** {'+' if a['change_abs'] >= 0 else ''}{a['change_abs']} BYN ({'+' if a['change_pct'] >= 0 else ''}{a['change_pct']}%)",
        f"\n**Тренд:** {a['trend_emoji']} {a['trend'].capitalize()}",
        f"**Волатильность:** {a['volatility']:.4f} BYN ({'высокая' if a['volatility'] > 0.01 else 'умеренная' if a['volatility'] > 0.003 else 'низкая'})",
    ]
    if a.get("rsi"):
        lines.append(f"**RSI ({a['n_days']} дн.):** {a['rsi']} — {a['rsi_signal']}")

    lines.append(f"\n**📏 Уровни:**")
    lines.append(f"  Поддержка: {a['support']} BYN")
    lines.append(f"  Сопротивление: {a['resistance']} BYN")

    # Прогноз
    fc = a["forecast"]
    if fc:
        lines.append(f"\n**🔮 Прогноз на {a['forecast_days']} дней** (линейный тренд):")
        lines.append(f"  Через 3 дня: ~{fc[2]['rate'] if len(fc) > 2 else fc[-1]['rate']} BYN")
        lines.append(f"  Через {a['forecast_days']} дней: ~{fc[-1]['rate']} BYN")
        direction = "⬆️ рост" if fc[-1]["rate"] > a["last"] else "⬇️ снижение" if fc[-1]["rate"] < a["last"] else "➡️ стабильность"
        lines.append(f"  Ожидаемое направление: {direction}")
        lines.append(f"  _⚠️ Прогноз носит информационный характер_")

    return "\n".join(lines)


# ─── Кросс-курсовые возможности ───────────────────────────

def find_arbitrage_opportunities(nbrb_rates: dict, bank_rates: dict, city: str = "") -> str:
    """
    Ищет возможности заработка на кросс-курсах:
    - Сравнивает НБРБ vs банковские курсы
    - Находит наиболее выгодные пары для конвертации
    - Объясняет стратегию простым языком
    """
    lines = ["**💡 Возможности на кросс-курсах валют:**\n"]

    PAIRS = [
        ("USD", "EUR"), ("USD", "RUB"),
        ("EUR", "RUB"), ("USD", "PLN"),
        ("EUR", "PLN"),
    ]

    opportunities = []
    for cur_a, cur_b in PAIRS:
        nbrb_a = float(nbrb_rates.get(cur_a, 0))
        nbrb_b = float(nbrb_rates.get(cur_b, 0))
        if not nbrb_a or not nbrb_b:
            continue

        # Прямой кросс по НБРБ
        nbrb_cross = round(nbrb_a / nbrb_b, 6)

        # Банковский кросс
        bank_a_buy  = float(bank_rates.get(f"{cur_a}_in", 0))
        bank_a_sell = float(bank_rates.get(f"{cur_a}_out", 0))
        bank_b_buy  = float(bank_rates.get(f"{cur_b}_in", 0))
        bank_b_sell = float(bank_rates.get(f"{cur_b}_out", 0))

        if not (bank_a_buy and bank_b_sell):
            continue

        # Стратегия: купить cur_a за BYN, продать за cur_b, продать cur_b за BYN
        # Заработок = bank_b_buy * (bank_a_buy/bank_b_sell) - bank_a_sell
        # Упрощённо — спред НБРБ vs банк
        bank_cross = round(bank_a_buy / bank_b_sell, 6) if bank_b_sell else 0
        if not bank_cross:
            continue

        diff_pct = round((bank_cross - nbrb_cross) / nbrb_cross * 100, 3)
        opportunities.append({
            "pair": f"{cur_a}/{cur_b}",
            "nbrb_cross": nbrb_cross,
            "bank_cross": bank_cross,
            "diff_pct": diff_pct,
            "cur_a": cur_a, "cur_b": cur_b,
            "bank_a_buy": bank_a_buy, "bank_b_sell": bank_b_sell,
        })

    # Сортируем по абсолютному значению спреда
    opportunities.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

    if not opportunities:
        return "Нет данных для анализа кросс-курсов."

    # Показываем топ-3
    for opp in opportunities[:3]:
        sign = "+" if opp["diff_pct"] >= 0 else ""
        lines.append(f"**{opp['pair']}** (через BYN)")
        lines.append(f"  НБРБ кросс: 1 {opp['cur_a']} = {opp['nbrb_cross']} {opp['cur_b']}")
        lines.append(f"  Банк кросс:  1 {opp['cur_a']} = {opp['bank_cross']} {opp['cur_b']}")
        lines.append(f"  Отклонение: {sign}{opp['diff_pct']}%")

        # Объяснение стратегии
        if abs(opp["diff_pct"]) > 0.5:
            lines.append(f"  💡 **Стратегия:** Купить {opp['cur_a']} в банке за "
                         f"{opp['bank_a_buy']} BYN, обменять на {opp['cur_b']} "
                         f"(выгоднее НБРБ на {abs(opp['diff_pct'])}%)")
        lines.append("")

    lines.append("_⚠️ Учтите комиссии и спреды банка. Разница < 1% обычно нивелируется комиссией._")
    return "\n".join(lines)


# ─── Автономный поиск акций через Perplexity ──────────────

def search_store_promotions_autonomous(store_names: list = None) -> str:
    """
    Ищет актуальные акции и скидки в белорусских магазинах
    через Perplexity/sonar — полностью автономно, без фиксированных URL.
    Perplexity сам находит актуальные страницы акций.
    """
    import os
    OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
    if not OPENROUTER_KEY:
        return None

    stores_str = ", ".join(store_names) if store_names else \
        "Евроопт, Гиппо, Корона, ProStore, Белмаркет, Соседи"

    from datetime import date
    today = date.today().strftime("%d.%m.%Y")

    prompt = f"""Найди АКТУАЛЬНЫЕ акции и скидки в белорусских магазинах: {stores_str}.
Дата сегодня: {today}. Ищи только текущие акции, не просроченные.

Для каждого магазина найди:
1. Название и суть акции (конкретно: -30% на молочку, 2+1 на кофе и т.п.)
2. Срок действия
3. URL страницы акций (если нашёл)

Верни ТОЛЬКО JSON без лишнего текста:
{{
  "fetched_at": "{today}",
  "stores": [
    {{
      "name": "название магазина",
      "promos": [
        {{
          "title": "краткое название акции",
          "description": "детали: что, сколько, условия",
          "valid_until": "дата окончания или null",
          "url": "ссылка или null"
        }}
      ]
    }}
  ]
}}

Используй только реальные данные с официальных сайтов магазинов и агрегаторов акций Беларуси.
Если акций не найдено для магазина — не включай его в список."""

    try:
        import openai
        client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
            default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "Banking RAG"},
        )
        resp = client.chat.completions.create(
            model="perplexity/sonar",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content.strip()
        import json
        s, e = raw.find('{'), raw.rfind('}') + 1
        if s != -1 and e > s:
            return json.loads(raw[s:e])
    except Exception as ex:
        logger.warning(f"Autonomous promo search failed: {ex}")
    return None


def format_promos(promo_data: dict) -> str:
    """Форматирует данные об акциях для пользователя."""
    if not promo_data or not promo_data.get("stores"):
        return "Актуальных акций не найдено."

    lines = [f"**🛍️ Актуальные акции ({promo_data.get('fetched_at', '')}):**\n"]
    for store in promo_data["stores"]:
        name = store.get("name", "")
        promos = store.get("promos", [])
        if not promos:
            continue
        lines.append(f"🏪 **{name}**")
        for p in promos[:3]:
            lines.append(f"  • {p.get('title', '')}: {p.get('description', '')}")
            if p.get("valid_until"):
                lines.append(f"    📅 До: {p['valid_until']}")
        lines.append("")
    return "\n".join(lines)


# ─── Интеграция с БВФБ ────────────────────────────────────

def get_latest_bvfb_rate(cur: str) -> Optional[dict]:
    """
    Читает последние биржевые данные из trading_cache.json.
    Возвращает dict с полями: rate, change, percent, start, last, min, max, deals, volume, date
    """
    import json, os
    cache_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trading_cache.json")
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, encoding='utf-8') as f:
            cache = json.load(f)
        data = cache.get("data", {})
        entry = data.get(cur.upper())
        if not entry:
            return None

        # Нормализуем rate — убираем пробелы, берём float
        rate_str = str(entry.get("rate", "")).replace(",", ".").replace(" ", "")
        try:
            rate_val = float(rate_str)
        except ValueError:
            rate_val = None

        # Определяем множитель (RUB и CNY торгуются за 100/10 единиц)
        symbol = entry.get("symbol", cur)
        multiplier = 1
        if "100" in symbol:
            multiplier = 100
        elif "10" in symbol:
            multiplier = 10

        return {
            "cur": cur.upper(),
            "rate": rate_val,
            "rate_per_unit": round(rate_val / multiplier, 6) if rate_val else None,
            "multiplier": multiplier,
            "change": entry.get("change", ""),
            "percent": entry.get("percent", ""),
            "start": entry.get("start", ""),
            "last": entry.get("last", ""),
            "min": entry.get("min", ""),
            "max": entry.get("max", ""),
            "deals": entry.get("deals", ""),
            "volume": entry.get("volume", ""),
            "date": entry.get("date", ""),
            "timestamp": cache.get("timestamp", ""),
        }
    except Exception as e:
        logger.warning(f"БВФБ данные недоступны: {e}")
        return None


def format_bvfb_block(bvfb: dict) -> str:
    """Форматирует блок с биржевыми данными БВФБ."""
    if not bvfb:
        return ""

    cur = bvfb["cur"]
    mult = bvfb["multiplier"]
    unit_label = f"{mult} {cur}" if mult > 1 else cur

    change_emoji = "📈" if "+" in str(bvfb.get("change", "")) else "📉" if "-" in str(bvfb.get("change", "")) else "➡️"

    lines = [f"\n**🏛️ Биржа БВФБ — торги {unit_label}** ({bvfb.get('date', '')})"]
    lines.append(f"  Курс сессии: **{bvfb['rate']} BYN** {change_emoji} {bvfb.get('change','')} ({bvfb.get('percent','')})")
    lines.append(f"  Диапазон: {bvfb.get('min','')} – {bvfb.get('max','')} BYN")
    lines.append(f"  Стартовый: {bvfb.get('start','')} | Последняя сделка: {bvfb.get('last','')} BYN")
    lines.append(f"  Сделок: {bvfb.get('deals','')} | Объём: {bvfb.get('volume','')} BYN")

    return "\n".join(lines)


def add_bvfb_to_chart(ax, bvfb: dict, dates: list):
    """
    Добавляет на график горизонтальную линию биржевого курса БВФБ
    и аннотацию с деталями торгов.
    """
    if not bvfb or not bvfb.get("rate"):
        return

    rate = bvfb["rate"]
    mult = bvfb["multiplier"]
    # Если торгуется за 100/10 единиц — нормализуем к 1 единице для сравнения с НБРБ
    rate_norm = rate / mult if mult > 1 else rate

    ax.axhline(rate_norm, color="#f0a030", linewidth=1.2, linestyle="-.", alpha=0.9)
    ax.text(
        dates[-1], rate_norm,
        f"  БВФБ {rate_norm:.4f}",
        color="#f0a030", fontsize=8, va="bottom", alpha=0.95
    )

    # Диапазон торгов
    try:
        mn = float(str(bvfb.get("min","0")).replace(",",".")) / mult
        mx = float(str(bvfb.get("max","0")).replace(",",".")) / mult
        if mn > 0 and mx > 0 and mn != mx:
            ax.fill_between(
                [dates[0], dates[-1]], mn, mx,
                alpha=0.06, color="#f0a030",
                label=f"БВФБ диапазон ({bvfb.get('min')}–{bvfb.get('max')})"
            )
    except Exception:
        pass