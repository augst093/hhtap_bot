import logging
from typing import Optional
import httpx

from config import config

logger = logging.getLogger(__name__)

# Точный системный промпт B2B-переговорщика HHtap
SYSTEM_PROMPT = """Ты — топовый B2B-переговорщик проекта HHtap в Ташкенте. Мы создаём умные NFC-карты для локального бизнеса (рестораны, кофейни, барбершопы, клиники, клубы). Суть продукта: гость прикладывает телефон к карте, и через 1 секунду у него на экране открывается готовое окно Google Maps с 5 звёздами именно этого заведения.

Твоя задача — писать короткие, цепляющие персонализированные сообщения владельцам или управляющим заведений на основе реальных данных из парсера (Название, Улица, Рейтинг, Количество отзывов).

ЖЕСТКИЕ ПРАВИЛА СТИЛЯ (НАРУШЕНИЕ ЗАПРЕЩЕНО):
1. Никакой воды и канцеляризма. ЗАПРЕЩЕНЫ слова и фразы: "уникальный", "инновационный", "современный", "позвольте предложить", "вывести на новый уровень", "повысить лояльность", "в эпоху цифровизации", "эффективный инструмент".
2. Длина сообщения — строго до 55–65 слов (2–3 коротких абзаца с воздухом между ними). Сообщение должно читаться за 7 секунд с экрана телефона.
3. Пиши как живой предприниматель, который обращается к коллеге на равных: уважительно, просто, конкретно, с привязкой к их улице и их цифрам на карте.
4. Логика аргументации по цифрам:
   - Если отзывов мало (до 60): сделай упор на то, что в трафике и сторис у них круто, но на Google Картах всего {reviews_count} отзывов, из-за чего люди, ищущие место поблизости на {address}, уходят к соседям.
   - Если рейтинг ниже 4.6: сделай упор на то, что злые клиенты пишут отзывы сами, а довольные — ленятся искать точку вручную. NFC-карта позволяет за неделю перекрыть старые оценки свежими 5 звёздами.
   - Если рейтинг высокий (4.7+) и отзывов много: похвали за топ-уровень на {address} и предложи NFC-карту, чтобы официантам и админам было проще конвертировать каждого гостя в отзыв за 1 касание без неудобных QR-кодов.
5. Заканчивай сообщение ОДНИМ простым вопросом, на который легко ответить "Да" или коротким сообщением (никаких сложных вопросов в духе "когда вам удобно провести презентацию?").

ЭТАЛОННЫЕ ПРИМЕРЫ (ПИШИ В ТАКОМ ЖЕ СТИЛЕ):

Пример 1 (Для Instagram Direct — первый заход):
Здравствуйте! Часто бываю в районе Тараса Шевченко, у вас отличный сервис и живой Инстаграм, но заметил, что на Google Картах у Roni сейчас всего 48 отзывов. Из-за этого новые гости и туристы при поиске пиццерии поблизости часто видят конкурентов выше вас.

Подскажите, здесь на связи управляющий? Есть простое решение, как собирать по 5–10 живых отзывов в день прямо у столика или кассы.

Пример 2 (Для Telegram — сразу с нашим демо-видео):
Здравствуйте! Пишу по поводу точки {name} на {address}. 

Обычно 9 из 10 довольных гостей ленятся вручную вбивать название заведения в Google, чтобы оставить отзыв. Мы в HHtap делаем NFC-карты: гость просто касается телефоном карты при оплате, и за 1 секунду открывается окно с 5 звёздами сразу для вашей локации (на видео показал, как это работает вживую — ровно 2 секунды).

Мы уже сгенерировали и проверили прямую ссылку конкретно под ваш филиал. Могу отправить готовую карту курьером или заскочить на пару минут, чтобы вы протестировали на своём телефоне. Как вам идея?
"""


class AISalesError(Exception):
    """Исключение при генерации сообщения ИИ."""
    pass


