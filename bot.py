import asyncio
import hashlib
import html
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

from config import config
from services.ai_sales import AISalesError, ai_sales_service
from services.google_places import (
    GooglePlacesError,
    LinkExpansionError,
    PlaceInfo,
    QuotaExceededError,
    RequestDeniedError,
    ZeroResultsError,
    google_places_service,
)
from services.keyless_places import keyless_places_service
from services.lead_hunter import (
    HUNT_DISTRICTS,
    HUNT_NICHES,
    ScoredLead,
    lead_hunter_service,
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hhtap_bot")

# Инициализация роутера
router = Router()

# Регулярное выражение для поиска ссылок Google Maps в тексте
MAPS_URL_REGEX = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.com|www\.google\.com/maps)[^\s]+",
    re.IGNORECASE,
)

# Словарь названий возражений для передачи в промпт
OBJECTION_TITLES = {
    "obj_qr": "У нас уже есть QR-код",
    "obj_price": "Дорого / Не актуально",
    "obj_think": "Скиньте КП, мы подумаем",
}

# Тексты кнопок нижнего меню
BTN_HUNT_MENU = "🎯 Автопоиск ТОП-лидов"
BTN_QUICK_RAID = "🔥 Быстрый рейд (Топ-5)"
BTN_STATS = "📊 База и критерии"
BTN_RESET_SEEN = "🔄 Сбросить просмотренные"


# ==============================================================================
# In-memory кэш найденных заведений и каруселей ТОП-5
# ==============================================================================
@dataclass
class CachedPlace:
    place_id: str
    name: str
    address: str
    rating: Optional[float]
    reviews_count: Optional[int]
    review_link: str
    batch_id: Optional[str] = None
    slide_idx: Optional[int] = None


@dataclass
class HuntCarouselBatch:
    batch_id: str
    niche_key: str
    district_key: str
    scanned_total: int
    leads: List[ScoredLead]


place_cache: Dict[str, CachedPlace] = {}
carousel_cache: Dict[str, HuntCarouselBatch] = {}


def cache_place(
    place: PlaceInfo,
    batch_id: Optional[str] = None,
    slide_idx: Optional[int] = None,
) -> str:
    """Сохраняет данные заведения в кэш и возвращает короткий 8-символьный хэш."""
    raw = f"{place.place_id}:{place.name}"
    short_id = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    place_cache[short_id] = CachedPlace(
        place_id=place.place_id,
        name=place.name,
        address=place.address,
        rating=place.rating,
        reviews_count=place.user_ratings_total,
        review_link=place.review_url,
        batch_id=batch_id,
        slide_idx=slide_idx,
    )
    if len(place_cache) > 2000:
        oldest_key = next(iter(place_cache))
        del place_cache[oldest_key]
    return short_id


def cache_carousel_batch(
    niche_key: str,
    district_key: str,
    scanned_total: int,
    leads: List[ScoredLead],
) -> str:
    """Сохраняет пачку из 5 лидов для перелистывания в одном сообщении."""
    batch_id = uuid.uuid4().hex[:8]
    carousel_cache[batch_id] = HuntCarouselBatch(
        batch_id=batch_id,
        niche_key=niche_key,
        district_key=district_key,
        scanned_total=scanned_total,
        leads=leads,
    )
    if len(carousel_cache) > 500:
        oldest = next(iter(carousel_cache))
        del carousel_cache[oldest]
    return batch_id


# ==============================================================================
# Клавиатуры и карточки
# ==============================================================================
def make_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное нижнее меню для запуска Автопоиска лидов в 1 клик."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_HUNT_MENU),
                KeyboardButton(text=BTN_QUICK_RAID),
            ],
            [
                KeyboardButton(text=BTN_STATS),
                KeyboardButton(text=BTN_RESET_SEEN),
            ],
        ],
        resize_keyboard=True,
    )


