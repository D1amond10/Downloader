FROM python:3.11-slim

# Устанавливаем системные зависимости, включая ffmpeg для склейки видео и аудио
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . .

# Порт по умолчанию для Render Web Service
ENV PORT=10000

# Запуск бота
CMD ["python", "bot.py"]
