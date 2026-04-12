#!/bin/bash

# Папка
cd /root/tiktok_bot

# Venv
source /root/shared_env/bin/activate

# Обработка сигналов
trap 'echo "Бот остановлен"; exit 0' SIGTERM

# Цикл
while true; do
    echo "🚀 Запуск TikTok Bot..."
    python main.py
    EXIT_CODE=$?
    
    # Если код выхода 0 - нормальное завершение, выходим
    if [ $EXIT_CODE -eq 0 ]; then
        echo "✅ Бот завершил работу нормально."
        exit 0
    fi
    
    # Иначе пробуем перезапустить
    echo "⚠️ Бот впав (код выхода: $EXIT_CODE)! Перезапуск через 3 секунди..."
    sleep 3
done
