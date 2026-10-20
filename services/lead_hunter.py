import hashlib
import json
import logging
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx

from config import config
from services.ai_sales import clean_address
from services.google_places import PlaceInfo, build_review_url, clean_place_id, google_places_service
from services.keyless_places import keyless_places_service

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SEEN_FILE = DATA_DIR / "seen_leads.json"

# ==============================================================================
# Глубокая матрица ниш и улиц Ташкента (сотни уникальных комбинаций без повторов)
# ==============================================================================
HUNT_NICHES: Dict[str, Dict[str, Any]] = {
    "rest": {
        "title": "🍽 Рестораны и Гастробары",
        "short": "Рестораны",
        "queries": [
            "ресторан",
            "стейк хаус ресторан",
            "семейный ресторан",
            "итальянский ресторан пиццерия",
            "гастробар бистро",
            "восточный ресторан",
            "европейский ресторан",
            "мясной ресторан гриль",
            "азиатский ресторан суши",
            "турецкий ресторан",
            "кавказский грузинский ресторан",
            "ресторан терраса",
        ],
    },
    "bar": {
        "title": "🍸 Бары, Пабы и Лаунжи",
        "short": "Бары / Лаунжи",
        "queries": [
            "бар паб",
            "лаунж бар",
            "кальянная hookah lounge",
            "коктейльный бар",
            "крафтовый паб пивной бар",
            "караоке бар клуб",
            "винный бар гастробар",
            "вечерний лаунж терраса",
        ],
    },
    "coffee": {
        "title": "☕️ Кофейни и Кондитерские",
        "short": "Кофейни",
        "queries": [
            "кофейня",
            "specialty coffee кофейня",
            "кафе кондитерская",
            "пекарня кофейня десерты",
            "кофейня завтраки бранч",
            "уютное кафе",
            "арт кафе кофейня",
            "чайная кофейня",
        ],
    },
    "clinic": {
        "title": "🦷 Стоматологии и Клиники",
        "short": "Клиники / Стоматологии",
        "queries": [
            "стоматология",
            "dental clinic стоматологическая клиника",
            "медицинский центр клиника",
            "клиника косметологии",
            "частная клиника диагностика",
            "клиника эстетической медицины",
            "ортодонтия имплантация стоматология",
            "дерматология лазерная клиника",
        ],
    },
    "beauty": {
        "title": "💈 Барбершопы и Салоны",
        "short": "Салоны / Барбершопы",
        "queries": [
            "барбершоп barbershop",
            "салон красоты",
            "beauty salon студия красоты",
            "спа салон массаж",
            "ногтевая студия маникюр",
            "студия окрашивания волос салон",
            "броу бар студия макияжа",
            "мужская парикмахерская барбершоп",
        ],
    },
    "all": {
        "title": "🔥 Все топовые ниши (Авто-микс)",
        "short": "Все ниши",
        "queries": [
            "ресторан гастробар",
            "бар лаунж паб",
            "кофейня кондитерская",
            "стоматология dental clinic",
            "барбершоп салон красоты",
            "стейк хаус гриль ресторан",
            "кальянная лаунж",
            "клиника косметологии",
            "итальянский ресторан кафе",
            "кофейня завтраки",
        ],
    },
}

