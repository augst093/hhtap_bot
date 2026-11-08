"""
Сервис обработки, мгновенного поиска и генерации ссылок для модуля Instagram NFC (HHtap).
Поддерживает:
- Извлечение и валидацию чистого username из любого формата (@nick, nick, https://instagram.com/nick?igsh=...)
- Мгновенный 4-уровневый поиск официального Instagram-аккаунта любого бизнеса по названию с припиской insta/инста
  (например: 'Kamolon Osh insta', 'Камолон Ош инста', 'Roni инста', 'Breadly инста', 'Про Хинкали инста')
- Генерацию двух проверенных форматов ссылок для записи в NFC Tools (Web Handle + Deep Link)
- Автоопределение Instagram-запросов в общем потоке сообщений
"""
import asyncio
import html
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

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
    "session",
    "_u",
    "share",
    "popular",
    "seo",
    "sem",
    "directory",
    "tags",
    "locations",
}

# Валидный username в Instagram: от 1 до 30 символов (буквы латиницы, цифры, точка, нижнее подчеркивание)
IG_USERNAME_REGEX = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Регулярное выражение для извлечения username из любой ссылки instagram.com / instagr.am (включая /_u/ и /stories/)
IG_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:_u/|stories/)?([A-Za-z0-9._]{1,30})(?:[/?#]|$)",
    re.IGNORECASE,
)

# Проверенный параметр igsh для гарантированного открытия профиля в мобильном приложении на iOS и Android
DEFAULT_IGSH_TOKEN = "OGQ5ZDc2ODk2ZA=="

# Ключевые слова приписки для мгновенного поиска Instagram бизнеса по названию
INSTA_TRIGGER_WORDS = r"(?:инстаграм|инста|instagram|insta|ig|иг)"

# Таблица транслитерации кириллицы (русский + узбекский кириллический алфавит) в латиницу
CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ў": "o", "ф": "f", "х": "x", "ҳ": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


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
    profile_title: Optional[str] = None
    followers_count: Optional[int] = None
    alternative_usernames: List[str] = field(default_factory=list)


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

    # 2. Если пользователь написал префикс 'инста @nick' / 'insta nick_with_underscore'
    prefix_match = re.match(
        rf"^{INSTA_TRIGGER_WORDS}\s+(@?[A-Za-z0-9._]{{1,30}})$",
        text,
        re.IGNORECASE,
    )
    if prefix_match:
        token = prefix_match.group(1).lstrip("@").strip("/")
    else:
        # 3. Если пользователь прислал @username или чистый username
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
            "• <code>https://instagram.com/phoenix_math_school</code>\n"
            "• Или название бизнеса с припиской: <code>Kamolon Osh insta</code> / <code>Камолон Ош инста</code>"
        )
    return valid


def extract_insta_search_query(text: str) -> Optional[str]:
    """
    Определяет, написал ли пользователь название бизнеса с припиской 'insta' / 'инста' / 'ig' / 'instagram'
    в конце или в начале (например:
      - 'Kamolon Osh insta'
      - 'Kamolon Osh инста'
      - 'Камолон Ош инста'
      - 'Камолон Ош insta'
      - 'Roni инста'
      - 'Soul Bakery Паркент инста'
      - 'инста Камолон Ош'
    ).
    Если внутри приписки уже передан готовый точный @username или ссылка instagram.com,
    возвращает None (чтобы сработал мгновенный парсер прямого @username).
    Иначе возвращает очищенное название бизнеса для мгновенного поиска его Instagram!
    """
    if not text:
        return None
    stripped = text.strip()
    low = stripped.lower()

    # Прямая ссылка на Instagram или Google Maps не является текстовым поиском по названию
    if "instagram.com/" in low or "instagr.am/" in low or "google.com/maps" in low or "goo.gl/" in low:
        return None

    biz_part: Optional[str] = None

    # 1. Приписка в КОНЦЕ: "Kamolon Osh insta", "Камолон Ош инста", "Roni инста"
    suffix_m = re.match(
        rf"^(.+?)\s*[\s,.\-–—]+\s*{INSTA_TRIGGER_WORDS}[.!?\s]*$",
        stripped,
        re.IGNORECASE,
    )
    if suffix_m:
        biz_part = suffix_m.group(1).strip(" ,.-–—\"'«»")

    # 2. Приписка в НАЧАЛЕ: "инста Kamolon Osh", "insta Камолон Ош"
    if not biz_part:
        prefix_m = re.match(
            rf"^{INSTA_TRIGGER_WORDS}\s*[\s:,\-–—]+\s*(.+?)[.!?\s]*$",
            stripped,
            re.IGNORECASE,
        )
        if prefix_m:
            biz_part = prefix_m.group(1).strip(" ,.-–—\"'«»")

    if not biz_part:
        return None

    # Если пользователь написал "инста @kamolonosh" или "@kamolonosh insta" или "soulbakery_uz insta"
    # (то есть уже передал готовый точный @никнейм или ник с '_' / '.') — отдаём прямому парсеру
    tokens = biz_part.split()
    if len(tokens) == 1:
        tok = tokens[0]
        if tok.startswith("@") and validate_ig_username(tok) is not None:
            return None
        if ("_" in tok or "." in tok) and validate_ig_username(tok) is not None:
            return None

    return biz_part


