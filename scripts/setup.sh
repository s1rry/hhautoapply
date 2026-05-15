#!/bin/bash
set -e

echo "=== Job Hunter Setup ==="

# Копируем env если нет
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✓ Создан .env — заполни его перед запуском"
fi

# Устанавливаем зависимости
pip install -e ".[dev]"
echo "✓ Зависимости установлены"

# Устанавливаем Playwright
playwright install chromium
echo "✓ Браузер Chromium установлен"

# Создаём директории
mkdir -p data/browser_sessions logs configs
echo "✓ Директории созданы"

echo ""
echo "=== Готово! ==="
echo "1. Заполни .env файл"
echo "2. Отредактируй configs/resume.txt"
echo "3. Запуск: python -m app.main"
echo "4. Или через Docker: docker compose up -d"
