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
from services.instagram_service import (
    InstagramCardData,
    InstagramParseError,
    build_instagram_card_data,
    format_instagram_card,
    is_instagram_input,
)
from services.lead_hunter import (
    CRM_STATUSES,
    HUNT_DISTRICTS,
    HUNT_NICHES,
    ScoredLead,
    build_insta_search_url,
    lead_hunter_service,
    parse_uz_phone,
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

# Тексты кнопок выбора режима и нижнего меню
BTN_MODE_GOOGLE = "⭐️ Google Reviews NFC"
BTN_MODE_INSTA = "📸 Instagram NFC"
BTN_HUNT_MENU = "🎯 Автопоиск ТОП-лидов"
BTN_QUICK_RAID = "🔥 Быстрый рейд (Топ-5)"
BTN_CRM = "📋 Мои сделки (CRM)"
BTN_STATS = "📊 База и критерии"
BTN_RESET_SEEN = "🔄 Сбросить просмотренные"

# Текущий выбранный режим пользователя: "google" (по умолчанию) или "insta"
user_modes: Dict[int, str] = {}


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
    phone: Optional[str] = None
    website: Optional[str] = None
    score: int = 85
    tier_badge: str = "📌 В работе"
    why_hot: str = ""
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
    score: int = 85,
    tier_badge: str = "📌 В работе",
    why_hot: str = "",
) -> str:
    """Сохраняет данные заведения в кэш и возвращает короткий 8-символьный хэш."""
    raw = f"{place.place_id}:{place.name}"
    short_id = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    existing = place_cache.get(short_id)
    place_cache[short_id] = CachedPlace(
        place_id=place.place_id,
        name=place.name,
        address=place.address,
        rating=place.rating,
        reviews_count=place.user_ratings_total,
        review_link=place.review_url,
        phone=place.phone or (existing.phone if existing else None),
        website=place.website or (existing.website if existing else None),
        score=score or (existing.score if existing else 85),
        tier_badge=tier_badge or (existing.tier_badge if existing else "📌 В работе"),
        why_hot=why_hot or (existing.why_hot if existing else ""),
        batch_id=batch_id if batch_id is not None else (existing.batch_id if existing else None),
        slide_idx=slide_idx if slide_idx is not None else (existing.slide_idx if existing else None),
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
    """Сохраняет пачку лидов для перелистывания в одном сообщении."""
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
    """Постоянное нижнее меню с быстрым переключением режимов (Google Reviews / Instagram NFC) и Автопоиском."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_MODE_GOOGLE),
                KeyboardButton(text=BTN_MODE_INSTA),
            ],
            [
                KeyboardButton(text=BTN_HUNT_MENU),
                KeyboardButton(text=BTN_QUICK_RAID),
            ],
            [
                KeyboardButton(text=BTN_CRM),
                KeyboardButton(text=BTN_STATS),
            ],
            [
                KeyboardButton(text=BTN_RESET_SEEN),
            ],
        ],
        resize_keyboard=True,
    )


def make_start_mode_inline_keyboard() -> InlineKeyboardMarkup:
    """Стартовое Inline-меню выбора режима работы бота."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐️ Google Reviews NFC",
                    callback_data="mode:google",
                ),
                InlineKeyboardButton(
                    text="📸 Instagram NFC",
                    callback_data="mode:insta",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Запустить Автопоиск ТОП-лидов",
                    callback_data="hunt:menu",
                )
            ],
        ]
    )


def make_instagram_card_keyboard(clean_username: str) -> InlineKeyboardMarkup:
    """
    Inline-меню под результатом Instagram NFC-карты:
    Row 1: [🔍 Проверить аккаунт] (URL -> https://instagram.com/{clean_username})
    Row 2: [🎯 Питч для клиента (Текст)] | [🗣 Скрипт для живого разговора]
    Row 3: [💥 Комбо-оффер (Google + Insta)]
    Row 4: [⬅️ В главное меню]
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Проверить аккаунт",
                    url=f"https://instagram.com/{clean_username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Питч для клиента (Текст)",
                    callback_data=f"ig:ig_pitch:{clean_username}",
                ),
                InlineKeyboardButton(
                    text="🗣 Скрипт для живого разговора",
                    callback_data=f"ig:ig_live:{clean_username}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💥 Комбо-оффер (Google + Insta)",
                    callback_data=f"ig:ig_combo:{clean_username}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ В главное меню",
                    callback_data="mode:main",
                )
            ],
        ]
    )


def make_instagram_ai_response_keyboard(clean_username: str, action: str) -> InlineKeyboardMarkup:
    """Кнопки под сгенерированным ИИ текстом для Instagram NFC."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Перегенерировать",
                    callback_data=f"ig:r:{action}:{clean_username}",
                ),
                InlineKeyboardButton(
                    text="⬅️ К карточке Instagram",
                    callback_data="ig:hide",
                ),
            ]
        ]
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


