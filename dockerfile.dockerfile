FROM python:3.10-slim

# Устанавливаем git и golang (для сборки PhoneInfoga)
RUN apt-get update && apt-get install -y \
    git \
    golang-go \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исправлено: копируем все файлы из текущей папки в /app
COPY . .

CMD ["python", "bot.py"]