HUNT_DISTRICTS: Dict[str, Dict[str, Any]] = {
    "mirabad": {
        "title": "🏙 Мирабад (Т. Шевченко / Ойбек / Шахрисабз)",
        "short": "Мирабад",
        "zones": [
            "улица Тараса Шевченко Ташкент",
            "улица Ойбек Ташкент",
            "улица Шахрисабз Ташкент",
            "улица Нукус Ташкент",
            "улица Афросиаб Ташкент",
            "улица Саид Барака Ташкент",
            "улица Фидокор Ташкент",
            "улица Мирабад Ташкент",
            "улица Авлиёота Ташкент",
            "Госпитальный рынок Мирабад Ташкент",
            "улица Султонали Машхадий Ташкент",
            "улица Якуба Коласа Ташкент",
            "улица Чехова Мирабад Ташкент",
            "Северный вокзал Мирабад Ташкент",
        ],
    },
    "yakkasaray": {
        "title": "🏛 Яккасарай (Ш. Руставели / Бабура / Seoul Mun)",
        "short": "Яккасарай",
        "zones": [
            "улица Шота Руставели Ташкент",
            "улица Бабура Ташкент",
            "Seoul Mun Набережная Ташкент",
            "улица Мукими Ташкент",
            "улица Кичик Бешагач Ташкент",
            "улица Усмана Носира Ташкент",
            "улица Абдуллы Каххара Ташкент",
            "улица Глинки Яккасарай Ташкент",
            "улица Братислава Ташкент",
            "Парк Бабура Яккасарай Ташкент",
            "улица Кушбеги Ташкент",
            "улица Богибустон Ташкент",
            "улица Малая кольцевая Яккасарай Ташкент",
        ],
    },
    "yunusabad": {
        "title": "🌳 Юнусабад (Ц-4 / Ц-5 / Амир Темур)",
        "short": "Юнусабад",
        "zones": [
            "проспект Амира Темура Ташкент",
            "Ц-4 Юнусабад Ташкент",
            "Ц-5 Юнусабад Ташкент",
            "Ц-6 Юнусабад Ташкент",
            "Бодомзор Юнусабад Ташкент",
            "улица Осиё Новомосковская Ташкент",
            "улица Чингиза Айтматова Ташкент",
            "улица Ниёзбек Йули Ташкент",
            "улица Шахристанская Юнусабад Ташкент",
            "Мега Планет Юнусабад Ташкент",
            "улица Ахмада Дониша Ташкент",
            "Минор Юнусабад Ташкент",
        ],
    },
    "mirzo": {
        "title": "✨ Мирзо-Улугбек (Ц-1 / Паркентская / Буюк Ипак Йули)",
        "short": "Мирзо-Улугбек",
        "zones": [
            "Ц-1 Мирзо Улугбек Ташкент",
            "Ц-2 Ташкент",
            "улица Паркентская Ташкент",
            "улица Буюк Ипак Йули Максим Горький Ташкент",
            "улица Салар Ташкент",
            "улица Мустакиллик Дархан Ташкент",
            "улица Хамида Алимджана Ташкент",
            "улица Олтинтепа Ташкент",
            "улица Зиёлилар Ташкент",
            "улица Мирзо Улугбека Ташкент",
            "улица Кары Ниязова Ташкент",
            "улица Аккурган Ташкент",
        ],
    },
    "center": {
        "title": "🎡 Tashkent City / Шайхантахур / Чиланзар",
        "short": "Центр / Сити / Чиланзар",
        "zones": [
            "Tashkent City Ташкент",
            "улица Алишера Навои Ташкент",
            "улица Лабзак Ташкент",
            "проспект Бунёдкор Чиланзар Ташкент",
            "улица Катартал Чиланзар Ташкент",
            "улица Чопонота Чиланзар Ташкент",
            "улица Гагарина Чиланзар Ташкент",
            "улица Фурката Ташкент",
            "улица Самарканд Дарвоза Ташкент",
            "улица Нурафшон Ташкент",
            "улица Заркайнар Ташкент",
            "улица Мукими Чиланзар Ташкент",
        ],
    },
    "auto": {
        "title": "🚀 Весь Ташкент (Самые денежные улицы)",
        "short": "Весь Ташкент",
        "zones": [
            "улица Тараса Шевченко Ташкент",
            "улица Шота Руставели Ташкент",
            "улица Ойбек Ташкент",
            "Ц-1 Мирзо Улугбек Ташкент",
            "Tashkent City Ташкент",
            "Seoul Mun Ташкент",
            "Ц-4 Юнусабад Ташкент",
            "улица Шахрисабз Ташкент",
            "улица Бабура Ташкент",
            "улица Нукус Ташкент",
            "улица Лабзак Ташкент",
            "Дархан Хамида Алимджана Ташкент",
            "улица Саид Барака Ташкент",
            "улица Паркентская Ташкент",
            "улица Катартал Ташкент",
            "улица Осиё Ташкент",
        ],
    },
}

