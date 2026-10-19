"""
HHtap AI Sales — быстрый генератор высококонверсионных холодных сообщений (Instagram Direct / Telegram / Voice).

Архитектура:
1. Первая строка до 40 символов бьёт точно в превью пуш-уведомления и папки «Запросы».
2. Короткая длина (18–36 слов для Директа) и уровень чтения 5-го класса (предложения по 6–10 слов, без двоеточий).
3. Продажа РЕЗУЛЬТАТА (Outcome) или ДВУХШАГОВОГО ВОПРОСА (Poke the Bear) вместо описания пластика.
4. Мгновенная детерминированная зачистка текста на Python за 0.1 мс (никаких повторных запросов к API, которые вешают бота на 2–3 минуты).
"""
import logging
import random
import re
from typing import Optional, Dict, Any, List

import httpx

from config import config

logger = logging.getLogger(__name__)

TEMPERATURE = 0.7
MAX_TOKENS = 350

# ==============================================================================
# Компактный и быстрый системный промпт
# ==============================================================================
SYSTEM_PROMPT = """Ты пишешь холодные B2B-сообщения от имени основателя проекта HHtap в Ташкенте владельцам и управляющим заведений.

ЦЕЛЬ ПЕРВОГО СООБЩЕНИЯ — получить быстрый ответ в чате (начать диалог или получить согласие скинуть 8-секундное видео), а не рассказать устройство технологии.

ЖЕСТКИЕ ПРАВИЛА:
1. Деловой уважительный тон на «вы». Приветствие только «Добрый день.» или «Здравствуйте.». Без сленга («Салам», «Привет», «ребята», «вайб», «пушка» запрещены).
2. Короткие предложения по 6–10 слов. Без двоеточий «:» и без кавычек «...».
3. Не называй само заведение по имени — пиши «у вас», «ваша точка», «ваш зал».
4. Пиши «Гугл», «Гугл Карты», «в Гугле» кириллицей. Улицу пиши только кириллицей.
5. Не пиши фразу «Мы делаем подставки к счёту». Пиши через измеримый результат для владельца (получать по 8–12 свежих пятёрок за вечер прямо при оплате счёта, за две недели перекрыть старые оценки, обойти соседей по улице в поиске Гугла без QR-кодов и просьб персонала).

ЭТАЛОННЫЕ ПРИМЕРЫ:

[Двухшаговый вопрос Poke the Bear — 22 слова]
Добрый день. Вопрос по вашей точке на Султонали Машхадий.

Подскажите, вы сейчас как-то собираете отзывы гостей в Гугл Картах, или они приходят только сами по себе?

[Результат + интрига на видео — 32 слова]
Здравствуйте. Заметил деталь по вашей точке на Мирабаде в Гугл Картах.

Посадка вечером у вас плотная, а новых оценок почти нет. Мы помогаем барам получать по 10–15 пятёрок за вечер прямо при оплате счёта, без QR-кодов.

Прислать короткое видео на 8 секунд, как это устроено?

[Перекрытие старых оценок — 31 слово]
Добрый день. Смотрел вашу карточку в Гугл Картах — сейчас там 4.2 из-за пары старых оценок, хотя кухню хвалят.

Есть способ за две недели перекрыть их свежими пятёрками от довольных гостей прямо при расчёте. Без QR и просьб официантов.

Показать на коротком видео, как это работает?

[Обход соседей в поиске — 30 слов]
Здравствуйте. Вопрос по вашей точке на Шахрисабзской.

Вы стоите в самом потоке, но в поиске Гугла выше выходят соседи, у которых просто больше свежих отзывов. Мы помогаем собирать по 10 новых пятёрок в день прямо у кассы в одно касание.

Интересно посмотреть на 8-секундном видео?

Выдавай ТОЛЬКО чистый текст сообщения без пояснений."""


