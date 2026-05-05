import os
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process
from crewai.tools import BaseTool
from langchain_nomic.embeddings import NomicEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_community.llms import Ollama
from typing import Any, Dict, List
import logging
from langchain.schema import Document

# ========================================
# 0. Логирование + .env
# ========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
CHROMA_PATH = os.getenv("CHROMA_DB_PATH", "chroma_db")
logger.info(f"CHROMA_PATH: {CHROMA_PATH}")

# # ========================================
# # 1. LLM
# # ========================================
# logger.info("Инициализация Ollama...")
# try:
#     llm = Ollama(
#         model="ollama/mistral:7b",
#         base_url="http://localhost:11434",
#         temperature=0.3,
#         num_predict=1500,
#         system="""Ты — русскоязычный банковский ассистент.
#         Отвечай ТОЛЬКО на русском, вежливо.
#         Используй только данные из поиска.
#         Заканчивай: 'Обращайтесь, если у вас появятся другие вопросы!'"""
#     )
# except Exception as e:
#     logger.error(f"Ошибка Ollama: {e}")
#     raise
# Было (удалить!)
# from langchain_openai import ChatOpenAI

# ========================================
# 1. LLM
# ========================================

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-3.5-turbo")

if OPENROUTER_API_KEY:
    # Передаём строку с префиксом провайдера – CrewAI сам создаст нужный LiteLLM‑объект
    llm = f"openrouter/{OPENROUTER_MODEL}"
    logger.info(f"Будет использоваться OpenRouter: {llm}")
else:
    # Локальный Ollama
    try:
        from langchain_community.llms import Ollama
    except ImportError:
        from langchain_ollama import OllamaLLM as Ollama

    llm = Ollama(
        model="ollama/mistral:7b",
        base_url="http://localhost:11434",
        temperature=0.3,
        num_predict=1500,
    )
    logger.info("Используется локальный Ollama")


# ========================================
# 2. Retriever
# ========================================
try:
    embeddings = NomicEmbeddings(model="nomic-embed-text-v1")
    # vector_store_retriever = Chroma(
    #     persist_directory=CHROMA_PATH,
    #     embedding_function=embeddings
    # )

    vector_store_retriever = None  # Будет установлено позже

    def set_retriever(store):
        """Устанавливает глобальный retriever для поискового инструмента."""
        global vector_store_retriever
        vector_store_retriever = store

    def get_retriever():
        """Возвращает текущий retriever (для внутреннего использования)."""
        return vector_store_retriever
except Exception as e:
    logger.error(f"Ошибка Chroma: {e}")
    raise

def update_retriever(new_store):
    global vector_store_retriever
    vector_store_retriever = new_store 
    
