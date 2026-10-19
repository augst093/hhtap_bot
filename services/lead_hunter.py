import json
import logging
import math
import os
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
# Справочник коммерческих ниш и денежных локаций Ташкента
# ==============================================================================
HUNT_NICHES: Dict[str, Dict[str, Any]] = {
    "rest": {
        "title": "🍽 Рестораны и Стейк-хаусы",
        "short": "Рестораны",
        "queries": [
            "ресторан",
            "стейк хаус ресторан",
            "семейный ресторан",
            "итальянский ресторан",
            "гастробар ресторан",
            "восточный ресторан премиум",
        ],
    },
    "bar": {
        "title": "🍸 Бары, Пабы и Лаунжи",
        "short": "Бары / Лаунжи",
        "queries": [
            "бар паб",
            "лаунж бар кальянная",
            "коктейльный бар",
            "крафтовый бар паб",
            "караоке бар лаунж",
        ],
    },
    "coffee": {
        "title": "☕️ Кофейни и Кондитерские",
        "short": "Кофейни",
        "queries": [
            "кофейня",
            "specialty coffee кофейня",
            "кафе кондитерская пекарня",
            "кофейня завтраки",
            "уютное кафе кофейня",
        ],
    },
    "clinic": {
        "title": "🦷 Стоматологии и Клиники",
        "short": "Клиники / Стоматологии",
        "queries": [
            "стоматология клиника",
            "dental clinic стоматология",
            "медицинский центр клиника",
            "клиника косметологии и эстетики",
            "частная клиника диагностика",
        ],
    },
    "beauty": {
        "title": "💈 Барбершопы и Салоны",
        "short": "Салоны / Барбершопы",
        "queries": [
            "барбершоп barbershop",
            "салон красоты студия",
            "beauty salon студия красоты",
            "спа салон массаж",
            "ногтевая студия салон красоты",
        ],
    },
    "all": {
        "title": "🔥 Все топовые ниши (Авто-микс)",
        "short": "Все ниши",
        "queries": [
            "ресторан гастробар",
            "бар лаунж паб",
            "кофейня кондитерская",
            "стоматология клиника",
            "барбершоп салон красоты",
            "стейк хаус кафе",
        ],
    },
}

HUNT_DISTRICTS: Dict[str, Dict[str, Any]] = {
    "mirabad": {
        "title": "🏙 Мирабад (Т. Шевченко / Ойбек / Шахрисабз)",
        "short": "Мирабад",
        "zones": [
            "Тараса Шевченко Ташкент",
            "Мирабадский район Ташкент",
            "улица Ойбек Ташкент",
            "улица Шахрисабз Ташкент",
            "улица Нукус Ташкент",
        ],
    },
    "yakkasaray": {
        "title": "🏛 Яккасарай (Ш. Руставели / Бабура / Seoul Mun)",
        "short": "Яккасарай",
        "zones": [
            "Шота Руставели Ташкент",
            "Яккасарайский район Ташкент",
            "улица Бабура Ташкент",
            "Seoul Mun Ташкент",
            "улица Мукими Ташкент",
        ],
    },
    "yunusabad": {
        "title": "🌳 Юнусабад (Ц-4 / Ц-5 / Амир Темур)",
        "short": "Юнусабад",
        "zones": [
            "Юнусабад Ташкент",
            "проспект Амира Темура Ташкент",
            "Ц-4 Юнусабад Ташкент",
            "Ц-5 Ташкент",
            "Бодомзор Ташкент",
        ],
    },
    "mirzo": {
        "title": "✨ Мирзо-Улугбек (Ц-1 / Паркентская / Максим Горький)",
        "short": "Мирзо-Улугбек",
        "zones": [
            "Мирзо-Улугбекский район Ташкент",
            "Ц-1 Ташкент",
            "улица Паркентская Ташкент",
            "Буюк Ипак Йули Ташкент",
            "улица Салар Ташкент",
        ],
    },
    "center": {
        "title": "🎡 Tashkent City / Шайхантахур / Чиланзар",
        "short": "Центр / Сити / Чиланзар",
        "zones": [
            "Tashkent City Ташкент",
            "улица Алишера Навои Ташкент",
            "Шайхантахурский район Ташкент",
            "Чиланзар Ташкент",
            "проспект Бунёдкор Ташкент",
        ],
    },
    "auto": {
        "title": "🚀 Весь Ташкент (Самые денежные улицы)",
        "short": "Весь Ташкент",
        "zones": [
            "Тараса Шевченко Ташкент",
            "Шота Руставели Ташкент",
            "Мирабадский район Ташкент",
            "Ц-1 Ташкент",
            "Tashkent City Ташкент",
            "улица Ойбек Ташкент",
            "Юнусабад Ц-4 Ташкент",
            "Seoul Mun Ташкент",
            "Мирзо-Улугбекский район Ташкент",
            "Яккасарайский район Ташкент",
        ],
    },
}