# ==============================================================================
# Справочники ниш, хуков превью и архетипов
# ==============================================================================
NICHES: Dict[str, Dict[str, Any]] = {
    "bar": {
        "keywords": ["бар", "bar", "паб", "pub", "пив", "beer", "жигули", "lounge", "лаунж", "кальян", "hookah", "бургер", "burger", "craft", "wine", "вино"],
        "label": "бар / паб / лаунж",
        "context_hint": "вечерняя посадка, плотный поток на выходных, гости отдыхают и при оплате счёта не ищут карточку в Гугле вручную",
        "outcome_hint": "получать по 10–15 новых пятёрок за вечер прямо при подаче счёта, без QR-кодов и просьб барменов",
    },
    "pizza": {
        "keywords": ["pizza", "пицц", "napoli", "неапол", "italian", "итальян", "pasta", "паст"],
        "label": "пиццерия / итальянская кухня",
        "context_hint": "обеденный и вечерний поток, много довольных гостей, которые уходят без оценки в Гугле",
        "outcome_hint": "собирать по 8–12 свежих пятёрок каждый день прямо при расчёте за столиком, без QR-наклеек",
    },
    "coffee": {
        "keywords": ["coffee", "кофе", "кофей", "cafe", "café", "espresso", "brew", "bakery", "пекарн", "десерт", "dessert"],
        "label": "кофейня / пекарня",
        "context_hint": "большой ежедневный поток у кассы, но в Гугл Картах оставляют отзыв единицы",
        "outcome_hint": "за пару недель выйти выше соседей по улице в Гугл Картах за счёт ежедневного потока пятёрок у кассы",
    },
    "beauty": {
        "keywords": ["барбер", "barber", "салон", "salon", "beauty", "бьюти", "nail", "ногт", "spa", "спа", "массаж", "brow", "lash", "космет", "hair"],
        "label": "салон красоты / барбершоп / спа",
        "context_hint": "клиент благодарит мастера на словах и уходит, а новые клиенты выбирают салон по числу свежих отзывов в Гугле",
        "outcome_hint": "собирать пятёрки от каждого второго клиента на ресепшне за 1 секунду при оплате, без просьб администратора",
    },
    "clinic": {
        "keywords": ["клиник", "clinic", "стомат", "dental", "dent", "med", "мед", "doctor", "врач", "лаборат"],
        "label": "клиника / стоматология",
        "context_hint": "пациенты выбирают клинику по рейтингу и свежим отзывам в Гугле, но довольные пациенты редко пишут сами",
        "outcome_hint": "стабильно получать свежие пятёрки в Гугл Картах на ресепшне за 1 секунду после приёма",
    },
    "restaurant": {
        "keywords": [],
        "label": "ресторан / кафе",
        "context_hint": "полная посадка, гости хвалят кухню официантам, но уходят, не оставив отзыв в Гугл Картах",
        "outcome_hint": "получать по 8–12 новых пятёрок в день прямо при подаче счёта, без QR-кодов и неловких просьб официантов",
    },
}

PREVIEW_HOOKS_WITH_STREET = [
    "Добрый день. Вопрос по вашей точке на {street}.",
    "Здравствуйте. Заметил деталь по вашей точке на {street}.",
    "Добрый день. Смотрел вашу карточку на {street} в Гугле.",
    "Здравствуйте. Короткий вопрос по вашему заведению на {street}.",
]