def format_contacts_and_crm_block(place: PlaceInfo) -> str:
    """
    Формирует блок прямых контактов (телефон, мобильный/ТГ, сайт/Instagram)
    и текущего статуса сделки в Мини-CRM прямо внутри карточки.
    """
    lines: List[str] = []
    p_info = parse_uz_phone(place.phone)
    if p_info["raw"]:
        if p_info["is_mobile"]:
            lines.append(f"📱 <b>Мобильный (ТГ / Звонок):</b> <code>{html.escape(p_info['raw'])}</code>")
        else:
            lines.append(f"☎️ <b>Телефон (городской):</b> <code>{html.escape(p_info['raw'])}</code>")

    if place.website:
        w_clean = html.escape(place.website)
        if "instagram.com" in place.website.lower():
            lines.append(f"📸 <b>Instagram:</b> <a href=\"{w_clean}\">Открыть профиль</a>")
        elif "t.me/" in place.website.lower():
            lines.append(f"✈️ <b>Telegram:</b> <a href=\"{w_clean}\">Открыть канал/чат</a>")
        else:
            lines.append(f"🌐 <b>Сайт / Соцсети:</b> <a href=\"{w_clean}\">Перейти на сайт</a>")

    crm_entry = lead_hunter_service.get_crm_entry(place.place_id)
    if crm_entry:
        st_label = crm_entry.get("status_label", "📌 В CRM")
        upd = crm_entry.get("updated_at", "")
        upd_part = f" <i>({html.escape(upd)})</i>" if upd else ""
        lines.append(f"📌 <b>Статус в CRM:</b> <b>{html.escape(st_label)}</b>{upd_part}")

    if not lines:
        return ""
    return "\n" + "\n".join(lines) + "\n"