class AISalesService:
    """
    Асинхронный сервис для генерации B2B-питчей и отработки возражений
    через OpenAI-совместимый API Anymodel.org.
    """

    def __init__(self):
        pass

    def build_user_prompt(
        self,
        action: str,
        name: str,
        address: str,
        rating: Optional[float] = None,
        reviews_count: Optional[int] = None,
        objection_type: Optional[str] = None,
    ) -> str:
        """Формирует точный user-запрос к модели на основе действия и данных заведения."""
        rating_str = f"{rating:.1f}" if rating is not None else "не указан"
        reviews_str = str(reviews_count) if reviews_count is not None else "мало"

        if action == "insta":
            return (
                f"Напиши первое короткое сообщение (крючок на 2 абзаца) для Instagram Direct "
                f"заведения {name} (адрес: {address}, рейтинг: {rating_str}, отзывов: {reviews_str}). "
                f"Цель — узнать, на связи ли управляющий, и зацепить цифрами их карточки."
            )
        elif action == "tg":
            return (
                f"Напиши сообщение для Telegram (до 3 коротких абзацев), которое мы отправим вместе с нашим "
                f"8-секундным видео работы NFC-карты. Данные точки: {name}, {address}, рейтинг {rating_str}, "
                f"отзывов {reviews_str}. Укажи, что прямая ссылка под их локацию уже готова и протестирована."
            )
        elif action == "voice":
            return (
                f"Напиши 3 коротких тезиса разговорным языком (ровно на 15 секунд речи), чтобы я зачитал их "
                f"голосом в голосовом сообщении владельцу {name} ({address}, рейтинг {rating_str}, отзывов {reviews_str})."
            )
        elif action.startswith("obj_") or action == "obj":
            obj_name = objection_type or "У нас уже есть QR-код"
            return (
                f"Клиент из заведения {name} ({address}) ответил возражением: '{obj_name}'. "
                f"Напиши короткий, уверенный ответ на 2–3 предложения без давления, но с твёрдым аргументом в пользу NFC в одно касание."
            )
        else:
            return (
                f"Напиши короткое персонализированное предложение для {name} ({address}, рейтинг {rating_str}, отзывов {reviews_str})."
            )

    async def generate_message(
        self,
        action: str,
        name: str,
        address: str,
        rating: Optional[float] = None,
        reviews_count: Optional[int] = None,
        objection_type: Optional[str] = None,
    ) -> str:
        """
        Отправляет запрос к Anymodel.org API и возвращает сгенерированный текст.
        Параметры модели: temperature=0.45, max_tokens=350.
        """
        api_key = config.anymodel_api_key.strip()
        if not api_key or api_key == "your_anymodel_api_key_here":
            raise AISalesError(
                "API-ключ Anymodel.org не задан.\n"
                "Добавьте ANYMODEL_API_KEY в файл .env или переменные окружения хостинга."
            )

        base_url = config.anymodel_base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        model = config.anymodel_model.strip() or "gpt-6-luna"

        user_prompt = self.build_user_prompt(
            action=action,
            name=name,
            address=address,
            rating=rating,
            reviews_count=reviews_count,
            objection_type=objection_type,
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HHtapBot/1.0",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.45,
            "max_tokens": 350,
        }

        logger.info(
            "Отправка запроса в Anymodel.org (%s), action=%s, venue='%s', endpoint=%s",
            model,
            action,
            name,
            endpoint,
        )

        timeout_config = httpx.Timeout(90.0, connect=20.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            try:
                response = await client.post(endpoint, headers=headers, json=payload)
            except httpx.TimeoutException:
                logger.error("Таймаут запроса к Anymodel API (%s, модель %s)", endpoint, model)
                raise AISalesError(
                    f"Сервер Anymodel.org долго отвечает для модели '{model}'.\n"
                    "Попробуйте нажать кнопку еще раз через пару секунд."
                )
            except Exception as e:
                logger.error("Сетевая ошибка при запросе к Anymodel API: %s", e)
                raise AISalesError(f"Сетевая ошибка подключения к Anymodel API: {e}")


        if response.status_code == 401:
            raise AISalesError("Ошибка авторизации Anymodel.org (401): неверный ANYMODEL_API_KEY.")
        elif response.status_code == 429:
            raise AISalesError("Превышен лимит запросов к Anymodel.org (429 Rate Limit). Подождите немного.")
        elif response.status_code != 200:
            err_msg = response.text[:200]
            logger.error("Anymodel API returned %d: %s", response.status_code, response.text)
            raise AISalesError(f"Ошибка Anymodel API ({response.status_code}): {err_msg}")

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content
        except Exception as e:
            logger.error("Ошибка парсинга ответа от Anymodel API: %s, raw: %s", e, response.text[:300])
            raise AISalesError(f"Не удалось прочитать ответ модели: {e}")


ai_sales_service = AISalesService()