def is_instagram_input(text: str, mode_hint: str = "google") -> bool:
    """
    Умное одновременное распознавание прямого Instagram-ника или ссылки:
    Возвращает True ТОЛЬКО если сообщение уже содержит готовый Instagram-handle:
    1) Содержит ссылку instagram.com / instagr.am
    2) Начинается с '@' (например '@phoenix_math_school')
    3) Одно слово без пробелов и без кириллицы, содержащее '_' или '.' (например 'phoenix_math_school', 'coffee.uz')
    4) Одно латинское слово без пробелов (например 'safiauz'), если пользователь перед этим нажал '📸 Instagram NFC'.
    """
    if not text:
        return False
    stripped = text.strip()
    low = stripped.lower()

    # Если это поисковый запрос бизнеса с припиской "Kamolon Osh insta" — его обрабатывает extract_insta_search_query
    if extract_insta_search_query(stripped) is not None:
        return False

    # 1. Прямая ссылка на Instagram
    if "instagram.com/" in low or "instagr.am/" in low:
        return True

    # 2. Никнейм с собачкой @username (включая случайные пробелы по краям или приписку insta)
    if stripped.startswith("@") and len(stripped) > 1:
        first_tok = stripped.split()[0]
        if validate_ig_username(first_tok) is not None:
            if len(stripped.split()) == 1:
                return True
            if re.match(rf"^@[A-Za-z0-9._]{{1,30}}\s+{INSTA_TRIGGER_WORDS}$", stripped, re.IGNORECASE):
                return True

    # 2b. Явный префикс/суффикс с готовым ником (@username или username с '_' / '.')
    if re.match(rf"^{INSTA_TRIGGER_WORDS}\s+(@[A-Za-z0-9._]{{1,30}}|[A-Za-z0-9]+[_A-Za-z0-9.]*[_.][A-Za-z0-9._]*)$", stripped, re.IGNORECASE):
        return True
    if re.match(rf"^(@[A-Za-z0-9._]{{1,30}}|[A-Za-z0-9]+[_A-Za-z0-9.]*[_.][A-Za-z0-9._]*)\s+{INSTA_TRIGGER_WORDS}$", stripped, re.IGNORECASE):
        return True

    # 3. Если в запросе есть пробелы, кириллица или другая http-ссылка — это не одиночный никнейм
    if " " in stripped or low.startswith("http") or re.search(r"[а-яА-ЯёЁ]", stripped):
        return False

    # 4. Одиночное латинское слово (валидный Instagram handle)
    if validate_ig_username(stripped) is not None:
        if "_" in stripped or "." in stripped:
            return True
        if mode_hint == "insta":
            return True

    return False


def build_instagram_card_data(
    raw_input: str,
    profile_title: Optional[str] = None,
    followers_count: Optional[int] = None,
    alternative_usernames: Optional[List[str]] = None,
) -> InstagramCardData:
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
        profile_title=profile_title,
        followers_count=followers_count,
        alternative_usernames=alternative_usernames or [],
    )


def _format_followers_badge(followers: Optional[int]) -> str:
    if not followers or followers <= 0:
        return ""
    if followers >= 1_000_000:
        return f"{followers / 1_000_000:.1f}M подписчиков"
    if followers >= 1_000:
        val = followers / 1_000
        s = f"{val:.1f}K" if val < 10 and val % 1 != 0 else f"{int(round(val))}K"
        return f"{s} подписчиков"
    return f"{followers} подписчиков"