def format_place_card(place: PlaceInfo) -> str:
    """Форматирует стандартную карточку заведения (для ручного поиска) с контактами и CRM."""
    rating_part = ""
    if place.rating is not None:
        rating_part = f"⭐️ {place.rating:.1f}"
        if place.user_ratings_total is not None:
            rating_part += f" | {place.user_ratings_total} отзывов"
    else:
        rating_part = "⭐️ Точка найдена"

    contacts_block = format_contacts_and_crm_block(place)

    card = (
        f"🏢 <b>{html.escape(place.name)}</b> ({rating_part})\n"
        f"📍 <i>{html.escape(place.address)}</i>\n"
        f"{contacts_block}\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )
    return card


def format_carousel_slide(batch: HuntCarouselBatch, idx: int) -> str:
    """
    Форматирует один слайд карусели ТОП-лидов (или карусели сделок CRM) в едином сообщении.
    """
    total = len(batch.leads)
    lead = batch.leads[idx]
    place = lead.place

    if batch.niche_key == "crm":
        filter_titles = {
            "all": "Все активные сделки",
            "contacted": "⏳ Написали (Ждём ответ)",
            "meeting": "🤝 Заехать на 2-мин показ",
            "won": "💰 Купили карту",
        }
        f_title = filter_titles.get(batch.district_key, "Все сделки")
        header_line = f"📋 <b>Мини-CRM HHtap • {html.escape(f_title)}</b>"
    else:
        niche_info = HUNT_NICHES.get(batch.niche_key, HUNT_NICHES["all"])
        district_info = HUNT_DISTRICTS.get(batch.district_key, HUNT_DISTRICTS["auto"])
        header_line = (
            f"🎯 <b>{html.escape(niche_info['short'])} • {html.escape(district_info['short'])}</b> "
            f"<i>(просканировано: {batch.scanned_total})</i>"
        )

    if place.rating is not None:
        rating_part = f"⭐️ {place.rating:.1f}"
        if place.user_ratings_total is not None:
            rating_part += f" | {place.user_ratings_total} отзывов"
    else:
        rating_part = "⭐️ Мало оценок"

    contacts_block = format_contacts_and_crm_block(place)

    return (
        f"{header_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>ТОП-{idx + 1} из {total} | Оценка лида: {lead.score}/100</b>\n"
        f"🏷 <b>Категория:</b> {html.escape(lead.tier_badge)}\n\n"
        f"🏢 <b>{html.escape(place.name)}</b> ({rating_part})\n"
        f"📍 <i>{html.escape(place.address)}</i>\n"
        f"{contacts_block}\n"
        f"💡 <b>Почему купят:</b>\n{html.escape(lead.why_hot)}\n\n"
        f"🔗 <b>Ссылка для NFC Tools (тапни для копирования):</b>\n"
        f"<code>{place.review_url}</code>"
    )


def make_carousel_keyboard(batch: HuntCarouselBatch, idx: int) -> InlineKeyboardMarkup:
    """
    Минималистичная и эстетичная клавиатура слайда карусели:
    Ряд 1: [ ✉️ Написать ] | [ 📞 Контакты и CRM ] | [ 🔗 5⭐️ ]
    Ряд 2: [ ⬅️ ] | [ 📍 1 / 5 ] | [ ➡️ ]
    Ряд 3: [ ⚡️ Поиск и локации ] (или фильтры CRM, если открыта CRM)
    """
    total = len(batch.leads)
    lead = batch.leads[idx]
    short_id = cache_place(
        lead.place,
        batch_id=batch.batch_id,
        slide_idx=idx,
        score=lead.score,
        tier_badge=lead.tier_badge,
        why_hot=lead.why_hot,
    )

    crm_entry = lead_hunter_service.get_crm_entry(lead.place.place_id)
    crm_btn_text = "✅ Контакты и CRM" if crm_entry else "📞 Контакты и CRM"

    nav_row: List[InlineKeyboardButton] = []
    if idx > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"hunt:page:{batch.batch_id}:{idx - 1}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"📍 {idx + 1} / {total}",
            callback_data="hunt:noop",
        )
    )
    if idx < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"hunt:page:{batch.batch_id}:{idx + 1}",
            )
        )

    bottom_row: List[InlineKeyboardButton] = []
    if batch.niche_key == "crm":
        bottom_row.append(
            InlineKeyboardButton(
                text="🗂 Фильтр сделок CRM",
                callback_data="crm:overview",
            )
        )
    else:
        bottom_row.append(
            InlineKeyboardButton(
                text="⚡️ Поиск и локации",
                callback_data=f"hunt:ctrl:{batch.batch_id}:{idx}",
            )
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Написать",
                    callback_data=f"ai:{short_id}:msg_menu",
                ),
                InlineKeyboardButton(
                    text=crm_btn_text,
                    callback_data=f"crm:{short_id}:menu",
                ),
                InlineKeyboardButton(
                    text="🔗 5⭐️",
                    url=lead.place.review_url,
                ),
            ],
            nav_row,
            bottom_row,
        ]
    )


