import asyncio
import hashlib
import html
import logging
import re
import sys
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
    Message,
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


# ==============================================================================
# In-memory кэш найденных заведений (для коротких callback_data до 64 байт)
# ==============================================================================
@dataclass
class CachedPlace:
    place_id: str
    name: str
    address: str
    rating: Optional[float]
    reviews_count: Optional[int]
    review_link: str


place_cache: Dict[str, CachedPlace] = {}


def cache_place(place: PlaceInfo) -> str:
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
    )
    # Ограничение размера кэша для предотвращения утечки памяти
    if len(place_cache) > 1000:
        oldest_key = next(iter(place_cache))
        del place_cache[oldest_key]
    return short_id


# ==============================================================================
# Клавиатуры и карточки
# ==============================================================================
def format_place_card(place: PlaceInfo) -> str:
    """
    Форматирует карточку заведения в соответствии с требованиями HHtap.
    Ссылка обернута в <code>...</code> для моментального копирования по тапу.
    """
    rating_part = ""
    if place.rating is not None:
        rating_part = f"⭐️ {place.rating:.1f}"
        if place.user_ratings_total is not None:
            rating_part += f" | {place.user_ratings_total} отзывов"
    else:
        rating_part = "⭐️ Точка найдена"

    card = (
        f"🏢 <b>{place.name}</b> ({rating_part})\n"
        f"📍 <i>{place.address}</i>\n\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )
    return card


def make_place_keyboard(place: PlaceInfo) -> InlineKeyboardMarkup:
    """
    Создает Inline-меню под карточкой заведения:
    Row 1: [🔍 Проверить ссылку]
    Row 2: [📸 Инста Direct] | [✈️ Телеграм (под видео)]
    Row 3: [🎤 Текст для ГС/Кружка] | [🛡 Отработать возражение]
    """
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
                    text="⬅️ Назад",
                    callback_data=f"ai:{short_id}:back_main",
                )
            ],
        ]
    )
    return keyboard


def make_ai_response_keyboard(short_id: str, action: str) -> InlineKeyboardMarkup:
    """
    Кнопки под сгенерированным ИИ сообщением:
    [🔄 Перегенерировать] | [⬅️ К меню заведения]
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
# Команды бота
# ==============================================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветственное сообщение и инструкция по работе с ботом."""
    mode_desc = (
        "🟢 <b>Режим парсера:</b> Google Places API"
        if config.has_google_api_key()
        else "🟢 <b>Режим парсера:</b> Автономный бесплатный (без API-ключей и карт)"
    )
    ai_desc = (
        f"🤖 <b>ИИ-ассистент:</b> Подключен ({config.anymodel_model})"
        if config.has_anymodel_key()
        else "🤖 <b>ИИ-ассистент:</b> Ожидает ключ ANYMODEL_API_KEY"
    )

    text = (
        "👋 <b>Добро пожаловать в бота HHtap!</b>\n\n"
        "Я генерирую прямые ссылки на <b>Google Maps (5 звёзд)</b> "
        "для быстрой записи на NFC-карты и создаю <b>персонализированные B2B-сообщения</b> "
        "для продаж владельцам заведений.\n\n"
        f"{mode_desc}\n"
        f"{ai_desc}\n\n"
        "<b>Как со мной работать:</b>\n"
        "1️⃣ <b>Сценарий А (Текст):</b> Отправьте название заведения и ориентир/улицу:\n"
        "   👉 <i>Roni Тараса Шевченко Ташкент</i>\n"
        "   👉 <i>Black Star Burger Шахрисабз</i>\n\n"
        "2️⃣ <b>Сценарий Б (Ссылка):</b> Отправьте короткую ссылку из Google Maps:\n"
        "   👉 <code>https://maps.app.goo.gl/...</code>\n\n"
        "3️⃣ <b>Сценарий В (Геолокация):</b> Нажмите 📎 ➔ <b>Геопозиция</b>, "
        "и бот найдет заведения рядом с вами.\n\n"
        "⚡️ <i>Под каждой найденной точкой доступны кнопки ИИ для генерации питчей под Instagram, Telegram и отработки возражений!</i>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка и инструкция по записи NFC Tools."""
    text = (
        "📖 <b>Инструкция для команды HHtap</b>\n\n"
        "<b>1. Запись чипа в NFC Tools:</b>\n"
        "• Отправьте боту название заведения или ссылку.\n"
        "• Нажмите <b>«🔍 Проверить ссылку»</b>, чтобы убедиться, что открывается окно с 5 звёздами.\n"
        "• Тапните по ссылке в сообщении — она скопируется в буфер.\n"
        "• В приложении <b>NFC Tools</b> (Write ➔ Add a record ➔ Custom URL) вставьте ссылку и запишите на чип.\n\n"
        "<b>2. ИИ-ассистент по продажам:</b>\n"
        "• <b>📸 Инста Direct:</b> первое короткое цепляющее сообщение на 2 абзаца с цифрами карточки.\n"
        "• <b>✈️ Телеграм:</b> сопроводительный текст для отправки вместе с демо-видео работы карты.\n"
        "• <b>🎤 Текст для ГС:</b> 3 тезиса на 15 секунд разговорной речи для голосовых сообщений.\n"
        "• <b>🛡 Отработать возражение:</b> ответы на частые возражения («Есть QR», «Дорого», «Скиньте КП»).\n\n"
        "🆔 Узнать свой ID: /id"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


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

    # 2. Возврат в главное меню карточки заведения
    if action == "back_main":
        cached = place_cache.get(short_id)
        if cached:
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
            "⚠️ Данные точки устарели. Пожалуйста, отправьте запрос заново.",
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
                # --------------------------------------------------------------
                # Сценарий Б: короткая ссылка Google Maps
                # --------------------------------------------------------------
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
                # --------------------------------------------------------------
                # Сценарий А: Текстовый поиск
                # --------------------------------------------------------------
                logger.info("Текстовый поиск: '%s'", text)
                places = await resolve_search_text(query=text, max_results=3)

                if not places:
                    await message.reply(
                        "🔍 <b>Ничего не найдено</b>\n\n"
                        f"По запросу «<i>{text}</i>» заведений не обнаружено.\n\n"
                        "💡 <b>Совет:</b>\n"
                        "• Добавьте город или улицу (например: <i>Roni Тараса Шевченко Ташкент</i>)\n"
                        "• Или скопируйте ссылку на заведение прямо из Google Maps и отправьте боту.",
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
    """
    Отправляет пользователю карточки найденных точек (до 3 заведений).
    Каждая карточка содержит:
    - Название, рейтинг, адрес
    - Чистую ссылку в <code>...</code> для копирования в 1 клик
    - Меню с кнопками проверки и генерации ИИ-питчей
    """
    if header and len(places) > 1:
        await message.answer(header, parse_mode=ParseMode.HTML)

    for idx, place in enumerate(places, start=1):
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
        logger.info("Режим парсера: Автономный БЕСПЛАТНЫЙ режим (без Google API ключей и без карт).")

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
