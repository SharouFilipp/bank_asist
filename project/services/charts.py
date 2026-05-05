import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from collections import defaultdict
from typing import List, Dict
from database import Receipt
import logging

logger = logging.getLogger(__name__)

CHART_DIR = "static"


def build_expense_chart(receipts: List[Receipt], user_id: int, chart_type: str = "pie") -> str:
    """Генерирует диаграмму и возвращает путь к файлу."""
    if not receipts:
        # Создаём пустой график
        plt.figure(figsize=(8, 8))
        plt.text(0.5, 0.5, 'Нет данных о расходах', ha='center', va='center', fontsize=16)
        plt.title("Расходы", fontsize=16)
    else:
        # Агрегация по категориям
        category_sums: Dict[str, float] = defaultdict(float)
        total = 0.0
        for r in receipts:
            # Конвертация в BYN (упрощённо, используем курсы из currency.py)
            from services.currency import convert
            amount_byn = convert(r.amount, r.currency, "BYN")
            category_sums[r.category] += amount_byn
            total += amount_byn

        categories = list(category_sums.keys())
        amounts = list(category_sums.values())

        plt.figure(figsize=(8, 8))
        if chart_type == "bar":
            plt.bar(categories, amounts, color='#4B8BFF')
            plt.ylabel("Сумма (BYN)")
        else:  # pie
            plt.pie(amounts, labels=categories, autopct='%1.1f%%', startangle=140)
        plt.title(f"Всего потрачено: {total:.2f} BYN", fontsize=16)

    filename = f"expense_chart_{user_id}.png"
    filepath = os.path.join(CHART_DIR, filename)
    os.makedirs(CHART_DIR, exist_ok=True)
    plt.savefig(filepath)
    plt.close()
    logger.info(f"График сохранён: {filepath}")
    return filepath