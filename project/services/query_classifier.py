# services/query_classifier.py
import os
import pickle
import logging
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

logger = logging.getLogger(__name__)

MODEL_PATH = "query_classifier.pkl"

# Обучающие примеры (можно дополнять)
TRAINING_DATA = [
        ("курсы валют в этом отделении", "currency_rate"),
    ("курс в отделении 400/4001", "currency_rate"),
    ("какой курс в филиале", "currency_rate"),
    ("покажи курс в этом отделении", "currency_rate"),
    ("курсы валют в отделении", "currency_rate"),
    # Расходы
    ("потратил сегодня", "expense"),
    ("покажи расходы", "expense"),
    ("сколько я потратил на продукты", "expense"),
    ("статистика трат", "expense"),
    ("чеки за месяц", "expense"),
    ("мои расходы", "expense"),
    ("траты на транспорт", "expense"),
    ("какие у меня расходы", "expense"),
    ("сколько денег ушло", "expense"),
    ("покажи траты", "expense"),
    ("сколько я потратил на продукты и на какие товары", "expense"),
    ("расходы на еду и что я покупал", "expense"),
    ("сколько на кафе и что именно", "expense"),
    ("траты на здоровье с деталями", "expense"),
    ("мои расходы по категориям", "expense"),
    ("покажи что я купил в категории продукты", "expense"),
    ("сколько я потратил на услуги", "expense"),
    ("расходы за последний месяц", "expense"),
    # Курсы валют
    ("курс доллара", "currency_rate"),
    ("какой курс евро", "currency_rate"),
    ("сколько стоит доллар", "currency_rate"),
    ("курс rub", "currency_rate"),
    ("покажи курс валют", "currency_rate"),
    ("курс российского рубля", "currency_rate"),
    ("сегодняшний курс usd", "currency_rate"),
    ("в банке какой курс", "currency_rate"),
    ("узнать курс валюты", "currency_rate"),
    ("курс доллара в беларусбанке", "currency_rate"),
    
    # Динамика
    ("динамика доллара за месяц", "currency_dynamics"),
    ("покажи график евро с 01.01.2024", "currency_dynamics"),
    ("динамика rub", "currency_dynamics"),
    ("как менялся курс usd", "currency_dynamics"),
    ("график курса евро", "currency_dynamics"),
    ("изменение курса рубля с 2022 по 2024", "currency_dynamics"),
    ("динамика курса валют", "currency_dynamics"),
    # Конвертация
    ("100 долларов в рубли", "conversion"),
    ("конвертировать 50 евро в byn", "conversion"),
    ("сколько будет 20 usd в рублях", "conversion"),
    ("переведи 1000 рублей в доллары", "conversion"),
    ("конвертация валют", "conversion"),
    ("сколько в рублях 10 долларов", "conversion"),
    ("калькулятор валют", "conversion"),
    # Филиалы
    ("где находится отделение в минске", "filial"),
    ("филиал в бресте", "filial"),
    ("режим работы банка в гродно", "filial"),
    ("адрес филиала", "filial"),
    ("ближайшее отделение", "filial"),
    ("банкомат в витебске", "filial"),
    ("где обменять валюту", "filial"),
    # Общее (RAG)
    ("что такое кредит", "rag"),
    ("как открыть депозит", "rag"),
    ("какие документы нужны для ипотеки", "rag"),
    ("помощь", "rag"),
    ("привет", "rag"),
    ("что ты умеешь", "rag"),
    ("спасибо", "rag"),
    ("как дела", "rag"),
    ("курс доллара в минске", "currency_rate"),
    ("курс в бресте", "currency_rate"),
    ("узнать курс в гродно", "currency_rate"),
    ("филиал в гомеле", "filial"),
    ("все отделения в витебске", "filial"),
    ("график работы в могилеве", "filial"),
    ("это все филиалы в гродно", "filial"),
        ("время работы отделений в гродно", "filial"),
    ("график работы филиалов", "filial"),
    ("как работает отделение в минске", "filial"),
    ("когда открыто отделение", "filial"),
    ("все отделения в бресте", "filial"),
    ("полный список филиалов", "filial"),
        # Уточняющие вопросы по конвертации
    ("это по какому курсу", "conversion"),
    ("какой курс использовался", "conversion"),
    ("из какого отделения курс", "conversion"),
    ("какой банк использовался", "conversion"),
    ("почему такой курс", "conversion"),
    # Уточняющие вопросы по филиалам
    ("это все отделения", "filial"),
    ("это все филиалы", "filial"),
    ("а в другом городе", "filial"),
    ("показать все", "filial"),
    ("полный список", "filial"),
    # Детали конкретного филиала
    ("часы работы отделения 400/4001", "filial_detail"),
    ("когда открыто отделение горького 91", "filial_detail"),
    ("режим работы этого отделения", "filial_detail"),
    ("курс в отделении 400/4003", "filial_detail"),
    ("какие курсы в этом филиале", "filial_detail"),
    ("курс доллара в отделении на советских пограничников", "filial_detail"),
    ("адрес этого отделения", "filial_detail"),
    ("часы работы этого отделения", "filial_detail"),
    ("когда работает первое отделение", "filial_detail"),
    ("курсы валют в первом отделении", "filial_detail"),
    ("расскажи про отделение 1", "filial_detail"),
    ("информация об отделении", "filial_detail"),
    ("часы работы отделений в гродно", "filial_detail"),
    ("график работы первого отделения из списка", "filial_detail"),
    # Расходы за период
    ("сколько потратил на прошлой неделе", "expense_period"),
    ("расходы за прошлый месяц", "expense_period"),
    ("что я покупал в январе", "expense_period"),
    ("траты за январь 2026", "expense_period"),
    ("сколько потратил 12 апреля", "expense_period"),
    ("расходы за эту неделю", "expense_period"),
    ("сколько потратил вчера", "expense_period"),
    ("покупки за последние 7 дней", "expense_period"),
    ("траты в марте 2025", "expense_period"),
    ("что покупал на прошлой неделе", "expense_period"),
    ("сколько потратил в прошлом месяце", "expense_period"),
    ("расходы за сегодня", "expense_period"),
    ("траты за 2025 год", "expense_period"),
    # Советы по экономии
    ("как сэкономить", "saving_advice"),
    ("дай советы по экономии", "saving_advice"),
    ("где я трачу больше всего", "saving_advice"),
    ("помоги сократить расходы", "saving_advice"),
    ("анализ трат с советами", "saving_advice"),
    ("программы лояльности магазинов", "saving_advice"),
    ("как оптимизировать расходы", "saving_advice"),
    ("хочу тратить меньше", "saving_advice"),
    ("проанализируй мои расходы", "saving_advice"),
    ("посоветуй как экономить", "saving_advice"),
    ("в каком магазине выгоднее", "saving_advice"),
]

