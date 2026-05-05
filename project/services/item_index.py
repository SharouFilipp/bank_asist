import logging
from langchain_nomic import NomicEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document

logger = logging.getLogger(__name__)
CHROMA_ITEMS_PATH = "chroma_db_items"
embeddings = NomicEmbeddings(model="nomic-embed-text-v1")

def index_receipt_items(user_id: int, items: list[dict], receipt_date: str = None,
                        store: str = None, receipt_category: str = None):
    """Индексирует позиции чека в векторный store пользователя."""
    if not items:
        return

    docs = []
    for item in items:
        text = f"{item['name']}. Цена: {item['total_price']} BYN, количество: {item['quantity']}"
        if receipt_date:
            text += f", дата: {receipt_date}"
        if item.get('category'):
            text += f", категория: {item['category']}"
        if store:
            text += f", магазин: {store}"
        if receipt_category:
            text += f", общая категория: {receipt_category}"

        metadata = {
            "user_id": user_id,
            "name": item['name'],
            "total_price": item['total_price'],
            "quantity": item['quantity'],
            "date": receipt_date or "",
            "category": item.get('category', ""),
            "store": store or "",
            "receipt_category": receipt_category or ""
        }
        docs.append(Document(page_content=text, metadata=metadata))

    if docs:
        store = Chroma(
            persist_directory=CHROMA_ITEMS_PATH,
            embedding_function=embeddings,
            collection_name=f"user_items_{user_id}"
        )
        store.add_documents(docs)
        store.persist()
        logger.info(f"Проиндексировано {len(docs)} позиций для пользователя {user_id}")