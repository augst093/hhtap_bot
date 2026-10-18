import re
import urllib.parse
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any
import httpx

from config import config

logger = logging.getLogger(__name__)

# Регулярные выражения для парсинга ссылок Google Maps
RE_SHORT_URL = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|www\.google\.com/maps)/[^\s]+",
    re.IGNORECASE,
)
RE_PLACE_NAME = re.compile(r"/maps/place/([^/@?]+)", re.IGNORECASE)
RE_COORDS_AT = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
RE_COORDS_DATA = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
RE_CID = re.compile(r"(?:cid=|!1s0x[0-9a-fA-F]+:(0x[0-9a-fA-F]+))")


class GooglePlacesError(Exception):
    """Базовое исключение для ошибок Google Places API."""

    def __init__(self, message: str, status_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ZeroResultsError(GooglePlacesError):
    """Заведение не найдено по данному запросу."""
    pass


class RequestDeniedError(GooglePlacesError):
    """Ошибка доступа или неверный API-ключ."""
    pass


class QuotaExceededError(GooglePlacesError):
    """Превышена квота запросов."""
    pass


class LinkExpansionError(GooglePlacesError):
    """Не удалось развернуть или распарсить ссылку Google Maps."""
    pass


def clean_place_id(place_id: str) -> str:
    """
    Очищает place_id от любых пробелов, невидимых символов,
    переносов строк и zero-width символов.
    Google Place ID содержит только символы [A-Za-z0-9_-].
    """
    if not place_id:
        return ""
    # Удаляем невидимые Unicode-символы, пробелы, переводы строк
    cleaned = re.sub(r"[\s\u200b\u200c\u200d\ufeff\u00a0\r\n\t]+", "", place_id)
    # Оставляем только допустимые для base64/place_id символы
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "", cleaned)
    return cleaned.strip()


def build_review_url(place_id: str) -> str:
    """
    Генерирует прямую ссылку для записи на NFC-чип (NFC Tools),
    которая открывает всплывающее окно оценки 5 звезд и написания отзыва.
    Гарантированно не содержит пробелов и символов %20.
    """
    clean_id = clean_place_id(place_id)
    if not clean_id:
        raise ValueError("Невозможно создать ссылку для пустого place_id")
    url = f"https://search.google.com/local/writereview?placeid={clean_id}".strip()
    return url


@dataclass
class PlaceInfo:
    place_id: str
    name: str
    address: str
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    review_url: str = ""
    google_maps_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @classmethod
    def from_places_new(cls, item: Dict[str, Any]) -> "PlaceInfo":
        """Создает объект PlaceInfo из ответа Google Places API (New)."""
        raw_id = item.get("id", "")
        clean_id = clean_place_id(raw_id)
        display_name_obj = item.get("displayName") or {}
        name = display_name_obj.get("text") or "Без названия"
        address = item.get("formattedAddress") or "Адрес не указан"
        rating = item.get("rating")
        if rating is not None:
            rating = float(rating)
        user_ratings_total = item.get("userRatingCount")
        if user_ratings_total is not None:
            user_ratings_total = int(user_ratings_total)

        loc = item.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")

        review_url = build_review_url(clean_id)
        maps_url = item.get("googleMapsUri")

        return cls(
            place_id=clean_id,
            name=name.strip(),
            address=address.strip(),
            rating=rating,
            user_ratings_total=user_ratings_total,
            review_url=review_url,
            google_maps_url=maps_url,
            latitude=lat,
            longitude=lng,
        )

    @classmethod
    def from_places_legacy(cls, item: Dict[str, Any]) -> "PlaceInfo":
        """Создает объект PlaceInfo из ответа Google Places API (Legacy)."""
        raw_id = item.get("place_id", "")
        clean_id = clean_place_id(raw_id)
        name = item.get("name") or "Без названия"
        address = item.get("formatted_address") or item.get("vicinity") or "Адрес не указан"
        rating = item.get("rating")
        if rating is not None:
            rating = float(rating)
        user_ratings_total = item.get("user_ratings_total")
        if user_ratings_total is not None:
            user_ratings_total = int(user_ratings_total)

        geometry = item.get("geometry") or {}
        loc = geometry.get("location") or {}
        lat = loc.get("lat")
        lng = loc.get("lng")

        review_url = build_review_url(clean_id)
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{clean_id}"

        return cls(
            place_id=clean_id,
            name=name.strip(),
            address=address.strip(),
            rating=rating,
            user_ratings_total=user_ratings_total,
            review_url=review_url,
            google_maps_url=maps_url,
            latitude=lat,
            longitude=lng,
        )