def make_hunt_niche_keyboard() -> InlineKeyboardMarkup:
    """Шаг 1 Автопоиска: выбор ниши."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🍽 Рестораны и Гастробары", callback_data="hunt:niche:rest"),
                InlineKeyboardButton(text="🍸 Бары и Лаунжи", callback_data="hunt:niche:bar"),
            ],
            [
                InlineKeyboardButton(text="☕️ Кофейни и Пекарни", callback_data="hunt:niche:coffee"),
                InlineKeyboardButton(text="🦷 Клиники и Стоматологии", callback_data="hunt:niche:clinic"),
            ],
            [
                InlineKeyboardButton(text="💈 Барбершопы и Салоны", callback_data="hunt:niche:beauty"),
            ],
            [
                InlineKeyboardButton(
                    text="🔥 Все топовые ниши сразу (Авто-микс)",
                    callback_data="hunt:niche:all",
                )
            ],
        ]
    )


def make_hunt_district_keyboard(niche_key: str) -> InlineKeyboardMarkup:
    """Шаг 2 Автопоиска: выбор района / денежных улиц Ташкента."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Весь Ташкент (Самые денежные улицы)",
                    callback_data=f"hunt:run:{niche_key}:auto",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏙 Мирабад (Т. Шевченко / Ойбек / Шахрисабз)",
                    callback_data=f"hunt:run:{niche_key}:mirabad",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏛 Яккасарай (Ш. Руставели / Бабура / Seoul Mun)",
                    callback_data=f"hunt:run:{niche_key}:yakkasaray",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌳 Юнусабад (Ц-4 / Ц-5 / Амир Темур)",
                    callback_data=f"hunt:run:{niche_key}:yunusabad",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Мирзо-Улугбек (Ц-1 / Паркентская / Дархан)",
                    callback_data=f"hunt:run:{niche_key}:mirzo",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎡 Tashkent City / Центр / Чиланзар",
                    callback_data=f"hunt:run:{niche_key}:center",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к выбору ниши",
                    callback_data="hunt:menu",
                )
            ],
        ]
    )


def format_place_card(place: PlaceInfo) -> str:
    """Форматирует стандартную карточку заведения (для ручного поиска)."""
    rating_part = ""
    if place.rating is not None:
        rating_part = f"⭐️ {place.rating:.1f}"
        if place.user_ratings_total is not None:
            rating_part += f" | {place.user_ratings_total} отзывов"
    else:
        rating_part = "⭐️ Точка найдена"

    card = (
        f"🏢 <b>{html.escape(place.name)}</b> ({rating_part})\n"
        f"📍 <i>{html.escape(place.address)}</i>\n\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )
    return card


def format_carousel_slide(batch: HuntCarouselBatch, idx: int) -> str:
    """
    Форматирует один слайд карусели ТОП-лидов в едином сообщении.
    """
    total = len(batch.leads)
    lead = batch.leads[idx]
    place = lead.place
    niche_info = HUNT_NICHES.get(batch.niche_key, HUNT_NICHES["all"])
    district_info = HUNT_DISTRICTS.get(batch.district_key, HUNT_DISTRICTS["auto"])

    if place.rating is not None:
        rating_part = f"⭐️ {place.rating:.1f}"
        if place.user_ratings_total is not None:
            rating_part += f" | {place.user_ratings_total} отзывов"
    else:
        rating_part = "⭐️ Мало оценок"

    return (
        f"🎯 <b>{html.escape(niche_info['short'])} • {html.escape(district_info['short'])}</b> "
        f"<i>(просканировано: {batch.scanned_total})</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>ТОП-{idx + 1} из {total} | Оценка лида: {lead.score}/100</b>\n"
        f"🏷 <b>Статус:</b> {html.escape(lead.tier_badge)}\n\n"
        f"🏢 <b>{html.escape(place.name)}</b> ({rating_part})\n"
        f"📍 <i>{html.escape(place.address)}</i>\n\n"
        f"💡 <b>Почему купят:</b>\n{html.escape(lead.why_hot)}\n\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )


def make_carousel_keyboard(batch: HuntCarouselBatch, idx: int) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру для текущего слайда карусели:
    - Все кнопки работы с заведением (Проверить ссылку, Инста, Телеграм, ГС, Возражения)
    - Стрелки перелистывания ⬅️ [ 1 / 5 ] ➡️
    - Кнопка добора следующих 5 новых лидов и смены района
    """
    total = len(batch.leads)
    lead = batch.leads[idx]
    short_id = cache_place(lead.place, batch_id=batch.batch_id, slide_idx=idx)

    # Строка навигации со стрелками влево/вправо
    nav_row: List[InlineKeyboardButton] = []
    if idx > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Предыдущий",
                callback_data=f"hunt:page:{batch.batch_id}:{idx - 1}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"📍 {idx + 1} из {total}",
            callback_data="hunt:noop",
        )
    )
    if idx < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Следующий ➡️",
                callback_data=f"hunt:page:{batch.batch_id}:{idx + 1}",
            )
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Проверить ссылку (5⭐️)",
                    url=lead.place.review_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="📸 Инста Direct",
                    callback_data=f"ai:{short_id}:insta",
                ),
                InlineKeyboardButton(
                    text="✈️ Телеграм (под видео)",
                    callback_data=f"ai:{short_id}:tg",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎤 Текст для ГС/Кружка",
                    callback_data=f"ai:{short_id}:voice",
                ),
                InlineKeyboardButton(
                    text="🛡 Отработать возражение",
                    callback_data=f"ai:{short_id}:obj_menu",
                ),
            ],
            nav_row,
            [
                InlineKeyboardButton(
                    text="🔥 Ещё 5 новых лидов",
                    callback_data=f"hunt:run:{batch.niche_key}:{batch.district_key}",
                ),
                InlineKeyboardButton(
                    text="🧭 Сменить район",
                    callback_data="hunt:menu",
                ),
            ],
        ]
    )


def make_place_keyboard(place: PlaceInfo) -> InlineKeyboardMarkup:
    """Создает Inline-меню под одиночной карточкой заведения (при ручном поиске)."""
    short_id = cache_place(place)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Проверить ссылку",
                    url=place.review_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="📸 Инста Direct",
                    callback_data=f"ai:{short_id}:insta",
                ),
                InlineKeyboardButton(
                    text="✈️ Телеграм (под видео)",
                    callback_data=f"ai:{short_id}:tg",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎤 Текст для ГС/Кружка",
                    callback_data=f"ai:{short_id}:voice",
                ),
                InlineKeyboardButton(
                    text="🛡 Отработать возражение",
                    callback_data=f"ai:{short_id}:obj_menu",
                ),
            ],
        ]
    )
    return keyboard


def make_objections_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """
    Подменю отработки возражений:
    [У нас уже есть QR-код]
    [Дорого / Не актуально]
    [Скиньте КП, мы подумаем]
    [⬅️ Назад]
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="У нас уже есть QR-код",
                    callback_data=f"ai:{short_id}:obj_qr",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Дорого / Не актуально",
                    callback_data=f"ai:{short_id}:obj_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Скиньте КП, мы подумаем",
                    callback_data=f"ai:{short_id}:obj_think",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к карточке",
                    callback_data=f"ai:{short_id}:back_main",
                )
            ],
        ]
    )
    return keyboard


