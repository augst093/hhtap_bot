import asyncio
import logging
import re
import sys
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from config import config
from services.google_places import (
    GooglePlacesError,
    LinkExpansionError,
    PlaceInfo,
    QuotaExceededError,
    RequestDeniedError,
    ZeroResultsError,
    google_places_service,
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
        rating_part = "⭐️ Нет оценок"

    card = (
        f"🏢 <b>{place.name}</b> ({rating_part})\n"
        f"📍 <i>{place.address}</i>\n\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )
    return card


def make_place_keyboard(place: PlaceInfo) -> InlineKeyboardMarkup:
    """Создает Inline-кнопку для мгновенной проверки ссылки в браузере."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Проверить ссылку в браузере",
                    url=place.review_url,
                )
            ]
        ]
    )
    return keyboard


# ==============================================================================
# Middleware / Проверка доступа (Белый список)
# ==============================================================================
@router.message.outer_middleware()
async def admin_whitelist_middleware(handler, event: Message, data):
    """
    Проверяет, входит ли пользователь в белый список ADMIN_IDS (если он задан).
    """
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


# ==============================================================================
# Команды бота
# ==============================================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветственное сообщение и инструкция по работе с ботом."""
    text = (
        "👋 <b>Добро пожаловать в бота HHtap!</b>\n\n"
        "Я генерирую прямые ссылки на <b>Google Maps (5 звёзд)</b> "
        "для быстрой записи на NFC-карты и стенды через приложение <b>NFC Tools</b>.\n\n"
        "<b>Как со мной работать:</b>\n"
        "1️⃣ <b>Сценарий А (Текст):</b> Отправьте название заведения и ориентир/улицу:\n"
        "   👉 <i>Roni Тараса Шевченко Ташкент</i>\n"
        "   👉 <i>Black Star Burger Шахрисабз</i>\n\n"
        "2️⃣ <b>Сценарий Б (Ссылка):</b> Отправьте короткую ссылку из Google Maps:\n"
        "   👉 <code>https://maps.app.goo.gl/...</code>\n\n"
        "3️⃣ <b>Сценарий В (Геолокация):</b> Нажмите 📎 ➔ <b>Геопозиция</b>, "
        "и бот найдет заведения в радиусе 200 метров вокруг вас.\n\n"
        "💡 <i>Сгенерированная ссылка открывает окно оценки 5 звезд в 1 клик!</i>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка и инструкция по записи NFC Tools."""
    text = (
        "📖 <b>Инструкция для команды HHtap</b>\n\n"
        "<b>Шаги записи чипа в NFC Tools:</b>\n"
        "1. Отправьте боту название заведения или ссылку.\n"
        "2. Тапните по ссылке в сообщении — она автоматически скопируется в буфер.\n"
        "3. Нажмите кнопку <b>«🔍 Проверить ссылку в браузере»</b>, чтобы убедиться, "
        "что на экране смартфона открывается окно выставления 5 звёзд.\n"
        "4. Откройте приложение <b>NFC Tools</b> на телефоне:\n"
        "   • Вкладка <b>Write</b> (Запись) ➔ <b>Add a record</b> (Добавить запись)\n"
        "   • Выберите <b>Custom URL / URI</b>\n"
        "   • Вставьте скопированную ссылку (без пробелов!)\n"
        "   • Нажмите <b>Write</b> и приложите NFC-карту к спинке телефона.\n\n"
        "🆔 Узнать свой ID: /id\n"
        "⚡️ Приоритет поиска: Ташкент / Узбекистан (поддерживаются любые города)."
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
# Обработка геолокации
# ==============================================================================
@router.message(F.location)
async def handle_location(message: Message):
    """
    Поиск заведений рядом с переданной геопозицией (радиус 200 метров).
    """
    loc = message.location
    lat, lng = loc.latitude, loc.longitude
    caption = (message.caption or "").strip()

    logger.info("Поиск по геолокации: lat=%s, lng=%s, caption='%s'", lat, lng, caption)

    async with ChatActionSender.find_location(bot=message.bot, chat_id=message.chat.id):
        try:
            places = await google_places_service.search_nearby(
                lat=lat,
                lng=lng,
                radius=200,
                keyword=caption if caption else None,
                max_results=3,
            )

            if not places:
                # Если в радиусе 200м ничего не найдено, пробуем расширить до 500м
                places = await google_places_service.search_nearby(
                    lat=lat,
                    lng=lng,
                    radius=500,
                    keyword=caption if caption else None,
                    max_results=3,
                )

            if not places:
                await message.reply(
                    "📍 <b>Поблизости заведений не найдено</b>\n\n"
                    "В радиусе 200–500 метров от указанной точки заведения Google Maps не обнаружены. "
                    "Попробуйте отправить текстовое название заведения.",
                    parse_mode=ParseMode.HTML,
                )
                return

            await send_places_results(message, places, header="📍 <b>Ближайшие заведения (до 200–500м):</b>")

        except RequestDeniedError as e:
            await message.reply(
                f"⚠️ <b>Ошибка доступа Google Places API</b>\n\n"
                f"Запрос отклонен (REQUEST_DENIED).\n"
                f"Проверьте, что <code>GOOGLE_API_KEY</code> указан корректно и в Google Cloud Console "
                f"включен <b>Places API</b>.\n\n"
                f"<i>Детали: {e.message}</i>",
                parse_mode=ParseMode.HTML,
            )
        except QuotaExceededError:
            await message.reply(
                "⚠️ <b>Превышен лимит запросов Google API</b>\n\n"
                "Пожалуйста, подождите немного и повторите попытку.",
                parse_mode=ParseMode.HTML,
            )
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

    # Проверяем, есть ли в сообщении ссылка Google Maps
    url_match = MAPS_URL_REGEX.search(text)

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            if url_match:
                # --------------------------------------------------------------
                # Сценарий Б: короткая ссылка Google Maps
                # --------------------------------------------------------------
                found_url = url_match.group(0).strip()
                logger.info("Обрабатываем ссылку Google Maps: %s", found_url)

                places = await google_places_service.search_by_url(found_url)
                if not places:
                    await message.reply(
                        "🔍 <b>Заведение не найдено</b>\n\n"
                        "Не удалось сопоставить ссылку с объектом в базе Google Places. "
                        "Попробуйте написать текстовое название заведения и улицу.",
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
                places = await google_places_service.search_text(query=text, max_results=3)

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
                f"<b>Что проверить:</b>\n"
                f"1. Правильность <code>GOOGLE_API_KEY</code> в файле <code>.env</code>.\n"
                f"2. В Google Cloud Console включены <b>Places API</b> и/или <b>Places API (New)</b>.\n"
                f"3. Ключ не ограничен по IP-адресу сервера деплоя.\n"
                f"4. К аккаунту привязан платежный аккаунт (Billing Account).\n\n"
                f"<i>Сообщение от Google: {e.message}</i>",
                parse_mode=ParseMode.HTML,
            )
        except QuotaExceededError:
            await message.reply(
                "⚠️ <b>Превышен лимит запросов Google API</b>\n\n"
                "Квота запросов исчерпана. Повторите попытку позже.",
                parse_mode=ParseMode.HTML,
            )
        except GooglePlacesError as e:
            logger.error("Ошибка GooglePlacesError: %s", e)
            await message.reply(
                f"⚠️ <b>Ошибка Google Places API:</b>\n<code>{e.message}</code>",
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
    - Inline-кнопку для мгновенного тестирования в браузере
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
    # Проверка конфигурации
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

    bot = Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        # Удаляем вебхуки и пропускаем накопившиеся апдейты
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот успешно запущен и готов к работе в Telegram!")
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logger.info("Остановка бота по сигналу...")
    finally:
        await bot.session.close()
        logger.info("Сессия бота закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
