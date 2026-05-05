
import logging
import json
import easyocr
from services.llm_utils import ask_llm

logger = logging.getLogger(__name__)

_reader = easyocr.Reader(['ru', 'en'], gpu=False)

def ocr_image(image_path: str) -> str:
    """Возвращает распознанный текст из изображения чека."""
    try:
        result = _reader.readtext(image_path, detail=0)
        return "\n".join(result)
    except Exception as e:
        logger.error(f"OCR ошибка {image_path}: {e}")
        return ""

def extract_receipt_data_with_llm(ocr_text: str) -> dict:
    """Извлекает из текста чека структурированные данные через LLM."""
    prompt = f"""
Ты — эксперт по извлечению данных из чеков. 
Из предоставленного распознанного текста чека извлеки следующие данные и верни их СТРОГО в формате JSON без дополнительных пояснений:
{{
  "store": "Название магазина/сервиса или null",
  "date": "Дата покупки в формате ДД.ММ.ГГГГ или null",
  "total_amount": "Общая сумма чека (число с точкой) или null",
  "receipt_category": "продукты | транспорт | здоровье | развлечения | услуги | другое | null",
  "items": [
    {{
      "name": "Название товара/услуги",
      "quantity": число,
      "unit_price": число,
      "total_price": число,
      "category": "строка с предполагаемой категорией товара или null"
    }}
  ]
}}
category - категорию определяй самостоятельно, исходя из названия и контекста. Можешь предлагать любую подходящую категорию.
receipt_category — общая категория всего чека. Определи её по составу товаров/услуг.
Если какой-то параметр не удается определить, установи его в null.
Не добавляй комментарии, просто верни JSON.

Текст чека:
{ocr_text}
"""
    try:
        raw = ask_llm(prompt, temperature=0.0, max_tokens=1500)
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
    except Exception as e:
        logger.error(f"LLM извлечение данных чека не удалось: {e}")
    return {"store": None, "date": None, "total_amount": None, "receipt_category": None, "items": []}