class GooglePlacesService:
    """
    Асинхронный сервис для работы с Google Places API
    с поддержкой Places API (New) и автоматическим fallback на Legacy Text Search API,
    а также разворачивания коротких ссылок maps.app.goo.gl.
    """

    NEW_API_BASE = "https://places.googleapis.com/v1"
    LEGACY_API_BASE = "https://maps.googleapis.com/maps/api/place"

    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or config.google_api_key).strip()

    async def expand_short_url(self, short_url: str) -> Dict[str, Any]:
        """
        Разворачивает короткую ссылку Google Maps (maps.app.goo.gl, goo.gl/maps и т.д.)
        до полного URL и парсит название заведения, координаты и другие параметры.
        """
        cleaned_url = short_url.strip()
        logger.info("Разворачиваем короткую ссылку: %s", cleaned_url)

        async with httpx.AsyncClient(
            headers=self.BROWSER_HEADERS,
            follow_redirects=True,
            timeout=15.0,
        ) as client:
            try:
                response = await client.get(cleaned_url)
            except Exception as e:
                logger.error("Ошибка при запросе к ссылке %s: %s", cleaned_url, e)
                raise LinkExpansionError(f"Не удалось подключиться к ссылке: {e}")

        final_url = str(response.url)
        logger.info("Итоговый URL после редиректов: %s", final_url)

        # Если попали на consent.google.com или google.com/url, извлекаем целевой URL
        parsed = urllib.parse.urlparse(final_url)
        qs = urllib.parse.parse_qs(parsed.query)

        if "continue" in qs:
            final_url = qs["continue"][0]
        elif "url" in qs:
            final_url = qs["url"][0]
        elif "q" in qs and qs["q"][0].startswith("http"):
            final_url = qs["q"][0]

        # Если страница вернула HTML с meta-редиректом или канонической ссылкой
        if response.text and ("maps.app.goo.gl" in final_url or "consent" in final_url):
            meta_refresh = re.search(
                r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\'][^"\']*url=([^"\']+)["\']',
                response.text,
                re.IGNORECASE,
            )
            if meta_refresh:
                final_url = meta_refresh.group(1).replace("&amp;", "&")
            else:
                canonical = re.search(
                    r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']+)["\']',
                    response.text,
                    re.IGNORECASE,
                )
                if canonical:
                    final_url = canonical.group(1).replace("&amp;", "&")

        # Парсим параметры из итогового URL
        place_name = None
        lat, lng = None, None

        # 1. Проверяем /place/Name/
        name_match = RE_PLACE_NAME.search(final_url)
        if name_match:
            raw_name = name_match.group(1)
            # URL-декодируем имя заведения и заменяем плюсы на пробелы
            place_name = urllib.parse.unquote_plus(raw_name).strip()
            # Очищаем от мусорных параметров, если попали
            place_name = re.sub(r"\s+", " ", place_name)

        # 2. Проверяем координаты @lat,lng
        coords_match = RE_COORDS_AT.search(final_url)
        if coords_match:
            lat = float(coords_match.group(1))
            lng = float(coords_match.group(2))
        else:
            # 3. Проверяем data=!3d...!4d...
            data_coords = RE_COORDS_DATA.search(final_url)
            if data_coords:
                lat = float(data_coords.group(1))
                lng = float(data_coords.group(2))

        # 4. Проверяем query params
        parsed_final = urllib.parse.urlparse(final_url)
        final_qs = urllib.parse.parse_qs(parsed_final.query)
        if not place_name:
            if "q" in final_qs:
                q_val = final_qs["q"][0].strip()
                # Если в q переданы координаты, берем их
                coord_q = re.match(r"^(-?\d+\.\d+),(-?\d+\.\d+)$", q_val)
                if coord_q:
                    lat, lng = float(coord_q.group(1)), float(coord_q.group(2))
                else:
                    place_name = q_val
            elif "query" in final_qs:
                place_name = final_qs["query"][0].strip()

        logger.info(
            "Извлечено из ссылки: name='%s', lat=%s, lng=%s",
            place_name,
            lat,
            lng,
        )

        if not place_name and not (lat and lng):
            raise LinkExpansionError(
                "Не удалось извлечь название заведения или координаты из ссылки. "
                "Убедитесь, что это ссылка на конкретное заведение в Google Maps."
            )

        return {
            "final_url": final_url,
            "place_name": place_name,
            "latitude": lat,
            "longitude": lng,
        }

    async def search_by_url(self, url: str) -> List[PlaceInfo]:
        """
        Сценарий Б: поиск заведения по короткой или полной ссылке Google Maps.
        """
        extracted = await self.expand_short_url(url)
        place_name = extracted.get("place_name")
        lat = extracted.get("latitude")
        lng = extracted.get("longitude")

        if place_name:
            # Если есть имя и координаты, ищем по имени с жестким гео-приоритетом
            radius = 2000 if (lat and lng) else config.default_radius_meters
            return await self.search_text(
                query=place_name,
                lat=lat,
                lng=lng,
                radius=radius,
                max_results=3,
            )
        elif lat and lng:
            # Если есть только координаты, ищем ближайшее заведение
            return await self.search_nearby(
                lat=lat,
                lng=lng,
                radius=150,
                max_results=3,
            )

        raise ZeroResultsError("Не удалось определить заведение по данной ссылке.")

    async def search_text(
        self,
        query: str,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        radius: Optional[int] = None,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """
        Сценарий А: текстовый поиск по названию/адресу.
        Сначала пробует Places API (New), при ошибке или отсутствии доступа —
        автоматически переключается на Legacy Text Search API.
        """
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Поисковый запрос не может быть пустым.")

        use_lat = lat if lat is not None else config.default_lat
        use_lng = lng if lng is not None else config.default_lng
        use_radius = radius if radius is not None else config.default_radius_meters

        # 1. Попытка запроса к Places API (New)
        try:
            results = await self._search_text_new(
                query=clean_query,
                lat=use_lat,
                lng=use_lng,
                radius=use_radius,
                max_results=max_results,
            )
            if results:
                return results
            logger.info("Places API (New) вернул пустой список, пробуем Legacy API...")
        except (RequestDeniedError, QuotaExceededError) as e:
            # Критические ошибки доступа пробрасываем дальше, чтобы уведомить пользователя
            logger.warning("Places API (New) ошибка доступа: %s, пробуем Legacy...", e)
        except Exception as e:
            logger.warning("Places API (New) выбросил ошибку: %s. Переключаемся на Legacy API.", e)

        # 2. Fallback на Legacy Text Search API
        return await self._search_text_legacy(
            query=clean_query,
            lat=use_lat,
            lng=use_lng,
            radius=use_radius,
            max_results=max_results,
        )

    async def _search_text_new(
        self,
        query: str,
        lat: float,
        lng: float,
        radius: int,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """Реализация поиска через Places API (New) v1/places:searchText."""
        url = f"{self.NEW_API_BASE}/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.rating,places.userRatingCount,places.location,places.googleMapsUri"
            ),
        }
        payload: Dict[str, Any] = {
            "textQuery": query,
            "languageCode": config.default_language,
            "pageSize": min(max_results, 5),
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius),
                }
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)

        if resp.status_code == 403:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            msg = data.get("error", {}).get("message", "REQUEST_DENIED")
            raise RequestDeniedError(f"Places API (New) 403: {msg}")

        if resp.status_code == 429:
            raise QuotaExceededError("Превышена квота Places API (New).")

        resp.raise_for_status()
        data = resp.json()

        places = data.get("places", [])
        if not places:
            return []

        results = []
        for item in places[:max_results]:
            p = PlaceInfo.from_places_new(item)
            if p.place_id:
                results.append(p)
        return results

    async def _search_text_legacy(
        self,
        query: str,
        lat: float,
        lng: float,
        radius: int,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """Реализация поиска через Legacy Text Search API."""
        url = f"{self.LEGACY_API_BASE}/textsearch/json"
        params = {
            "query": query,
            "key": self.api_key,
            "language": config.default_language,
            "location": f"{lat},{lng}",
            "radius": str(radius),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        resp.raise_for_status()
        data = resp.json()

        status = data.get("status", "")
        if status == "ZERO_RESULTS":
            return []
        elif status == "REQUEST_DENIED":
            error_msg = data.get("error_message", "Проверьте GOOGLE_API_KEY")
            raise RequestDeniedError(f"Доступ отклонен Google API: {error_msg}")
        elif status == "OVER_QUERY_LIMIT":
            raise QuotaExceededError("Превышен лимит запросов Google Places API.")
        elif status not in ("OK", ""):
            error_msg = data.get("error_message", status)
            raise GooglePlacesError(f"Ошибка Google Places API: {error_msg}", status_code=status)

        items = data.get("results", [])
        results = []
        for item in items[:max_results]:
            p = PlaceInfo.from_places_legacy(item)
            if p.place_id:
                results.append(p)
        return results

    async def search_nearby(
        self,
        lat: float,
        lng: float,
        radius: int = 200,
        keyword: Optional[str] = None,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """
        Поиск заведений в радиусе (по умолчанию 200 метров) от геопозиции.
        Используется при отправке пользователем точки геолокации в Telegram.
        """
        # Сначала пробуем Places API (New) nearbySearch
        try:
            results = await self._search_nearby_new(
                lat=lat,
                lng=lng,
                radius=radius,
                keyword=keyword,
                max_results=max_results,
            )
            if results:
                return results
        except Exception as e:
            logger.warning("Places API (New) nearbySearch не сработал: %s, переходим на Legacy", e)

        # Fallback на Legacy Nearby Search
        return await self._search_nearby_legacy(
            lat=lat,
            lng=lng,
            radius=radius,
            keyword=keyword,
            max_results=max_results,
        )

    async def _search_nearby_new(
        self,
        lat: float,
        lng: float,
        radius: int = 200,
        keyword: Optional[str] = None,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """Реализация nearby поиска через Places API (New)."""
        url = f"{self.NEW_API_BASE}/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.rating,places.userRatingCount,places.location,places.googleMapsUri"
            ),
        }
        payload: Dict[str, Any] = {
            "maxResultCount": min(max_results, 5),
            "languageCode": config.default_language,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius),
                }
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)

        if resp.status_code == 403:
            raise RequestDeniedError("Places API (New) 403: Доступ отклонен")
        if resp.status_code == 429:
            raise QuotaExceededError("Превышена квота Places API")

        resp.raise_for_status()
        data = resp.json()
        places = data.get("places", [])
        results = []
        for item in places[:max_results]:
            p = PlaceInfo.from_places_new(item)
            if p.place_id:
                results.append(p)
        return results

    async def _search_nearby_legacy(
        self,
        lat: float,
        lng: float,
        radius: int = 200,
        keyword: Optional[str] = None,
        max_results: int = 3,
    ) -> List[PlaceInfo]:
        """Реализация nearby поиска через Legacy Nearby Search API."""
        url = f"{self.LEGACY_API_BASE}/nearbysearch/json"
        params: Dict[str, str] = {
            "location": f"{lat},{lng}",
            "radius": str(radius),
            "language": config.default_language,
            "key": self.api_key,
        }
        if keyword:
            params["keyword"] = keyword

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        resp.raise_for_status()
        data = resp.json()

        status = data.get("status", "")
        if status == "ZERO_RESULTS":
            return []
        elif status == "REQUEST_DENIED":
            error_msg = data.get("error_message", "Проверьте GOOGLE_API_KEY")
            raise RequestDeniedError(f"Доступ отклонен Google API: {error_msg}")
        elif status == "OVER_QUERY_LIMIT":
            raise QuotaExceededError("Превышен лимит запросов Google Places API.")
        elif status not in ("OK", ""):
            error_msg = data.get("error_message", status)
            raise GooglePlacesError(f"Ошибка Google Places API: {error_msg}", status_code=status)

        items = data.get("results", [])
        results = []
        for item in items[:max_results]:
            p = PlaceInfo.from_places_legacy(item)
            if p.place_id:
                results.append(p)
        return results


google_places_service = GooglePlacesService()