def format_instagram_card(data: InstagramCardData, searched_query: Optional[str] = None) -> str:
    """Форматирует HTML-сообщение карточки Instagram NFC по стандарту HHtap."""
    u = html.escape(data.clean_username)
    primary = html.escape(data.primary_nfc_url)
    deep = html.escape(data.deep_link_url)
    web_link = f"https://www.instagram.com/{u}/"

    header_extra = ""
    if searched_query:
        header_extra += f"🔎 <b>Бизнес:</b> {html.escape(searched_query)}\n"
    if data.profile_title or data.followers_count:
        clean_title = (data.profile_title or "").split("(@")[0].split("•")[0].strip()
        flw_str = _format_followers_badge(data.followers_count)
        parts = [p for p in (html.escape(clean_title) if clean_title else "", flw_str) if p]
        if parts:
            header_extra += f"✅ <b>Профиль:</b> {' • '.join(parts)}\n"

    return (
        f"📸 <b>Instagram NFC: @{u}</b> — <a href=\"{web_link}\">Перейти на аккаунт ↗️</a>\n"
        f"{header_extra}\n"
        f"🔗 <b>Основная ссылка для NFC Tools (нажми, чтобы скопировать):</b>\n"
        f"<code>{primary}</code>\n\n"
        f"⚡️ <b>Прямой Deep Link (альтернатива):</b>\n"
        f"<code>{deep}</code>"
    )


# ==============================================================================
# МГНОВЕННЫЙ 4-УРОВНЕВЫЙ ПОИСК INSTAGRAM БИЗНЕСА ПО НАЗВАНИЮ
# ==============================================================================

def _cyr_to_latin(text: str, y_as_i: bool = False, kh_mode: bool = False, k_as_c: bool = False) -> str:
    """Транслитерирует название бизнеса из кириллицы в латиницу во всех возможных вариантах написания."""
    low = text.lower()
    replacements = [
        ("жигули", "jiguli"),
        ("джиотто", "giotto"),
        ("джотто", "giotto"),
        ("кукучина", "cucucina"),
        ("козон", "qozon"),
        ("казон", "qozon"),
        ("камолон", "kamolon"),
        ("афсона", "afsona"),
        ("сафия", "safia"),
        ("брэдли", "breadly"),
        ("бредли", "breadly"),
        ("бенедикт", "benedict"),
        ("япона мама", "yaponamama"),
        ("японамама", "yaponamama"),
        ("сыроварня", "sirovarnya" if y_as_i else "syrovarnya"),
        ("хинкали", "khinkali" if kh_mode else "xinkali"),
        ("караван", "caravan" if k_as_c else "karavan"),
        ("пекарня", "bakery"),
        ("кофейня", "coffee"),
        ("ресторан", "restaurant"),
        ("пиццерия", "pizza"),
        ("пицца", "pizza"),
    ]
    for k, v in replacements:
        low = low.replace(k, v)
    out = []
    for ch in low:
        if y_as_i and ch == "ы":
            out.append("i")
        elif kh_mode and ch == "х":
            out.append("kh")
        elif k_as_c and ch == "к":
            out.append("c")
        else:
            out.append(CYR_TO_LAT.get(ch, ch))
    return "".join(out)


def _parse_followers(desc: str) -> int:
    if not desc:
        return 0
    m = re.search(r"([\d.,]+)\s*([KMBkmb]|тыс|млн)?\s*Followers", desc, re.IGNORECASE)
    if not m:
        return 0
    val_str = m.group(1).replace(",", "")
    try:
        val = float(val_str)
    except ValueError:
        return 0
    mult = (m.group(2) or "").upper()
    if mult in ("K", "ТЫС"):
        val *= 1_000
    elif mult in ("M", "МЛН"):
        val *= 1_000_000
    return int(val)


GENERIC_PLACE_WORDS = {
    "tashkent", "toshkent", "tashkente", "uzbekistan", "parkent", "shevchenko",
    "mirabad", "city", "street", "avenue", "restaurant", "restoran", "cafe",
    "kafe", "coffee", "bakery", "bar", "pub", "lounge", "pizza", "pizzeria",
    "oshxona", "choyxona", "markazi", "filial", "v", "na", "ulitsa", "co",
    "novomoskovskaya", "ekopark",
}


