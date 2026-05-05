import os
import json
import logging
import shutil
from datetime import datetime
from collections import defaultdict
from typing import List, Dict

import pytesseract
from PIL import Image, ImageEnhance, ImageOps
import re

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_nomic.embeddings import NomicEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

from database import SessionLocal, Receipt, User
from services.expenses import create_receipt

pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

DATA_PATH = "data/"
CHROMA_PATH = "chroma_db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def format_worktime(worktime_str: str) -> str:
    """Красиво форматирует часы работы."""
    if not worktime_str or not worktime_str.strip():
        return "Не указано"

    days_map = {"Пн": "Пн", "Вт": "Вт", "Ср": "Ср", "Чт": "Чт", "Пт": "Пт", "Сб": "Сб", "Вс": "Вс"}
    parts = [p.strip() for p in worktime_str.split("|") if p.strip()]
    formatted = []

    for part in parts:
        if len(part) < 2:
            continue
        day_code = part[:2]
        day_name = days_map.get(day_code, day_code)

        times = re.findall(r"\d{2} \d{2}", part)
        if len(times) >= 2:
            start = times[0].replace(" ", ":")
            end = times[1].replace(" ", ":")
            formatted.append(f"{day_name}: {start}–{end}")
        elif times:
            formatted.append(f"{day_name}: {times[0].replace(' ', ':')}")
        else:
            formatted.append(f"{day_name}: закрыто")

    return " | ".join(formatted) if formatted else "Не указано"


def format_filial_info(item: Dict) -> str:
    """Форматирует информацию о филиале БЕЗ курсов."""
    if not isinstance(item, dict):
        return ""

    lines = []
    info_mapping = [
        ("filials_text", "Название"),
        ("name_type", "Тип"),
        ("name", "Город"),
        ("street_type", "Тип улицы"),
        ("street", "Улица"),
        ("home_number", "Дом"),
        ("info_worktime", "Часы работы"),
        ("filial_id", "ID филиала"),
        ("sap_id", "SAP ID"),
    ]

    for key, label in info_mapping:
        value = item.get(key)
        if value and str(value).strip():
            if key == "info_worktime":
                value = format_worktime(value)
            lines.append(f"{label}: {value}")

    return "\n".join(lines)


def build_filial_with_rates(item: Dict) -> str:
    """Собирает полную карточку филиала: адрес + его курсы."""
    base = format_filial_info(item)

    # Собираем курсы (только ненулевые)
    rate_lines = []
    for key, value in item.items():
        if key.endswith(('_in', '_out')) and value and value != "0.0000":
            rate_lines.append(f"{key}: {value}")

    if rate_lines:
        base += "\n\nКурсы валют в отделении:\n" + "\n".join(rate_lines)

    return base


def extract_text_from_image(file_path: str) -> dict:
    """Извлекает текст из чека и парсит сумму, категорию, дату."""
    try:
        image = Image.open(file_path)
        image = ImageOps.exif_transpose(image)
        image = image.convert('L')
        image = ImageEnhance.Contrast(image).enhance(2)
        image = ImageEnhance.Sharpness(image).enhance(2)

        text = pytesseract.image_to_string(image, lang='rus+eng', config='--psm 6')
        logging.debug(f"extract_text_from_image: Текст: {text[:100]}...")

        expense_data = {
            "amount": None,
            "category": None,
            "date": None,
            "source": "expense_image",
            "type": "expense_image"
        }

        # === Сумма ===
        amount_patterns = [
            r'(?:сумма|итого|total|чек|оплата|к оплате)[:\s]*([\d.,]+)\s*(?:BYN|руб)?',
            r'([\d.,]+)\s*(?:BYN|USD|EUR|руб)',
            r'итого\s*([\d.,]+)'
        ]
        for pattern in amount_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace(',', '.')
                try:
                    expense_data["amount"] = float(amount_str)
                    break
                except ValueError:
                    continue

        # === Дата ===
        date_match = re.search(r'\b(\d{2}[.-/]\d{2}[.-/]\d{4})\b', text)
        if date_match:
            expense_data["date"] = date_match.group(1)

        # === Категория ===
        categories = {
            "продукты": ["продукты", "супермаркет", "еда", "grocery", "слойка", "магазин", "евроопт"],
            "транспорт": ["такси", "бензин", "проезд", "transport", "yandex.go", "заправка", "автобус"],
            "услуги": ["коммунальные", "интернет", "телефон", "services", "связь", "жкх"],
            "развлечения": ["кино", "ресторан", "кафе", "кофе", "билет", "бар", "ticketpro"],
            "здоровье": ["аптека", "врач", "медицина", "больница", "таблетки"]
        }

        text_lower = text.lower()
        for cat, keywords in categories.items():
            if any(keyword in text_lower for keyword in keywords):
                expense_data["category"] = cat
                break
        else:
            expense_data["category"] = "другое"

        return expense_data

    except Exception as e:
        logging.error(f"extract_text_from_image: Ошибка {file_path}: {e}")
        return {
            "amount": None, "category": None, "date": None,
            "source": "expense_image", "type": "expense_image"
        }