PREVIEW_HOOKS_NO_STREET = [
    "Добрый день. Вопрос по вашей карточке в Гугл Картах.",
    "Здравствуйте. Заметил деталь по вашей точке в Гугле.",
    "Добрый день. Короткий вопрос по вашим отзывам в Гугле.",
]

ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "poke_the_bear": {
        "word_limit": "18–26 слов (ровно 2 коротких абзаца)",
        "instruction": (
            "ДВУХШАГОВЫЙ ВОПРОС (Poke the Bear). Вообще НЕ рассказывай про наш продукт и NFC! "
            "После первой строки-хука задай один простой вопрос — как они сейчас собирают отзывы гостей в Гугл Картах "
            "(просят персонал, стоят QR-коды или отзывы приходят только сами по себе). "
            "Цель — чтобы человек ответил одним предложением."
        ),
    },
    "outcome_gap": {
        "word_limit": "26–36 слов (2–3 коротких абзаца)",
        "instruction": (
            "КОНТРАСТ И РЕЗУЛЬТАТ. Покажи разрыв между живым потоком гостей в зале и малым числом свежих оценок в Гугл Картах. "
            "Не описывай пластик или чип — напиши только результат, что мы помогаем заведениям в Ташкенте {outcome_hint}. "
            "Заверши вопросом про 8-секундное видео."
        ),
    },
    "reputation_shield": {
        "word_limit": "26–36 слов (2–3 коротких абзаца)",
        "instruction": (
            "ПЕРЕКРЫТИЕ СТАРЫХ ОЦЕНОК. Напомни факт — недовольный гость пишет отзыв сам, а сотни довольных просто уходят домой. "
            "Покажи результат — есть способ за две недели перекрыть старые оценки потоком свежих пятёрок прямо при подаче счёта, без QR-кодов. "
            "Заверши коротким вопросом про 8-секундное видео или бесплатный тест на пару дней."
        ),
    },
    "search_top": {
        "word_limit": "26–36 слов (2–3 коротких абзаца)",
        "instruction": (
            "ОБХОД СОСЕДЕЙ В ВЫДАЧЕ ГУГЛА. В поиске Гугл Карт выше стоят те места, у которых каждую неделю идут свежие отзывы. "
            "Покажи результат — как {outcome_hint} и забирать гостей из поиска. "
            "В конце спроси, показать ли на коротком видео (8 секунд), как это работает в зале."
        ),
    },
    "qr_replacement": {
        "word_limit": "26–36 слов (2–3 коротких абзаца)",
        "instruction": (
            "ЗАМЕНА МЁРТВЫХ QR-КОДОВ. Персоналу неловко выпрашивать отзывы, а бумажные QR-коды на столах гости игнорируют. "
            "Покажи результат — решение в одно касание к счёту даёт в разы больше пятёрок в Гугле за смену без участия персонала. "
            "В конце предложи прислать 8-секундное видео или завезти образец на бесплатный двухдневный тест."
        ),
    },
}

CTAS: Dict[str, List[str]] = {
    "video": [
        "Прислать короткое видео на 8 секунд, как это устроено?",
        "Показать на коротком видео, как это работает в зале?",
        "Прислать 8-секундное видео, как это выглядит у столика?",
    ],
    "test": [
        "Могу привезти один образец на пару дней бесплатно — вам удобно?",
        "Оставить вам одну на бесплатный тест на пару дней?",
    ],
    "curiosity": [
        "Актуально для вас сейчас?",
        "Интересно посмотреть, как это устроено?",
    ],
}


class AISalesError(Exception):
    """Исключение при генерации сообщения ИИ."""
    pass


# ==============================================================================
# Анализ контекста и транслитерация адресов
# ==============================================================================
_CITY_TOKENS = {"ташкент", "tashkent", "toshkent", "узбекистан", "uzbekistan", "o'zbekiston", "oʻzbekiston"}

_UZ_DIGRAPHS = [
    ("o'", "о"), ("g'", "г"), ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"),
    ("ya", "я"), ("ye", "е"), ("ts", "ц"), ("ng", "нг"),
]
_UZ_LETTERS = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к",
    "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
    "c": "к", "w": "в",
}


def uz_latin_to_cyrillic(text: str) -> str:
    """Sultonali Mashxadiy -> Султонали Машхадий. Кириллица остаётся как есть."""
    if not re.search(r"[A-Za-z]", text):
        return text
    words = []
    for word in re.sub(r"[‘’ʻʼ`]", "'", text).split():
        if word.upper() in ("NFC", "QR", "VIP", "SMS", "IT", "PR", "SMM", "PDF"):
            words.append(word.upper())
            continue
        low = word.lower()
        out, i = "", 0
        while i < len(low):
            for lat, cyr in _UZ_DIGRAPHS:
                if low.startswith(lat, i):
                    out += cyr
                    i += len(lat)
                    break
            else:
                ch = low[i]
                if ch == "e" and i == 0:
                    out += "э"
                elif ch == "y" and i + 1 < len(low) and low[i + 1] in "aeiou":
                    out += "й"
                else:
                    out += _UZ_LETTERS.get(ch, "" if ch == "'" else ch)
                i += 1
        words.append(out[:1].upper() + out[1:] if word[:1].isupper() else out)
    return " ".join(words)