STOP_NAME_WORDS = (
    "столовая", "ошхона", "oshxona", "шаурма", "хот-дог", "хот дог", "самса",
    "пайнет", "paynet", "аптека", "dorixona", "магазин", "супермаркет", "корзинка",
    "korzinka", "makro", "havas", "школа", "детский сад", "колледж", "университет",
    "общежитие", "банк", "банкомат", "азс", "заправка", "автомойка", "шиномонтаж",
    "рынок", "базар", "кладбище", "мечеть", "церковь", "почта", "нотариус",
    "государственн", "поликлиника №", "больница №", "жэк", "махалля", "киоск",
)

PRIME_STREETS = (
    "шевченко", "shevchenko", "ойбек", "oybek", "шахрисабз", "shahrisabz",
    "руставели", "rustaveli", "бабур", "bobur", "нукус", "nukus", "афросиаб",
    "afrosiyob", "саид барака", "said baraka", "tashkent city", "сеул", "seoul",
    "амир темур", "amir temur", "ц-1", "ц-4", "дархан", "лабзак", "ниёзбек",
)

HIGH_CHECK_KEYWORDS = (
    "ресторан", "restaurant", "ristorante", "steak", "стейк", "gastro", "гастро",
    "lounge", "лаунж", "bar", "бар", "pub", "паб", "coffee", "кофейня", "roasters",
    "bistro", "бистро", "dental", "стоматолог", "clinic", "клиника", "med",
    "barber", "барбер", "beauty", "салон", "spa", "спа",
)


@dataclass
class ScoredLead:
    place: PlaceInfo
    score: int                  # Точный скоринг 60..98 (без одинаковых 99 у всех подряд)
    tier_badge: str             # Категория лида
    why_hot: str                # Конкретный коммерческий расчёт и аргумент
    needed_5stars: int          # Сколько 5⭐ нужно до целевого порога
    target_rating: float        # Целевой рейтинг (4.5, 4.7 или 4.8)


def normalize_name_key(name: str) -> str:
    """Нормализует название для защиты от дублей одного и того же бренда/точки."""
    cleaned = re.sub(r"[^a-zа-яё0-9]+", "", (name or "").lower())
    return cleaned[:24]


def calc_5stars_needed(rating: Optional[float], reviews: Optional[int]) -> Tuple[int, float]:
    """
    Точная математика: сколько 5-звёздочных отзывов подряд нужно заведению,
    чтобы выйти на следующий психологический порог в Гугл Картах (4.5, 4.7 или 4.8).
    """
    if rating is None or not reviews or reviews <= 0:
        return (25, 4.8)

    if rating < 4.45:
        target = 4.5
    elif rating < 4.65:
        target = 4.7
    elif rating < 4.78:
        target = 4.8
    else:
        return (25, 4.9)

    raw_x = (reviews * (target - rating)) / (5.0 - target)
    needed = max(5, int(math.ceil(raw_x)))
    return (needed, target)