def load_documents() -> List[Document]:
    """
    Загружает все документы из DATA_PATH.
    - JSON: каждый филиал становится документом, содержащим и адрес, и его курсы.
    - PDF: индексируются как документы.
    - Изображения чеков: данные извлекаются и сохраняются в БД с user_id=0 (системный).
    """
    documents = []
    filial_items = []

    if not os.path.exists(DATA_PATH):
        logger.error(f"Директория {DATA_PATH} не существует!")
        return documents

        # Удаляем старые JSON, чтобы не дублировались
    for fn in os.listdir(DATA_PATH):
        if fn.endswith('.json') and fn != 'kursExchange.json':
            os.remove(os.path.join(DATA_PATH, fn))

    # --- Обработка JSON (филиалы) ---
    for filename in os.listdir(DATA_PATH):
        filepath = os.path.join(DATA_PATH, filename)
        if not filename.endswith(".json"):
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
                if not isinstance(json_data, list):
                    json_data = [json_data]

                for item in json_data:
                    # Убираем только курсы, если они будут использоваться отдельно (пока оставляем всё)
                    filial_items.append(item)
        except Exception as e:
            logger.error(f"Ошибка JSON {filename}: {e}")

    # --- Филиалы как единые документы (адрес + курсы) ---
    seen_ids = set()
    for item in filial_items:
        filial_id = item.get("filial_id")
        if not filial_id or filial_id in seen_ids:
            continue
        seen_ids.add(filial_id)

        text = build_filial_with_rates(item)
        if text.strip():
            documents.append(Document(
                page_content=text,
                metadata={
                    "source": "belarusbank_api",
                    "type": "filial_info",
                    "filial_id": filial_id,
                    "city": item.get("name"),
                    "street": item.get("street"),
                    "date": datetime.now().strftime("%Y-%m-%d")
                }
            ))

    # --- PDF и изображения ---
    db = SessionLocal()
    try:
        for filename in os.listdir(DATA_PATH):
            filepath = os.path.join(DATA_PATH, filename)

            if filename.endswith(".json"):
                continue

            if filename.endswith(".pdf"):
                loader = PyPDFLoader(filepath)
                docs = loader.load()
                for doc in docs:
                    doc.metadata.update({
                        "source": "docs", "file_type": "pdf",
                        "filename": filename, "loaded_at": datetime.now().isoformat()
                    })
                documents.extend(docs)

            elif filename.lower().endswith((".jpg", ".jpeg", ".png")):
                expense_data = extract_text_from_image(filepath)
                if expense_data["amount"] is not None:
                    create_receipt(
                        db=db,
                        user_id=0,
                        amount=expense_data["amount"],
                        currency=expense_data.get("currency", "BYN"),
                        category=expense_data.get("category", "другое"),
                        receipt_date=expense_data.get("date"),
                        image_path=filepath
                    )
    finally:
        db.close()

    logger.info(f"Загружено документов: {len(documents)}")
    return documents


def split_documents(documents: List[Document]) -> List[Document]:
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    return text_splitter.split_documents(documents)


def create_vector_store(texts: List[Document]):
    embeddings = NomicEmbeddings(model="nomic-embed-text-v1")
    if os.path.exists(CHROMA_PATH):
        shutil.rmtree(CHROMA_PATH)
    vector_store = Chroma.from_documents(
        documents=texts,
        embedding=embeddings,
        persist_directory=CHROMA_PATH
    )
    return vector_store


if __name__ == "__main__":
    docs = load_documents()
    if docs:
        chunks = split_documents(docs)
        create_vector_store(chunks)