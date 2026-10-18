FROM python:3.12-slim

# Отключаем буферизацию и создание .pyc файлов
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Установка системных зависимостей для Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Копируем и устанавливаем Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Chromium и системных библиотек для Playwright
RUN playwright install chromium --with-deps

# Копируем исходный код проекта
COPY . .

# Запуск бота
CMD ["python", "bot.py"]
