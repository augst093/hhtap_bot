import asyncio
import logging
import re
import urllib.parse
from typing import List, Optional
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
    Автономный сервис для получения Place ID и прямых ссылок для NFC Tools
    БЕЗ использования платных ключей Google Cloud и БЕЗ привязки банковских карт.
    Использует легковесный фоновый Chromium (Playwright).
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    async def _ensure_page(self) -> Page:
        """Поддерживает активную вкладку поиска с минимальным потреблением ресурсов."""
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
        logger.info("Загрузка сервиса Google Maps...")
        await self._page.goto(FINDER_URL, wait_until="networkidle", timeout=30000)
        await self._page.wait_for_selector("#pac-input", timeout=15000)
        logger.info("Фоновый браузер успешно инициализирован.")
        return self._page

    async def search_text(self, query: str, max_results: int = 3) -> List[PlaceInfo]:
        """
        Ищет заведение по текстовому названию и возвращает список найденных точек с Place ID.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        async with self._lock:
            try:
                page = await self._ensure_page()

                # Находим строку поиска
                input_elem = await page.wait_for_selector("#pac-input", timeout=10000)
                if not input_elem:
                    return []

                # Очищаем поле и кликаем
                await input_elem.click()
                await input_elem.fill("")
                await asyncio.sleep(0.1)

                # Печатаем запрос с небольшой задержкой для срабатывания Google Autocomplete
                await input_elem.type(clean_query, delay=35)

                # Ожидаем появления выпадающего списка вариантов Google (.pac-item)
                try:
                    await page.wait_for_selector(".pac-item", timeout=5000)
                except Exception:
                    logger.info("Подсказки Google Autocomplete не появились для '%s'", clean_query)
                    return []

                # Проверяем количество доступных подсказок
                items_count = await page.evaluate(
                    "() => document.querySelectorAll('.pac-item').length"
                )

                if items_count == 0:
                    return []

                count_to_fetch = min(items_count, max_results)
                results: List[PlaceInfo] = []

                # Извлекаем данные для заведений
                for idx in range(count_to_fetch):
                    # Если нужно повторно ввести запрос для следующего филиала
                    if idx > 0:
                        await input_elem.click()
                        await input_elem.fill("")
                        await input_elem.type(clean_query, delay=25)
                        try:
                            await page.wait_for_selector(".pac-item", timeout=3000)
                        except Exception:
                            break

                    # Нажимаем стрелку вниз нужное количество раз и Enter
                    for _ in range(idx + 1):
                        await page.keyboard.press("ArrowDown")
                        await asyncio.sleep(0.1)

                    await page.keyboard.press("Enter")
                    await asyncio.sleep(0.8)

                    # Считываем результат
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
        и находит по ней точный Place ID.
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

        # 1. Извлекаем название из /maps/place/Name/
        name_match = re.search(r"/maps/place/([^/@?]+)", final_url)
        place_name = None
        if name_match:
            place_name = urllib.parse.unquote_plus(name_match.group(1)).strip()
            place_name = re.sub(r"\s+", " ", place_name)

        if not place_name:
            # 2. Извлекаем из query параметра q
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