def evaluate_lead_potential(item: Dict[str, Any]) -> Optional[ScoredLead]:
    """
    Многофакторная система оценки лида HHtap (0..98 баллов с точным дифференциалом):
    1. Острота боли рейтинга / дефицита отзывов (до 55 баллов)
    2. Скорость исправления ситуации нашей NFC-картой (до 18 баллов)
    3. Платёжеспособность формата (ресторан/бар/клиника/кофейня — до 13 баллов)
    4. Престижность улицы и трафик в Гугл Картах (до 12 баллов)
    """
    raw_id = item.get("place_id", "").strip()
    clean_id = clean_place_id(raw_id)
    if not clean_id:
        return None

    name = (item.get("name") or "").strip()
    address = (item.get("address") or "Ташкент").strip()
    status = item.get("business_status") or "OPERATIONAL"

    if not name or status != "OPERATIONAL":
        return None

    low_name = name.lower()
    low_addr = address.lower()
    if any(w in low_name for w in STOP_NAME_WORDS):
        return None

    rating = item.get("rating")
    reviews = item.get("user_ratings_total")

    # Отсекаем раскачанных гигантов (>380 отзывов при высоком рейтинге) и убитые точки (<3.2)
    if rating is not None and reviews is not None:
        if reviews > 380 and rating >= 4.7:
            return None
        if rating < 3.2:
            return None

    needed_5, target_r = calc_5stars_needed(rating, reviews)
    street = clean_address(address)
    loc_label = f"на {street}" if street else "в плотной локации"

    # Фактор 3: Премиальность формата (чек и наличие папки со счётом / ресепшена)
    format_bonus = 8
    if any(k in low_name for k in HIGH_CHECK_KEYWORDS):
        format_bonus = 13

    # Фактор 4: Денежность улицы
    street_bonus = 6
    if any(st in low_addr for st in PRIME_STREETS):
        street_bonus = 11
    elif street:
        street_bonus = 8

    # Микро-вариация (-1..+1) от хэша place_id для честного ранжирования без одинаковых цифр
    tie_breaker = int(hashlib.md5(clean_id.encode("utf-8")).hexdigest()[:2], 16) % 3 - 1

    # --------------------------------------------------------------------------
    # СЕГМЕНТ А: «Огромный потенциал, мало отзывов» (4.5 .. 5.0 ⭐️, но всего 3..60 отзывов)
    # --------------------------------------------------------------------------
    if rating is not None and rating >= 4.5 and (reviews is None or reviews <= 60):
        rev_count = reviews or 0
        # Чем меньше отзывов у хорошей точки (особенно 6..32), тем выше ценность первых 50 пятёрок
        if 6 <= rev_count <= 25:
            pain_score = 54
        elif 26 <= rev_count <= 42:
            pain_score = 50
        elif rev_count < 6:
            pain_score = 48
        else:
            pain_score = 45

        # Бонус за высокий текущий рейтинг (владелец трясётся над репутацией)
        speed_bonus = 17 if rating >= 4.7 else 14
        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(72, min(98, raw_score))

        tier_badge = "🚀 Огромный потенциал (Мало отзывов)"
        if rev_count > 0:
            to_100 = max(35, 100 - rev_count)
            shifts = max(3, round(to_100 / 12))
            why_hot = (
                f"Высокий уровень ({rating:.1f}⭐️), но в Гугле всего {rev_count} отзывов — "
                f"точка {loc_label} теряется на фоне соседей, а 1–2 случайные тройки сразу собьют рейтинг. "
                f"NFC-карта к счёту даст +{to_100} пятёрок (до первой сотни) за {shifts}–{shifts + 2} смен."
            )
        else:
            why_hot = (
                f"Перспективная точка {loc_label} с пустой карточкой в Гугле. "
                f"С NFC-картой к счёту соберут первые 50–100 живых пятёрок в первый же месяц."
            )

    # --------------------------------------------------------------------------
    # СЕГМЕНТ Б: «Острая боль — Просаженный рейтинг» (3.4 .. 4.4 ⭐️ при 8..180 отзывах)
    # --------------------------------------------------------------------------
    elif rating is not None and 3.4 <= rating <= 4.4 and reviews is not None and 8 <= reviews <= 180:
        # Идеальный клиент: рейтинг 3.9–4.3 (чуть ниже психологических 4.5⭐️) и нужно 10–55 пятёрок
        if 3.9 <= rating <= 4.3:
            pain_score = 55
        elif 3.6 <= rating < 3.9:
            pain_score = 51
        else:
            pain_score = 47

        # Чем быстрее окупается (меньше смен нужно до 4.5⭐️), тем выше балл
        if needed_5 <= 22:
            speed_bonus = 18  # Исправляется за 2 смены!
        elif needed_5 <= 45:
            speed_bonus = 15  # Исправляется за 3-4 смены
        elif needed_5 <= 75:
            speed_bonus = 12
        else:
            speed_bonus = 8

        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(74, min(98, raw_score))

        shifts = max(2, round(needed_5 / 12))
        tier_badge = "🔥 Острая боль (Просажен рейтинг)"
        why_hot = (
            f"Точка {loc_label} теряет гостей из поиска из-за оценки {rating:.1f}⭐️ ({reviews} отз.). "
            f"Математика простая: чтобы выйти на {target_r:.1f}⭐️, нужно ровно ~{needed_5} пятёрок "
            f"— с нашей NFC-картой к счёту они закроют это за {shifts}–{shifts + 2} смены."
        )

    # --------------------------------------------------------------------------
    # СЕГМЕНТ В: «Точка роста — Борьба за ТОП улицы» (4.45 .. 4.75 ⭐️ при 61..240 отзывах)
    # --------------------------------------------------------------------------
    elif rating is not None and 4.4 < rating <= 4.75 and reviews is not None and 61 <= reviews <= 260:
        pain_score = 43 if rating <= 4.6 else 39
        speed_bonus = 13 if needed_5 <= 50 else 10
        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(68, min(89, raw_score))

        shifts = max(3, round(needed_5 / 12))
        tier_badge = "💎 Точка роста (Обгон конкурентов)"
        why_hot = (
            f"Активное заведение {loc_label} ({rating:.1f}⭐️, {reviews} отз.), но до порога {target_r:.1f}⭐️ "
            f"и отрыва от соседей не хватает ~{needed_5} пятёрок (~{shifts} смен с NFC-картой вместо QR)."
        )

    # --------------------------------------------------------------------------
    # СЕГМЕНТ Г: Прочие рабочие точки
    # --------------------------------------------------------------------------
    else:
        raw_score = 36 + format_bonus + street_bonus + tie_breaker
        score = max(62, min(82, raw_score))
        tier_badge = "📈 Автоматизация потока отзывов"
        r_info = f"{rating:.1f}⭐️ ({reviews} отз.) " if (rating and reviews) else ""
        why_hot = (
            f"Заведение {r_info}{loc_label}: 95% довольных гостей уходят после счёта молча, "
            f"а бумажные QR-коды сканируют меньше 2% посетителей."
        )

    place = PlaceInfo(
        place_id=clean_id,
        name=name,
        address=address,
        rating=rating,
        user_ratings_total=reviews,
        review_url=build_review_url(clean_id),
        google_maps_url=f"https://www.google.com/maps/place/?q=place_id:{clean_id}",
        latitude=item.get("lat"),
        longitude=item.get("lng"),
    )

    return ScoredLead(
        place=place,
        score=score,
        tier_badge=tier_badge,
        why_hot=why_hot,
        needed_5stars=needed_5,
        target_rating=target_r,
    )