def make_ai_response_keyboard(short_id: str, action: str) -> InlineKeyboardMarkup:
    """
    Кнопки под сгенерированным ИИ сообщением:
    [🔄 Перегенерировать] | [⬅️ Скрыть сообщение]
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Перегенерировать",
                    callback_data=f"ai:{short_id}:regen:{action}",
                ),
                InlineKeyboardButton(
                    text="⬅️ К меню заведения",
                    callback_data=f"ai:{short_id}:to_card",
                ),
            ]
        ]
    )
    return keyboard


# ==============================================================================
# Универсальные функции поиска (Официальный API или Автономный режим)
# ==============================================================================
async def resolve_search_text(query: str, max_results: int = 3) -> List[PlaceInfo]:
    """Выполняет поиск через официальный API или автономный бесплатный сервис."""
    if config.has_google_api_key():
        return await google_places_service.search_text(query, max_results=max_results)
    return await keyless_places_service.search_text(query, max_results=max_results)


async def resolve_search_by_url(url: str) -> List[PlaceInfo]:
    """Разворачивает ссылку через официальный API или автономный сервис."""
    if config.has_google_api_key():
        return await google_places_service.search_by_url(url)
    return await keyless_places_service.search_by_url(url)


async def resolve_search_nearby(
    lat: float, lng: float, radius: int = 200, keyword: Optional[str] = None
) -> List[PlaceInfo]:
    """Поиск по координатам."""
    if config.has_google_api_key():
        return await google_places_service.search_nearby(
            lat=lat, lng=lng, radius=radius, keyword=keyword
        )
    query = keyword if keyword else f"{lat},{lng}"
    return await keyless_places_service.search_text(query)


# ==============================================================================
# Middleware / Проверка доступа (Белый список)
# ==============================================================================
@router.message.outer_middleware()
async def admin_whitelist_message_middleware(handler, event: Message, data):
    """Проверяет, входит ли пользователь в белый список ADMIN_IDS (если он задан)."""
    user_id = event.from_user.id if event.from_user else 0
    if not config.is_admin(user_id):
        logger.warning(
            "Отклонен неавторизованный запрос от user_id=%s (@%s)",
            user_id,
            event.from_user.username if event.from_user else "unknown",
        )
        await event.answer(
            f"⛔️ <b>Доступ ограничен</b>\n\n"
            f"Этот бот предназначен исключительно для команды <b>HHtap</b>.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            f"Передайте этот ID администратору для добавления в белый список.",
            parse_mode=ParseMode.HTML,
        )
        return
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def admin_whitelist_callback_middleware(handler, event: CallbackQuery, data):
    """Проверяет права доступа на нажатие inline-кнопок."""
    user_id = event.from_user.id if event.from_user else 0
    if not config.is_admin(user_id):
        await event.answer("⛔️ Доступ ограничен для команды HHtap.", show_alert=True)
        return
    return await handler(event, data)


# ==============================================================================
# Команды бота и Автопоиск ТОП-лидов
# ==============================================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветственное сообщение, нижняя клавиатура и меню Автопоиска."""
    mode_desc = (
        "🟢 <b>Режим парсера:</b> Google Places API"
        if config.has_google_api_key()
        else "🟢 <b>Режим парсера:</b> Автономный SDK-парсер (с рейтингами и отзывами, без ключей)"
    )
    ai_desc = (
        f"🤖 <b>ИИ-ассистент:</b> Подключен ({config.anymodel_model})"
        if config.has_anymodel_key()
        else "🤖 <b>ИИ-ассистент:</b> Ожидает ключ ANYMODEL_API_KEY"
    )

    text = (
        "👋 <b>Добро пожаловать в комбайн лидогенерации HHtap!</b>\n\n"
        f"{mode_desc}\n"
        f"{ai_desc}\n\n"
        "🎯 <b>ГЛАВНАЯ ФИЧА — Автопоиск ТОП-лидов:</b>\n"
        "Нажмите кнопку <b>«🎯 Автопоиск ТОП-лидов»</b> или <b>«🔥 Быстрый рейд»</b> внизу экрана — "
        "бот сам просканирует улицы Ташкента, отберёт 5 самых горячих заведений и покажет их "
        "<b>в одном удобном сообщении с перелистыванием ⬅️ ➡️</b> и защитой от повторов!\n\n"
        "🔎 <b>Ручной режим также работает:</b>\n"
        "• Отправьте название и улицу (<i>Жигули Бар Ташкент</i>)\n"
        "• Или ссылку из Google Maps (<code>https://maps.app.goo.gl/...</code>)\n"
        "• Или геопозицию 📎"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_reply_keyboard(),
    )