# ========================================
# 3. Document Search Tool — БЕЗ Pydantic!
# ========================================
class DocumentSearchTool(BaseTool):
    name: str = "Document Search Tool"
    description: str = (
        "Ищет в базе знаний по запросу и фильтру.\n"
        "Аргументы:\n"
        "- query: строка с запросом\n"
        "- metadata: словарь с фильтром (например, {'source': 'belarusbank_api', 'type': 'filial_info'})\n"
        "Пример: query='филиал в Минске', metadata={'source': 'belarusbank_api', 'type': 'filial_info'}"
    )

    def _run(self, query: str, metadata: Dict[str, Any] = None) -> str:
        # Проверка, что retriever уже загружен
        
        metadata = metadata or {}

        # === АВТОДОБАВЛЕНИЕ ГОРОДА ===
        cities = {
            "минск": "Минск",
            "гродно": "Гродно",
            "брест": "Брест",
            "гомель": "Гомель",
            "витебск": "Витебск",
            "могилёв": "Могилёв",
            "великий камень": "Китайско-Белорусский индустриальный парк «Великий камень»"
        }

        query_lower = query.lower()
        for short, full in cities.items():
            if short in query_lower:
                metadata["city"] = full
                break

        # === УНИВЕРСАЛЬНОЕ ФОРМИРОВАНИЕ ФИЛЬТРА ===
        if not metadata:
            filter_clause = None  # Без фильтра
        elif len(metadata) == 1:
            # Один фильтр — просто словарь
            filter_clause = dict(metadata)
        else:
            # Несколько — оборачиваем в $and
            filter_clause = {"$and": [{k: v} for k, v in metadata.items() if v is not None]}

        if not query.strip():
            return "Ошибка: пустой запрос."

        try:
                        # Определяем k по типу документа
            if metadata.get("type") == "filial_info" or metadata.get("type") == "currency_rates":
                k = 30
            elif metadata.get("type") == "docs":
                k = 5
            else:  # aggregated_currency_rates или неизвестный — ставим небольшое значение
                k = 5

        
            # Формируем retriever с фильтром (если есть)
            search_kwargs = {"k": k,"fetch_k": 30,     # сколько рассмотреть кандидатов
                "lambda_mult": 0.5 # 0.0 = максимум разнообразия, 1.0 = максимум релевантности
            }
            if filter_clause is not None:
                search_kwargs["filter"] = filter_clause
            retriever = vector_store_retriever.as_retriever(search_type="mmr",search_kwargs=search_kwargs)
            docs = retriever.invoke(query)
            if not docs:
                return "Информация не найдена."

            # === ОПРЕДЕЛЯЕМ ТИП ДОКУМЕНТОВ ===
            doc_type = docs[0].metadata.get("type", "unknown")

            # === 1. СРЕДНИЕ КУРСЫ ВАЛЮТ ===
            if doc_type == "aggregated_currency_rates":
                return self._format_currency_rates(docs[0])

            # === 2. ФИЛИАЛЫ ===
            elif doc_type == "filial_info":
                return self._format_filials(docs)

            # === 3. ОБЫЧНЫЕ ДОКУМЕНТЫ (FAQ, новости, инструкции) ===
            else:
                return self._format_generic_docs(docs)

        except Exception as e:
            return f"Ошибка: {str(e)}"

    # === ФОРМАТИРОВАНИЕ КУРСОВ ===
    def _format_currency_rates(self, doc: Document) -> str:
        content = doc.page_content.strip()
        update_time = doc.metadata.get("update_time", "—")
        total_filials = doc.metadata.get("total_filials", "—")
        date = doc.metadata.get("date", "—")

        meta = f"Обновлено: {update_time} | Филиалов: {total_filials} | Дата: {date}"
        return f"**{content.split(chr(10))[0]}**\n\n{content}\n\n_{meta}_"

    # === ФОРМАТИРОВАНИЕ ФИЛИАЛОВ ===
    def _format_filials(self, docs: List[Document]) -> str:
        seen_ids = set()
        unique_docs = []
        for doc in docs:
            fid = doc.metadata.get("filial_id")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                unique_docs.append(doc)

        # === Формируем ответ ===
        result = []
        for doc in unique_docs:
            content = doc.page_content.strip()
            # Исключаем source и пустые значения
            meta_lines = [
                f"{k}: {v}" for k, v in doc.metadata.items()
                if v and k != "source"
            ]
            meta = "\n".join(meta_lines) if meta_lines else "—"
            result.append(f"{content}\n\n--- Метаданные ---\n{meta}")

        return "\n\n".join(result)

    # === ФОРМАТИРОВАНИЕ ОБЫЧНЫХ ДОКУМЕНТОВ ===
    def _format_generic_docs(self, docs: List[Document]) -> str:
        result = []
        seen_titles = set()

        for doc in docs:
            title = doc.page_content.strip().split("\n")[0]
            if title in seen_titles:
                continue
            seen_titles.add(title)

            content = doc.page_content.strip()
            source = doc.metadata.get("source", "—")
            doc_type = doc.metadata.get("type", "—")
            date = doc.metadata.get("date", "—")

            meta = f"Источник: {source} | Тип: {doc_type}"
            if date != "—":
                meta += f" | Дата: {date}"

            result.append(f"**{title}**\n{content}\n\n_{meta}_")

        return "\n\n".join(result) if result else "Документы не найдены."
document_search_tool = DocumentSearchTool()