def _extract_brand_words(raw_text: str, y_as_i: bool = False, kh_mode: bool = False, k_as_c: bool = False) -> List[str]:
    lat = _cyr_to_latin(raw_text, y_as_i=y_as_i, kh_mode=kh_mode, k_as_c=k_as_c)
    all_words = re.findall(r"[a-z0-9]{2,}", lat)
    filtered = [w for w in all_words if w not in GENERIC_PLACE_WORDS]
    return filtered if filtered else all_words


def _build_candidate_handles(
    raw_query: str,
    places: list,
    website_igs: List[str],
    search_engine_igs: List[str],
) -> List[str]:
    cands: List[str] = []

    def add(h: str):
        v = validate_ig_username(h.lower().rstrip("."))
        if v and len(v) >= 2 and v not in cands:
            cands.append(v)

    # 1. Прямые ссылки на Instagram с Google Maps, с официального сайта точки и из поисковиков
    for p in (places or [])[:3]:
        w = (p.website or "").strip()
        if not w:
            continue
        m_ig = IG_URL_REGEX.search(w)
        if m_ig:
            add(m_ig.group(1))
        m_tap = re.search(r"taplink\.cc/([A-Za-z0-9._]{2,30})", w, re.IGNORECASE)
        if m_tap:
            tap_u = m_tap.group(1)
            add(tap_u)
            add(tap_u.replace("info", ""))

    for u in website_igs:
        add(u)

    for u in search_engine_igs:
        add(u)

    # 2. Домен и путь официального сайта из Google Maps (например benedict-cafe.uz, lali.rest, /prohinkalytashkent/)
    for p in (places or [])[:3]:
        w = (p.website or "").strip()
        if not w:
            continue
        m_dom = re.search(r"https?://(?:www\.)?([a-z0-9\-]+)\.(uz|com|ru|rest|cafe|bar|org)(?:/([a-z0-9._\-]{3,30}))?", w, re.IGNORECASE)
        if m_dom:
            raw_host = m_dom.group(1).lower()
            tld = m_dom.group(2).lower()
            path_seg = (m_dom.group(3) or "").lower().strip("/")
            stem = raw_host.replace("-", "")
            if stem not in ("taplink", "instagram", "facebook", "google", "abnmbgroup", "familygarden", "t", "bit", "linktr"):
                add(f"{stem}.{tld}")
                add(stem)
                add(f"{stem}_uz")
                add(f"{stem}.uz")
                add(f"{stem}uz")
                add(f"{stem}_tashkent")
                add(f"{stem}.tashkent")
                if "-" in raw_host:
                    under_host = raw_host.replace("-", "_")
                    dot_host = raw_host.replace("-", ".")
                    add(under_host)
                    add(f"{under_host}_tashkent")
                    add(f"{under_host}_uz")
                    add(dot_host)
            if path_seg and path_seg not in ("ru", "uz", "en", "home", "index", "menu"):
                add(path_seg.replace("-", "_"))
                add(path_seg.replace("-", ""))

    # 3. Генерация комбинаций из запроса пользователя (во всех вариантах транслитерации) и названия в Google Maps
    word_sets = [
        _extract_brand_words(raw_query, y_as_i=False, kh_mode=True, k_as_c=False),
        _extract_brand_words(raw_query, y_as_i=False, kh_mode=False, k_as_c=False),
        _extract_brand_words(raw_query, y_as_i=True, kh_mode=True, k_as_c=True),
    ]
    if places:
        word_sets.append(_extract_brand_words(places[0].name, y_as_i=False, kh_mode=True, k_as_c=False))
        word_sets.append(_extract_brand_words(places[0].name, y_as_i=True, kh_mode=False, k_as_c=True))

    # Сначала добавляем самые высокоприоритетные паттерны Ташкента для всех вариантов слов
    for words in word_sets:
        if not words:
            continue
        w1 = words[0]
        joined2 = "".join(words[:2])
        under2 = "_".join(words[:2])
        dot2 = ".".join(words[:2])

        # Если 3 слова (например 'Testo by Zamira' или 'Black Bear Kofi')
        if len(words) >= 3:
            add("_".join(words[:3]))
            add("".join(words[:3]))
            add(".".join(words[:3]))

        # Если 2 и более слов — сначала проверяем двусловные комбинации (joined2, under2, dot2), а затем w1
        bases_order = (joined2, under2, dot2, w1) if len(words) >= 2 else (w1,)
        for base in bases_order:
            add(base)
            add(f"{base}_uz")
            add(f"{base}.uz")
            add(f"{base}uz")
            add(f"{base}.uzb")
            add(f"{base}_tashkent")
            add(f"{base}.tashkent")
            add(f"{base}tashkent")
            add(f"{base}_restaurant")
            add(f"{base}.restaurant")
            add(f"{base}_rest")

        add(f"{w1}_uzbekistan")
        add(f"{w1}cafe.uz")
        add(f"{w1}_cafe")
        add(f"{w1}_cafe_tashkent")
        add(f"{w1}_boulangerie")
        add(f"{w1}.bar")
        add(f"{w1}_bar")
        add(f"{w1}bakery_uz")
        add(f"{w1}pizzauz")

    return cands[:36]


