import asyncio
import html
import logging
import math
import re
import statistics
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.google_places import (
    PlaceInfo,
    build_fast_reviews_app_url,
    build_review_url,
)
from services.instagram_service import (
    DEFAULT_IGSH_TOKEN,
    IG_URL_REGEX,
    _cyr_to_latin,
    _extract_brand_words,
    _format_followers_badge,
    _parse_followers,
    validate_ig_username,
)
from services.keyless_places import keyless_places_service
from services.lead_hunter import (
    STOP_NAME_WORDS,
    calc_5stars_needed,
    evaluate_lead_potential,
)

logger = logging.getLogger("hhtap_bot.high_chance_parser")


@dataclass
class HighChanceGoogleLead:
    place: PlaceInfo
    score: int
    tier_badge: str
    pain_text: str
    needed_5stars: int
    target_rating: float
    fast_app_url: str
    direct_5star_url: str


@dataclass
class HighChanceInstaLead:
    place: PlaceInfo
    ig_username: str
    ig_title: str
    followers: int
    posts: int
    score: int
    tier_badge: str
    pain_text: str
    verification_badge: str
    primary_nfc_url: str
    deep_link_url: str
    profile_web_url: str


@dataclass
class HighChanceLocationPack:
    pack_id: str
    location_query: str
    mode: str  # "google" | "insta"
    total_scanned: int
    google_leads: List[HighChanceGoogleLead] = field(default_factory=list)
    insta_leads: List[HighChanceInstaLead] = field(default_factory=list)


# Быстрые пресеты популярных коммерческих улиц Ташкента для выбора в 1 клик (Страница 1)
POPULAR_LOCATIONS: Dict[str, str] = {
    "shevchenko": "Тараса Шевченко",
    "parkent": "Паркентская",
    "rustaveli": "Шота Руставели",
    "oybek": "Ойбек / Мирабад",
    "c1": "Ц-1 / Шахрисабз",
    "yunusabad": "Юнусабад / Амир Темур",
    "chilanzar": "Чиланзар / Катартал",
    "boulevard": "Tashkent City / Boulevard",
}

# Дополнительные улицы и локации Ташкента (Страница 2)
EXTRA_LOCATIONS: Dict[str, str] = {
    "nukus": "Нукусская",
    "babur": "Улица Бабура",
    "azimov": "Садыка Азимова",
    "makhtumkuli": "Махтумкули",
    "labzak": "Лабзак / Ганга",
    "ipakyuli": "Буюк Ипак Йули (Максимка)",
    "novomoskovskaya": "Аккурган / Новомосковская",
    "farhad": "Фархадский / Учтепа",
    "sergeli": "Сергели / Янги Сергели",
    "chorsu": "Чорсу / Самарканд Дарвоза",
}

ALL_PRESET_LOCATIONS: Dict[str, str] = {**POPULAR_LOCATIONS, **EXTRA_LOCATIONS}

# Крупные сетевые гиганты и агрегаторы, которым не продают одиночные карты с улицы
CHAIN_BLACKLIST = (
    "evos", "эвос", "kfc", "кфс", "bellissimo", "беллиссимо", "dodo", "додо",
    "oqtepa", "октепа", "maxway", "максвей", "feed up", "фидап", "wendy",
    "safia", "сафия", "korzinka", "корзинка", "havas", "хавас", "makro",
)

# Брендовые слова слишком общего характера, по которым нельзя угадывать чужие однословные ники
GENERIC_ONLY_NAMES = {
    "stomatologiya", "стоматология", "barbershop", "барбершоп", "kafe", "кафе",
    "restoran", "ресторан", "aptek", "аптека", "salon", "салон", "klinika", "клиника",
    "burger", "бургер", "pizza", "пицца", "coffee", "кофе", "lounge", "лаунж",
    "the", "new", "big", "top", "vip", "pro", "one", "art", "my", "na", "на",
    "republic", "house", "club", "time", "city", "studio", "dental", "clinic",
    "denta", "дента", "bar", "бар", "sushi", "суши", "market", "маркет",
    "podarki", "подарки", "tsvetochnaya", "цветочная", "lavka", "лавка",
    "tsvety", "цветы", "flowers", "flower", "buket", "букет", "magazin", "магазин",
}