# Стоп-слова в названиях, которые точно не являются целевыми B2B-клиентами HHtap
STOP_NAME_WORDS = (
    "столовая", "ошхона", "oshxona", "шаурма", "хот-дог", "хот дог", "самса",
    "пайнет", "paynet", "аптека", "dorixona", "магазин", "супермаркет", "корзинка",
    "korzinka", "makro", "havas", "школа", "детский сад", "колледж", "университет",
    "общежитие", "банк", "банкомат", "азс", "заправка", "автомойка", "шиномонтаж",
    "рынок", "базар", "кладбище", "мечеть", "церковь", "почта", "нотариус",
    "государственн", "поликлиника №", "больница №", "жэк", "махалля",
)


@dataclass
class ScoredLead:
    place: PlaceInfo
    score: int                  # 0..99
    tier_badge: str             # Например: "🔥 Острая боль: просажен рейтинг"
    why_hot: str                # Конкретный расчёт и аргумент, почему это топовый лид
    needed_5stars: int          # Сколько 5⭐ нужно до целевого рейтинга
    target_rating: float        # Целевой рейтинг (4.5, 4.7 или 4.8)


def calc_5stars_needed(rating: Optional[float], reviews: Optional[int]) -> Tuple[int, float]:
    """
    Считает, сколько подряд 5-звёздочных отзывов нужно получить заведению,
    чтобы выйти на следующий психологический порог в Гугл Картах (4.5, 4.7 или 4.8).
    Формула: (R * N + 5 * X) / (N + X) >= Target  =>  X = ceil(N * (Target - R) / (5 - Target))
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
        return (20, 4.9)

    raw_x = (reviews * (target - rating)) / (5.0 - target)
    needed = max(5, int(math.ceil(raw_x)))
    return (needed, target)


def evaluate_lead_potential(item: Dict[str, Any]) -> Optional[ScoredLead]:
    """
    Оценивает сырую карточку заведения из парсера по 100-балльной системе HHtap.
    Возвращает None, если заведение мусорное или не подходит под продукт.
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
    if any(w in low_name for w in STOP_NAME_WORDS):
        return None

    rating = item.get("rating")
    reviews = item.get("user_ratings_total")

    # Отсеиваем слишком раскачанных гигантов (уже >400 отзывов и отличный рейтинг >= 4.7)
    if rating is not None and reviews is not None:
        if reviews > 400 and rating >= 4.7:
            return None
        # Слишком убитые точки (< 3.1 на большом числе отзывов) или микроточки с 1 отзывом 2.0
        if rating < 3.1:
            return None

    needed_5, target_r = calc_5stars_needed(rating, reviews)
    street = clean_address(address)
    loc_label = f"на {street}" if street else "в хорошей локации"

    # --------------------------------------------------------------------------
    # КАТЕГОРИЯ 1: «Огромный потенциал, мало отзывов» (4.5 .. 5.0 ⭐️, но всего 3..65 отзывов)
    # --------------------------------------------------------------------------
    if rating is not None and rating >= 4.5 and (reviews is None or reviews <= 65):
        rev_count = reviews or 0
        base = 93
        if 6 <= rev_count <= 40:
            base += 5  # Золотая жила: заведение крутое, но карточка полупустая
        elif rev_count < 6:
            base += 3  # Совсем свежая точка
        if rating >= 4.7:
            base += 1
        score = min(99, base)
        tier_badge = "🚀 Огромный потенциал (Мало отзывов в Гугле)"
        if rev_count > 0:
            to_100 = max(30, 100 - rev_count)
            shifts = max(3, round(to_100 / 12))
            why_hot = (
                f"Сильная точка {loc_label} с рейтингом {rating:.1f}⭐️, но в Гугле всего {rev_count} отзывов — "
                f"проигрывают соседям по заметности, а любая случайная двойка сразу обрушит оценку. "
                f"С нашей NFC-картой к счёту соберут +{to_100} пятёрок (до первой сотни) всего за {shifts}–{shifts + 2} смен."
            )
        else:
            why_hot = (
                f"Активная точка {loc_label}, но карточка в Гугле ещё почти пустая. "
                f"Идеальный момент зайти с NFC-картой к счёту и быстро собрать первую сотню 5⭐️ отзывов."
            )

    # --------------------------------------------------------------------------
    # КАТЕГОРИЯ 2: «Острая боль — Просаженный рейтинг» (3.4 .. 4.4 ⭐️ при 8..180 отзывах)
    # --------------------------------------------------------------------------
    elif rating is not None and 3.4 <= rating <= 4.4 and reviews is not None and 8 <= reviews <= 180:
        base = 92
        if 12 <= reviews <= 75:
            base += 5
        if 3.8 <= rating <= 4.3:
            base += 2
        score = min(99, base)
        shifts = max(2, round(needed_5 / 12))
        tier_badge = "🔥 Острая боль (Просажен рейтинг)"
        why_hot = (
            f"Точка {loc_label} теряет гостей из поиска из-за оценки {rating:.1f}⭐️ ({reviews} отз.). "
            f"Чтобы поднять рейтинг до {target_r:.1f}⭐️, им нужно всего ~{needed_5} пятёрок "
            f"(это {shifts}–{shifts + 2} смен с нашей NFC-картой к счёту)."
        )

    # --------------------------------------------------------------------------
    # КАТЕГОРИЯ 3: «Хорошая точка на обгон конкурентов» (4.4 .. 4.7 ⭐️ при 66..220 отзывах)
    # --------------------------------------------------------------------------
    elif rating is not None and 4.4 < rating <= 4.75 and reviews is not None and 66 <= reviews <= 250:
        score = 82 if rating <= 4.6 else 77
        shifts = max(3, round(needed_5 / 12))
        tier_badge = "💎 Точка роста (Борьба за ТОП выдачи)"
        why_hot = (
            f"Крепкое заведение {loc_label} ({rating:.1f}⭐️, {reviews} отз.), но у лидеров улицы больше свежих оценок. "
            f"До порога {target_r:.1f}⭐️ не хватает ~{needed_5} пятёрок (~{shifts} смен с NFC-картой без QR-кодов)."
        )

    # --------------------------------------------------------------------------
    # КАТЕГОРИЯ 4: Точки без цифр или средние показатели
    # --------------------------------------------------------------------------
    elif rating is None or reviews is None:
        score = 78
        tier_badge = "🚀 Свежая точка (Не прокачана в Гугле)"
        why_hot = (
            f"Заведение {loc_label} практически не собирает отзывы в Гугл Картах — "
            f"95% довольных гостей уходят молча после оплаты счёта."
        )
    else:
        score = 68
        tier_badge = "📈 Стабильная точка (Автоматизация отзывов)"
        why_hot = (
            f"В Гугле {rating:.1f}⭐️ ({reviews} отз.) {loc_label}. "
            f"Можно предложить замену неработающих QR-кодов на касание NFC-карты в папке со счётом."
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
    Автономный охотник за лидами HHtap:
    1. Сканирует реальные карточки заведений Ташкента по нишам и районам.
    2. Отсеивает уже показанные точки (база seen_leads.json) и нецелевой мусор.
    3. Вычисляет математический потенциал каждого лида (0..99 баллов + сколько 5⭐ нужно).
    4. При помощи Anymodel ИИ дополнительно валидирует топ-кандидатов и обогащает вердикт.
    """

    def __init__(self) -> None:
        self._seen_ids: Set[str] = self._load_seen()
        self._query_Validate_decks: Dict[str, List[str]] = {}

    def _load_seen(self) -> Set[str]:
        try:
            if SEEN_FILE.exists():
                data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(str(x) for x in data)
        except Exception as e:
            logger.warning("Не удалось загрузить seen_leads.json: %s", e)
        return set()

    def _save_seen(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SEEN_FILE.write_text(
                json.dumps(list(self._seen_ids)[-3000:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Не удалось сохранить seen_leads.json: %s", e)

    def mark_seen(self, place_ids: List[str]) -> None:
        for pid in place_ids:
            if pid:
                self._seen_ids.add(pid)
        self._save_seen()

    def reset_seen(self) -> int:
        count = len(self._seen_ids)
        self._seen_ids.clear()
        self._save_seen()
        return count

    def seen_count(self) -> int:
        return len(self._seen_ids)

    def _pick_search_queries(self, niche_key: str, district_key: str, count: int = 2) -> List[str]:
        """Формирует разнообразные поисковые запросы без повторов."""
        niche = HUNT_NICHES.get(niche_key, HUNT_NICHES["all"])
        district = HUNT_DISTRICTS.get(district_key, HUNT_DISTRICTS["auto"])

        deck_key = f"{niche_key}:{district_key}"
        deck = self._query_Validate_decks.get(deck_key)
        if not deck:
            combos = [
                f"{q} {z}"
                for q in niche["queries"]
                for z in district["zones"]
            ]
            random.shuffle(combos)
            deck = combos
            self._query_Validate_decks[deck_key] = deck

        picked = []
        while len(picked) < count and deck:
            picked.append(deck.pop())
        return picked

    async def _fetch_raw_candidates(self, queries: List[str]) -> List[Dict[str, Any]]:
        """Собирает до 40-60 сырых карточек заведений из парсера по списку запросов."""
        all_items: List[Dict[str, Any]] = []
        seen_in_batch: Set[str] = set()

        for q in queries:
            logger.info("LeadHunter сканирует запрос: '%s'", q)
            if config.has_google_api_key():
                try:
                    places = await google_places_service.search_text(q, max_results=10)
                    raw_batch = [
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
                    logger.warning("Ошибка Google API при охоте '%s': %s, используем автономный парсер", q, e)
                    raw_batch = await keyless_places_service.search_batch_raw(q, max_results=20)
            else:
                raw_batch = await keyless_places_service.search_batch_raw(q, max_results=20)

            for item in raw_batch:
                pid = clean_place_id(item.get("place_id", ""))
                if pid and pid not in seen_in_batch:
                    seen_in_batch.add(pid)
                    all_items.append(item)

        return all_items

    async def _ai_refine_top_leads(self, candidates: List[ScoredLead]) -> List[ScoredLead]:
        """
        Быстрый ИИ-фильтр через Anymodel (если ключ активен):
        проверяет список топ-кандидатов, отсеивает случайные бюджетные столовки/магазины,
        которые могли проскочить по ключевым словам, и оставляет самых платёжеспособных.
        Если API задумывается дольше 12 сек — мгновенно возвращает математический топ без задержки.
        """
        if not config.has_anymodel_key() or len(candidates) <= 3:
            return candidates

        short_list = []
        for idx, c in enumerate(candidates[:10]):
            r_str = f"{c.place.rating:.1f}⭐️ ({c.place.user_ratings_total or 0} отз.)" if c.place.rating else "без оценки"
            short_list.append(f"{idx}: {c.place.name} | {c.place.address} | {r_str} | Score={c.score}")

        prompt = (
            "Ты — коммерческий директор HHtap в Ташкенте (продаём умные NFC-карты к счёту для сбора 5⭐️ отзывов в Гугл Картах).\n"
            "Перед тобой кандидаты из парсера карт:\n"
            + "\n".join(short_list)
            + "\n\nВыбери индексы 5 самых перспективных и платёжеспособных заведений (где есть посадка, официанты/администраторы и острая нужда в 5⭐️ отзывах), "
            "отсеяв уличный фастфуд, госучреждения или случайные магазины. "
            "Ответь ТОЛЬКО JSON-массивом номеров в порядке убывания крутости, например: [0, 2, 1, 4, 5]"
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
            async with httpx.AsyncClient(timeout=12.0) as client:
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
            logger.info("AI-ранжирование пропущено (используем точный математический скоринг): %s", e)

        return candidates

    async def hunt_best_leads(
        self,
        niche_key: str = "all",
        district_key: str = "auto",
        top_n: int = 5,
    ) -> Tuple[List[ScoredLead], int]:
        """
        Главная функция:
        1. Берёт 2 поисковых запроса по выбранной нише и району Ташкента.
        2. Собирает реальные карточки с Гугл Карт (до 40 заведений за проход).
        3. Отсеивает уже просмотренные и считает HHtap Score (0..99).
        4. Если новых мало — автоматически добирает ещё один сектор поиска.
        5. Возвращает (топ-N лучших лидов, общее число просканированных точек).
        """
        queries = self._pick_search_queries(niche_key, district_key, count=2)
        raw_items = await self._fetch_raw_candidates(queries)

        scored_fresh: List[ScoredLead] = []
        scored_seen_fallback: List[ScoredLead] = []

        for item in raw_items:
            lead = evaluate_lead_potential(item)
            if not lead:
                continue
            if lead.place.place_id not in self._seen_ids:
                scored_fresh.append(lead)
            else:
                scored_seen_fallback.append(lead)

        # Если свежих лидов меньше top_n, делаем ещё один быстрый добор по другому подзапросу
        if len(scored_fresh) < top_n:
            extra_queries = self._pick_search_queries(niche_key, district_key, count=1)
            extra_items = await self._fetch_raw_candidates(extra_queries)
            raw_items.extend(extra_items)
            for item in extra_items:
                lead = evaluate_lead_potential(item)
                if not lead:
                    continue
                if (
                    lead.place.place_id not in self._seen_ids
                    and not any(x.place.place_id == lead.place.place_id for x in scored_fresh)
                ):
                    scored_fresh.append(lead)

        pool = scored_fresh if scored_fresh else scored_seen_fallback
        pool.sort(key=lambda x: x.score, reverse=True)

        refined = await self._ai_refine_top_leads(pool[:10])
        selected = refined[:top_n]

        # Запоминаем показанные заведения, чтобы не дублировать в следующих выдачах
        self.mark_seen([x.place.place_id for x in selected])

        return selected, len(raw_items)


lead_hunter_service = LeadHunterService()
