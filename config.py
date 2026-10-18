import os
import logging
from dataclasses import dataclass, field
from typing import List, Set
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

logger = logging.getLogger(__name__)


def _parse_admin_ids(raw_ids: str) -> Set[int]:
    """Парсит строку с ID администраторов через запятую."""
    if not raw_ids or not raw_ids.strip():
        return set()
    result = set()
    for item in raw_ids.split(","):
        cleaned = item.strip()
        if cleaned.isdigit():
            result.add(int(cleaned))
        elif cleaned:
            logger.warning("Некорректный ID администратора в ADMIN_IDS: '%s'", cleaned)
    return result


@dataclass
class Config:
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    google_api_key: str = field(
        default_factory=lambda: os.getenv("GOOGLE_API_KEY", "").strip()
    )
    admin_ids: Set[int] = field(
        default_factory=lambda: _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    )
    default_lat: float = field(
        default_factory=lambda: float(os.getenv("DEFAULT_LAT", "41.2995"))
    )
    default_lng: float = field(
        default_factory=lambda: float(os.getenv("DEFAULT_LNG", "69.2401"))
    )
    default_radius_meters: int = field(
        default_factory=lambda: int(os.getenv("DEFAULT_RADIUS_METERS", "30000"))
    )
    default_language: str = field(
        default_factory=lambda: os.getenv("DEFAULT_LANGUAGE", "ru").strip()
    )
    default_region: str = field(
        default_factory=lambda: os.getenv("DEFAULT_REGION", "uz").strip()
    )

    def is_admin(self, user_id: int) -> bool:
        """
        Проверяет, разрешен ли доступ пользователю:
        - Если admin_ids пуст, доступ открыт для всех.
        - Если admin_ids задан, доступ только пользователям из списка.
        """
        if not self.admin_ids:
            return True
        return user_id in self.admin_ids

    def validate(self) -> None:
        """Базовая валидация наличия обязательных ключей."""
        errors = []
        if not self.telegram_bot_token or self.telegram_bot_token == "your_telegram_bot_token_here":
            errors.append("Не задан TELEGRAM_BOT_TOKEN в файле .env")
        if not self.google_api_key or self.google_api_key == "your_google_places_api_key_here":
            errors.append("Не задан GOOGLE_API_KEY в файле .env")
        if errors:
            logger.warning("Конфигурация не завершена: %s", "; ".join(errors))


config = Config()
