import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from playwright.async_api import Browser, Page, Playwright, async_playwright

from services.google_places import (
    LinkExpansionError,
    PlaceInfo,
    build_review_url,
    clean_place_id,
    encode_hex_to_place_id,
)

logger = logging.getLogger(__name__)

# Официальный веб-интерфейс Google Place ID Finder (резервный движок Tier-2)
FINDER_URL = "https://geo-devrel-javascript-samples.web.app/samples/places-placeid-finder/app/dist/"

# Прямой protobuf-шаблон Google Maps (tbm=map) для мгновенного HTTP-поиска без браузера и ключей (Tier-1)
GOOGLE_MAPS_PB_TEMPLATE = (
    "!4m8!1m3!1d{dist}!2d{lng}!3d{lat}!3m2!1i1024!2i768!4f13.1!7i20!10b1"
    "!12m58!1m5!18b1!30b1!31m1!1b1!34e1!2m4!5m1!6e2!20e3!39b1"
    "!6m29!32i1!49b1!63m0!66b1!85b1!114b1!149b1!206b1!209b1!212b1!215b1!216b1!222b1!223b1!234b1!235b1!239b1!246b1!253b1!260b1!262b1!266b1!270b1!271b1!273b1!281b1!294b1!302i300!303i100"
    "!10b1!12b1!13b1!14b1!16b1!17m1!3e1!20m3!5e2!6b1!14b1!46m1!1b0!96b1!97m1!2b1!99b1"
    "!19m4!2m3!1i360!2i120!4i8!20m57!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
    "!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
    "!24m105!1m25!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1!18m14!3b1!4b1!5b1!6b1!13b1!14b1!17b1!21b1!22b1!32b1!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1!43b1!52b1!55b1!56m1!1b1!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b1"
    "!26m4!2m3!1i80!2i92!4i8!30m28!1m6!1m2!1i0!2i0!2m2!1i530!2i768!1m6!1m2!1i974!2i0!2m2!1i1024!2i768!1m6!1m2!1i0!2i0!2m2!1i1024!2i20!1m6!1m2!1i0!2i748!2m2!1i1024!2i768"
    "!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1!9b1!12b1!14b1!20b1!23b1!25b1!26b1!31b1!37m1!1e81!42b1!49m10!3b1!6m2!1b1!2b1!7m2!1e3!2b1!8b1!9b1!10e2!50m3!2e2!3m1!3b1!61b1!67m5!7b1!10b1!14b1!15m1!1b0!69i801!77b1"
)

NON_BUSINESS_CATEGORIES = (
    "маршрут",
    "улица",
    "район",
    "город",
    "остановка",
    "станция метро",
    "автобусная остановка",
)