class LeadHunterService:
    """
    Автономный охотник за лидами HHtap с жёсткой анти-дубль памятью:
    - Хранит как place_id, так и нормализованные имена заведений в data/seen_leads.json.
    - Никогда не отправляет повторно те заведения, которые уже были показаны.
    - Если по первым подзапросам попались знакомые точки, сам углубляется по улицам района,
      пока не наберёт свежую пятёрку уникальных лидов.
    """

    def __init__(self) -> None:
        self._seen_ids: Set[str] = set()
        self._seen_names: Set[str] = set()
        self._load_seen()
        self._query_decks: Dict[str, List[str]] = {}

    def _load_seen(self) -> None:
        try:
            if SEEN_FILE.exists():
                data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._seen_ids = set(str(x) for x in data.get("ids", []))
                    self._seen_names = set(str(x) for x in data.get("names", []))
                elif isinstance(data, list):
                    self._seen_ids = set(str(x) for x in data)
        except Exception as e:
            logger.warning("Не удалось загрузить seen_leads.json: %s", e)

    def _save_seen(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "ids": list(self._seen_ids)[-5000:],
                "names": list(self._seen_names)[-5000:],
            }
            SEEN_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Не удалось сохранить seen_leads.json: %s", e)

    def is_seen(self, place_id: str, name: str) -> bool:
        if place_id in self._seen_ids:
            return True
        nkey = normalize_name_key(name)
        if nkey and len(nkey) >= 4 and nkey in self._seen_names:
            return True
        return False

    def mark_seen(self, leads: List[ScoredLead]) -> None:
        for lead in leads:
            if lead.place.place_id:
                self._seen_ids.add(lead.place.place_id)
            nkey = normalize_name_key(lead.place.name)
            if nkey and len(nkey) >= 4:
                self._seen_names.add(nkey)
        self._save_seen()

    def reset_seen(self) -> int:
        count = len(self._seen_ids)
        self._seen_ids.clear()
        self._seen_names.clear()
        self._query_decks.clear()
        self._save_seen()
        return count

    def seen_count(self) -> int:
        return len(self._seen_ids)

    def _pick_search_queries(self, niche_key: str, district_key: str, count: int = 2) -> List[str]:
        """Берёт следующие неповторяющиеся комбинации (под-ниша + улица района)."""
        niche = HUNT_NICHES.get(niche_key, HUNT_NICHES["all"])
        district = HUNT_DISTRICTS.get(district_key, HUNT_DISTRICTS["auto"])

        deck_key = f"{niche_key}:{district_key}"
        deck = self._query_decks.get(deck_key)
        if not deck:
            combos = [
                f"{q} {z}"
                for q in niche["queries"]
                for z in district["zones"]
            ]
            random.shuffle(combos)
            deck = combos
            self._query_decks[deck_key] = deck

        picked = []
        while len(picked) < count and deck:
            picked.append(deck.pop())
        return picked

    async def _fetch_raw_query(self, query: str) -> List[Dict[str, Any]]:
        """Запрашивает до 20 карточек по одному поисковому запросу."""
        logger.info("LeadHunter сканирует запрос: '%s'", query)
        if config.has_google_api_key():
            try:
                places = await google_places_service.search_text(query, max_results=10)
                return [
                    {
                        "place_id": p.place_id,
                        "name": p.name,
                        "address": p.address,
                        "rating": p.rating,
                        "user_ratings_total": p.user_ratings_total,
                        "business_status": "OPERATIONAL",
                        "lat": p.latitude,
                        "lng": p.longitude,
                    }
                    for p in places
                ]
            except Exception as e:
                logger.warning("Ошибка Google API при охоте '%s': %s, используем автономный парсер", query, e)
        return await keyless_places_service.search_batch_raw(query, max_results=20)

    async def _ai_refine_top_leads(self, candidates: List[ScoredLead]) -> List[ScoredLead]:
        """
        Быстрый ИИ-аудит через Anymodel (если активен):
        отсеивает случайные нецелевые точки и сортирует лучших по коммерческому потенциалу.
        """
        if not config.has_anymodel_key() or len(candidates) <= 3:
            return candidates

        short_list = []
        for idx, c in enumerate(candidates[:10]):
            r_str = f"{c.place.rating:.1f}⭐️ ({c.place.user_ratings_total or 0} отз.)" if c.place.rating else "без оценки"
            short_list.append(f"{idx}: {c.place.name} | {c.place.address} | {r_str} | Score={c.score}")

        prompt = (
            "Ты — коммерческий директор HHtap в Ташкенте (умные NFC-карты к счёту для сбора 5⭐️ отзывов в Гугл Картах).\n"
            "Кандидаты из парсера:\n"
            + "\n".join(short_list)
            + "\n\nВыбери индексы 5 самых перспективных и платёжеспособных заведений (где есть посадка или ресепшен, хороший чек и острая нужда в 5⭐️ отзывах), "
            "отсеяв уличный фастфуд, госучреждения или случайные магазины. "
            "Ответь ТОЛЬКО JSON-массивом номеров, например: [0, 2, 1, 4, 3]"
        )

        endpoint = f"{config.anymodel_base_url.rstrip('/')}/chat/completions"
        model = config.anymodel_model.strip() or "cx/gpt-6-luna"
        headers = {
            "Authorization": f"Bearer {config.anymodel_api_key.strip()}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 60,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                match = re.search(r"\[[\d,\s]+\]", content)
                if match:
                    indices = json.loads(match.group(0))
                    picked: List[ScoredLead] = []
                    for i in indices:
                        if isinstance(i, int) and 0 <= i < len(candidates) and candidates[i] not in picked:
                            picked.append(candidates[i])
                    for c in candidates:
                        if c not in picked:
                            picked.append(c)
                    return picked
        except Exception as e:
            logger.info("AI-ранжирование пропущено (используем многофакторный скоринг): %s", e)

        return candidates

    async def hunt_best_leads(
        self,
        niche_key: str = "all",
        district_key: str = "auto",
        top_n: int = 5,
    ) -> Tuple[List[ScoredLead], int]:
        """
        Сканирует улицы выбранного района до тех пор, пока не наберёт минимум top_n
        АБСОЛЮТНО НОВЫХ (ранее не показанных) целевых лидов (до 6 глубоких проходов за раз).
        Никогда не возвращает уже показанные заведения.
        """
        scored_fresh: List[ScoredLead] = []
        batch_ids: Set[str] = set()
        batch_names: Set[str] = set()
        total_scanned = 0

        # Делаем до 6 проходов по разным улицам/подкатегориям района, пока не наберём достаточно новых точек
        for attempt in range(6):
            queries_per_step = 2 if attempt == 0 else 1
            queries = self._pick_search_queries(niche_key, district_key, count=queries_per_step)
            if not queries:
                break

            for q in queries:
                raw_items = await self._fetch_raw_query(q)
                total_scanned += len(raw_items)

                for item in raw_items:
                    lead = evaluate_lead_potential(item)
                    if not lead:
                        continue
                    pid = lead.place.place_id
                    nkey = normalize_name_key(lead.place.name)

                    # Жёсткая проверка памяти: ни по place_id, ни по имени точка ещё не показывалась
                    if self.is_seen(pid, lead.place.name):
                        continue
                    if pid in batch_ids or (nkey and nkey in batch_names):
                        continue

                    batch_ids.add(pid)
                    if nkey:
                        batch_names.add(nkey)
                    scored_fresh.append(lead)

            # Как только набрали хотя бы 8 свежих кандидатов для выбора лучших 5 — выходим из цикла
            if len(scored_fresh) >= max(top_n + 3, 8):
                break

        if not scored_fresh:
            return [], total_scanned

        # Сортируем по многофакторному скорингу
        scored_fresh.sort(key=lambda x: x.score, reverse=True)

        # Балансируем топ-кандидатов: чтобы в пятёрке были и «🚀 Мало отзывов», и «🔥 Просажен рейтинг»
        high_potential = [x for x in scored_fresh if "Огромный потенциал" in x.tier_badge]
        rating_pain = [x for x in scored_fresh if "Острая боль" in x.tier_badge]
        others = [x for x in scored_fresh if x not in high_potential and x not in rating_pain]

        balanced_pool: List[ScoredLead] = []
        hp_idx, rp_idx = 0, 0
        while len(balanced_pool) < 10 and (hp_idx < len(high_potential) or rp_idx < len(rating_pain)):
            if hp_idx < len(high_potential):
                balanced_pool.append(high_potential[hp_idx])
                hp_idx += 1
            if rp_idx < len(rating_pain) and len(balanced_pool) < 10:
                balanced_pool.append(rating_pain[rp_idx])
                rp_idx += 1

        for o in others:
            if len(balanced_pool) < 10:
                balanced_pool.append(o)

        balanced_pool.sort(key=lambda x: x.score, reverse=True)
        refined = await self._ai_refine_top_leads(balanced_pool[:10])
        selected = refined[:top_n]
        selected.sort(key=lambda x: x.score, reverse=True)

        # Записываем только те 5 заведений, которые реально отправляем в карусель пользователю
        self.mark_seen(selected)

        return selected, total_scanned


lead_hunter_service = LeadHunterService()