def clean_address(raw_address: Optional[str]) -> Optional[str]:
    """Возвращает человеческое название улицы/района на кириллице или None, если адрес — просто город."""
    if not raw_address:
        return None
    parts = [p.strip() for p in raw_address.split(",") if p.strip()]
    for part in parts:
        low = part.lower()
        if low in _CITY_TOKENS or re.fullmatch(r"\d{5,6}", part):
            continue
        street = re.sub(r"^(ул\.?|улица|проспект|пр-т|пр\.)\s*", "", part, flags=re.IGNORECASE)
        street = re.sub(r"\s+(ko[‘'ʻ`’]?chasi|shoh\s+ko[‘'ʻ`’]?chasi|kochasi|улица|ул\.?|mavzesi|massivi)\b.*$", "", street, flags=re.IGNORECASE)
        street = re.sub(r"[\s,]*\d+[a-zA-Zа-яА-Я/]*$", "", street).strip()
        if street and not street.isdigit() and street.lower() not in _CITY_TOKENS:
            return uz_latin_to_cyrillic(street)
    return None


def detect_niche(name: str) -> str:
    low = name.lower()
    for key, data in NICHES.items():
        if any(k in low for k in data["keywords"]):
            return key
    return "restaurant"


def analyze_venue_context(
    name: str,
    address: str,
    rating: Optional[float] = None,
    reviews_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Превращает сырые данные парсера в конверсионный бриф без утечки технических значений."""
    clean_name = (name or "").strip().strip('"\'«»') or "заведение"
    street = clean_address(address)
    loc = f"на {street}" if street else "в Ташкенте"
    niche = detect_niche(clean_name)

    if street:
        preview_hook = random.choice(PREVIEW_HOOKS_WITH_STREET).format(street=street)
    else:
        preview_hook = random.choice(PREVIEW_HOOKS_NO_STREET)

    has_data = rating is not None and reviews_count
    if not has_data:
        angle = "no_data"
        facts = "Точных цифр по их карточке в Гугл Картах нет — НЕ называй рейтинг и число отзывов и не пиши, что карточка пустая."
        problem = "Поток гостей в зале высокий, но 95% довольных гостей уходят без оценки в Гугле."
        allowed_archetypes = ["poke_the_bear", "outcome_gap", "qr_replacement", "reputation_shield"]
    elif rating < 4.5:
        angle = "low_rating"
        facts = f"В Гугл Картах сейчас рейтинг {rating:.1f} при {reviews_count} отзывах. Можно упомянуть оценку {rating:.1f}."
        problem = "Оценка в Гугл Картах занижена из-за старых отзывов, хотя реальный уровень заведения выше."
        allowed_archetypes = ["reputation_shield", "poke_the_bear", "outcome_gap"]
    elif reviews_count < 80:
        angle = "few_reviews"
        facts = f"Рейтинг хороший ({rating:.1f}), но отзывов пока всего {reviews_count}. Можно назвать эти цифры."
        problem = "Оценка хорошая, но отзывов мало относительно потока — соседи с большим числом отзывов стоят выше в поиске."
        allowed_archetypes = ["search_top", "poke_the_bear", "outcome_gap", "qr_replacement"]
    else:
        angle = "leader"
        facts = f"Сильная карточка — рейтинг {rating:.1f} и {reviews_count} отзывов."
        problem = "Поддерживать постоянный прирост свежих пятёрок без уговоров со стороны официантов и без QR-наклеек."
        allowed_archetypes = ["qr_replacement", "poke_the_bear", "outcome_gap"]

    if not street and "search_top" in allowed_archetypes:
        allowed_archetypes.remove("search_top")

    return {
        "clean_name": clean_name,
        "street": street,
        "loc": loc,
        "niche": niche,
        "angle": angle,
        "facts": facts,
        "problem": problem,
        "preview_hook": preview_hook,
        "allowed_archetypes": allowed_archetypes,
    }


# ==============================================================================
# Сервис
# ==============================================================================
class AISalesService:
    """Быстрая генерация B2B-сообщений через OpenAI-совместимый API Anymodel.org."""

    def __init__(self) -> None:
        self._archetype_decks: Dict[str, List[str]] = {}

    def _pick_archetype(self, name: str, allowed: List[str]) -> str:
        deck = self._archetype_decks.get(name)
        if not deck:
            deck = list(allowed)
            random.shuffle(deck)
            self._archetype_decks[name] = deck
        return deck.pop()

    def build_user_prompt(
        self,
        action: str,
        name: str,
        address: str,
        rating: Optional[float] = None,
        reviews_count: Optional[int] = None,
        objection_type: Optional[str] = None,
    ) -> str:
        ctx = analyze_venue_context(name, address, rating, reviews_count)
        niche = NICHES[ctx["niche"]]

        brief = (
            f"НИША: {niche['label']}. Локация: {ctx['loc']}.\n"
            f"КОНТЕКСТ НИШИ: {niche['context_hint']}.\n"
            f"ФАКТЫ ПО КАРТАМ: {ctx['facts']}\n"
        )

        if action == "insta":
            archetype_key = self._pick_archetype(ctx["clean_name"], ctx["allowed_archetypes"])
            arch = ARCHETYPES[archetype_key]
            arch_instruction = arch["instruction"].format(outcome_hint=niche["outcome_hint"])
            cta_line = random.choice(CTAS[random.choice(["video", "test", "curiosity"])])

            extra_cta = (
                "Вопрос уже встроен в сам архетип — дополнительный вопрос не нужен."
                if archetype_key == "poke_the_bear"
                else f"В конце задай один короткий вопрос по смыслу — «{cta_line}»."
            )

            return brief + (
                f"\nКАНАЛ: Instagram Direct (первое касание).\n"
                f"ОБЯЗАТЕЛЬНАЯ ПЕРВАЯ СТРОКА: {ctx['preview_hook']}\n"
                f"ЗАДАЧА: {arch_instruction}\n"
                f"ДЛИНА: строго {arch['word_limit']}. Короткие предложения по 6–10 слов, без двоеточий и без названия заведения.\n"
                f"ФИНАЛ: {extra_cta}"
            )

        if action == "tg":
            return brief + (
                f"\nКАНАЛ: Telegram (сообщение отправляется СРАЗУ ПОД нашим 8-секундным видео).\n"
                f"- Первая строка: {ctx['preview_hook']}\n"
                f"- Второй абзац: коротко свяжи видео выше с результатом для их точки ({niche['outcome_hint']}). Упомяни, что прямую ссылку под их точку мы уже проверили.\n"
                f"- Финал: предложи бесплатно завезти один тестовый образец на 2 дня в зал — вопрос на 5–7 слов.\n"
                f"- Длина: строго 32–45 слов. Без двоеточий и без названия заведения."
            )

        if action == "voice":
            return brief + (
                f"\nКАНАЛ: Текст для голосового сообщения / видео-кружка на 12–15 секунд речи.\n"
                f"- Длина: строго 35–48 слов. Короткие устные фразы с точками, без двоеточий.\n"
                f"- Деловой уважительный тон на «вы», без сленга.\n"
                f"- Все числа пиши словами («четыре и два», «десять пятёрок»).\n"
                f"- Суть: {ctx['problem']} Мы помогаем {niche['outcome_hint']}. Предложи привезти один образец на двухдневный тест в зал."
            )

        if action.startswith("obj"):
            obj_rules = {
                "obj_qr": (
                    "Возражение «У нас уже есть QR-код». Согласись, что QR привычен. "
                    "Покажи разницу — гостю лень доставать камеру, ловить фокус в полумраке и ждать загрузку, поэтому QR сканируют единицы. "
                    "Касание к счёту занимает 1 секунду и даёт в разы больше пятёрок за смену. "
                    "Предложи бесплатно поставить наш образец рядом с их QR на 2 дня и сравнить вживую."
                ),
                "obj_price": (
                    "Возражение «Дорого / Не актуально». Согласись без спора. "
                    "Отметь, что подставка покупается один раз без абонентской платы и окупается с первых двух гостей, пришедших из поиска Гугла. "
                    "Предложи оставить один образец на бесплатный двухдневный тест без обязательств."
                ),
                "obj_think": (
                    "Возражение «Скиньте КП, мы подумаем». Согласись. "
                    "Скажи, что тратить их время на чтение длинного PDF нет смысла, когда вся механика видна за 8 секунд. "
                    "Предложи скинуть короткое видео прямо сюда и завезти один образец на бесплатный тест на пару дней."
                ),
            }
            rule = obj_rules.get(action) or (
                f"Возражение «{objection_type or 'У нас уже есть QR-код'}». "
                f"Согласись, покажи выгоду на практике и предложи двухдневный бесплатный тест."
            )
            return brief + (
                f"\nКАНАЛ: Ответ на возражение клиента в переписке.\n{rule}\n"
                f"- Строго 28–42 слова, 2 коротких абзаца. Без приветствия и без двоеточий."
            )

        return brief + "\nНапиши короткое сообщение владельцу на 25–35 слов без двоеточий."

    @staticmethod
    def _postprocess(raw: str, venue_name: str = "") -> str:
        """Мгновенная детерминированная зачистка текста без повторного вызова API."""
        text = raw.strip()
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        if len(text) > 1 and text[0] in "\"'«" and text[-1] in "\"'»":
            text = text[1:-1].strip()

        # Убираем маркдаун и кавычки
        text = text.replace("**", "").replace("__", "").replace("`", "")
        text = text.replace("«", "").replace("»", "").replace('"', "")

        # Заменяем Google Maps / Google на Гугл Карты / Гугл
        text = re.sub(r"Google\s+Maps", "Гугл Картах", text, flags=re.IGNORECASE)
        text = re.sub(r"Google\s+Карт", "Гугл Карт", text, flags=re.IGNORECASE)
        text = re.sub(r"\bGoogle\b", "Гугл", text, flags=re.IGNORECASE)

        # Заменяем неформальные приветствия в начале, если модель вдруг сорвалась
        text = re.sub(r"^(Салам|Приветствую|Привет)(,\s*ребята)?[!.,]?\s*", "Добрый день. ", text, flags=re.IGNORECASE)

        # Если модель случайно вставила название заведения — аккуратно убираем
        clean_v = (venue_name or "").strip().strip('"\'«»')
        if clean_v and len(clean_v) >= 3:
            text = re.sub(rf"\s+в\s+{re.escape(clean_v)}\b", " у вас", text, flags=re.IGNORECASE)
            text = re.sub(rf"\s+{re.escape(clean_v)}\b", "", text, flags=re.IGNORECASE)

        # Убираем книжные двоеточия и точки с запятой внутри предложений
        text = re.sub(r"\s*[:;]\s+", " — ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()

    async def _call_api(self, user_prompt: str) -> str:
        api_key = config.anymodel_api_key.strip()
        if not api_key or api_key == "your_anymodel_api_key_here":
            raise AISalesError(
                "API-ключ Anymodel.org не задан.\n"
                "Добавьте ANYMODEL_API_KEY в .env или переменные окружения хостинга."
            )
        endpoint = f"{config.anymodel_base_url.rstrip('/')}/chat/completions"
        model = config.anymodel_model.strip() or "cx/gpt-6-luna"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        payload = {"model": model, "messages": messages, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HHtapBot/1.0",
        }

        # Быстрый таймаут 40с с одной автоматической перепопыткой при зависании воркера Anymodel
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=12.0)) as client:
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                    break
                except httpx.TimeoutException:
                    if attempt == 0:
                        logger.warning("Anymodel timeout on attempt 1, retrying immediately...")
                        continue
                    raise AISalesError(
                        f"Сервер Anymodel.org сейчас перегружен (модель '{model}'). Нажмите кнопку ещё раз."
                    )
                except Exception as e:
                    raise AISalesError(f"Сетевая ошибка подключения к Anymodel API: {e}")

        if response.status_code == 401:
            raise AISalesError("Ошибка авторизации Anymodel.org (401): неверный ANYMODEL_API_KEY.")
        if response.status_code == 429:
            raise AISalesError("Превышен лимит запросов к Anymodel.org (429). Подождите немного.")
        if response.status_code != 200:
            logger.error("Anymodel API %d: %s", response.status_code, response.text)
            raise AISalesError(f"Ошибка Anymodel API ({response.status_code}): {response.text[:200]}")

        try:
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise AISalesError(f"Не удалось прочитать ответ модели: {e}")

    async def generate_message(
        self,
        action: str,
        name: str,
        address: str,
        rating: Optional[float] = None,
        reviews_count: Optional[int] = None,
        objection_type: Optional[str] = None,
    ) -> str:
        user_prompt = self.build_user_prompt(action, name, address, rating, reviews_count, objection_type)
        logger.info("AI sales: action=%s venue='%s'", action, name)
        raw = await self._call_api(user_prompt)
        return self._postprocess(raw, venue_name=name)


ai_sales_service = AISalesService()
