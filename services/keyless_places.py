import asyncio
import logging
import re
import urllib.parse
from typing import List, Optional, Dict, Any
import httpx
from playwright.async_api import async_playwright, Playwright, Browser, Page

from services.google_places import (
    clean_place_id,
    build_review_url,
    PlaceInfo,
    LinkExpansionError,
)

logger = logging.getLogger(__name__)

# Официальный веб-интерфейс Google Place ID Finder (работает без API-ключей)
FINDER_URL = "https://geo-devrel-javascript-samples.web.app/samples/places-placeid-finder/app/dist/"


class KeylessPlacesService:
    """
    Автономный сервис для получения Place ID, рейтингов, количества отзывов
    и прямых ссылок для NFC Tools БЕЗ платных ключей Google Cloud.
    Использует встроенный объект google.maps.places.PlacesService внутри
    фонового Chromium (Playwright), получая до 20 точек за 0.3 сек.
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    async def _ensure_page(self) -> Page:
        """Поддерживает активную вкладку с загруженным Google Maps Places SDK."""
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

    async def _text_search_via_sdk(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """
        Выполняет быстрый запрос через google.maps.places.PlacesService.textSearch
        прямо внутри страницы, возвращая до 20 заведений с точным рейтингом и числом отзывов.
        """
        page = await self._ensure_page()
        raw_items = await page.evaluate(
            """(q) => new Promise((resolve) => {
                try {
                    if (!window.google || !google.maps || !google.maps.places) {
                        return resolve([]);
                    }
                    const svc = new google.maps.places.PlacesService(document.createElement('div'));
                    svc.textSearch({ query: q, language: 'ru' }, (results, status) => {
                        if (status !== 'OK' || !results) {
                            return resolve([]);
                        }
                        const mapped = results.map(r => ({
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
                        resolve(mapped);
                    });
                } catch (err) {
                    resolve([]);
                }
            })""",
            query,
        )
        if not raw_items:
            return []
        return raw_items[:max_results]

    async def _fetch_details_via_sdk(self, place_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
        """
        Параллельно (через Promise.all за 0.2 сек) вытаскивает телефоны и сайты/Инстаграмы
        для списка place_id из карточки Google Maps.
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
        """Обогащает список объектов PlaceInfo номерами телефонов, сайтами/Инстаграмами и быстрой CID-ссылкой."""
        pids = [p.place_id for p in places if p.place_id and not p.phone]
        if not pids:
            return
        async with self._lock:
            try:
                details_map = await self._fetch_details_via_sdk(pids)
                for p in places:
                    info = details_map.get(p.place_id)
                    if info:
                        if info.get("phone"):
                            p.phone = info["phone"]
                        if info.get("website"):
                            p.website = info["website"]
                        if info.get("url"):
                            p.google_maps_url = info["url"]
            except Exception as e:
                logger.warning("Не удалось обогатить контакты для точек: %s", e)

    async def search_batch_raw(self, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
        """
        Массовый поиск заведений по запросу (для Автопоиска ТОП-лидов).
        Возвращает сырые словари с рейтингами, количеством отзывов, типами и статусом.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        async with self._lock:
            try:
                return await self._text_search_via_sdk(clean_query, max_results=max_results)
            except Exception as e:
                logger.error("Ошибка при массовом поиске '%s': %s", clean_query, e)
                await self.close()
                return []

    async def search_text(self, query: str, max_results: int = 3) -> List[PlaceInfo]:
        """
        Ищет заведение по текстовому названию и возвращает список найденных точек
        с Place ID, реальным рейтингом, количеством отзывов, телефоном и сайтом.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        async with self._lock:
            try:
                # 1. Сначала пробуем быстрый SDK textSearch (сразу даёт рейтинг и отзывы за 0.3 сек)
                sdk_items = await self._text_search_via_sdk(clean_query, max_results=max_results)
                if sdk_items:
                    results: List[PlaceInfo] = []
                    for item in sdk_items:
                        raw_id = item.get("place_id", "").strip()
                        if not raw_id:
                            continue
                        clean_id = clean_place_id(raw_id)
                        if not clean_id or any(x.place_id == clean_id for x in results):
                            continue
                        results.append(
                            PlaceInfo(
                                place_id=clean_id,
                                name=(item.get("name") or clean_query).strip(),
                                address=(item.get("address") or "Адрес в Google Maps").strip(),
                                rating=item.get("rating"),
                                user_ratings_total=item.get("user_ratings_total"),
                                review_url=build_review_url(clean_id),
                                google_maps_url=f"https://www.google.com/maps/place/?q=place_id:{clean_id}",
                                latitude=item.get("lat"),
                                longitude=item.get("lng"),
                            )
                        )
                    if results:
                        top_res = results[:max_results]
                        details_map = await self._fetch_details_via_sdk([p.place_id for p in top_res])
                        for p in top_res:
                            info = details_map.get(p.place_id)
                            if info:
                                p.phone = info.get("phone")
                                p.website = info.get("website")
                                if info.get("url"):
                                    p.google_maps_url = info["url"]
                        return top_res

                # 2. Fallback на Autocomplete (#pac-input), если textSearch ничего не вернул
                page = await self._ensure_page()
                input_elem = await page.wait_for_selector("#pac-input", timeout=10000)
                if not input_elem:
                    return []

                await input_elem.click()
                await input_elem.fill("")
                await asyncio.sleep(0.1)
                await input_elem.type(clean_query, delay=35)

                try:
                    await page.wait_for_selector(".pac-item", timeout=5000)
                except Exception:
                    logger.info("Подсказки Google Autocomplete не появились для '%s'", clean_query)
                    return []

                items_count = await page.evaluate(
                    "() => document.querySelectorAll('.pac-item').length"
                )
                if items_count == 0:
                    return []

                count_to_fetch = min(items_count, max_results)
                results = []

                for idx in range(count_to_fetch):
                    if idx > 0:
                        await input_elem.click()
                        await input_elem.fill("")
                        await input_elem.type(clean_query, delay=25)
                        try:
                            await page.wait_for_selector(".pac-item", timeout=3000)
                        except Exception:
                            break

                    for _ in range(idx + 1):
                        await page.keyboard.press("ArrowDown")
                        await asyncio.sleep(0.1)

                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.8)

                    data = await page.evaluate(
                        """() => {
                            const placeId = document.getElementById('place-id')?.textContent || '';
                            const placeName = document.getElementById('place-name')?.textContent || '';
                            const placeAddress = document.getElementById('place-address')?.textContent || '';
                            return { placeId, placeName, placeAddress };
                        }"""
                    )

                    raw_id = data.get("placeId", "").strip()
                    if raw_id:
                        clean_id = clean_place_id(raw_id)
                        name = (data.get("placeName") or clean_query).strip()
                        address = (data.get("placeAddress") or "Адрес в Google Maps").strip()
                        review_url = build_review_url(clean_id)

                        p = PlaceInfo(
                            place_id=clean_id,
                            name=name,
                            address=address,
                            rating=None,
                            user_ratings_total=None,
                            review_url=review_url,
                        )

                        if not any(x.place_id == clean_id for x in results):
                            results.append(p)

                return results

            except Exception as e:
                logger.error("Ошибка при автономном поиске: %s", e)
                await self.close()
                return []

    async def search_by_url(self, short_url: str) -> List[PlaceInfo]:
        """
        Разворачивает короткую ссылку Google Maps (maps.app.goo.gl)
        и находит по ней точный Place ID вместе с рейтингом и отзывами.
        """
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            },
        ) as client:
            try:
                resp = await client.get(short_url)
                final_url = str(resp.url)
            except Exception as e:
                raise LinkExpansionError(f"Не удалось подключиться к ссылке: {e}")

        logger.info("Развернутая ссылка: %s", final_url)

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
            raise LinkExpansionError(
                "Не удалось извлечь название заведения из ссылки. "
                "Попробуйте отправить текстовое название заведения."
            )

        logger.info("Найдено название заведения из ссылки: '%s'", place_name)
        return await self.search_text(place_name)

    async def close(self):
        """Освобождает ресурсы браузера при остановке бота."""
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


keyless_places_service = KeylessPlacesService()
