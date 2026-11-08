"""
Автономный Парсер Высоких Шансов Продаж (HHtap High-Conversion Scanner).
По любой заданной пользователем локации (улица, район, ориентир, например «Тараса Шевченко»)
собирает ВСЕ релевантные бизнесы и ранжирует их в 2 целевых режимах:
1) ⭐️ Google Reviews: бизнесы с плохим/уязвимым рейтингом и малым числом отзывов (острая боль в картах).
2) 📸 Instagram: бизнесы с малым количеством подписчиков в Instagram и слабым переливом трафика из офлайна.
Выдаёт результаты пачками по 5 лучших бизнесов с мгновенным перелистыванием (Дальше ещё 5 / Назад).
"""
import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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

logger = logging.getLogger(__name__)


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
    ig_username: Optional[str]
    ig_title: Optional[str]
    followers: Optional[int]
    score: int
    tier_badge: str
    pain_text: str
    primary_nfc_url: Optional[str]
    deep_link_url: Optional[str]


@dataclass
class HighChanceLocationPack:
    pack_id: str
    location_query: str
    mode: str  # "google" | "insta"
    total_scanned: int
    google_leads: List[HighChanceGoogleLead] = field(default_factory=list)
    insta_leads: List[HighChanceInstaLead] = field(default_factory=list)


# Быстрые пресеты популярных коммерческих улиц Ташкента для выбора в 1 клик
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
}

DIRECTORY_DOMAINS = {
    "taplink", "instagram", "facebook", "google", "abnmbgroup", "tanukifamily",
    "t", "2gis", "goldenpages", "yellowpages", "sprav", "zoon", "yandex",
    "alteg", "yclients", "linktr", "hipolink", "mssg",
}


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


async def collect_all_location_places(location_query: str) -> List[PlaceInfo]:
    """
    Параллельно сканирует заданную локацию по 6 коммерческим категориям через быстрый HTTP-движок Google Maps.
    Собирает 40–65 заведений за ~1.5 секунды и убирает дубли.
    """
    loc = location_query.strip()
    if "ташкент" not in loc.lower() and "tashkent" not in loc.lower():
        loc_with_city = f"{loc} Ташкент"
    else:
        loc_with_city = loc

    queries = [
        f"ресторан гастробар {loc_with_city}",
        f"пиццерия бургерная суши {loc_with_city}",
        f"кафе кофейня пекарня кондитерская {loc_with_city}",
        f"бар паб лаунж кальянная {loc_with_city}",
        f"барбершоп салон красоты косметология {loc_with_city}",
        f"стоматология клиника цветы магазин {loc_with_city}",
    ]

    async def _fetch_cat(q: str) -> List[PlaceInfo]:
        try:
            return await asyncio.wait_for(
                keyless_places_service.search_text(q, max_results=12),
                timeout=4.5,
            )
        except Exception as e:
            logger.warning("Ошибка сканирования категории '%s': %s", q, e)
            return []

    batches = await asyncio.gather(*[_fetch_cat(q) for q in queries])

    unique_places: List[PlaceInfo] = []
    seen_ids: set[str] = set()
    seen_brands: Dict[str, int] = {}

    for batch in batches:
        for p in batch:
            if not p.place_id or p.place_id in seen_ids:
                continue
            low_name = (p.name or "").lower()
            if any(w in low_name for w in STOP_NAME_WORDS):
                continue
            if any(ch in low_name for ch in CHAIN_BLACKLIST):
                continue
            # Отсекаем безымянные государственные/общие точки вида "4 стоматология", "СТОМАТОЛОГИЯ", "Barbershop"
            clean_alpha = re.sub(r"[^a-zа-яё]+", " ", low_name).strip()
            if clean_alpha in GENERIC_ONLY_NAMES:
                continue
            b_key = _normalize_brand_key(p.name)
            # Не более 1 филиала одной и той же сети в рамках одной локации
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
            # Дополнительный захват заведений с просаженным рейтингом (2.8..4.4⭐️) на конкретной улице
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