def train_model():
    """Обучает модель на TRAINING_DATA и сохраняет в файл."""
    texts, labels = zip(*TRAINING_DATA)
    pipeline = Pipeline([
        ('tfidf', TfidfVectorizer(ngram_range=(1, 2))),
        ('clf', LogisticRegression(max_iter=200, random_state=42))
    ])
    pipeline.fit(texts, labels)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(pipeline, f)
    logger.info(f"Модель классификатора запросов обучена и сохранена в {MODEL_PATH}")
    return pipeline

def load_model():
    """Загружает модель из файла, если есть; иначе обучает заново."""
    if not os.path.exists(MODEL_PATH):
        return train_model()
    with open(MODEL_PATH, 'rb') as f:
        model = pickle.load(f)
    return model

def classify_query(query: str) -> str:
    """Возвращает предсказанную категорию для запроса."""
    model = load_model()
    return model.predict([query])[0]

def retrain_with_feedback(correct_query: str, correct_category: str):
    """Дообучает модель на новом корректном примере (можно вызывать из админки)."""
    # Добавляем пример в TRAINING_DATA (в памяти, для сохранения потребуется пересохранить)
    TRAINING_DATA.append((correct_query, correct_category))
    # Переобучаем и перезаписываем модель
    train_model()
# services/query_classifier.py  (дописать после classify_query)

def get_confidence(query: str) -> float:
    """Возвращает максимальную вероятность среди классов для данного запроса."""
    model = load_model()
    probs = model.predict_proba([query])[0]
    return max(probs)

def add_training_example(query: str, category: str, retrain: bool = True):
    """Добавляет новый пример и опционально переобучает модель."""
    TRAINING_DATA.append((query, category))
    if retrain:
        train_model()

if __name__ == "__main__": 
    train_model()