# ========================================
# 4. Агенты
# ========================================
researcher_agent = Agent(
    role="Поиск в базе знаний",
    goal="Найти данные по запросу.",
    backstory=(
        "Ты — поисковый модуль. "
        "Вызывай Document Search Tool с аргументами:\n"
        "- query: текст запроса\n"
        "- metadata: словарь с фильтром (например, {'source': 'belarusbank_api', 'type': 'filial_info'})\n"
        "Пример: query='филиал в Бресте', metadata={'source': 'belarusbank_api', 'type': 'filial_info'}\n"
        # "Если не уверен — используй metadata={}\n"
        "НИКОГДА не пропускай metadata!"
    ),
    verbose=True,
    allow_delegation=False,
    tools=[document_search_tool],
    llm=llm
)
advisor_agent = Agent(
    role="Финансовый консультант",
    goal="Дать ответ на русском.",
    backstory=(
        "Отвечай ТОЛЬКО на русском. "
        "Если данных нет — скажи, что информации нет. "
        "Заканчивай: 'Обращайтесь, если у вас появятся другие вопросы!'"
    ),
    verbose=True,
    allow_delegation=False,
    llm=llm
)


# ========================================
# 5. Задачи
# ========================================
research_task = Task(
    description=(
        "Вопрос пользователя: '{user_query}'.\n\n"
        "Ты должен вызвать инструмент **Document Search Tool** с правильными аргументами:\n\n"
        "→ Для **филиалов**: query='{user_query}', metadata={'source': 'belarusbank_api', 'type': 'filial_info'}\n"
        "→ Для **курсов**: query='{user_query}', metadata={'source': 'belarusbank_api', 'type': 'aggregated_currency_rates'}\n"
        "→ Иначе: query='{user_query}', metadata={'source': 'docs'}\n"
        "ВЕРНИ ТОЛЬКО результат поиска. НИЧЕГО НЕ ДОБАВЛЯЙ.НЕ МЕНЯЙ АРГУМЕНТЫ."
        # "→ Если не уверен — просто query='{user_query}'\n\n"
    ),
    expected_output="Текст из базы знаний с метаданными.",
    agent=researcher_agent,
)

answer_task = Task(
    description=(
        "Оригинальный вопрос: '{user_query}'.\n"
        "На основе данных ответь на русском. Не придумывай ничего! \n\n"
        "- **Филиалы**: перечисли адрес, часы работы \n"
        "- **Курсы**: покажи покупку/продажу и больше ничего не надо\n"
        "- **PDF/TXT**: процитируй релевантный фрагмент\n"
        "- Нет данных → 'К сожалению, в базе знаний нет информации по вашему запросу.'\n\n"
        "Заканчивай: 'Обращайтесь, если у вас появятся другие вопросы!'"
    ),
    expected_output="Четкий, вежливый ответ на русском.",
    agent=advisor_agent,
    context=[research_task]
)


# ========================================
# 6. Crew
# ========================================
banking_crew = Crew(
    agents=[researcher_agent, advisor_agent],
    tasks=[research_task, answer_task],
    process=Process.sequential,
    verbose=True
)

logger.info("Crew готов!")


# ========================================
# 7. API
# ========================================
def get_banking_crew():
    return banking_crew


# ========================================
# 8. Тест
# ========================================
if __name__ == "__main__":
    # test_queries = [
    #     "Какой курс доллара?",
    #     "Какие у меня расходы?",
    #     "Где обменник в Бресте?",
    # ]

    # for q in test_queries:
    #     print(f"\n{'='*60}")
    #     print(f"ТЕСТ: {q}")
    #     print('='*60)
    #     try:
    #         result = banking_crew.kickoff(inputs={'user_query': q})
    #         print(f"ОТВЕТ:\n{result}")
    #     except Exception as e:
    #         print(f"ОШИБКА: {e}")
    #         import traceback
    #         traceback.print_exc()
# if __name__ == "__main__":
        # Создаём инструмент
        tool = DocumentSearchTool()
     
        # # Тест 1: Филиалы в Гродно
        print("\n" + "="*60)
        print("ТЕСТ: Филиалы в Гродно")
        print("="*60)
        result = tool._run(
            query="филиалы в Гродно",
            metadata={"source": "belarusbank_api", "type": "filial_info", "city": "Гродно"}
        )
        print(result)

        # Тест 2: Курс доллара
        print("\n" + "="*60)
        print("ТЕСТ: Курсы валют")
        print("="*60)
        result = tool._run(
            query="Средние курсы валют",
            metadata={"source": "belarusbank_api", "type": "aggregated_currency_rates"}
        )
        print(result)

        # Тест 3: Расходы
        print("\n" + "="*60)
        print("ТЕСТ: Сколько кредитов")
        print("="*60)
        result = tool._run(
            query="Что такое кредит?",
            metadata={"source": "docs"}
        )
        print(result)
        