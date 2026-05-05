import time
import json
import os
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from database import FilialLocation
from staticmap import StaticMap, CircleMarker, Line
import os
import folium

CACHE_FILE = "static/filial_coords_cache.json"
USER_AGENT = "banking_assistant_bot"

# Инициализируем геокодер с увеличенным таймаутом
geolocator = Nominatim(user_agent=USER_AGENT, timeout=30)

def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save_cache(cache):
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_coords_nominatim(address: str) -> Optional[Tuple[float, float]]:
    """Запрашивает координаты у Nominatim с кэшированием."""
    cache = _load_cache()
    if address in cache:
        return tuple(cache[address])

    # Обязательная пауза — не меньше 1.5 секунд
    time.sleep(1.5)

    try:
        location = geolocator.geocode(address)
        if location:
            coords = (location.latitude, location.longitude)
            cache[address] = list(coords)
            _save_cache(cache)
            return coords
        else:
            print(f"Не найден: {address}")
    except Exception as e:
        print(f"Ошибка для '{address}': {e}")
        # При ошибке ждём ещё и пробуем позже (если нужно, можно реализовать повторы)

    return None

def populate_filial_locations(db: Session):
    """Заполняет координаты филиалов, используя Nominatim."""
    with open("data/kursExchange.json", 'r', encoding='utf-8') as f:
        data = json.load(f)

    new_locations = 0
    for item in data:
        fid = item.get("filial_id")
        if not fid or db.query(FilialLocation).filter(FilialLocation.filial_id == fid).first():
            continue

        city = item.get("name", "")
        street = item.get("street", "")
        home = item.get("home_number", "")
        # Формируем адрес в виде "Беларусь, город, улица, дом"
        address = f"Беларусь, {city}, {street}, {home}"

        coords = get_coords_nominatim(address)
        if coords:
            lat, lon = coords
            loc = FilialLocation(
                filial_id=fid,
                latitude=lat,
                longitude=lon,
                address=f"{city}, {street} {home}",
                city=city
            )
            db.add(loc)
            new_locations += 1
            print(f"OK: {address}")
        else:
            print(f"Пропущен: {address}")

    db.commit()
    print(f"\nДобавлено {new_locations} координат")

def find_nearest_filial(db: Session, user_lat: float, user_lon: float, city: Optional[str] = None) -> Optional[dict]:
    """Возвращает ближайший филиал, опционально фильтруя по городу."""
    query = db.query(FilialLocation)
    if city:
        query = query.filter(FilialLocation.city.ilike(f"%{city}%"))
    locations = query.all()
    if not locations:
        return None
    nearest = min(locations, key=lambda loc: geodesic((user_lat, user_lon), (loc.latitude, loc.longitude)).km)
    distance = geodesic((user_lat, user_lon), (nearest.latitude, nearest.longitude)).km
    return {
        "filial_id": nearest.filial_id,
        "address": nearest.address,
        "latitude": nearest.latitude,
        "longitude": nearest.longitude,
        "distance_km": round(distance, 2)
    }



def find_nearest_filials(db: Session, user_lat: float, user_lon: float, top_n: int = 5, city: Optional[str] = None) -> list:
    """
    Возвращает список из `top_n` ближайших филиалов с расстоянием и координатами.
    Опционально фильтрует по городу.
    """
    query = db.query(FilialLocation)
    if city:
        query = query.filter(FilialLocation.city.ilike(f"%{city}%"))
    locations = query.all()
    if not locations:
        return []
    # Сортируем по расстоянию и берём top_n
    sorted_locations = sorted(locations, key=lambda loc: geodesic((user_lat, user_lon), (loc.latitude, loc.longitude)).km)
    result = []
    for loc in sorted_locations[:top_n]:
        distance = geodesic((user_lat, user_lon), (loc.latitude, loc.longitude)).km
        result.append({
            "filial_id": loc.filial_id,
            "address": loc.address,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "distance_km": round(distance, 2)
        })
    return result

def create_map_html(user_lat: float, user_lon: float, filials: list, output_path: str) -> str:
    """
    Создаёт HTML-файл с интерактивной картой, на которой отмечены пользователь и филиалы.
    Возвращает путь к файлу.
    """
    m = folium.Map(location=[user_lat, user_lon], zoom_start=13)
    # Маркер пользователя
    folium.Marker(
        [user_lat, user_lon],
        popup="Вы здесь",
        icon=folium.Icon(color="red", icon="user")
    ).add_to(m)
    # Маркеры филиалов
    for f in filials:
        folium.Marker(
            [f["latitude"], f["longitude"]],
            popup=f"{f['address']}<br>{f['distance_km']} км",
            icon=folium.Icon(color="blue", icon="university")
        ).add_to(m)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    m.save(output_path)
    return output_path



def create_map_image(user_lat: float, user_lon: float, filials: list, output_path: str, size=(800, 600)) -> str:
    """Создаёт PNG-карту с маркерами пользователя и филиалов."""
    # Без указания url_template – используется стандартный OSM тайл
    m = StaticMap(size[0], size[1])
    
    # Красный маркер – пользователь
    m.add_marker(CircleMarker((user_lon, user_lat), 'red', 12))
    # Синие маркеры – филиалы
    for f in filials:
        m.add_marker(CircleMarker((f["longitude"], f["latitude"]), 'blue', 10))
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    image = m.render()
    image.save(output_path)
    return output_path