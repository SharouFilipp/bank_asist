from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from database import Receipt, SessionLocal
from typing import List, Optional, Dict
import datetime
from services.currency import convert   # <-- используем конвертацию валют



def get_category_spending(user_id: int, category: str) -> Dict:
    """Возвращает статистику расходов по заданной категории (нечёткое совпадение)."""
    # Синонимы категорий — нормализуем разные варианты к одному
    CATEGORY_SYNONYMS = {
        "продукты": ["продукты", "еда", "food", "groceries", "питание", "продовольствие"],
        "транспорт": ["транспорт", "transport", "такси", "taxi", "проезд"],
        "кафе": ["кафе", "ресторан", "cafe", "restaurant", "общепит", "столовая"],
        "здоровье": ["здоровье", "аптека", "медицина", "health", "pharmacy"],
        "одежда": ["одежда", "одежды", "clothing", "clothes"],
        "услуги": ["услуги", "service", "services"],
        "развлечения": ["развлечения", "entertainment", "досуг"],
        "другое": ["другое", "other", "прочее"],
    }
    cat_lower = category.lower().strip()
    # Находим каноническую группу
    matched_group = None
    for canonical, synonyms in CATEGORY_SYNONYMS.items():
        if cat_lower in synonyms or cat_lower == canonical:
            matched_group = synonyms
            break
    # Если нет в словаре — ищем по точному совпадению (без регистра)
    if not matched_group:
        matched_group = [cat_lower]

    db = SessionLocal()
    try:
        all_user_receipts = db.query(Receipt).filter(Receipt.user_id == user_id).all()
        receipts = [
            r for r in all_user_receipts
            if r.category and r.category.lower().strip() in matched_group
        ]
        total = sum(convert(r.amount, r.currency, "BYN") for r in receipts)
        dates = [r.receipt_date for r in receipts if r.receipt_date]
        period = f"{min(dates)} – {max(dates)}" if dates else "неизвестен"
        return {
            "total": round(total, 2),
            "count": len(receipts),
            "period": period,
            "receipts": receipts,  # возвращаем объекты для дальнейшего анализа
        }
    finally:
        db.close()

def create_receipt(db: Session, user_id: int, amount: float, currency: str,
                   category: str, receipt_date: str, image_path: Optional[str] = None,
                   comment: Optional[str] = None) -> Receipt:
    if receipt_date:
        try:
            parsed_date = datetime.datetime.strptime(receipt_date, "%d.%m.%Y").date()
        except ValueError:
            parsed_date = datetime.date.today()
    else:
        parsed_date = datetime.date.today()

    receipt = Receipt(
        user_id=user_id,
        amount=amount,
        currency=currency,
        category=category,
        receipt_date=parsed_date,
        image_path=image_path,
        comment=comment
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


def get_receipts_by_user(db: Session, user_id: int) -> List[Receipt]:
    return db.query(Receipt).filter(Receipt.user_id == user_id).order_by(Receipt.receipt_date.desc()).all()


def update_receipt(db: Session, receipt_id: int, user_id: int, **kwargs) -> Optional[Receipt]:
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id, Receipt.user_id == user_id).first()
    if not receipt:
        return None
    for key, value in kwargs.items():
        if hasattr(receipt, key):
            setattr(receipt, key, value)
    db.commit()
    db.refresh(receipt)
    return receipt


def delete_receipt(db: Session, receipt_id: int, user_id: int) -> bool:
    receipt = db.query(Receipt).filter(Receipt.id == receipt_id, Receipt.user_id == user_id).first()
    if receipt:
        db.delete(receipt)
        db.commit()
        return True
    return False

from collections import defaultdict

def get_monthly_expenses_converted(db: Session, user_id: int, target_currency: str = "BYN"):
    """
    Возвращает помесячную статистику с конвертацией всех сумм в целевую валюту.
    Возвращает список словарей с полями:
        year, month, total, count,
        categories: dict {категория: сумма_в_BYN}
    """
    receipts = db.query(Receipt).filter(Receipt.user_id == user_id).all()
    monthly = {}  # ключ: (year, month)

    for r in receipts:
        converted_amount = convert(r.amount, r.currency, target_currency)
        key = (r.receipt_date.year, r.receipt_date.month)
        if key not in monthly:
            monthly[key] = {'total': 0.0, 'count': 0, 'categories': defaultdict(float)}
        monthly[key]['count'] += 1
        monthly[key]['total'] += converted_amount
        monthly[key]['categories'][r.category] += converted_amount

    # Сортируем по году, месяцу и преобразуем defaultdict в обычный dict
    result = []
    for (year, month), data in sorted(monthly.items()):
        result.append({
            "year": year,
            "month": month,
            "total": round(data['total'], 2),
            "count": data['count'],
            "categories": {k: round(v, 2) for k, v in data['categories'].items()}
        })
    return result