def make_hunt_ctrl_keyboard(batch: HuntCarouselBatch, idx: int) -> InlineKeyboardMarkup:
    """Раскрывающееся подменю управления поиском под каруселью."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Ещё 5 новых лидов в этом секторе",
                    callback_data=f"hunt:run:{batch.niche_key}:{batch.district_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧭 Сменить нишу или район",
                    callback_data="hunt:menu",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к карточке",
                    callback_data=f"hunt:page:{batch.batch_id}:{idx}",
                )
            ],
        ]
    )


def make_place_keyboard(place: PlaceInfo) -> InlineKeyboardMarkup:
    """Минималистичное меню под одиночной карточкой заведения (при ручном поиске)."""
    short_id = cache_place(place)
    crm_entry = lead_hunter_service.get_crm_entry(place.place_id)
    crm_btn_text = "✅ Контакты и CRM" if crm_entry else "📞 Контакты и CRM"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Написать",
                    callback_data=f"ai:{short_id}:msg_menu",
                ),
                InlineKeyboardButton(
                    text=crm_btn_text,
                    callback_data=f"crm:{short_id}:menu",
                ),
                InlineKeyboardButton(
                    text="🔗 5⭐️",
                    url=place.review_url,
                ),
            ],
        ]
    )


def make_contacts_crm_drawer_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """
    Раскрывающееся меню «📞 Контакты и CRM» (Фича 1 + Фича 2):
    - Прямые кнопки 1-клик в Telegram (+998...), WhatsApp и поиск Instagram/Сайта
    - Кнопки Мини-CRM в 1 клик: [ ⏳ Написали ] | [ 🤝 На показ ] | [ 💰 Купили ]
    """
    cached = place_cache.get(short_id)
    rows: List[List[InlineKeyboardButton]] = []

    if cached:
        p_info = parse_uz_phone(cached.phone)
        # Если мобильный номер — даём прямые кнопки 1-клик в Telegram и WhatsApp
        if p_info["is_mobile"] and p_info["tg_url"]:
            messenger_row = [
                InlineKeyboardButton(
                    text=f"✈️ Открыть Telegram ({p_info['e164']})",
                    url=p_info["tg_url"],
                ),
            ]
            if p_info["wa_url"]:
                messenger_row.append(
                    InlineKeyboardButton(
                        text="💬 WhatsApp",
                        url=p_info["wa_url"],
                    )
                )
            rows.append(messenger_row)

        # Кнопка мгновенного перехода или поиска Instagram точки + Сайт
        insta_label, insta_url = build_insta_search_url(cached.name, cached.website)
        social_row = [
            InlineKeyboardButton(
                text=insta_label,
                url=insta_url,
            )
        ]
        if cached.website and "instagram.com" not in cached.website.lower():
            social_row.append(
                InlineKeyboardButton(
                    text="🌐 Сайт точки",
                    url=cached.website,
                )
            )
        rows.append(social_row)

        # Кнопки статусов Мини-CRM (с галочкой на текущем статусе)
        crm_entry = lead_hunter_service.get_crm_entry(cached.place_id)
        cur_status = crm_entry.get("status") if crm_entry else None

        btn_contacted = "✅ ⏳ Написали" if cur_status == "contacted" else "⏳ Написали"
        btn_meeting = "✅ 🤝 На показ" if cur_status == "meeting" else "🤝 На показ"
        btn_won = "✅ 💰 Купили" if cur_status == "won" else "💰 Купили"

        rows.append(
            [
                InlineKeyboardButton(
                    text=btn_contacted,
                    callback_data=f"crm:{short_id}:set:contacted",
                ),
                InlineKeyboardButton(
                    text=btn_meeting,
                    callback_data=f"crm:{short_id}:set:meeting",
                ),
                InlineKeyboardButton(
                    text=btn_won,
                    callback_data=f"crm:{short_id}:set:won",
                ),
            ]
        )

        if crm_entry:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🗑 Убрать из CRM",
                        callback_data=f"crm:{short_id}:del",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Свернуть меню",
                callback_data=f"ai:{short_id}:back_main",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_messages_drawer_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """
    Раскрывающееся меню выбора формата сообщения (открывается по нажатию «✉️ Написать»):
    [ 📸 Инста Direct ] | [ ✈️ Телеграм (видео) ]
    [ 🎤 Текст для ГС ] | [ 🔔 Дожим (Молчат) ]
    [ 🛡 Отработать возражение ]
    [ ⬅️ Свернуть меню ]
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📸 Инста Direct",
                    callback_data=f"ai:{short_id}:insta",
                ),
                InlineKeyboardButton(
                    text="✈️ Телеграм (видео)",
                    callback_data=f"ai:{short_id}:tg",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎤 Текст для ГС",
                    callback_data=f"ai:{short_id}:voice",
                ),
                InlineKeyboardButton(
                    text="🔔 Дожим (Молчат)",
                    callback_data=f"ai:{short_id}:followup",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🛡 Отработать возражение",
                    callback_data=f"ai:{short_id}:obj_menu",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Свернуть меню",
                    callback_data=f"ai:{short_id}:back_main",
                )
            ],
        ]
    )