def _build_fast_place_ig_candidates(place: PlaceInfo) -> Tuple[List[str], Optional[str], set[str]]:
    """
    Генерирует до 6 самых вероятных Instagram-никнеймов для конкретного заведения на улице.
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
        if w not in ("the", "na", "v", "and")
    ]
    distinct_words = [w for w in raw_words if w not in GENERIC_ONLY_NAMES]
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
            add(f"{w1}pizzauz")

    return cands[:4], direct_ig, web_stems


async def rank_instagram_low_followers_leads(places: List[PlaceInfo]) -> List[HighChanceInstaLead]:
    """
    Параллельно проверяет Instagram-аккаунты бизнесов на локации и ранжирует их
    по вероятности продажи Instagram NFC-карты (упор на МАЛОЕ число подписчиков и слабый поток):
    - 🔥 Приоритет №1 (92–99 баллов): Живой аккаунт, но ОЧЕНЬ МАЛО подписчиков (30 – 2 500 подп.) — острая боль!
    - ⚡️ Приоритет №2 (82–91 балл): Мало подписчиков (2 501 – 6 500 подп.) — слабый перелив гостей из зала.
    - 📌 Приоритет №3 (74–81 балл): Средний аккаунт (6 501 – 14 500 подп.) ИЛИ точка вообще не привязала Инсту к Картам.
    - 🚫 Отсев: Крупные сети с > 25 000 подписчиков полностью исключаются.
    """
    place_meta = {}
    all_handles: List[str] = []

    for p in places:
        cands, direct_ig, web_stems = _build_fast_place_ig_candidates(p)
        b_words = _extract_brand_words(p.name, y_as_i=False, kh_mode=True)
        place_meta[p.place_id] = {
            "cands": cands,
            "direct_ig": direct_ig,
            "web_stems": web_stems,
            "brand_words": b_words,
        }
        for h in cands:
            if h not in all_handles:
                all_handles.append(h)

    verified_map: Dict[str, Dict[str, any]] = {}
    sem = asyncio.Semaphore(48)

    async with httpx.AsyncClient(
        headers={"User-Agent": "TelegramBot (like TwitterBot)"},
        follow_redirects=True,
        timeout=2.8,
    ) as client:
        async def _check_handle(u: str):
            async with sem:
                try:
                    r = await client.get(f"https://www.instagram.com/{u}/")
                    if r.status_code != 200:
                        return
                    og_m = re.search(r'property="og:title"\s+content="([^"]+)"', r.text)
                    desc_m = re.search(r'property="og:description"\s+content="([^"]+)"', r.text)
                    if not og_m:
                        return
                    title = html.unescape(og_m.group(1)).strip()
                    desc = html.unescape(desc_m.group(1)).strip() if desc_m else ""
                    flw = _parse_followers(desc)
                    verified_map[u] = {
                        "username": u,
                        "title": title,
                        "desc": desc,
                        "followers": flw,
                    }
                except Exception:
                    pass

        await asyncio.gather(*[_check_handle(u) for u in all_handles])

    leads: List[HighChanceInstaLead] = []
    seen_ig_handles: set[str] = set()

    for p in places:
        meta = place_meta[p.place_id]
        cands = meta["cands"]
        direct_ig = meta["direct_ig"]
        web_stems = meta["web_stems"]
        b_words = [w for w in meta["brand_words"] if w not in GENERIC_ONLY_NAMES]

        found_profiles = [verified_map[c] for c in cands if c in verified_map]

        # Выбираем наиболее достоверный профиль этого бизнеса
        best_prof = None
        best_match_sc = -999.0
        for prof in found_profiles:
            u = prof["username"].lower()
            flw = prof["followers"]
            raw_title_low = prof["title"].lower()
            raw_desc_low = (prof.get("desc") or "").lower()
            title_lat = _cyr_to_latin(raw_title_low, y_as_i=False, kh_mode=True)
            u_clean = re.sub(r"[._]", "", u)

            # Отсекаем пустые/мёртвые однофамильные заглушки с < 55 подписчиками (если это не прямая ссылка с карт)
            if flw < 55 and u != direct_ig and u not in web_stems:
                continue

            m_sc = 0.0
            is_verified_link = False
            if direct_ig and u == direct_ig:
                m_sc += 500.0
                is_verified_link = True
            if any(st in u_clean for st in web_stems if len(st) >= 3):
                m_sc += 180.0
                is_verified_link = True

            brand_hit = False
            for bw in b_words:
                if len(bw) >= 3:
                    if bw in title_lat or bw in raw_title_low:
                        m_sc += 95.0
                        brand_hit = True
                    if bw in u_clean:
                        m_sc += 70.0
                        brand_hit = True

            # Если это не прямая ссылка с сайта/карт и в профиле нет совпадения по бренду — пропускаем
            if not is_verified_link and not brand_hit:
                continue

            if any(uz in u or uz in raw_title_low or uz in raw_desc_low for uz in ("_uz", ".uz", "tashkent", "ташкент", "узбекистан")):
                m_sc += 110.0
            if re.search(r"[а-яА-ЯёЁ]", raw_title_low) or re.search(r"[а-яА-ЯёЁ]", raw_desc_low):
                m_sc += 45.0
            if flw >= 120:
                m_sc += 40.0

            if m_sc > best_match_sc:
                best_match_sc = m_sc
                best_prof = prof

        # Если нашли подтверждённый Instagram-профиль заведения
        if best_prof:
            u = best_prof["username"].lower()
            if u in seen_ig_handles:
                continue
            seen_ig_handles.add(u)
            flw = best_prof["followers"]
            title = best_prof["title"].split("(@")[0].split("•")[0].strip()

            # Крупные аккаунты (> 25 000 подписчиков) отсекаем — у них нет боли малого числа подписчиков!
            if flw > 25_000:
                continue

            if 20 <= flw <= 1_200:
                score = 98 - int(flw / 200)
                tier_badge = "🔥 ОСТРАЯ БОЛЬ: Мало подписчиков"
                pain_text = (
                    f"В Instagram всего <b>{flw} подписчиков</b> (@{u})! "
                    f"Гости сидят в зале, но не подписываются — идеальный клиент под Instagram NFC-карту."
                )
            elif 1_201 <= flw <= 3_500:
                score = 91 - int((flw - 1200) / 400)
                tier_badge = "🔥 Высокий шанс (Слабый поток в Инсту)"
                pain_text = (
                    f"В профиле @{u} всего <b>{_format_followers_badge(flw)}</b>. "
                    f"Аккаунт ведут, но перелив трафика из офлайна слабый — легко зацепить за живое!"
                )
            elif 3_501 <= flw <= 8_500:
                score = 84 - int((flw - 3500) / 800)
                tier_badge = "⚡️ Хороший потенциал (Добить до 10K+)"
                pain_text = (
                    f"У @{u} сейчас <b>{_format_followers_badge(flw)}</b> — не хватает живого трафика гостей до красивой цифры 10K+."
                )
            else:
                score = 75
                tier_badge = "📌 Точка роста Instagram"
                pain_text = (
                    f"Профиль @{u} (<b>{_format_followers_badge(flw)}</b>) — можно усилить органический поток подписчиков с каждого стола."
                )

            primary_url = f"https://instagram.com/{u}?igsh={DEFAULT_IGSH_TOKEN}"
            deep_url = f"instagram://user?username={u}"
            leads.append(
                HighChanceInstaLead(
                    place=p,
                    ig_username=u,
                    ig_title=title,
                    followers=flw,
                    score=score,
                    tier_badge=tier_badge,
                    pain_text=pain_text,
                    primary_nfc_url=primary_url,
                    deep_link_url=deep_url,
                )
            )
        else:
            # Если у заведения в Google Картах вообще не привязан сайт/Инста, а отзывов немного —
            # это локальная несетевая точка со слабым маркетингом
            revs = p.user_ratings_total or 0
            if 2 <= revs <= 180 and not (p.website and "instagram.com" in p.website.lower()):
                leads.append(
                    HighChanceInstaLead(
                        place=p,
                        ig_username=None,
                        ig_title=None,
                        followers=None,
                        score=78 if revs <= 60 else 73,
                        tier_badge="🎯 Нет Инсты в Картах (Слабый поток)",
                        pain_text=(
                            "Инстаграм не привязан к карточке заведения — теряют весь трафик гостей! "
                            "Нажмите номер точки ниже, чтобы бот пробил их точный @ник за 1 секунду."
                        ),
                        primary_nfc_url=None,
                        deep_link_url=None,
                    )
                )

    # Сортируем: сначала подтверждённые аккаунты с самым малым числом подписчиков (высокий score), затем остальные
    leads.sort(
        key=lambda item: (
            1 if item.ig_username else 0,
            item.score,
            -(item.followers or 99999),
        ),
        reverse=True,
    )
    return leads
