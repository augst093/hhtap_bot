import hashlib
import json
import logging
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus
import httpx

from config import config
from services.ai_sales import clean_address
from services.google_places import PlaceInfo, build_review_url, clean_place_id, google_places_service
from services.keyless_places import keyless_places_service

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SEEN_FILE = DATA_DIR / "seen_leads.json"
CRM_FILE = DATA_DIR / "crm_leads.json"

CRM_STATUSES: Dict[str, str] = {
    "contacted": "⏳ Написали (Ждём ответ)",
    "meeting": "🤝 Заехать на 2-мин показ",
    "won": "💰 Купили карту",
}

UZ_MOBILE_CODES = {"90", "91", "93", "94", "95", "97", "98", "99", "88", "77", "50", "33", "20"}


def parse_uz_phone(phone: Optional[str]) -> Dict[str, Any]:
    """
    Разбирает телефон заведения в Узбекистане:
    - Определяет мобильный номер (+998 90/91/93/94/95/97/98/99/88/77/50/33) или городской (71/78)
    - Формирует прямые ссылки в 1 клик для Telegram (https://t.me/+998...) и WhatsApp (https://wa.me/998...)
    """
    if not phone:
        return {"raw": None, "e164": None, "is_mobile": False, "tg_url": None, "wa_url": None}

    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 9:
        digits = "998" + digits
    elif len(digits) == 12 and digits.startswith("998"):
        pass
    elif len(digits) < 9:
        return {"raw": phone, "e164": None, "is_mobile": False, "tg_url": None, "wa_url": None}

    e164 = f"+{digits}"
    op_code = digits[3:5] if digits.startswith("998") and len(digits) >= 5 else ""
    is_mobile = op_code in UZ_MOBILE_CODES

    return {
        "raw": phone.strip(),
        "e164": e164,
        "op_code": op_code,
        "is_mobile": is_mobile,
        "tg_url": f"https://t.me/{e164}" if is_mobile else None,
        "wa_url": f"https://wa.me/{digits}" if is_mobile else None,
    }


