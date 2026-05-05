import requests
import json
import os
import logging
from datetime import datetime
from index_data_new import load_documents, split_documents, create_vector_store
import rag_crew

logger = logging.getLogger(__name__)

KURS_URL = "https://belarusbank.by/api/kursExchange"
DATA_FILE = "data/kursExchange.json"


def fetch_and_save_kurs_exchange():
    """Загружает JSON с курсами и филиалами из API Беларусбанка, сохраняет в файл."""
    try:
        resp = requests.get(KURS_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Курсы обновлены и сохранены в {DATA_FILE} ({len(data)} записей)")
        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении курсов: {e}")
        return False

import shutil


def rebuild_chroma():
    """Полностью перестраивает векторное хранилище Chroma из свежих данных."""
    # 1. Закрываем старый клиент, если он был
    old_store = rag_crew.get_retriever()
    if old_store is not None:
        try:
            old_store._client.close()      # Закрываем внутренний клиент chromadb
        except Exception as e:
            logger.warning(f"Не удалось закрыть старый Chroma: {e}")

    # 2. Удаляем старую папку, чтобы не было конфликтов
    if os.path.exists(rag_crew.CHROMA_PATH):
        shutil.rmtree(rag_crew.CHROMA_PATH)

    # 3. Загружаем новые документы и создаём хранилище
    docs = load_documents()
    if not docs:
        logger.warning("Нет документов для перестройки Chroma")
        return False
    chunks = split_documents(docs)
    new_store = create_vector_store(chunks)

    # 4. Передаём новый retriever в rag_crew
    rag_crew.set_retriever(new_store)
    logger.info("Chroma перестроена и retriever обновлён")
    return True