async def _extract_ig_from_external_website(url: str) -> List[str]:
    """Быстро проверяет официальный сайт заведения (до 1.8 сек) на наличие прямой ссылки на его Instagram."""
    if not url or "instagram.com" in url.lower() or "instagr.am" in url.lower():
        return []
    found: List[str] = []

    async def _fetch():
        async with httpx.AsyncClient(follow_redirects=True, timeout=1.6, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                for m in re.finditer(r"instagram\.com/(?:_u/)?([A-Za-z0-9._]{2,30})", r.text, re.IGNORECASE):
                    v = validate_ig_username(m.group(1).lower().rstrip("."))
                    if v and v not in found:
                        found.append(v)

    try:
        await asyncio.wait_for(_fetch(), timeout=1.8)
    except Exception:
        pass
    return found


async def _search_web_instagram_handles(biz_query: str) -> List[str]:
    """
    Параллельный поиск Instagram-аккаунта бизнеса через поисковые зеркала Yahoo (до 1.8 сек).
    Находит нестандартные никнеймы (например @breadly_boulangerie, @benedict_nukus_tashkent, @testo_by_zamira).
    """
    uq = urllib.parse.quote(f"{biz_query} Ташкент instagram")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    mirrors = [
        f"https://search.yahoo.com/search?p={uq}",
        f"https://uk.search.yahoo.com/search?p={uq}",
    ]
    found: List[str] = []

    async def _query_mirror(url: str) -> List[str]:
        loc: List[str] = []
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=1.6) as c:
                r = await c.get(url)
                if r.status_code == 200:
                    txt = urllib.parse.unquote(r.text)
                    for m in re.finditer(r"instagram\.com/(?:_u/)?([A-Za-z0-9._]{3,30})", txt, re.IGNORECASE):
                        v = validate_ig_username(m.group(1).lower().rstrip("."))
                        if v and v not in loc:
                            loc.append(v)
        except Exception:
            pass
        return loc

    try:
        res_lists = await asyncio.wait_for(
            asyncio.gather(*[_query_mirror(u) for u in mirrors]),
            timeout=1.9,
        )
        for lst in res_lists:
            for item in lst:
                if item not in found:
                    found.append(item)
    except Exception:
        pass
    return found[:6]