class KeylessPlacesService:
    """
    Двухуровневый отказоустойчивый сервис поиска заведений в Google Maps БЕЗ платных ключей:
    - Уровень 1 (Основной — Direct HTTP Google Maps tbm=map):
      работает напрямую через быстрый HTTP-запрос (0.3 сек) к Google Maps без запуска браузера,
      без потребления оперативной памяти Chromium и без лимитов демо-сессии. Сразу возвращает
      Place ID (ChIJ...), рейтинг, отзывы, телефон (+998...), сайт/Instagram и координаты.
    - Уровень 2 (Резервный — Self-Healing Playwright SDK):
      включается автоматически, если HTTP-парсер вернул пустой ответ, и умеет сам перезапускать
      браузер при устаревании сессии.
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=12.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                },
            )
        return self._http_client

    @staticmethod
    def _parse_tbm_place_node(pnode: List[Any], hex_idx: int) -> Optional[Dict[str, Any]]:
        """Извлекает полную карточку заведения из узла JSON-ответа Google Maps (tbm=map)."""
        try:
            hex_id = pnode[hex_idx]
            name = pnode[hex_idx + 1] if len(pnode) > hex_idx + 1 else None
            if not isinstance(name, str) or not name.strip():
                return None
            name = name.strip()

            # Извлекаем ChIJ place_id (обычно на индексе 78 или кодируем из hex_id)
            place_id: Optional[str] = None
            if len(pnode) > 78 and isinstance(pnode[78], str) and pnode[78].startswith("ChIJ"):
                place_id = clean_place_id(pnode[78])
            if not place_id:
                dump_str = json.dumps(pnode, ensure_ascii=False)
                m_chij = re.search(r"ChIJ[A-Za-z0-9_-]{20,35}", dump_str)
                if m_chij:
                    place_id = clean_place_id(m_chij.group(0))
            if not place_id and isinstance(hex_id, str):
                place_id = encode_hex_to_place_id(hex_id)
            if not place_id:
                return None

            # Категории точки (обычно pnode[hex_idx + 3])
            categories: List[str] = []
            if len(pnode) > hex_idx + 3 and isinstance(pnode[hex_idx + 3], list):
                categories = [str(c) for c in pnode[hex_idx + 3] if isinstance(c, str)]
            low_cats = " ".join(categories).lower()
            if categories and any(bad in low_cats for bad in NON_BUSINESS_CATEGORIES):
                return None

            # Адрес (обычно pnode[hex_idx + 29] == pnode[39] или список в pnode[hex_idx - 8] == pnode[2])
            address = ""
            if len(pnode) > hex_idx + 29 and isinstance(pnode[hex_idx + 29], str):
                address = pnode[hex_idx + 29].strip()
            if not address and hex_idx >= 8 and isinstance(pnode[hex_idx - 8], list):
                addr_parts = [str(x).strip() for x in pnode[hex_idx - 8] if isinstance(x, str) and str(x).strip()]
                address = ", ".join(addr_parts)
            if not address:
                address = "Ташкент"

            # Рейтинг и количество отзывов (обычно pnode[hex_idx - 6] == pnode[4], индексы [7] и [8])
            rating: Optional[float] = None
            reviews_total: Optional[int] = None
            if hex_idx >= 6 and isinstance(pnode[hex_idx - 6], list):
                r_block = pnode[hex_idx - 6]
                if len(r_block) > 7 and isinstance(r_block[7], (int, float)):
                    r_val = float(r_block[7])
                    if 1.0 <= r_val <= 5.0:
                        rating = r_val
                if len(r_block) > 8 and isinstance(r_block[8], (int, float)):
                    reviews_total = int(r_block[8])

            # Сайт / Instagram (обычно pnode[hex_idx - 3] == pnode[7], индекс [0])
            website: Optional[str] = None
            if hex_idx >= 3 and isinstance(pnode[hex_idx - 3], list):
                w_block = pnode[hex_idx - 3]
                if len(w_block) > 0 and isinstance(w_block[0], str):
                    w_cand = w_block[0].strip()
                    if w_cand.startswith("http") and "google.com" not in w_cand.lower():
                        website = w_cand

            # Координаты (обычно pnode[hex_idx - 1] == pnode[9], индексы [2] и [3])
            lat: Optional[float] = None
            lng: Optional[float] = None
            if hex_idx >= 1 and isinstance(pnode[hex_idx - 1], list):
                c_block = pnode[hex_idx - 1]
                if len(c_block) > 3 and isinstance(c_block[2], (int, float)) and isinstance(c_block[3], (int, float)):
                    lat = float(c_block[2])
                    lng = float(c_block[3])

            # Телефон (обычно pnode[178][0][0] или через регулярное выражение по узлу)
            phone: Optional[str] = None
            if len(pnode) > 178 and isinstance(pnode[178], list) and pnode[178]:
                first_p = pnode[178][0]
                if isinstance(first_p, list) and first_p and isinstance(first_p[0], str):
                    phone = first_p[0].strip()
            if not phone:
                dump_str = json.dumps(pnode, ensure_ascii=False)
                m_phone = re.search(r"\+998[\d\s\-]{9,16}", dump_str)
                if m_phone:
                    phone = m_phone.group(0).strip()

            fast_url = (
                f"https://www.google.com/maps/place//data=!4m4!3m3!1s{hex_id}!9m1!1b1"
                if isinstance(hex_id, str) and hex_id.startswith("0x")
                else f"https://www.google.com/maps/place/?q=place_id:{place_id}"
            )

            return {
                "place_id": place_id,
                "name": name,
                "address": address,
                "rating": rating,
                "user_ratings_total": reviews_total,
                "types": categories,
                "business_status": "OPERATIONAL",
                "lat": lat,
                "lng": lng,
                "phone": phone,
                "website": website,
                "url": fast_url,
            }
        except Exception as e:
            logger.debug("Ошибка разбора узла tbm=map: %s", e)
            return None

    async def _http_search_once(
        self,
        query: str,
        max_results: int = 20,
        lat: float = 41.3111,
        lng: float = 69.2797,
        radius: int = 30000,
    ) -> List[Dict[str, Any]]:
        """Один прямой HTTP-запрос к Google Maps JSON API (tbm=map) без браузера."""
        client = self._get_http_client()
        dist = max(600.0, float(radius) * 3.2)
        pb = GOOGLE_MAPS_PB_TEMPLATE.format(dist=dist, lat=lat, lng=lng)

        resp = await client.get(
            "https://www.google.com/search",
            params={
                "tbm": "map",
                "authuser": "0",
                "hl": "ru",
                "gl": "uz",
                "q": query,
                "pb": pb,
            },
        )
        if resp.status_code != 200 or not resp.text:
            return []

        raw = resp.text
        if raw.startswith(")]}'"):
            parts = raw.split("\n", 1)
            raw = parts[1] if len(parts) > 1 else ""
        if not raw:
            return []

        data = json.loads(raw)
        raw_nodes: List[tuple[int, List[Any]]] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                hex_idx = -1
                for i, el in enumerate(node):
                    if (
                        isinstance(el, str)
                        and el.startswith("0x")
                        and not el.startswith("0x0:")
                        and re.match(r"^0x[0-9a-fA-F]+:0x[0-9a-fA-F]+$", el)
                    ):
                        hex_idx = i
                        break
                if hex_idx >= 6 and len(node) > hex_idx + 1 and isinstance(node[hex_idx + 1], str):
                    raw_nodes.append((hex_idx, node))
                    return
                for el in node:
                    walk(el)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(data)

        results: List[Dict[str, Any]] = []
        seen_ids = set()
        for h_idx, pnode in raw_nodes:
            parsed = self._parse_tbm_place_node(pnode, h_idx)
            if parsed and parsed["place_id"] not in seen_ids:
                seen_ids.add(parsed["place_id"])
                results.append(parsed)
                if len(results) >= max_results:
                    break

        return results

    async def _http_search_google_maps(
        self,
        query: str,
        max_results: int = 20,
        lat: float = 41.3111,
        lng: float = 69.2797,
        radius: int = 30000,
    ) -> List[Dict[str, Any]]:
        """
        Уровень 1 (Основной): прямой быстрый поиск через HTTP к Google Maps (tbm=map).
        Автоматически добавляет «Ташкент» первым приоритетом, если город не указан в запросе,
        чтобы на серверах Railway (США/Европа) с первой же попытки за 0.4 сек находить точки Ташкента.
        """
        try:
            low_q = query.lower()
            has_city = any(
                c in low_q
                for c in ("ташкент", "tashkent", "toshkent", "самарканд", "бухара", "узбекистан", "uzbekistan")
            )
            primary_q = query if has_city else f"{query} Ташкент"

            items = await self._http_search_once(
                query=primary_q, max_results=max_results, lat=lat, lng=lng, radius=radius
            )
            if items:
                return items

            if primary_q != query:
                items_raw = await self._http_search_once(
                    query=query, max_results=max_results, lat=lat, lng=lng, radius=radius
                )
                if items_raw:
                    return items_raw
        except Exception as e:
            logger.warning("HTTP-поиск Google Maps ('%s') вызвал ошибку: %s", query, e)

        return []

    async def _ensure_page(self) -> Page:
        """Поддерживает активную вкладку с загруженным Google Maps Places SDK (резервный Tier-2)."""
        if self._browser and self._page and not self._page.is_closed():
            return self._page

        logger.info("Запуск автономного фонового браузера Chromium...")
        if not self._playwright:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--mute-audio",
            ],
        )

        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="ru-RU",
        )

        self._page = await context.new_page()
        logger.info("Загрузка сервиса Google Maps SDK...")
        await self._page.goto(FINDER_URL, wait_until="networkidle", timeout=30000)
        await self._page.wait_for_selector("#pac-input", timeout=15000)
        logger.info("Фоновый браузер успешно инициализирован.")
        return self._page

    async def _text_search_via_sdk(
        self,
        query: str,
        max_results: int = 20,
        lat: float = 41.3111,
        lng: float = 69.2797,
        radius: int = 30000,
    ) -> List[Dict[str, Any]]:
        """
        Резервный поиск через google.maps.places.PlacesService.textSearch внутри Chromium
        с автоматическим перезапуском браузера при зависании или истечении сессии.
        """
        for attempt in range(2):
            try:
                page = await self._ensure_page()
                res_obj = await page.evaluate(
                    """(params) => new Promise((resolve) => {
                        try {
                            if (!window.google || !google.maps || !google.maps.places) {
                                return resolve({ status: 'NO_SDK', items: [] });
                            }
                            const svc = new google.maps.places.PlacesService(document.createElement('div'));
                            const req = {
                                query: params.q,
                                language: 'ru',
                                location: new google.maps.LatLng(params.lat, params.lng),
                                radius: params.radius
                            };
                            svc.textSearch(req, (results, status) => {
                                if (status !== 'OK' || !results) {
                                    return resolve({ status: String(status), items: [] });
                                }
                                const skipTypes = new Set(['route', 'street_address', 'locality', 'political', 'neighborhood', 'sublocality', 'postal_code']);
                                const filtered = results.filter(r => {
                                    const t = Array.isArray(r.types) ? r.types : [];
                                    if (t.length > 0 && t.every(x => skipTypes.has(x))) {
                                        return false;
                                    }
                                    return true;
                                });
                                const mapped = filtered.map(r => ({
                                    place_id: r.place_id || '',
                                    name: r.name || '',
                                    address: r.formatted_address || '',
                                    rating: typeof r.rating === 'number' ? r.rating : null,
                                    user_ratings_total: typeof r.user_ratings_total === 'number' ? r.user_ratings_total : null,
                                    types: Array.isArray(r.types) ? r.types : [],
                                    business_status: r.business_status || 'OPERATIONAL',
                                    lat: r.geometry && r.geometry.location ? r.geometry.location.lat() : null,
                                    lng: r.geometry && r.geometry.location ? r.geometry.location.lng() : null
                                }));
                                resolve({ status: 'OK', items: mapped });
                            });
                        } catch (err) {
                            resolve({ status: 'EVAL_ERR', items: [] });
                        }
                    })""",
                    {"q": query, "lat": lat, "lng": lng, "radius": radius},
                )
                status = (res_obj or {}).get("status", "UNKNOWN")
                items = (res_obj or {}).get("items", [])
                if status == "OK" and items:
                    return items[:max_results]
                if status == "ZERO_RESULTS":
                    return []
                logger.warning("SDK textSearch вернул статус %s для '%s' (попытка %s), перезапускаем вкладку...", status, query, attempt + 1)
                await self.close_browser()
            except Exception as e:
                logger.warning("Ошибка SDK textSearch ('%s'): %s, перезапускаем браузер...", query, e)
                await self.close_browser()
        return []

    async def _fetch_details_via_sdk(self, place_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
        """
        Параллельно вытаскивает телефоны и сайты/Инстаграмы для тех точек, у которых они ещё не заполнены.
        """
        if not place_ids:
            return {}
        page = await self._ensure_page()
        raw_details = await page.evaluate(
            """(pids) => Promise.all(pids.map(pid => new Promise((resolve) => {
                try {
                    if (!window.google || !google.maps || !google.maps.places) {
                        return resolve({ pid, phone: null, website: null });
                    }
                    const svc = new google.maps.places.PlacesService(document.createElement('div'));
                    svc.getDetails(
                        { placeId: pid, fields: ['formatted_phone_number', 'international_phone_number', 'website', 'url'] },
                        (place, status) => {
                            if (status !== 'OK' || !place) {
                                return resolve({ pid, phone: null, website: null, url: null });
                            }
                            let fastUrl = place.url || null;
                            if (fastUrl && fastUrl.includes('maps.google.com/?cid=')) {
                                fastUrl = fastUrl.replace('https://maps.google.com/?cid=', 'https://www.google.com/maps?cid=');
                            }
                            resolve({
                                pid,
                                phone: place.international_phone_number || place.formatted_phone_number || null,
                                website: place.website || null,
                                url: fastUrl,
                            });
                        }
                    );
                } catch (e) {
                    resolve({ pid, phone: null, website: null, url: null });
                }
            })))""",
            place_ids,
        )
        out: Dict[str, Dict[str, Optional[str]]] = {}
        if isinstance(raw_details, list):
            for d in raw_details:
                pid = d.get("pid")
                if pid:
                    out[pid] = {
                        "phone": d.get("phone"),
                        "website": d.get("website"),
                        "url": d.get("url"),
                    }
        return out

    async def enrich_places_contacts(self, places: List[PlaceInfo]) -> None:
        """
        Обогащает список объектов PlaceInfo номерами телефонов и сайтами через резервный браузер
        ТОЛЬКО в том случае, если работал резервный браузерный движок (self._page уже открыт).
        Основной HTTP-движок (Tier-1) сразу возвращает телефоны и Instagram из JSON за 0.3 сек без запуска Chromium.
        """
        if not self._page or self._page.is_closed():
            return
        pids = [p.place_id for p in places if p.place_id and not p.phone and not p.website]
        if not pids:
            return
        async with self._lock:
            try:
                details_map = await self._fetch_details_via_sdk(pids)
                for p in places:
                    info = details_map.get(p.place_id)
                    if info:
                        if info.get("phone") and not p.phone:
                            p.phone = info["phone"]
                        if info.get("website") and not p.website:
                            p.website = info["website"]
                        if info.get("url") and not p.google_maps_url:
                            p.google_maps_url = info["url"]
            except Exception as e:
                logger.warning("Не удалось обогатить контакты для точек: %s", e)

    async def search_batch_raw(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """
        Массовый поиск заведений по запросу (для Автопоиска ТОП-лидов).
        Сначала использует мгновенный прямой HTTP-движок Google Maps (Tier-1),
        а при необходимости — резервный SDK (Tier-2).
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        # Tier-1: прямой HTTP-запрос к Google Maps (не требует браузера и не зависает на Railway)
        http_items = await self._http_search_google_maps(clean_query, max_results=max_results)
        if http_items:
            return http_items

        # Tier-2: резервный поиск через браузерный SDK
        async with self._lock:
            try:
                return await self._text_search_via_sdk(clean_query, max_results=max_results)
            except Exception as e:
                logger.error("Ошибка при массовом поиске '%s': %s", clean_query, e)
                await self.close_browser()
                return []

    @staticmethod
    def _items_to_place_infos(items: List[Dict[str, Any]], fallback_name: str, max_results: int) -> List[PlaceInfo]:
        results: List[PlaceInfo] = []
        for item in items:
            raw_id = (item.get("place_id") or "").strip()
            if not raw_id:
                continue
            clean_id = clean_place_id(raw_id)
            if not clean_id or any(x.place_id == clean_id for x in results):
                continue
            results.append(
                PlaceInfo(
                    place_id=clean_id,
                    name=(item.get("name") or fallback_name).strip(),
                    address=(item.get("address") or "Адрес в Google Maps").strip(),
                    rating=item.get("rating"),
                    user_ratings_total=item.get("user_ratings_total"),
                    review_url=build_review_url(clean_id),
                    google_maps_url=item.get("url") or f"https://www.google.com/maps/place/?q=place_id:{clean_id}",
                    latitude=item.get("lat"),
                    longitude=item.get("lng"),
                    phone=item.get("phone"),
                    website=item.get("website"),
                )
            )
            if len(results) >= max_results:
                break
        return results

    async def search_text(
        self,
        query: str,
        max_results: int = 3,
        lat: float = 41.3111,
        lng: float = 69.2797,
        radius: int = 30000,
    ) -> List[PlaceInfo]:
        """
        Ищет заведение по текстовому названию и возвращает список найденных точек
        с Place ID, реальным рейтингом, количеством отзывов, телефоном и сайтом.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        # 1. Уровень 1 (Основной): Прямой HTTP-движок Google Maps (0.3 сек, без браузера и без лимитов сессии)
        http_items = await self._http_search_google_maps(
            clean_query, max_results=max_results, lat=lat, lng=lng, radius=radius
        )
        if http_items:
            places = self._items_to_place_infos(http_items, clean_query, max_results)
            if places:
                return places

        # 2. Уровень 2 (Резервный): Фоновый браузер с авто-перезапуском при сбое
        async with self._lock:
            try:
                sdk_items = await self._text_search_via_sdk(
                    clean_query, max_results=max_results, lat=lat, lng=lng, radius=radius
                )
                if not sdk_items and "ташкент" not in clean_query.lower() and "tashkent" not in clean_query.lower():
                    sdk_items = await self._text_search_via_sdk(
                        f"{clean_query} Ташкент", max_results=max_results, lat=lat, lng=lng, radius=radius
                    )
                if sdk_items:
                    top_res = self._items_to_place_infos(sdk_items, clean_query, max_results)
                    if top_res:
                        details_map = await self._fetch_details_via_sdk([p.place_id for p in top_res])
                        for p in top_res:
                            info = details_map.get(p.place_id)
                            if info:
                                p.phone = info.get("phone") or p.phone
                                p.website = info.get("website") or p.website
                                if info.get("url"):
                                    p.google_maps_url = info["url"]
                        return top_res
                return []
            except Exception as e:
                logger.error("Ошибка при резервном поиске '%s': %s", clean_query, e)
                await self.close_browser()
                return []

    async def search_nearby(
        self,
        lat: float,
        lng: float,
        radius: int = 180,
        keyword: Optional[str] = None,
        max_results: int = 5,
    ) -> List[PlaceInfo]:
        """
        Поиск ближайших заведений вокруг отправленной геопозиции (в радиусе 180м).
        """
        q = keyword.strip() if keyword and keyword.strip() else "ресторан кафе бар кофейня клиника салон"
        http_items = await self._http_search_once(
            query=q, max_results=max_results, lat=lat, lng=lng, radius=max(200, radius)
        )
        if http_items:
            places = self._items_to_place_infos(http_items, "Заведение рядом", max_results)
            if places:
                return places

        return await self.search_text(q, max_results=max_results, lat=lat, lng=lng, radius=max(300, radius))

    async def search_by_url(self, short_url: str) -> List[PlaceInfo]:
        """
        Разворачивает короткую ссылку Google Maps (maps.app.goo.gl)
        и находит по ней точный Place ID вместе с рейтингом, отзывами и координатами филиала.
        """
        client = self._get_http_client()
        try:
            resp = await client.get(short_url)
            final_url = str(resp.url)
        except Exception as e:
            raise LinkExpansionError(f"Не удалось подключиться к ссылке: {e}")

        logger.info("Развернутая ссылка: %s", final_url)

        # Вытаскиваем точные координаты филиала из развернутой ссылки (если есть)
        lat_val, lng_val, rad_val = 41.3111, 69.2797, 30000
        coord_3d4d = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", final_url)
        coord_at = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final_url)
        if coord_3d4d:
            lat_val = float(coord_3d4d.group(1))
            lng_val = float(coord_3d4d.group(2))
            rad_val = 1500
        elif coord_at:
            lat_val = float(coord_at.group(1))
            lng_val = float(coord_at.group(2))
            rad_val = 2000

        name_match = re.search(r"/maps/place/([^/@?]+)", final_url)
        place_name = None
        if name_match:
            place_name = urllib.parse.unquote_plus(name_match.group(1)).strip()
            place_name = re.sub(r"\s+", " ", place_name)

        if not place_name:
            parsed = urllib.parse.urlparse(final_url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "q" in qs:
                place_name = qs["q"][0].strip()

        if not place_name:
            if rad_val <= 2000:
                return await self.search_nearby(lat=lat_val, lng=lng_val, radius=150)
            raise LinkExpansionError(
                "Не удалось извлечь название заведения из ссылки. "
                "Попробуйте отправить текстовое название заведения."
            )

        logger.info("Найдено название заведения из ссылки: '%s' (coords: %s, %s)", place_name, lat_val, lng_val)
        return await self.search_text(place_name, lat=lat_val, lng=lng_val, radius=rad_val)

    async def close_browser(self):
        """Перезапускает/освобождает только ресурсы браузера Chromium."""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._browser = None
        self._playwright = None

    async def close(self):
        """Освобождает все ресурсы (HTTP-клиент и браузер) при остановке бота."""
        await self.close_browser()
        try:
            if self._http_client and not self._http_client.is_closed:
                await self._http_client.aclose()
        except Exception:
            pass
        self._http_client = None


# Глобальный экземпляр автономного сервиса
keyless_places_service = KeylessPlacesService()