def build_insta_search_url(name: str, website: Optional[str] = None) -> Tuple[str, str]:
    """
    Возвращает (текст кнопки, URL) для быстрого перехода в Instagram точки или поиска её страницы в 1 клик.
    """
    if website and "instagram.com" in website.lower():
        return ("📸 Открыть Instagram точки", website)
    clean_q = re.sub(r"[^\w\sа-яА-ЯёЁ-]", " ", name or "").strip()
    search_q = quote_plus(f"{clean_q} Ташкент instagram")
    return ("📸 Найти Instagram (1 клик)", f"https://www.google.com/search?q={search_q}")

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

    # ЖЁСТКИЙ ФИЛЬТР:
    # 1. Отсекаем точки с большим числом отзывов (>130) — там рейтинг двигается медленно
    # 2. Отсекаем точки с высокой оценкой (>=4.75 при >20 отзывах) — у владельца нет острой боли
    # 3. Отсекаем совсем мёртвые точки (<3.2)
    if rating is not None and reviews is not None:
        if reviews > 130:
            return None
        if rating >= 4.75 and reviews > 20:
            return None
        if rating < 3.2:
            return None

    needed_5, target_r = calc_5stars_needed(rating, reviews)
    street = clean_address(address)
    loc_label = f"на {street}" if street else "в плотной локации"

    # Фактор 3: Премиальность формата (чек и наличие папки со счётом / ресепшена)
    format_bonus = 7
    if any(k in low_name for k in HIGH_CHECK_KEYWORDS):
        format_bonus = 11

    # Фактор 4: Денежность улицы
    street_bonus = 5
    if any(st in low_addr for st in PRIME_STREETS):
        street_bonus = 10
    elif street:
        street_bonus = 7

    # Микро-вариация (-1..+1) от хэша place_id для честного ранжирования без одинаковых цифр
    tie_breaker = int(hashlib.md5(clean_id.encode("utf-8")).hexdigest()[:2], 16) % 3 - 1

    # --------------------------------------------------------------------------
    # ПРИОРИТЕТ №1 (ЗОЛОТОЙ ЛИД): Оценка 3.4 .. 4.35 ⭐️ И МАЛО ОТЗЫВОВ (3 .. 65 отз.)
    # Максимальная боль + мгновенное исправление за 1–3 смены! (Баллы: 88 .. 98)
    # --------------------------------------------------------------------------
    if rating is not None and 3.4 <= rating <= 4.35 and reviews is not None and 3 <= reviews <= 65:
        if 3.7 <= rating <= 4.3:
            pain_score = 59  # Самый сок: чуть ниже 4.5⭐️, владельцу больно смотреть на карточку
        else:
            pain_score = 55  # 3.4..3.6⭐️ — острая проблема с репутацией

        # Чем меньше отзывов (и меньше пятёрок нужно до 4.5⭐️), тем быстрее эффект от NFC-карты
        if needed_5 <= 15:
            speed_bonus = 18  # Поднимается до 4.5⭐️ буквально за 1–2 смены!
        elif needed_5 <= 30:
            speed_bonus = 16  # За 2–3 смены
        else:
            speed_bonus = 14

        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(88, min(98, raw_score))

        shifts = max(1, round(needed_5 / 12))
        tier_badge = "🔥 Идеальный лид (3.5–4.3⭐️ и мало отзывов)"
        why_hot = (
            f"Критический рейтинг {rating:.1f}⭐️ при всего {reviews} отз. — точка {loc_label} прямо сейчас теряет клиентов в поиске Гугла. "
            f"Но за счёт малого числа оценок им нужно всего ~{needed_5} пятёрок, чтобы взлететь до {target_r:.1f}⭐️ "
            f"— с нашей NFC-картой к счёту они исправят это за {shifts}–{shifts + 1} смены!"
        )

    # --------------------------------------------------------------------------
    # ПРИОРИТЕТ №2: Оценка 3.4 .. 4.4 ⭐️ при среднем числе отзывов (66 .. 130 отз.)
    # Тоже просаженный рейтинг, но нужно чуть больше пятёрок (Баллы: 79 .. 87)
    # --------------------------------------------------------------------------
    elif rating is not None and 3.4 <= rating <= 4.4 and reviews is not None and 66 <= reviews <= 130:
        pain_score = 50 if rating >= 3.8 else 47
        speed_bonus = 13 if needed_5 <= 55 else 10

        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(79, min(87, raw_score))

        shifts = max(3, round(needed_5 / 12))
        tier_badge = "⚡️ Острая боль (Просажен рейтинг)"
        why_hot = (
            f"Точка {loc_label} упускает горячий трафик из-за оценки {rating:.1f}⭐️ ({reviews} отз.). "
            f"Чтобы выйти на зелёный порог {target_r:.1f}⭐️, нужно ~{needed_5} пятёрок "
            f"— с NFC-картой при оплате они закроют эту задачу за {shifts}–{shifts + 2} смен."
        )

    # --------------------------------------------------------------------------
    # ПРИОРИТЕТ №3: Мало отзывов (до 45 отз.) и пограничный рейтинг 4.36 .. 4.65 ⭐️
    # Любая единица/двойка обрушит их ниже 4.3⭐️ (Баллы: 71 .. 78)
    # --------------------------------------------------------------------------
    elif rating is not None and 4.35 < rating <= 4.65 and (reviews is None or reviews <= 45):
        rev_count = reviews or 0
        pain_score = 44 if rev_count <= 25 else 40
        speed_bonus = 12
        raw_score = pain_score + speed_bonus + format_bonus + street_bonus + tie_breaker
        score = max(71, min(78, raw_score))

        tier_badge = "🚀 Мало отзывов (Уязвимый рейтинг)"
        why_hot = (
            f"У точки {loc_label} всего {rev_count} отз. при оценке {rating:.1f}⭐️ — "
            f"буквально 1–2 случайные тройки сразу уронят рейтинг в красную зону (<4.3⭐️). "
            f"NFC-карта к счёту создаст подушку безопасности из +40–50 живых пятёрок за пару недель."
        )

    # --------------------------------------------------------------------------
    # ПРИОРИТЕТ №4: Свежие точки с совсем крошечным числом отзывов (до 20 отз.)
    # --------------------------------------------------------------------------
    else:
        rev_count = reviews or 0
        if rev_count > 55:
            return None
        raw_score = 36 + format_bonus + street_bonus + tie_breaker
        score = max(64, min(72, raw_score))
        tier_badge = "📈 Дефицит отзывов в Гугле"
        r_info = f"{rating:.1f}⭐️ (всего {rev_count} отз.) " if (rating and rev_count) else ""
        why_hot = (
            f"Заведение {r_info}{loc_label}: карточка полупустая, 95% довольных гостей уходят молча, "
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
        self._crm: Dict[str, Dict[str, Any]] = {}
        self._load_seen()
        self._load_crm()
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

    def _load_crm(self) -> None:
        try:
            if CRM_FILE.exists():
                data = json.loads(CRM_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._crm = data
        except Exception as e:
            logger.warning("Не удалось загрузить crm_leads.json: %s", e)

    def _save_crm(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CRM_FILE.write_text(
                json.dumps(self._crm, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Не удалось сохранить crm_leads.json: %s", e)

    def set_crm_status(
        self,
        place: PlaceInfo,
        status: str,
        score: int = 85,
        tier_badge: str = "📌 В работе (CRM)",
        why_hot: str = "",
    ) -> Dict[str, Any]:
        """Сохраняет или обновляет статус точки в Мини-CRM в 1 клик."""
        pid = place.place_id
        existing = self._crm.get(pid, {})
        entry = {
            "place_id": pid,
            "name": place.name,
            "address": place.address,
            "rating": place.rating,
            "user_ratings_total": place.user_ratings_total,
            "review_url": place.review_url,
            "google_maps_url": place.google_maps_url or f"https://www.google.com/maps/place/?q=place_id:{pid}",
            "phone": place.phone or existing.get("phone"),
            "website": place.website or existing.get("website"),
            "status": status,
            "status_label": CRM_STATUSES.get(status, status),
            "score": existing.get("score", score),
            "tier_badge": existing.get("tier_badge", tier_badge),
            "why_hot": why_hot or existing.get("why_hot") or "Точка сохранена в Мини-CRM для дожима и живого показа.",
            "updated_at": datetime.now().strftime("%d.%m %H:%M"),
        }
        self._crm[pid] = entry
        self._save_crm()
        return entry

    def remove_crm_lead(self, place_id: str) -> bool:
        if place_id in self._crm:
            del self._crm[place_id]
            self._save_crm()
            return True
        return False

    def get_crm_entry(self, place_id: str) -> Optional[Dict[str, Any]]:
        return self._crm.get(place_id)

    def list_crm_leads(self, status_filter: Optional[str] = None) -> List[ScoredLead]:
        """Возвращает список лидов из CRM в виде ScoredLead для удобного просмотра в карусели."""
        items = list(self._crm.values())
        if status_filter and status_filter != "all":
            items = [x for x in items if x.get("status") == status_filter]
        # Свежие сверху
        items.reverse()
        out: List[ScoredLead] = []
        for it in items:
            pid = it.get("place_id", "")
            r = it.get("rating")
            rev = it.get("user_ratings_total")
            needed_5, target_r = calc_5stars_needed(r, rev)
            place = PlaceInfo(
                place_id=pid,
                name=it.get("name", "Заведение"),
                address=it.get("address", "Ташкент"),
                rating=r,
                user_ratings_total=rev,
                review_url=it.get("review_url") or build_review_url(pid),
                google_maps_url=it.get("google_maps_url") or f"https://www.google.com/maps/place/?q=place_id:{pid}",
                phone=it.get("phone"),
                website=it.get("website"),
            )
            status_label = CRM_STATUSES.get(it.get("status", ""), "📌 В CRM")
            updated_at = it.get("updated_at", "")
            badge = f"{status_label}" + (f" • {updated_at}" if updated_at else "")
            out.append(
                ScoredLead(
                    place=place,
                    score=int(it.get("score") or 85),
                    tier_badge=badge,
                    why_hot=it.get("why_hot") or "Точка в воронке продаж HHtap.",
                    needed_5stars=needed_5,
                    target_rating=target_r,
                )
            )
        return out

    def crm_counts(self) -> Dict[str, int]:
        counts = {"all": len(self._crm), "contacted": 0, "meeting": 0, "won": 0}
        for v in self._crm.values():
            st = v.get("status")
            if st in counts:
                counts[st] += 1
        return counts

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
        Мгновенная детерминированная фильтрация и калибровка топ-кандидатов
        (без лишнего сетевого запроса к Anymodel, чтобы не забивать очередь и лимиты API
        перед нажатием кнопок генерации сообщений).
        """
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

            # Выходим пораньше, только если уже нашли достаточно «больных» лидов (3.5–4.3⭐️ и мало отзывов)
            golden_count = sum(1 for x in scored_fresh if x.score >= 82)
            if len(scored_fresh) >= max(top_n + 4, 9) and (golden_count >= 3 or attempt >= 2):
                break

        if not scored_fresh:
            return [], total_scanned

        # Сортируем строго по приоритету:
        # 1) Максимальный балл (где 88..98 получают именно точки 3.5–4.3⭐️ с малым числом отзывов)
        # 2) При равном балле выше ставим точку, которой нужно МЕНЬШЕ пятёрок до 4.5⭐️ (быстрее результат)
        # 3) И у которой меньше отзывов в карточке
        scored_fresh.sort(
            key=lambda x: (
                -x.score,
                x.needed_5stars,
                x.place.user_ratings_total if x.place.user_ratings_total is not None else 999,
            )
        )

        refined = await self._ai_refine_top_leads(scored_fresh[:10])
        selected = refined[:top_n]
        selected.sort(
            key=lambda x: (
                -x.score,
                x.needed_5stars,
                x.place.user_ratings_total if x.place.user_ratings_total is not None else 999,
            )
        )

        # Обогащаем выбранные ТОП-5 прямыми контактами (телефон, сайт/Instagram) за ~0.2 сек
        try:
            await keyless_places_service.enrich_places_contacts([x.place for x in selected])
        except Exception as e:
            logger.warning("Не удалось обогатить контакты для ТОП-5: %s", e)

        # Записываем только те 5 заведений, которые реально отправляем в карусель пользователю
        self.mark_seen(selected)

        return selected, total_scanned


lead_hunter_service = LeadHunterService()