def make_objections_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """
    Подменю отработки возражений:
    [📱 У нас уже есть QR-код]
    [💸 Дорого / Не актуально]
    [📄 Скиньте КП, мы подумаем]
    [⬅️ Назад к сообщениям]
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 У нас уже есть QR-код",
                    callback_data=f"ai:{short_id}:obj_qr",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Дорого / Не актуально",
                    callback_data=f"ai:{short_id}:obj_price",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📄 Скиньте КП, мы подумаем",
                    callback_data=f"ai:{short_id}:obj_think",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к сообщениям",
                    callback_data=f"ai:{short_id}:msg_menu",
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
def format_main_welcome_text() -> str:
    mode_desc = (
        "🟢 <b>Режим парсера:</b> Google Places API"
        if config.has_google_api_key()
        else "🟢 <b>Режим парсера:</b> Автономный SDK-парсер (с рейтингами и отзывами, без ключей)"
    )
    ai_desc = (
        f"🤖 <b>ИИ-ассистент:</b> Подключен ({config.anymodel_model})"
        if config.has_anymodel_key()
        else "🤖 <b>ИИ-ассистент:</b> Встроенный Smart-генератор HHtap"
    )
    return (
        "👋 <b>Добро пожаловать в комбайн лидогенерации HHtap!</b>\n\n"
        f"{mode_desc}\n"
        f"{ai_desc}\n\n"
        "🧭 <b>ДВА РАБОЧИХ МОДУЛЯ БОТА:</b>\n"
        "1️⃣ <b>⭐️ Google Reviews NFC:</b> автопоиск ТОП-5 заведений по улицам Ташкента, прямые 5⭐️ ссылки для NFC Tools, контакты и ИИ-питчи.\n"
        "2️⃣ <b>📸 Instagram NFC:</b> генерация ссылок для NFC-карт под любой Instagram-аккаунт (<code>@username</code> или ссылка) + скрипты продаж и комбо-оффер.\n\n"
        "💡 <i>Бот также поддерживает автоопределение: просто отправьте <code>@ник</code> или ссылку <code>instagram.com/...</code> либо название заведения!</i>"
    )


def format_insta_mode_prompt() -> str:
    return (
        "📸 <b>Режим: Instagram NFC</b>\n\n"
        "Отправьте Instagram-аккаунт клиента в любом удобном формате:\n"
        "• Чистый никнейм: <code>phoenix_math_school</code>\n"
        "• Ник с собачкой: <code>@phoenix_math_school</code>\n"
        "• Полную ссылку: <code>https://www.instagram.com/phoenix_math_school/</code>\n\n"
        "⚡️ Бот мгновенно выдаст <b>2 готовые ссылки для записи в NFC Tools</b> "
        "(основную с <code>igsh</code> для iOS/Android и прямой Deep Link) + кнопки генерации ИИ-питчей и Комбо-оффера!"
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветственное сообщение, нижняя клавиатура и стартовое меню выбора режима."""
    if message.from_user:
        user_modes[message.from_user.id] = "google"

    await message.answer(
        format_main_welcome_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_reply_keyboard(),
    )
    await message.answer(
        "👇 <b>Выберите режим работы или просто отправьте запрос в чат:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=make_start_mode_inline_keyboard(),
    )


@router.message(F.text == BTN_MODE_GOOGLE)
async def handle_mode_google_btn(message: Message):
    """Переключение в режим Google Reviews NFC."""
    if message.from_user:
        user_modes[message.from_user.id] = "google"

    text = (
        "⭐️ <b>Включён режим: Google Reviews NFC</b>\n\n"
        "• Нажмите <b>«🎯 Автопоиск ТОП-лидов»</b> или <b>«🔥 Быстрый рейд»</b> внизу экрана\n"
        "• Или отправьте название заведения и улицу (<i>Жигули Бар Ташкент</i>)\n"
        "• Или ссылку из Google Maps (<code>https://maps.app.goo.gl/...</code>)"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_reply_keyboard(),
    )


@router.message(Command("insta"))
@router.message(F.text == BTN_MODE_INSTA)
async def handle_mode_insta_btn(message: Message):
    """Переключение в режим Instagram NFC."""
    if message.from_user:
        user_modes[message.from_user.id] = "insta"

    await message.answer(
        format_insta_mode_prompt(),
        parse_mode=ParseMode.HTML,
        reply_markup=make_main_reply_keyboard(),
    )


@router.callback_query(F.data.startswith("mode:"))
async def handle_mode_callbacks(callback: CallbackQuery):
    """Обработка Inline-кнопок переключения режима (mode:google, mode:insta, mode:main)."""
    parts = callback.data.split(":")
    target = parts[1] if len(parts) > 1 else "main"
    user_id = callback.from_user.id if callback.from_user else 0

    if target == "insta":
        user_modes[user_id] = "insta"
        await callback.message.answer(
            format_insta_mode_prompt(),
            parse_mode=ParseMode.HTML,
            reply_markup=make_main_reply_keyboard(),
        )
        await callback.answer("📸 Режим Instagram NFC включён")
        return

    if target == "google":
        user_modes[user_id] = "google"
        text = (
            "⭐️ <b>Включён режим: Google Reviews NFC</b>\n\n"
            "Отправьте название заведения и улицу, ссылку Google Maps или выберите нишу для Автопоиска ТОП-5:"
        )
        await callback.message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=make_hunt_niche_keyboard(),
        )
        await callback.answer("⭐️ Режим Google Reviews NFC")
        return

    # mode:main — возврат в главное меню
    user_modes[user_id] = "google"
    await callback.message.answer(
        format_main_welcome_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=make_start_mode_inline_keyboard(),
    )
    await callback.answer("Главное меню")