@router.message(Command("hunt"))
@router.message(F.text == BTN_HUNT_MENU)
async def cmd_hunt_menu(message: Message):
    """Открывает интерактивное меню выбора ниши для Автопоиска ТОП-лидов."""
    seen = lead_hunter_service.seen_count()
    text = (
        "🎯 <b>Автопоиск самых горячих лидов HHtap</b>\n\n"
        "Бот просканирует улицы Ташкента, отсеет уже показанные точки "
        f"(в памяти сейчас: <b>{seen}</b> уникальных заведений) и соберёт <b>карусель из ТОП-5 клиентов</b>:\n"
        "• 🚀 <b>Огромный потенциал (4.5–5.0⭐️, но мало отзывов)</b>\n"
        "• 🔥 <b>Острая боль (просажен рейтинг 3.5–4.4⭐️)</b>\n\n"
        "👇 <b>Выберите нишу заведений:</b>"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=make_hunt_niche_keyboard(),
    )


@router.message(F.text == BTN_QUICK_RAID)
async def handle_quick_raid(message: Message):
    """Мгновенный запуск поиска ТОП-5 лидов по всем денежным улицам Ташкента в 1 клик."""
    await execute_lead_hunt(message, niche_key="all", district_key="auto", edit_existing=False)


@router.message(F.text == BTN_STATS)
async def handle_stats_info(message: Message):
    """Показывает статистику базы и логику скоринга."""
    seen = lead_hunter_service.seen_count()
    text = (
        "📊 <b>Память бота и многофакторный алгоритм оценки (0–98 баллов)</b>\n\n"
        f"🗂 <b>В анти-дубль памяти (уже просмотрено):</b> <code>{seen}</code> заведений.\n"
        "<i>Бот запоминает каждое показанное заведение по Place ID и названию, поэтому даже после 10–15 нажатий подряд в одном районе он углубляется по десяткам улиц и не присылает дубли.</i>\n\n"
        "🧠 <b>Из чего складывается точный балл лида:</b>\n"
        "1️⃣ <b>Острота боли и потенциал (до 55 баллов):</b>\n"
        "   • Просаженный рейтинг <code>3.9–4.3⭐️</code> (чуть ниже порога 4.5⭐️)\n"
        "   • Или высокий рейтинг <code>4.6–5.0⭐️</code> при малом числе отзывов (<code>6–40</code> отз.)\n"
        "2️⃣ <b>Скорость окупаемости в сменах (до 18 баллов):</b>\n"
        "   • Чем быстрее заведение выйдет на <code>4.5⭐️</code> или первую сотню отзывов с нашей NFC-картой (за 2–4 смены), тем выше балл.\n"
        "3️⃣ <b>Платёжеспособность формата (до 13 баллов):</b>\n"
        "   • Рестораны, стейк-хаусы, бары, лаунжи, кофейни, стоматологии и барбершопы получают приоритет.\n"
        "4️⃣ <b>Трафик и престижность улицы (до 12 баллов):</b>\n"
        "   • Денежные локации (Т. Шевченко, Ойбек, Ш. Руставели, Ц-1, Tashkent City, Seoul Mun и др.) получают бонус за плотность конкуренции."
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=make_main_reply_keyboard())