STREET_STOPWORDS = {
    "shevchenko", "шевченко", "taras", "тарас", "parkent", "паркент", "rustaveli",
    "руставели", "tashkent", "ташкент", "oybek", "ойбек", "mirabad", "мирабад",
    "chilanzar", "чиланзар", "yunusabad", "юнусабад", "boulevard", "c1", "ц1",
    "nukus", "nukusskaya", "нукусская", "babur", "babura", "бабура", "azimov",
    "azimova", "азимова", "makhtumkuli", "maxtumquli", "махтумкули", "labzak",
    "лабзак", "ganga", "ганга", "maksimka", "максимка", "akkurgan", "аккурган",
    "farhad", "farkhad", "фархадский", "uchtepa", "учтепа", "sergeli", "сергели",
    "chorsu", "чорсу", "darvoza", "дарвоза", "ulitsa", "улица", "kocha", "kochasi",
}

DIRECTORY_DOMAINS = {
    "taplink", "instagram", "facebook", "google", "abnmbgroup", "tanukifamily",
    "t", "2gis", "goldenpages", "yellowpages", "sprav", "zoon", "yandex",
    "alteg", "yclients", "linktr", "hipolink", "mssg",
}

# Признаки чужих стран/городов и нецелевых аккаунтов — отсекаются мгновенно
FOREIGN_MARKERS = (
    "+7", "8(9", "8 (9", "москва", "мск", "санкт-петербург", "спб", "питер",
    "алматы", "астана", "казахстан", "бишкек", "армавир", "краснодар", "сочи",
    "екатеринбург", "новосибирск", "казань", "минск", "киев", "павелецкая",
    "калининград", "ростов", "самара", "уфа", "челябинск", "воронеж", "пермь",
    "волгоград", "красноярск", "саратов", "тюмень", "тольятти", "ижевск", "барнаул",
    "иркутск", "хабаровск", "ярославль", "владивосток", "махачкала", "томск", "оренбург",
    "кемерово", "новокузнецк", "рязань", "астрахань", "пенза", "липецк", "тула", "киров",
    "чебоксары", "курск", "брянск", "магнитогорск", "иваново", "тверь", "ставрополь",
    "белгород", "сочи", " крым", "севастополь", "симферополь", "душанбе", "баку", "тбилиси",
    "ереван", "самарканд", "бухара", "фергана", "андижан", "наманган", "нукус", "карши",
    "hanoi", "france", "marzo", "peluquer", "especializados", "livraison",
    "barbados island", "🇦🇺", "🇵🇾", "🇷🇺", "🇰🇿", "🇰🇬", "🇬🇧", "🇧🇾",
    "профиль продается", "аккаунт продается", "+999",
)

# Признаки реального коммерческого заведения в шапке или описании профиля
REAL_BIZ_CATEGORY_WORDS = (
    "ресторан", "restaurant", "ristorante", "кафе", "cafe", "coffee", "кофейня",
    "пекарня", "bakery", "boulangerie", "кондитер", "гастро", "gastro", "bistro", "бистро",
    "бар", "bar", "pub", "паб", "lounge", "лаунж", "кальян", "shisha", "hookah",
    "пицц", "pizza", "pizzeria", "суши", "sushi", "роллы", "wok", "бургер", "burger",
    "кухня", "kitchen", "cuisine", "меню", "menu", "бронь", "резерв", "доставка", "delivery",
    "halal", "халяль", "барбер", "barber", "стриж", "брить", "салон", "beauty",
    "косметолог", "клиника", "clinic", "стоматолог", "dental", "stomatolog", "ортодонт",
    "имплант", "винир", "цветы", "букет", "flowers", "floral", "roses", "декор",
    "часы работы", "режим работы", "пн-вс", "ежедневно", "без выходных",
)

# Признаки привязки к Ташкенту / Узбекистану
UZ_GEO_MARKERS = (
    "+998", "998", "ташкент", "tashkent", "узбекистан", "uzbekistan", "тошкент", "🇺🇿",
    "шевченко", "shevchenko", "паркент", "parkent", "руставели", "rustaveli", "ойбек",
    "мирабад", "чиланзар", "юнусабад", "чехов", "chekhov", "амир темур", "дархан", "darhan",
    "тараккиёт", "taraqqiyot", "узум", "uzum", "ц-1", "шахрисабз", "катартал", "нукусская",
)


def _normalize_brand_key(name: str) -> str:
    """Нормализует название бизнеса для дедупликации сетевых филиалов на одной улице."""
    low = (name or "").lower()
    for suffix in (
        "shevchenko", "шевченко", "taras", "тарас", "parkent", "паркент",
        "tashkent", "ташкент", "c1", "ц1", "boulevard", "uno", "premium", "vip",
    ):
        low = re.sub(rf"\b{suffix}\b", "", low)
    cleaned = re.sub(r"[^a-zа-яё0-9]+", "", low)
    return cleaned[:16] if cleaned else low


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