@router.callback_query(F.data.startswith("ig:"))
async def handle_instagram_callbacks(callback: CallbackQuery):
    """
    Обработка кнопок под карточкой Instagram NFC:
    - ig:ig_pitch:{username} — 🎯 Питч для клиента (Текст)
    - ig:ig_live:{username}  — 🗣 Скрипт для живого разговора
    - ig:ig_combo:{username} — 💥 Комбо-оффер (Google + Insta)
    - ig:r:{action}:{username} — 🔄 Перегенерировать
    - ig:hide — ⬅️ К карточке Instagram
    """
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return

    if parts[1] == "hide":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("Вернулись к карточке")
        return

    is_regen = False
    if parts[1] == "r" and len(parts) >= 4:
        is_regen = True
        action = parts[2]
        clean_username = parts[3]
    elif len(parts) >= 3:
        action = parts[1]
        clean_username = parts[2]
    else:
        await callback.answer()
        return

    await callback.answer("⏳ Генерирую скрипт для Instagram NFC...")

    async with ChatActionSender.typing(bot=callback.bot, chat_id=callback.message.chat.id):
        try:
            generated_text = await ai_sales_service.generate_instagram_message(
                action=action,
                username=clean_username,
            )
            escaped_text = html.escape(generated_text.strip())
            output_text = f"<code>{escaped_text}</code>"
            reply_kb = make_instagram_ai_response_keyboard(clean_username, action)

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
        except Exception as e:
            logger.exception("Ошибка генерации текста Instagram NFC: %s", e)
            await callback.message.answer(
                f"❌ <b>Ошибка генерации:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def process_instagram_request(message: Message, raw_text: str):
    """Обрабатывает ввод аккаунта/ссылки Instagram и отправляет карточку с NFC-ссылками и кнопками ИИ."""
    try:
        card_data: InstagramCardData = build_instagram_card_data(raw_text)
        card_html = format_instagram_card(card_data)
        kb = make_instagram_card_keyboard(card_data.clean_username)
        await message.reply(
            card_html,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except InstagramParseError as e:
        await message.reply(
            f"⚠️ <b>Ошибка разбора аккаунта Instagram</b>\n\n{e.message}",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("hunt"))
@router.message(F.text == BTN_HUNT_MENU)
async def cmd_hunt_menu(message: Message):
    """Открывает интерактивное меню выбора ниши для Автопоиска ТОП-лидов."""
    seen = lead_hunter_service.seen_count()
    text = (
        "🎯 <b>Автопоиск самых горячих лидов HHtap</b>\n\n"
        "Бот просканирует улицы Ташкента, отсеет уже показанные точки "
        f"(в памяти сейчас: <b>{seen}</b> уникальных заведений) и выдаст <b>ТОП-5 клиентов по приоритету</b>:\n"
        "• 🔥 <b>Приоритет №1: оценка 3.5–4.3⭐️ и мало отзывов</b> (острая боль + исправляется за 1–3 смены!)\n"
        "• ⚡️ <b>Приоритет №2: просаженный рейтинг и уязвимые карточки</b>\n\n"
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


def make_crm_overview_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура фильтрации сделок в Мини-CRM."""
    counts = lead_hunter_service.crm_counts()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📋 Все сделки ({counts['all']})",
                    callback_data="crm:filter:all",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⏳ Написали • Ждём ответ ({counts['contacted']})",
                    callback_data="crm:filter:contacted",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🤝 Заехать на 2-мин показ ({counts['meeting']})",
                    callback_data="crm:filter:meeting",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💰 Купили карту ({counts['won']})",
                    callback_data="crm:filter:won",
                )
            ],
        ]
    )


def format_crm_overview_text() -> str:
    counts = lead_hunter_service.crm_counts()
    return (
        "📋 <b>Мини-CRM команды HHtap (Воронка сделок)</b>\n\n"
        f"• ⏳ <b>Написали (Ждём ответ):</b> <code>{counts['contacted']}</code>\n"
        f"• 🤝 <b>Заехать на 2-мин показ:</b> <code>{counts['meeting']}</code>\n"
        f"• 💰 <b>Купили карту:</b> <code>{counts['won']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🗂 <b>Всего точек в работе:</b> <code>{counts['all']}</code>\n\n"
        "💡 <i>Чтобы добавить заведение в CRM или написать ему в 1 клик, нажмите кнопку "
        "<b>«📞 Контакты и CRM»</b> под любой карточкой.</i>\n\n"
        "👇 <b>Выберите список для просмотра каруселью:</b>"
    )


@router.message(Command("crm"))
@router.message(F.text == BTN_CRM)
async def cmd_crm_menu(message: Message):
    """Открывает панель Мини-CRM со сводкой и фильтрами по статусам сделок."""
    await message.answer(
        format_crm_overview_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=make_crm_overview_keyboard(),
    )


@router.callback_query(F.data.startswith("crm:"))
async def handle_crm_callbacks(callback: CallbackQuery):
    """Обработчик Мини-CRM и меню прямых контактов в 1 клик."""
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return

    # 1. Сводка Мини-CRM
    if parts[1] == "overview":
        await callback.message.edit_text(
            format_crm_overview_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=make_crm_overview_keyboard(),
        )
        await callback.answer()
        return

    # 2. Открытие карусели сделок по фильтру (all / contacted / meeting / won)
    if parts[1] == "filter" and len(parts) >= 3:
        status_filter = parts[2]
        leads = lead_hunter_service.list_crm_leads(status_filter=status_filter)
        if not leads:
            await callback.answer(
                "В этой категории пока нет сохранённых точек. Отметьте статус под любой карточкой в «📞 Контакты и CRM»!",
                show_alert=True,
            )
            return

        batch_id = cache_carousel_batch(
            niche_key="crm",
            district_key=status_filter,
            scanned_total=len(leads),
            leads=leads,
        )
        batch = carousel_cache[batch_id]
        slide_html = format_carousel_slide(batch, idx=0)
        kb = make_carousel_keyboard(batch, idx=0)
        await callback.message.edit_text(
            slide_html,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await callback.answer(f"Открыто сделок: {len(leads)}")
        return

    # 3. Действия с конкретной точкой: crm:{short_id}:menu | set:{status} | del
    short_id = parts[1]
    sub_action = parts[2] if len(parts) >= 3 else "menu"
    cached = place_cache.get(short_id)

    if not cached:
        await callback.answer(
            "⚠️ Карточка устарела. Откройте поиск или CRM заново.",
            show_alert=True,
        )
        return

    if sub_action == "menu":
        await callback.message.edit_reply_markup(
            reply_markup=make_contacts_crm_drawer_keyboard(short_id)
        )
        await callback.answer()
        return

    if sub_action == "set" and len(parts) >= 4:
        new_status = parts[3]
        place_obj = PlaceInfo(
            place_id=cached.place_id,
            name=cached.name,
            address=cached.address,
            rating=cached.rating,
            user_ratings_total=cached.reviews_count,
            review_url=cached.review_link,
            phone=cached.phone,
            website=cached.website,
        )
        lead_hunter_service.set_crm_status(
            place=place_obj,
            status=new_status,
            score=cached.score,
            tier_badge=cached.tier_badge,
            why_hot=cached.why_hot,
        )
        # Обновляем текст карточки (чтобы сразу появился бейдж CRM) и галочку на кнопке
        try:
            if cached.batch_id and cached.slide_idx is not None and cached.batch_id in carousel_cache:
                batch = carousel_cache[cached.batch_id]
                new_html = format_carousel_slide(batch, cached.slide_idx)
            else:
                new_html = format_place_card(place_obj)
            await callback.message.edit_text(
                new_html,
                parse_mode=ParseMode.HTML,
                reply_markup=make_contacts_crm_drawer_keyboard(short_id),
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.edit_reply_markup(
                reply_markup=make_contacts_crm_drawer_keyboard(short_id)
            )
        label = CRM_STATUSES.get(new_status, new_status)
        await callback.answer(f"✅ Сохранено в CRM: {label}")
        return

    if sub_action == "del":
        lead_hunter_service.remove_crm_lead(cached.place_id)
        place_obj = PlaceInfo(
            place_id=cached.place_id,
            name=cached.name,
            address=cached.address,
            rating=cached.rating,
            user_ratings_total=cached.reviews_count,
            review_url=cached.review_link,
            phone=cached.phone,
            website=cached.website,
        )
        try:
            if cached.batch_id and cached.slide_idx is not None and cached.batch_id in carousel_cache:
                batch = carousel_cache[cached.batch_id]
                new_html = format_carousel_slide(batch, cached.slide_idx)
            else:
                new_html = format_place_card(place_obj)
            await callback.message.edit_text(
                new_html,
                parse_mode=ParseMode.HTML,
                reply_markup=make_contacts_crm_drawer_keyboard(short_id),
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.edit_reply_markup(
                reply_markup=make_contacts_crm_drawer_keyboard(short_id)
            )
        await callback.answer("🗑 Точка удалена из CRM")
        return


@router.message(F.text == BTN_STATS)
async def handle_stats_info(message: Message):
    """Показывает статистику базы и логику скоринга."""
    seen = lead_hunter_service.seen_count()
    text = (
        "📊 <b>Память бота и многофакторный алгоритм оценки (0–98 баллов)</b>\n\n"
        f"🗂 <b>В анти-дубль памяти (уже просмотрено):</b> <code>{seen}</code> заведений.\n"
        "<i>Бот запоминает каждое показанное заведение по Place ID и названию, поэтому даже после 10–15 нажатий подряд в одном районе он углубляется по десяткам улиц и не присылает дубли.</i>\n\n"
        "🧠 <b>Главный приоритет отбора (почему купят сразу):</b>\n"
        "1️⃣ <b>🔥 Золотой сегмент (88–98 баллов — всегда в ТОП-1):</b>\n"
        "   • Оценка <code>3.5–4.3⭐️</code> <b>И мало отзывов</b> (<code>3–65</code> отз.)\n"
        "   • У владельца горит рейтинг, а из-за малого числа оценок ему нужно всего <code>10–30</code> пятёрок, чтобы взлететь до <code>4.5⭐️</code> за 1–3 смены!\n"
        "2️⃣ <b>⚡️ Острая боль (79–87 баллов):</b>\n"
        "   • Просаженный рейтинг <code>3.5–4.4⭐️</code> при среднем числе отзывов.\n"
        "3️⃣ <b>🚫 Жёсткий отсев:</b>\n"
        "   • Заведения с высокой оценкой (<code>4.8–5.0⭐️</code>) и большим числом отзывов (<code>>130</code>) полностью отсекаются, чтобы вы не тратили время на тех, у кого нет боли."
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

    # Открытие подменю «⚡️ Поиск и локации» под слайдом карусели
    if action == "ctrl" and len(parts) >= 4:
        batch_id = parts[2]
        try:
            slide_idx = int(parts[3])
        except ValueError:
            await callback.answer()
            return

        batch = carousel_cache.get(batch_id)
        if not batch or not (0 <= slide_idx < len(batch.leads)):
            await callback.answer("⚠️ Эта подборка устарела.", show_alert=True)
            return

        await callback.message.edit_reply_markup(
            reply_markup=make_hunt_ctrl_keyboard(batch, slide_idx)
        )
        await callback.answer()
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

    # 0. Открытие меню выбора сообщений («✉️ Написать»)
    if action == "msg_menu":
        await callback.message.edit_reply_markup(
            reply_markup=make_messages_drawer_keyboard(short_id)
        )
        await callback.answer()
        return

    # 1. Открытие подменю возражений («🛡 Возражения»)
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
                    phone=cached.phone,
                    website=cached.website,
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
    Оба парсера (Google Maps и Instagram NFC) работают ОДНОВРЕМЕННО:
    - Если обнаружена ссылка Google Maps -> парсер Google Maps.
    - Если обнаружена ссылка instagram.com, @username, ник с '_' или одиночный латинский ник -> модуль Instagram NFC.
    - Любой поисковый запрос заведения (с кириллицей или из нескольких слов) -> всегда идёт в парсер Google Maps,
      даже если до этого нажималась кнопка Instagram NFC!
    """
    text = message.text.strip()
    if not text:
        return

    user_id = message.from_user.id if message.from_user else 0
    current_mode = user_modes.get(user_id, "google")
    url_match = MAPS_URL_REGEX.search(text)

    # Если это не ссылка Google Maps и запрос распознан как Instagram-аккаунт / ссылка Instagram
    if not url_match and is_instagram_input(text, mode_hint=current_mode):
        await process_instagram_request(message, text)
        return

    # Если пользователь ввёл обычный запрос для Google Карт — автоматически держим режим Google активным
    user_modes[user_id] = "google"

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