@router.message(F.text == BTN_RESET_SEEN)
async def handle_reset_seen(message: Message):
    """Очищает базу просмотренных заведений."""
    cleared = lead_hunter_service.reset_seen()
    await message.answer(
        f"🔄 <b>Память просмотренных заведений очищена!</b>\n"
        f"Удалено записей из анти-дубль фильтра: <code>{cleared}</code>.\n"
        f"Теперь все локации снова доступны с чистого листа.",
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_reply_keyboard(),
    )


@router.callback_query(F.data.startswith("hunt:"))
async def handle_hunt_callbacks(callback: CallbackQuery):
    """Обрабатывает перелистывание карусели (⬅️ ➡️), выбор ниши/района и запуск охоты."""
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    if action == "noop":
        await callback.answer("Используйте стрелки ⬅️ и ➡️ для перелистывания ТОП-5")
        return

    # Перелистывание слайдов ТОП-1 .. ТОП-5 внутри одного сообщения
    if action == "page" and len(parts) >= 4:
        batch_id = parts[2]
        try:
            slide_idx = int(parts[3])
        except ValueError:
            await callback.answer()
            return

        batch = carousel_cache.get(batch_id)
        if not batch or not (0 <= slide_idx < len(batch.leads)):
            await callback.answer(
                "⚠️ Эта подборка устарела. Нажмите «🔥 Ещё 5 новых лидов».",
                show_alert=True,
            )
            return

        slide_html = format_carousel_slide(batch, slide_idx)
        kb = make_carousel_keyboard(batch, slide_idx)
        try:
            await callback.message.edit_text(
                slide_html,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        await callback.answer(f"ТОП-{slide_idx + 1} из {len(batch.leads)}")
        return

    if action == "menu":
        seen = lead_hunter_service.seen_count()
        text = (
            "🎯 <b>Автопоиск самых горячих лидов HHtap</b>\n"
            f"<i>В анти-дубль памяти: {seen} точек</i>\n\n"
            "👇 <b>Выберите нишу заведений:</b>"
        )
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=make_hunt_niche_keyboard(),
        )
        await callback.answer()
        return

    if action == "niche" and len(parts) >= 3:
        niche_key = parts[2]
        niche_info = HUNT_NICHES.get(niche_key, HUNT_NICHES["all"])
        text = (
            f"🎯 <b>Выбрана ниша:</b> {niche_info['title']}\n\n"
            "👇 <b>Выберите район или денежную зону Ташкента:</b>"
        )
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=make_hunt_district_keyboard(niche_key),
        )
        await callback.answer()
        return

    if action == "run" and len(parts) >= 4:
        niche_key = parts[2]
        district_key = parts[3]
        await callback.answer("🕵️‍♂️ Ищу 5 новых горячих лидов без повторов...")
        await execute_lead_hunt(
            callback.message,
            niche_key=niche_key,
            district_key=district_key,
            edit_existing=True,
        )
        return