async def resolve_business_instagram(biz_query: str) -> List[Dict[str, Any]]:
    """
    Мгновенно находит и верифицирует реальный Instagram-аккаунт любого бизнеса по его названию
    (на латинице или кириллице, например 'Kamolon Osh', 'Камолон Ош', 'Soul Bakery Паркент', 'Breadly', 'Про Хинкали').
    Возвращает отсортированный по релевантности список словарей:
      [{"username": "kamolonosh", "title": "...", "followers": 224000, "score": ...}, ...]
    """
    from services.keyless_places import keyless_places_service

    clean_q = biz_query.strip()
    if not clean_q:
        return []

    # 1. Параллельно запускаем HTTP-поиск в Google Картах и веб-поиск нестандартных никнеймов
    async def _safe_gmaps():
        try:
            return await asyncio.wait_for(
                keyless_places_service.search_text(clean_q, max_results=3),
                timeout=3.5,
            )
        except Exception as e:
            logger.warning("Ошибка поиска Google Maps внутри resolve_business_instagram('%s'): %s", clean_q, e)
            return []

    places, search_engine_igs = await asyncio.gather(
        _safe_gmaps(),
        _search_web_instagram_handles(clean_q),
    )

    direct_gmaps_ig: Optional[str] = None
    taplink_handles: set[str] = set()
    external_web_urls: List[str] = []

    for p in (places or [])[:3]:
        w = (p.website or "").strip()
        if not w:
            continue
        if "instagram.com" in w.lower() or "instagr.am" in w.lower():
            m = IG_URL_REGEX.search(w)
            if m and validate_ig_username(m.group(1)):
                direct_gmaps_ig = validate_ig_username(m.group(1)).lower()
                break
        elif "taplink.cc/" in w.lower():
            m_t = re.search(r"taplink\.cc/([A-Za-z0-9._]{2,30})", w, re.IGNORECASE)
            if m_t and validate_ig_username(m_t.group(1)):
                taplink_handles.add(validate_ig_username(m_t.group(1)).lower())
            external_web_urls.append(w)
        else:
            external_web_urls.append(w)

    # 2. Если на карточке указан внешний сайт (.uz / taplink), быстро проверяем его HTML на ссылку instagram.com
    website_igs: List[str] = []
    if not direct_gmaps_ig and external_web_urls:
        unique_urls: List[str] = []
        seen_hosts: set[str] = set()
        for u in external_web_urls:
            host_m = re.search(r"https?://(?:www\.)?([^/]+)", u.lower())
            host = host_m.group(1) if host_m else u.lower()
            if host not in seen_hosts:
                seen_hosts.add(host)
                unique_urls.append(u)
        lists = await asyncio.gather(*[_extract_ig_from_external_website(u) for u in unique_urls[:2]])
        for lst in lists:
            for u in lst:
                if u not in website_igs:
                    website_igs.append(u)

    # 3. Генерируем список кандидатов и параллельно проверяем их через живые метаданные Instagram
    candidates = _build_candidate_handles(clean_q, places, website_igs, search_engine_igs)
    if not candidates:
        return []

    brand_words = list(
        dict.fromkeys(
            _extract_brand_words(clean_q, y_as_i=False, kh_mode=True, k_as_c=False)
            + _extract_brand_words(clean_q, y_as_i=False, kh_mode=False, k_as_c=False)
            + _extract_brand_words(clean_q, y_as_i=True, kh_mode=True, k_as_c=True)
        )
    )
    raw_query_words = [
        w for w in re.findall(r"[a-z0-9]{3,}", _cyr_to_latin(clean_q, y_as_i=False, kh_mode=True))
        if w not in ("tashkent", "toshkent", "uzbekistan", "parkent", "shevchenko")
    ]

    tg_headers = {"User-Agent": "TelegramBot (like TwitterBot)"}
    async with httpx.AsyncClient(headers=tg_headers, follow_redirects=True, timeout=4.0) as client:
        async def check_one(u: str) -> Optional[Dict[str, Any]]:
            try:
                r = await client.get(f"https://www.instagram.com/{u}/")
                if r.status_code != 200:
                    return None
                og_m = re.search(r'property="og:title"\s+content="([^"]+)"', r.text)
                desc_m = re.search(r'property="og:description"\s+content="([^"]+)"', r.text)
                if not og_m:
                    return None
                title = html.unescape(og_m.group(1)).strip()
                desc = html.unescape(desc_m.group(1)).strip() if desc_m else ""
                followers = _parse_followers(desc)
                return {
                    "username": u,
                    "title": title,
                    "desc": desc,
                    "followers": followers,
                }
            except Exception:
                return None

        results = await asyncio.gather(*[check_one(u) for u in candidates])

    valid = [r for r in results if r is not None]
    if not valid:
        return []

    uz_markers = ("uz", "tashkent", "toshkent", "ташкент", "узбекистан", "osh", "qozon", "birlashtiradi")
    biz_markers = (
        "rest", "cafe", "bar", "bakery", "boulangerie", "pizza", "lounge", "coffee", "kofi", "pub", "bistro",
        "burger", "sushi", "wok", "rolls", "khinkali", "hinkali", "xinkali", "clinic", "dental", "med",
        "beauty", "salon", "barber", "school", "edu", "shop", "store", "gullari", "flowers",
        "ресторан", "кафе", "кофейня", "пекарня", "бар", "пицц", "суши", "хинкал", "клиника", "салон", "школа", "магазин", "букет", "цвет",
    )

    def score_profile(prof: Dict[str, Any]) -> float:
        u = prof["username"].lower()
        raw_title_low = prof["title"].lower()
        title_lat_y = _cyr_to_latin(raw_title_low, y_as_i=False, kh_mode=True)
        title_lat_i = _cyr_to_latin(raw_title_low, y_as_i=True, kh_mode=False)
        u_clean = re.sub(r"[._]", "", u)
        sc = 0.0

        # 1. Если этот аккаунт напрямую прописан в поле Сайт на Google Картах
        if direct_gmaps_ig and u == direct_gmaps_ig:
            sc += 600.0

        if u in taplink_handles:
            sc += 220.0

        # 2. Совпадение ключевых слов бренда в заголовке профиля и в никнейме
        matched_brand = False
        for bw in brand_words:
            if len(bw) >= 3:
                if bw in title_lat_y or bw in title_lat_i or bw in raw_title_low:
                    sc += 95.0
                    matched_brand = True
                if bw in u_clean:
                    sc += 80.0
                    matched_brand = True

        # Дополнительный бонус за совпадение второго слова из запроса (например 'bar' в 'Жигули Бар' -> @jiguli.bar)
        for qw in raw_query_words:
            if qw not in brand_words and len(qw) >= 3:
                if qw in u_clean:
                    sc += 85.0
                if qw in title_lat_y or qw in raw_title_low:
                    sc += 45.0

        # 3. Если ссылка найдена на внешнем сайте точки или через веб-поиск по Ташкенту
        if u in website_igs:
            if matched_brand or prof["followers"] >= 1000:
                sc += 240.0
            else:
                sc += 40.0

        if u in search_engine_igs:
            if matched_brand:
                rank_idx = search_engine_igs.index(u)
                sc += max(90.0, 185.0 - rank_idx * 25.0)

        # 4. Признаки Ташкента / Узбекистана и коммерческого бизнеса
        has_biz = any(m in u or m in raw_title_low or m in title_lat_y for m in biz_markers)
        has_cyrillic_title = bool(re.search(r"[а-яА-ЯёЁўқғҳ]", raw_title_low))
        strong_uz_markers = ("_uz", ".uz", "uzb", "tashkent", "toshkent", "ташкент", "узбекистан", "osh", "qozon", "birlashtiradi")
        has_uz = any(m in u or m in raw_title_low or m in title_lat_y for m in strong_uz_markers)
        if not has_uz and u.endswith("uz"):
            # Защита от случайных иностранных личных анкет вида @benedictuz (188 подп., без бизнеса и без кириллицы)
            if prof["followers"] >= 500 or has_biz or has_cyrillic_title:
                has_uz = True

        if has_uz:
            sc += 130.0
        if has_biz:
            sc += 85.0
        if has_cyrillic_title:
            sc += 35.0

        # Штраф для чужих городов и стран в никнейме/заголовке (например dushanbe, almaty, moscow, istanbul/İstanbul)
        foreign_markers = ("dushanbe", "almaty", "astana", "moscow", "spb", "stanbul", "baku", "bishkek", "kazan")
        if any(fm in u or fm in raw_title_low for fm in foreign_markers) or re.search(r"[\u4e00-\u9fff]", raw_title_low):
            sc -= 320.0

        # Штраф для голых однословных глобальных аккаунтов людей/знаменитостей без признаков бизнеса/Узбекистана/кириллицы
        if ("_" not in u and "." not in u) and not has_uz and not has_biz and not has_cyrillic_title:
            sc -= 240.0

        # 5. Бонус за живую аудиторию подписчиков
        f = prof["followers"]
        if f >= 50_000:
            sc += 210.0
        elif f >= 10_000:
            sc += 140.0
        elif f >= 3_000:
            sc += 95.0
        elif f >= 500:
            sc += 55.0
        elif f < 50:
            sc -= 90.0

        prof["score"] = sc
        return sc

    valid.sort(key=score_profile, reverse=True)
    return valid
