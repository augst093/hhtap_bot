"""
Сервис обработки и генерации ссылок для модуля Instagram NFC (HHtap).
Поддерживает:
- Извлечение и валидацию чистого username из любого формата (@nick, nick, https://instagram.com/nick?igsh=...)
- Генерацию двух проверенных форматов ссылок для записи в NFC Tools (Web Handle + Deep Link)
- Автоопределение Instagram-запросов в общем потоке сообщений
"""
import html
import re
from dataclasses import dataclass
from typing import Optional


# Зарезервированные системные пути Instagram, которые не являются профилями пользователей
RESERVED_IG_PATHS = {
    "p",
    "reel",
    "reels",
    "stories",
    "explore",
    "tv",
    "direct",
    "accounts",
    "about",
    "legal",
    "developer",
    "challenge",
    "ar",
    " session",
}

# Валидный username в Instagram: от 1 до 30 символов (буквы латиницы, цифры, точка, нижнее подчеркивание)
IG_USERNAME_REGEX = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Регулярное выражение для извлечения username из любой ссылки instagram.com / instagr.am
IG_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/([A-Za-z0-9._]{1,30})(?:[/?#]|$)",
    re.IGNORECASE,
)

# Проверенный параметр igsh для гарантированного открытия профиля в мобильном приложении на iOS и Android
DEFAULT_IGSH_TOKEN = "OGQ5ZDc2ODk2ZA=="


class InstagramParseError(Exception):
    """Ошибка парсинга или невалидный Instagram username."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class InstagramCardData:
    clean_username: str
    primary_nfc_url: str
    deep_link_url: str
    profile_url: str


def validate_ig_username(candidate: str) -> Optional[str]:
    """
    Проверяет, соответствует ли строка правилам никнеймов Instagram:
    - 1..30 символов [A-Za-z0-9._]
    - не состоит только из точек и не содержит '..' подряд
    - не входит в список системных путей Instagram (reel, p, stories и т.д.)
    """
    clean = candidate.strip().lstrip("@").strip("/")
    if not clean:
        return None
    if clean.lower() in RESERVED_IG_PATHS:
        return None
    if not IG_USERNAME_REGEX.fullmatch(clean):
        return None
    if clean.startswith(".") or clean.endswith(".") or ".." in clean:
        return None
    return clean


def parse_instagram_username(raw_input: str) -> str:
    """
    Извлекает чистый валидный Instagram username из любого пользовательского ввода:
    - 'phoenix_math_school'
    - '@phoenix_math_school'
    - 'https://www.instagram.com/phoenix_math_school/'
    - 'https://instagram.com/phoenix_math_school?igsh=...'

    Выбрасывает InstagramParseError, если извлечь валидный username невозможно.
    """
    if not raw_input or not raw_input.strip():
        raise InstagramParseError("Пустой запрос. Отправьте никнейм или ссылку на профиль Instagram.")

    text = raw_input.strip()

    # 1. Проверяем, есть ли в тексте ссылка instagram.com / instagr.am
    if "instagram.com" in text.lower() or "instagr.am" in text.lower():
        url_match = IG_URL_REGEX.search(text)
        if not url_match:
            raise InstagramParseError(
                "Не удалось извлечь имя аккаунта из ссылки Instagram.\n"
                "Отправьте ссылку на профиль вида: <code>https://instagram.com/phoenix_math_school</code>"
            )
        candidate = url_match.group(1)
        valid = validate_ig_username(candidate)
        if not valid:
            raise InstagramParseError(
                f"Ссылка ведёт на системный раздел Instagram (<code>{html.escape(candidate)}</code>), а не на профиль клиента.\n"
                "Отправьте прямую ссылку на страницу аккаунта или его @никнейм."
            )
        return valid

    # 2. Если пользователь прислал @username или чистый username
    # Отрезаем случайные query-параметры и слеши, если скинули 'phoenix_math_school/?igsh=...'
    token = text.split()[0].strip()
    token = token.split("?")[0].split("#")[0].strip().lstrip("@").strip("/")

    valid = validate_ig_username(token)
    if not valid:
        raise InstagramParseError(
            "Некорректный формат Instagram-аккаунта.\n\n"
            "Никнейм может содержать только латинские буквы, цифры, точки и нижние подчёркивания (до 30 символов).\n"
            "Примеры:\n"
            "• <code>phoenix_math_school</code>\n"
            "• <code>@phoenix_math_school</code>\n"
            "• <code>https://instagram.com/phoenix_math_school</code>"
        )
    return valid


def is_instagram_input(text: str, mode_hint: str = "google") -> bool:
    """
    Умное одновременное распознавание (оба парсера работают параллельно без блокировки друг друга!):
    Возвращает True ТОЛЬКО если сообщение является Instagram-профилем:
    1) Содержит ссылку instagram.com / instagr.am
    2) Начинается с '@' (например '@phoenix_math_school')
    3) Одно слово без пробелов и без кириллицы, содержащее '_' или '.' (например 'phoenix_math_school', 'coffee.uz')
    4) Одно латинское слово без пробелов (например 'safiauz'), если пользователь перед этим нажал '📸 Instagram NFC'.
    Любой текст с пробелами ('Roni Тараса Шевченко'), кириллицей ('Жигули Бар') или ссылкой Google Maps
    ВСЕГДА возвращает False и уходит в парсер Google Карт!
    """
    if not text:
        return False
    stripped = text.strip()
    low = stripped.lower()

    # 1. Прямая ссылка на Instagram
    if "instagram.com/" in low or "instagr.am/" in low:
        return True

    # 2. Никнейм с собачкой @username
    if stripped.startswith("@") and len(stripped) > 1 and " " not in stripped:
        return True

    # 3. Если в запросе есть пробелы, кириллица или другая http-ссылка — это 100% поиск заведения в Google Картах!
    if " " in stripped or low.startswith("http") or re.search(r"[а-яА-ЯёЁ]", stripped):
        return False

    # 4. Одиночное латинское слово (валидный Instagram handle)
    if validate_ig_username(stripped) is not None:
        if "_" in stripped or "." in stripped:
            return True
        if mode_hint == "insta":
            return True

    return False


def build_instagram_card_data(raw_input: str) -> InstagramCardData:
    """Формирует полный пакет ссылок для NFC Tools по Instagram-аккаунту."""
    clean_username = parse_instagram_username(raw_input)
    primary_nfc_url = f"https://instagram.com/{clean_username}?igsh={DEFAULT_IGSH_TOKEN}"
    deep_link_url = f"instagram://user?username={clean_username}"
    profile_url = f"https://instagram.com/{clean_username}"

    return InstagramCardData(
        clean_username=clean_username,
        primary_nfc_url=primary_nfc_url,
        deep_link_url=deep_link_url,
        profile_url=profile_url,
    )


def format_instagram_card(data: InstagramCardData) -> str:
    """Форматирует HTML-сообщение карточки Instagram NFC по стандарту HHtap."""
    u = html.escape(data.clean_username)
    primary = html.escape(data.primary_nfc_url)
    deep = html.escape(data.deep_link_url)

    return (
        f"📸 <b>Instagram NFC карта для: @{u}</b>\n\n"
        f"🔗 <b>Основная ссылка (рекомендуется для NFC Tools):</b>\n"
        f"<code>{primary}</code>\n"
        f"<i>(Нажми на ссылку выше, чтобы скопировать без пробелов)</i>\n\n"
        f"⚡️ <b>Прямой Deep Link (альтернатива):</b>\n"
        f"<code>{deep}</code>"
    )