async def execute_lead_hunt(
    message: Message,
    niche_key: str = "all",
    district_key: str = "auto",
    edit_existing: bool = False,
):
    """
    Запускает парсер + многофакторный скоринг и выводит ТОП-5 лидов
    В ОДНОМ сообщении с инлайн-кнопками перелистывания (⬅️ ➡️).
    """
    niche_info = HUNT_NICHES.get(niche_key, HUNT_NICHES["all"])
    district_info = HUNT_DISTRICTS.get(district_key, HUNT_DISTRICTS["auto"])
    seen_before = lead_hunter_service.seen_count()

    loading_text = (
        f"🕵️‍♂️ <b>Сканирую улицы и отбираю свежие ТОП-5 лидов...</b>\n"
        f"• <b>Ниша:</b> {niche_info['title']}\n"
        f"• <b>Локация:</b> {district_info['title']}\n"
        f"• <b>Уже в памяти (исключены из повторов):</b> {seen_before} точек\n\n"
        f"⏳ <i>Считаю математику 5⭐️ отзывов и проверяю карточки...</i>"
    )

    if edit_existing:
        try:
            await message.edit_text(loading_text, parse_mode=ParseMode.HTML)
            status_msg = message
        except Exception:
            status_msg = await message.answer(loading_text, parse_mode=ParseMode.HTML)
    else:
        status_msg = await message.answer(loading_text, parse_mode=ParseMode.HTML)

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            leads, scanned_total = await lead_hunter_service.hunt_best_leads(
                niche_key=niche_key,
                district_key=district_key,
                top_n=5,
            )
        except Exception as e:
            logger.exception("Ошибка при Автопоиске лидов: %s", e)
            await status_msg.edit_text(
                f"❌ <b>Ошибка при сканировании:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    if not leads:
        await status_msg.edit_text(
            "🔍 <b>Все подходящие заведения по этим улицам уже были показаны ранее!</b>\n\n"
            "Выберите другой район/нишу или нажмите <b>«🔄 Сбросить просмотренные»</b> внизу экрана.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_hunt_niche_keyboard(),
        )
        return

    batch_id = cache_carousel_batch(
        niche_key=niche_key,
        district_key=district_key,
        scanned_total=scanned_total,
        leads=leads,
    )
    batch = carousel_cache[batch_id]

    first_slide_html = format_carousel_slide(batch, idx=0)
    first_slide_kb = make_carousel_keyboard(batch, idx=0)

    await status_msg.edit_text(
        first_slide_html,
        parse_mode=ParseMode.HTML,
        reply_markup=first_slide_kb,
        disable_web_page_preview=True,
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка и инструкция по записи NFC Tools."""
    text = (
        "📖 <b>Инструкция для команды HHtap</b>\n\n"
        "<b>1. Автопоиск горячих лидов (в одном окне):</b>\n"
        "• Нажмите <b>«🎯 Автопоиск ТОП-лидов»</b> или <b>«🔥 Быстрый рейд»</b>.\n"
        "• Листайте найденные 5 заведений кнопками <b>⬅️ Предыдущий / Следующий ➡️</b> прямо в одном сообщении.\n"
        "• Все показанные точки автоматически запоминаются и не повторяются.\n\n"
        "<b>2. Запись чипа в NFC Tools:</b>\n"
        "• Нажмите <b>«🔍 Проверить ссылку»</b>, чтобы убедиться, что открывается окно с 5 звёздами.\n"
        "• Тапните по ссылке в сообщении — она скопируется в буфер.\n"
        "• В приложении <b>NFC Tools</b> (Write ➔ Add a record ➔ Custom URL) вставьте ссылку и запишите на чип.\n\n"
        "<b>3. ИИ-ассистент по продажам:</b>\n"
        "• <b>📸 Инста Direct:</b> плотное 3-абзацное сообщение с локальной привязкой и расчётом выгоды.\n"
        "• <b>✈️ Телеграм:</b> сопроводительный текст под 8-секундное демо-видео работы карты.\n"
        "• <b>🎤 Текст для ГС:</b> готовый текст на 20–25 секунд уверенной речи для голосового или кружка.\n"
        "• <b>🛡 Отработать возражение:</b> аргументированные ответы («Есть QR», «Дорого», «Скиньте КП»).\n\n"
        "🆔 Узнать свой ID: /id"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=make_main_reply_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message):
    """Возвращает Telegram ID пользователя."""
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(
        f"Ваш Telegram User ID: <code>{user_id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ==============================================================================
# Обработка Inline-кнопок ИИ-ассистента
# ==============================================================================
@router.callback_query(F.data.startswith("ai:"))
async def handle_ai_callbacks(callback: CallbackQuery):
    """Обработка всех нажатий кнопок генератора продаж и возражений."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    short_id = parts[1]
    action = parts[2]

    # 1. Открытие подменю возражений
    if action == "obj_menu":
        await callback.message.edit_reply_markup(
            reply_markup=make_objections_keyboard(short_id)
        )
        await callback.answer()
        return

    # 2. Возврат в главное меню карточки заведения (с сохранением карусели, если это слайд ТОП-5)
    if action == "back_main":
        cached = place_cache.get(short_id)
        if cached:
            if cached.batch_id and cached.slide_idx is not None and cached.batch_id in carousel_cache:
                batch = carousel_cache[cached.batch_id]
                await callback.message.edit_reply_markup(
                    reply_markup=make_carousel_keyboard(batch, cached.slide_idx)
                )
            else:
                dummy = PlaceInfo(
                    place_id=cached.place_id,
                    name=cached.name,
                    address=cached.address,
                    rating=cached.rating,
                    user_ratings_total=cached.reviews_count,
                    review_url=cached.review_link,
                )
                await callback.message.edit_reply_markup(
                    reply_markup=make_place_keyboard(dummy)
                )
        await callback.answer()
        return

    # 3. Кнопка «К меню заведения» под сгенерированным ИИ-сообщением
    if action == "to_card":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("Вернулись к карточке")
        return

    # 4. Проверка регенерации
    is_regen = False
    if action == "regen" and len(parts) >= 4:
        is_regen = True
        action = parts[3]

    # Достаем данные точки из кэша
    cached = place_cache.get(short_id)
    if not cached:
        await callback.answer(
            "⚠️ Данные точки устарели. Пожалуйста, запустите поиск заново.",
            show_alert=True,
        )
        return

    objection_type = OBJECTION_TITLES.get(action)

    await callback.answer("⏳ Сочиняю сообщение...")

    async with ChatActionSender.typing(bot=callback.bot, chat_id=callback.message.chat.id):
        try:
            generated_text = await ai_sales_service.generate_message(
                action=action,
                name=cached.name,
                address=cached.address,
                rating=cached.rating,
                reviews_count=cached.reviews_count,
                objection_type=objection_type,
            )

            # Экранируем HTML для безопасности и оборачиваем в <code> для мгновенного копирования
            escaped_text = html.escape(generated_text.strip())
            output_text = f"<code>{escaped_text}</code>"
            reply_kb = make_ai_response_keyboard(short_id, action)

            if is_regen:
                await callback.message.edit_text(
                    output_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_kb,
                )
            else:
                await callback.message.reply(
                    output_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_kb,
                )

        except AISalesError as e:
            logger.error("Ошибка ИИ-продаж: %s", e)
            await callback.message.answer(
                f"⚠️ <b>Ошибка ИИ-генератора:</b>\n{html.escape(str(e))}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.exception("Непредвиденная ошибка при вызове ИИ: %s", e)
            await callback.message.answer(
                f"❌ <b>Ошибка генерации:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )


# ==============================================================================
# Обработка геолокации
# ==============================================================================
@router.message(F.location)
async def handle_location(message: Message):
    """Поиск заведений рядом с переданной геопозицией."""
    loc = message.location
    lat, lng = loc.latitude, loc.longitude
    caption = (message.caption or "").strip()

    logger.info("Поиск по геолокации: lat=%s, lng=%s, caption='%s'", lat, lng, caption)

    async with ChatActionSender.find_location(bot=message.bot, chat_id=message.chat.id):
        try:
            places = await resolve_search_nearby(
                lat=lat,
                lng=lng,
                radius=200,
                keyword=caption if caption else None,
            )

            if not places:
                await message.reply(
                    "📍 <b>Поблизости заведений не найдено</b>\n\n"
                    "В радиусе от указанной точки заведения Google Maps не обнаружены. "
                    "Попробуйте отправить текстовое название заведения.",
                    parse_mode=ParseMode.HTML,
                )
                return

            await send_places_results(message, places, header="📍 <b>Ближайшие заведения:</b>")

        except Exception as e:
            logger.exception("Ошибка при поиске по локации: %s", e)
            await message.reply(
                f"❌ <b>Произошла ошибка при поиске по локации:</b>\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )


# ==============================================================================
# Обработка текстовых запросов и ссылок
# ==============================================================================
@router.message(F.text)
async def handle_text_message(message: Message):
    """
    Обрабатывает:
    - Сценарий Б: если в тексте обнаружена ссылка Google Maps.
    - Сценарий А: текстовый запрос (название + адрес/ориентир).
    """
    text = message.text.strip()
    if not text:
        return

    url_match = MAPS_URL_REGEX.search(text)

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            if url_match:
                found_url = url_match.group(0).strip()
                logger.info("Обрабатываем ссылку Google Maps: %s", found_url)

                places = await resolve_search_by_url(found_url)
                if not places:
                    await message.reply(
                        "🔍 <b>Заведение не найдено</b>\n\n"
                        "Не удалось извлечь точный Place ID по этой ссылке. "
                        "Попробуйте отправить текстовое название заведения и улицу/город.",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                await send_places_results(
                    message,
                    places,
                    header="🎯 <b>Результат по ссылке Google Maps:</b>",
                )

            else:
                logger.info("Текстовый поиск: '%s'", text)
                places = await resolve_search_text(query=text, max_results=3)

                if not places:
                    await message.reply(
                        "🔍 <b>Ничего не найдено</b>\n\n"
                        f"По запросу «<i>{html.escape(text)}</i>» заведений не обнаружено.\n\n"
                        "💡 <b>Совет:</b>\n"
                        "• Добавьте город или улицу (например: <i>Roni Тараса Шевченко Ташкент</i>)\n"
                        "• Или нажмите <b>«🎯 Автопоиск ТОП-лидов»</b> внизу экрана!",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                await send_places_results(
                    message,
                    places,
                    header=f"🔎 <b>Результаты поиска (Топ-{len(places)}):</b>",
                )

        except LinkExpansionError as e:
            logger.warning("Ошибка разбора ссылки: %s", e)
            await message.reply(
                f"⚠️ <b>Ошибка при разборе ссылки</b>\n\n"
                f"{e.message}\n\n"
                f"Убедитесь, что ссылка скопирована из карточки конкретного заведения в Google Maps "
                f"(кнопка «Поделиться» ➔ «Скопировать ссылку»).",
                parse_mode=ParseMode.HTML,
            )
        except RequestDeniedError as e:
            logger.error("REQUEST_DENIED от Google Places API: %s", e)
            await message.reply(
                f"⚠️ <b>Ошибка доступа Google Places API</b>\n\n"
                f"Google API отклонил запрос (<code>REQUEST_DENIED</code>).\n\n"
                f"<i>Сообщение от Google: {e.message}</i>",
                parse_mode=ParseMode.HTML,
            )
        except QuotaExceededError:
            await message.reply(
                "⚠️ <b>Превышен лимит запросов</b>\n\n"
                "Повторите попытку через пару минут.",
                parse_mode=ParseMode.HTML,
            )
        except GooglePlacesError as e:
            logger.error("Ошибка GooglePlacesError: %s", e)
            await message.reply(
                f"⚠️ <b>Ошибка сервиса:</b>\n<code>{e.message}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.exception("Непредвиденная ошибка при обработке сообщения: %s", e)
            await message.reply(
                f"❌ <b>Произошла ошибка:</b>\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )


async def send_places_results(
    message: Message,
    places: List[PlaceInfo],
    header: Optional[str] = None,
):
    """Отправляет пользователю карточки найденных точек (до 3 заведений)."""
    if header and len(places) > 1:
        await message.answer(header, parse_mode=ParseMode.HTML)

    for place in places:
        card_text = format_place_card(place)
        keyboard = make_place_keyboard(place)
        await message.answer(
            card_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ==============================================================================
# Точка входа
# ==============================================================================
async def main():
    config.validate()

    if not config.telegram_bot_token or config.telegram_bot_token == "your_telegram_bot_token_here":
        logger.critical(
            "ОШИБКА: Задайте корректный TELEGRAM_BOT_TOKEN в файле .env перед запуском бота!"
        )
        return

    logger.info("Запуск бота HHtap...")
    if config.admin_ids:
        logger.info("Включен белый список. Разрешенные ID: %s", config.admin_ids)
    else:
        logger.info("Белый список пуст. Бот доступен всем пользователям.")

    if config.has_google_api_key():
        logger.info("Режим парсера: Официальный Google Places API.")
    else:
        logger.info("Режим парсера: Автономный БЕСПЛАТНЫЙ режим (с рейтингами и отзывами, без ключей).")

    if config.has_anymodel_key():
        logger.info("ИИ-ассистент: Активен (модель %s, URL: %s)", config.anymodel_model, config.anymodel_base_url)
    else:
        logger.info("ИИ-ассистент: ANYMODEL_API_KEY не задан (кнопки ИИ попросят указать ключ).")

    bot = Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот успешно запущен и готов к работе в Telegram!")
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logger.info("Остановка бота по сигналу...")
    finally:
        await keyless_places_service.close()
        await bot.session.close()
        logger.info("Все сессии и ресурсы закрыты.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