async def _resolve_street_osm(loc_query: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Определяет точные GPS-координаты ЛЮБОЙ улицы/квартала/ориентира в Ташкенте
    и его официальное узбекское/латинское название (например: 'улица Бабура' -> 'Bobur ko\\'chasi',
    'улица Чехова' -> 'Fidokor ko\\'chasi', 'Нукусская' -> 'Nukus ko\\'chasi').
    """
    q = loc_query.strip()
    if "ташкент" not in q.lower() and "tashkent" not in q.lower():
        q = f"{q} Ташкент"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "HHtapSalesBot/2.0 (Tashkent)"},
            timeout=2.2,
        ) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 2},
            )
            if r.status_code == 200:
                data = r.json()
                for item in data:
                    disp = item.get("display_name", "")
                    if any(c in disp.lower() for c in ("toshkent", "ташкент", "tashkent", "oʻzbekiston", "узбекистан")):
                        lat = float(item["lat"])
                        lon = float(item["lon"])
                        osm_name = disp.split(",")[0].strip()
                        return lat, lon, osm_name
    except Exception as e:
        logger.debug("Nominatim geocode fallback for '%s': %s", loc_query, e)
    return None, None, None


async def collect_all_location_places(location_query: str) -> List[PlaceInfo]:
    """
    Параллельно сканирует ЛЮБУЮ заданную улицу/локацию по коммерческим категориям
    (как по русскому, так и по официальному узбекскому названию улицы в Google Maps),
    вычисляет точный гео-центр улицы и отсекает случайные выбросы из других районов города.
    """
    loc = location_query.strip()
    if "ташкент" not in loc.lower() and "tashkent" not in loc.lower():
        loc_with_city = f"{loc} Ташкент"
    else:
        loc_with_city = loc

    osm_lat, osm_lng, osm_street = await _resolve_street_osm(loc)
    search_lat = osm_lat if osm_lat is not None else 41.3111
    search_lng = osm_lng if osm_lng is not None else 69.2797
    search_radius = 1200 if osm_lat is not None else 30000

    queries = [
        f"ресторан гастробар бистро {loc_with_city}",
        f"пиццерия бургерная суши роллы {loc_with_city}",
        f"кафе кофейня пекарня кондитерская {loc_with_city}",
        f"бар паб лаунж кальянная {loc_with_city}",
        f"барбершоп мужская парикмахерская {loc_with_city}",
        f"салон красоты косметология спа {loc_with_city}",
        f"стоматология dental клиника {loc_with_city}",
        f"цветы букеты подарки магазин {loc_with_city}",
    ]
    if osm_street and osm_street.lower() not in loc.lower():
        queries.extend(
            [
                f"ресторан кафе пиццерия {osm_street} Ташкент",
                f"кофейня бар кальянная {osm_street} Ташкент",
                f"барбершоп салон красоты {osm_street} Ташкент",
                f"стоматология цветы {osm_street} Ташкент",
            ]
        )

    async def _fetch_cat(q: str, lat: float = search_lat, lng: float = search_lng, radius: int = search_radius) -> List[PlaceInfo]:
        try:
            return await asyncio.wait_for(
                keyless_places_service.search_text(q, max_results=14, lat=lat, lng=lng, radius=radius),
                timeout=4.5,
            )
        except Exception as e:
            logger.warning("Ошибка сканирования категории '%s': %s", q, e)
            return []

    batches = await asyncio.gather(*[_fetch_cat(q) for q in queries])
    raw_places: List[PlaceInfo] = [p for b in batches for p in b if p.place_id]

    # Вычисляем точный GPS-центр выбранной улицы по точкам, чей адрес содержит название улицы (рус. или узб.)
    raw_loc_str = f"{loc} {osm_street or ''}".lower()
    loc_words = [
        w for w in re.findall(r"[a-zа-яё0-9]{3,}", raw_loc_str)
        if w not in (
            "ташкент", "tashkent", "toshkent", "улица", "район", "квартал",
            "массив", "проспект", "ko", "chasi", "kochasi", "street", "ulitsa",
        )
    ]
    loc_lat_words = [_cyr_to_latin(w) for w in loc_words]
    all_loc_tokens = set(loc_words + loc_lat_words)

    anchored_coords: List[Tuple[float, float]] = []
    all_coords: List[Tuple[float, float]] = []
    for p in raw_places:
        if p.latitude and p.longitude:
            all_coords.append((p.latitude, p.longitude))
            addr_low = f"{p.address or ''} {p.name or ''}".lower()
            addr_lat = _cyr_to_latin(addr_low)
            if any(tok[:5] in addr_low or tok[:5] in addr_lat for tok in all_loc_tokens if len(tok) >= 3):
                anchored_coords.append((p.latitude, p.longitude))

    med_lat: Optional[float] = osm_lat
    med_lng: Optional[float] = osm_lng
    if len(anchored_coords) >= 2:
        med_lat = statistics.median([c[0] for c in anchored_coords])
        med_lng = statistics.median([c[1] for c in anchored_coords])
    elif med_lat is None and all_coords:
        med_lat = statistics.median([c[0] for c in all_coords])
        med_lng = statistics.median([c[1] for c in all_coords])

    if med_lat is not None and med_lng is not None and not osm_street:
        # Дополнительно пробиваем плотный GPS-квадрат прямо вокруг центра улицы (радиус 850м)
        zoom_queries = [
            f"ресторан кафе пиццерия суши {loc}",
            f"кофейня пекарня бар лаунж кальянная {loc}",
            f"барбершоп салон красоты стоматология цветы {loc}",
        ]
        zoom_batches = await asyncio.gather(
            *[_fetch_cat(zq, lat=med_lat, lng=med_lng, radius=850) for zq in zoom_queries]
        )
        for zb in zoom_batches:
            raw_places.extend(zb)

    # Сортируем raw_places так, чтобы точки с прямым совпадением названия улицы в адресе шли первыми!
    def _street_priority(p: PlaceInfo) -> Tuple[int, float]:
        addr_low = f"{p.address or ''} {p.name or ''}".lower()
        addr_lat = _cyr_to_latin(addr_low)
        on_street = any(tok[:5] in addr_low or tok[:5] in addr_lat for tok in all_loc_tokens if len(tok) >= 3)
        dist = (
            _haversine_km(med_lat, med_lng, p.latitude, p.longitude)
            if (med_lat is not None and med_lng is not None and p.latitude and p.longitude)
            else 99.0
        )
        return (0 if on_street else 1, dist)

    raw_places.sort(key=_street_priority)

    unique_places: List[PlaceInfo] = []
    seen_ids: set[str] = set()
    seen_brands: Dict[str, int] = {}

    for p in raw_places:
        if not p.place_id or p.place_id in seen_ids:
            continue
        low_name = (p.name or "").lower()
        if any(w in low_name for w in STOP_NAME_WORDS):
            continue
        if any(ch in low_name for ch in CHAIN_BLACKLIST):
            continue
        clean_alpha = re.sub(r"[^a-zа-яё]+", " ", low_name).strip()
        if clean_alpha in GENERIC_ONLY_NAMES:
            continue

        # Гео-фильтр: оставляем точки на самой улице или в радиусе 1.6 км от центра улицы
        if med_lat is not None and med_lng is not None and p.latitude and p.longitude:
            dist_km = _haversine_km(med_lat, med_lng, p.latitude, p.longitude)
            addr_low = (p.address or "").lower()
            addr_lat = _cyr_to_latin(addr_low)
            on_street = any(tok[:5] in addr_low or tok[:5] in addr_lat for tok in all_loc_tokens if len(tok) >= 3)
            if not on_street and dist_km > 1.65:
                continue

        b_key = _normalize_brand_key(p.name)
        if b_key and seen_brands.get(b_key, 0) >= 1:
            continue
        seen_ids.add(p.place_id)
        if b_key:
            seen_brands[b_key] = seen_brands.get(b_key, 0) + 1
        unique_places.append(p)

    return unique_places


async def collect_all_nearby_places(lat: float, lng: float, radius_km: float = 1.3) -> List[PlaceInfo]:
    """
    Сканирует ВСЕ коммерческие заведения вокруг точной GPS-геопозиции пользователя (радиус ~1.3 км),
    чтобы работать на любой улице Ташкента даже без ввода названия улицы!
    """
    queries = [
        "ресторан гастробар бистро стейк",
        "пиццерия бургерная суши роллы донер",
        "кафе кофейня пекарня кондитерская",
        "бар паб лаунж кальянная",
        "барбершоп мужская парикмахерская",
        "салон красоты косметология спа",
        "стоматология dental клиника",
        "цветы букеты подарки магазин",
    ]

    async def _fetch_near(q: str) -> List[PlaceInfo]:
        try:
            return await asyncio.wait_for(
                keyless_places_service.search_text(q, max_results=15, lat=lat, lng=lng, radius=750),
                timeout=4.5,
            )
        except Exception:
            return []

    batches = await asyncio.gather(*[_fetch_near(q) for q in queries])
    unique_places: List[PlaceInfo] = []
    seen_ids: set[str] = set()
    seen_brands: Dict[str, int] = {}

    for batch in batches:
        for p in batch:
            if not p.place_id or p.place_id in seen_ids:
                continue
            low_name = (p.name or "").lower()
            if any(w in low_name for w in STOP_NAME_WORDS) or any(ch in low_name for ch in CHAIN_BLACKLIST):
                continue
            clean_alpha = re.sub(r"[^a-zа-яё]+", " ", low_name).strip()
            if clean_alpha in GENERIC_ONLY_NAMES:
                continue
            if p.latitude and p.longitude:
                if _haversine_km(lat, lng, p.latitude, p.longitude) > radius_km:
                    continue
            b_key = _normalize_brand_key(p.name)
            if b_key and seen_brands.get(b_key, 0) >= 1:
                continue
            seen_ids.add(p.place_id)
            if b_key:
                seen_brands[b_key] = seen_brands.get(b_key, 0) + 1
            unique_places.append(p)

    return unique_places


def rank_google_reviews_leads(places: List[PlaceInfo]) -> List[HighChanceGoogleLead]:
    """
    Отбирает и сортирует ВСЕ бизнесы локации по вероятности покупки Google Reviews NFC:
    - Фокус №1: плохие оценки (3.0–4.3⭐️) и мало отзывов (до 85 отз.) — максимальная боль + быстрый рост рейтинга.
    - Фокус №2: просаженный рейтинг (3.5–4.4⭐️) при среднем числе отзывов или уязвимая карточка с малым числом оценок.
    """
    leads: List[HighChanceGoogleLead] = []
    for p in places:
        low_name = (p.name or "").lower()
        if any(w in low_name for w in STOP_NAME_WORDS):
            continue
        item_dict = {
            "place_id": p.place_id,
            "name": p.name,
            "address": p.address,
            "rating": p.rating,
            "user_ratings_total": p.user_ratings_total,
            "url": p.google_maps_url,
            "lat": p.latitude,
            "lng": p.longitude,
            "phone": p.phone,
            "website": p.website,
            "business_status": "OPERATIONAL",
        }
        scored = evaluate_lead_potential(item_dict)
        if not scored:
            r = p.rating
            rev = p.user_ratings_total or 0
            if r is not None and 2.5 <= r <= 4.45 and rev <= 300:
                needed_5, target_r = calc_5stars_needed(r, rev)
                sc = 86 if (r <= 4.1 and rev <= 80) else 76
                badge = "🔥 Низкий рейтинг (Острая боль)" if r <= 4.1 else "⚡️ Просаженный рейтинг"
                pain = (
                    f"Рейтинг {r:.1f}⭐️ при {rev} отзывах — заведение теряет гостей в поиске Google Карт. "
                    f"Нужно ~{needed_5} оценок 5⭐️ для выхода на {target_r:.1f}⭐️."
                )
            else:
                continue
        else:
            sc = scored.score
            badge = scored.tier_badge
            pain = scored.why_hot
            needed_5 = scored.needed_5stars
            target_r = scored.target_rating

        fast_app_url = build_fast_reviews_app_url(p.place_id, p.google_maps_url)
        direct_5star_url = p.review_url or build_review_url(p.place_id)
        leads.append(
            HighChanceGoogleLead(
                place=p,
                score=sc,
                tier_badge=badge,
                pain_text=pain,
                needed_5stars=needed_5,
                target_rating=target_r,
                fast_app_url=fast_app_url,
                direct_5star_url=direct_5star_url,
            )
        )

    leads.sort(
        key=lambda item: (
            item.score,
            -(item.place.user_ratings_total or 9999),
            -(item.place.rating or 5.0),
        ),
        reverse=True,
    )
    return leads


def _extract_ig_handles_from_html(raw_html: str) -> List[str]:
    found: List[str] = []
    txt = urllib.parse.unquote(raw_html or "")
    for m in re.finditer(r"instagram\.com/(?:_u/)?([A-Za-z0-9._]{3,30})", txt, re.IGNORECASE):
        v = validate_ig_username(m.group(1).lower().rstrip("."))
        if v and v not in found:
            found.append(v)
    return found


def _build_fast_place_ig_candidates(place: PlaceInfo) -> Tuple[List[str], Optional[str], set[str]]:
    """
    Генерирует стартовые кандидаты Instagram-никнеймов для конкретного заведения.
    Возвращает (candidates, direct_gmaps_ig, website_stems).
    """
    cands: List[str] = []
    direct_ig: Optional[str] = None
    web_stems: set[str] = set()

    def add(h: str):
        v = validate_ig_username(h.lower().rstrip("."))
        if v and len(v) >= 3 and v not in cands:
            cands.append(v)

    w = (place.website or "").strip()
    if w:
        m_ig = IG_URL_REGEX.search(w)
        if m_ig and validate_ig_username(m_ig.group(1)):
            direct_ig = validate_ig_username(m_ig.group(1)).lower()
            add(direct_ig)
        m_tap = re.search(r"taplink\.cc/([A-Za-z0-9._]{2,30})", w, re.IGNORECASE)
        if m_tap and validate_ig_username(m_tap.group(1)):
            tap_u = validate_ig_username(m_tap.group(1)).lower()
            web_stems.add(tap_u)
            add(tap_u)
        m_dom = re.search(
            r"https?://(?:www\.)?([a-z0-9\-]+)\.(uz|com|ru|rest|cafe|bar|pro)",
            w,
            re.IGNORECASE,
        )
        if m_dom:
            raw_host = m_dom.group(1).lower()
            tld = m_dom.group(2).lower()
            stem = raw_host.replace("-", "")
            if stem not in DIRECTORY_DOMAINS and not any(d in stem for d in DIRECTORY_DOMAINS):
                web_stems.add(stem)
                add(f"{stem}.{tld}")
                add(f"{stem}.uz")
                add(f"{stem}_uz")
                add(f"{stem}uz")
                add(stem)
                if "-" in raw_host:
                    under_h = raw_host.replace("-", "_")
                    web_stems.add(under_h)
                    add(under_h)

    raw_words = [
        w for w in _extract_brand_words(place.name, y_as_i=False, kh_mode=True)
        if w not in ("the", "na", "v", "and") and w not in STREET_STOPWORDS
    ]
    distinct_words = [
        w for w in raw_words
        if w not in GENERIC_ONLY_NAMES and w not in STREET_STOPWORDS
    ]
    if raw_words:
        joined2 = "".join(raw_words[:2])
        under2 = "_".join(raw_words[:2])
        dot2 = ".".join(raw_words[:2])
        if len(raw_words) >= 2:
            add(joined2)
            add(under2)
            add(dot2)
            add(f"{joined2}.uz")
            add(f"{joined2}_uz")
            add(f"{under2}_tashkent")
            add(f"{joined2}_tashkent")
    if distinct_words:
        w1 = distinct_words[0]
        if len(w1) >= 4:
            add(f"{w1}_uz")
            add(f"{w1}.uz")
            add(f"{w1}uz")
            add(f"{w1}_tashkent")
            add(f"{w1}.tashkent")
            add(f"{w1}_restaurant")

    return cands[:5], direct_ig, web_stems


async def rank_instagram_low_followers_leads(places: List[PlaceInfo]) -> List[HighChanceInstaLead]:
    """
    Тщательно находит и 100% ВЕРИФИЦИРУЕТ реальные Instagram-аккаунты бизнесов Ташкента на выбранной локации:
    1. Проверяет прямую ссылку в Google Картах и парсит официальный сайт бизнеса (если указан).
    2. Запускает поисковый пробив "{Название} Ташкент instagram" для точек без прямой ссылки в Картах.
    3. Загружает полную биографию (<meta name="description">), телефон, адрес, число постов и подписчиков.
    4. Жёстко отсекает однофамильцев, личные страницы, пустые профили и другие города/страны.
    5. Сортирует ТОЛЬКО подтверждённые бизнесы с малым числом подписчиков (до 20 000 подп.) от самой острой боли к меньшей.
    """
    sem_search = asyncio.Semaphore(14)
    sem_ig = asyncio.Semaphore(36)

    place_cands: Dict[str, Tuple[List[str], Optional[str], set[str], set[str]]] = {}
    all_handles: List[str] = []

    async with httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
        follow_redirects=True,
        timeout=3.2,
    ) as search_client:

        async def _discover_for_place(p: PlaceInfo):
            cs, direct_ig, _ = _build_fast_place_ig_candidates(p)
            web_scraped_igs: List[str] = []
            yahoo_hits: List[str] = []

            # Шаг 1: Если у бизнеса указан собственный сайт (не справочник), ищем ссылку на Instagram прямо на сайте
            w = (p.website or "").strip()
            if w and not direct_ig and "instagram.com" not in w.lower():
                if not any(d in w.lower() for d in DIRECTORY_DOMAINS):
                    try:
                        rw = await search_client.get(w, timeout=2.2)
                        if rw.status_code == 200:
                            for u in _extract_ig_handles_from_html(rw.text)[:2]:
                                web_scraped_igs.append(u)
                                if u not in cs:
                                    cs.insert(0, u)
                    except Exception:
                        pass

            # Шаг 2: Если нет прямой привязки в Картах/сайте, пробиваем через поисковый индекс Ташкента
            if not direct_ig and not web_scraped_igs:
                async with sem_search:
                    try:
                        q = urllib.parse.quote(f"{p.name} Ташкент instagram")
                        r = await search_client.get(f"https://search.yahoo.com/search?p={q}")
                        if r.status_code == 200:
                            for u in _extract_ig_handles_from_html(r.text)[:3]:
                                yahoo_hits.append(u)
                                if u not in cs:
                                    cs.insert(0, u)
                    except Exception:
                        pass

            top_cs = cs[:6]
            place_cands[p.place_id] = (top_cs, direct_ig, set(web_scraped_igs), set(yahoo_hits))
            for u in top_cs:
                if u not in all_handles:
                    all_handles.append(u)

        await asyncio.gather(*[_discover_for_place(p) for p in places])

    verified_profiles: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(
        headers={"User-Agent": "TelegramBot (like TwitterBot)"},
        follow_redirects=True,
        timeout=3.8,
    ) as ig_client:

        async def _fetch_ig_profile(u: str):
            async with sem_ig:
                try:
                    r = await ig_client.get(f"https://www.instagram.com/{u}/")
                    if r.status_code != 200:
                        return
                    og_m = re.search(r'property="og:title"\s+content="([^"]+)"', r.text)
                    desc_m = re.search(r'<meta\s+content="([^"]+)"\s+name="description"', r.text) or re.search(
                        r'<meta\s+name="description"\s+content="([^"]+)"', r.text
                    )
                    og_desc_m = re.search(r'property="og:description"\s+content="([^"]+)"', r.text)
                    if not og_m:
                        return
                    raw_title = html.unescape(og_m.group(1)).strip()
                    clean_title = raw_title.split("(@")[0].split("•")[0].strip()
                    full_meta = html.unescape(desc_m.group(1)).strip() if desc_m else ""
                    og_desc = html.unescape(og_desc_m.group(1)).strip() if og_desc_m else full_meta

                    flw = _parse_followers(og_desc)
                    if flw is None:
                        return
                    m_flwing = re.search(r"([\d.,]+)\s*Following", og_desc, re.I)
                    flwing = int(re.sub(r"[^\d]", "", m_flwing.group(1)) or "0") if m_flwing else 0
                    m_posts = re.search(r"([\d.,]+)\s*Posts", og_desc, re.I)
                    posts = int(re.sub(r"[^\d]", "", m_posts.group(1)) or "0") if m_posts else 0

                    bio_text = ""
                    if "on Instagram:" in full_meta:
                        bio_text = full_meta.split("on Instagram:", 1)[1].strip().strip('"')

                    verified_profiles[u] = {
                        "username": u,
                        "title": clean_title,
                        "raw_title": raw_title,
                        "bio": bio_text,
                        "followers": flw,
                        "following": flwing,
                        "posts": posts,
                    }
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_ig_profile(u) for u in all_handles])

    leads: List[HighChanceInstaLead] = []
    seen_ig_handles: set[str] = set()

    for p in places:
        if p.place_id not in place_cands:
            continue
        cs, direct_ig, web_scraped_igs, yahoo_hits = place_cands[p.place_id]
        distinct_b_words = [
            w for w in _extract_brand_words(p.name, y_as_i=False, kh_mode=True)
            if len(w) >= 3 and w not in STREET_STOPWORDS and w not in GENERIC_ONLY_NAMES
        ]
        p_digits = re.sub(r"\D", "", p.phone or "")[-7:] if p.phone else ""

        best_match: Optional[Tuple[Dict[str, Any], str]] = None
        best_sc = -999.0

        for u in cs:
            prof = verified_profiles.get(u)
            if not prof:
                continue
            flw = prof["followers"]
            posts = prof["posts"]
            flwing = prof["following"]
            title_bio_low = f"{prof['title']} {prof['bio']}".lower()
            combined_low = f"{u} {title_bio_low}"
            title_lat = _cyr_to_latin(prof["title"].lower(), y_as_i=False, kh_mode=True)
            u_clean = re.sub(r"[._]", "", u)

            is_official_link = (direct_ig and u == direct_ig) or (u in web_scraped_igs)

            # 1. Мгновенный отсев чужих стран/городов и продающихся пустых ников
            if any(fm in combined_low for fm in FOREIGN_MARKERS):
                continue

            if not is_official_link:
                # 2. Живой бизнес-профиль: минимум 40 подписчиков и минимум 5 публикаций
                if flw < 40 or posts < 5:
                    continue
                # 3. У реального бизнеса число исходящих подписок не превышает число подписчиков
                if flw < 1500 and flwing > flw:
                    continue
                # 4. Запрет личных блогов, свадебных аккаунтов и нерелевантных магазинов
                if any(
                    pers in combined_low
                    for pers in ("🤵", "👰", "student", "mum", "личн", "блог", "спортивное питание", "sport ozuqalari")
                ):
                    continue

            has_phone_match = bool(p_digits and len(p_digits) == 7 and p_digits in re.sub(r"\D", "", prof["bio"]))
            has_uz_geo = any(g in combined_low for g in UZ_GEO_MARKERS) or u.endswith(("uz", "_uz", ".uz", "tashkent"))
            has_biz_category = any(bm in title_bio_low for bm in REAL_BIZ_CATEGORY_WORDS)
            has_brand = any(bw in title_lat or bw in prof["title"].lower() or bw in u_clean for bw in distinct_b_words)
            has_cyrillic = bool(re.search(r"[а-яА-ЯёЁ]", title_bio_low))

            if not is_official_link:
                # Если у бизнеса нет уникального слова в названии (например просто 'DENTA CLINIC'),
                # требуем 100% совпадения телефона из Google Карт!
                if not distinct_b_words:
                    if not has_phone_match:
                        continue
                else:
                    if not has_brand:
                        continue
                # Обязательно совпадение телефона ИЛИ явная категория бизнеса в шапке/описании
                if not (has_phone_match or has_biz_category):
                    continue
                # Обязательно явная гео-привязка к Ташкенту/Узбекистану или совпадение телефона Ташкента!
                if not (has_phone_match or has_uz_geo):
                    continue

            if is_official_link:
                sc = 1000.0
                v_badge = "✅ Привязан к Картам / сайту"
            elif has_phone_match:
                sc = 800.0
                v_badge = "✅ Совпал телефон в профиле"
            elif u in yahoo_hits and has_uz_geo:
                sc = 550.0
                v_badge = "✅ Подтверждён (Ташкент)"
            else:
                sc = 350.0
                v_badge = "✅ Проверен по биографии"

            if has_uz_geo:
                sc += 150.0
            if has_biz_category:
                sc += 120.0
            if sc > best_sc:
                best_sc = sc
                best_match = (prof, v_badge)

        if best_match:
            prof, v_badge = best_match
            u = prof["username"].lower()
            if u in seen_ig_handles:
                continue
            flw = prof["followers"]
            posts = prof["posts"]
            # Отсекаем раскрученные аккаунты (> 20 000 подписчиков) — оставляем только тех, кому реально нужен трафик!
            if flw > 20_000:
                continue

            seen_ig_handles.add(u)
            if flw <= 1_200:
                score = 99 - int(flw / 200)
                tier_badge = "🔥 Острая боль: Очень мало подписчиков"
                pain_text = f"Всего {flw} подп. ({posts} публ.) — острая нехватка трафика в Инсту"
            elif flw <= 3_500:
                score = 91 - int((flw - 1200) / 400)
                tier_badge = "🔥 Мало подписчиков (Слабый поток)"
                pain_text = f"Всего {_format_followers_badge(flw)} — слабый перелив гостей из зала"
            elif flw <= 8_500:
                score = 84 - int((flw - 3500) / 800)
                tier_badge = "⚡️ Потенциал роста (Добить до 10K+)"
                pain_text = f"Сейчас {_format_followers_badge(flw)} — легко продать добивку до 10K+"
            else:
                score = 76
                tier_badge = "📌 Усиление потока в Instagram"
                pain_text = f"Профиль {_format_followers_badge(flw)} — перевод гостей со столов в подписчики"

            primary_url = f"https://instagram.com/{u}?igsh={DEFAULT_IGSH_TOKEN}"
            deep_url = f"instagram://user?username={u}"
            profile_web_url = f"https://www.instagram.com/{u}/"

            leads.append(
                HighChanceInstaLead(
                    place=p,
                    ig_username=u,
                    ig_title=prof["title"],
                    followers=flw,
                    posts=posts,
                    score=score,
                    tier_badge=tier_badge,
                    pain_text=pain_text,
                    verification_badge=v_badge,
                    primary_nfc_url=primary_url,
                    deep_link_url=deep_url,
                    profile_web_url=profile_web_url,
                )
            )

    # Сортируем от самого малого числа подписчиков (наивысший шанс продажи!) к большему
    leads.sort(
        key=lambda item: (
            item.score,
            -item.followers,
        ),
        reverse=True,
    )
    return leads
