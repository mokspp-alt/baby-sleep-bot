#!/usr/bin/env bash
# Установка или обновление бота на сервере одной командой:
#   curl -fsSL https://raw.githubusercontent.com/mokspp-alt/baby-sleep-bot/main/deploy/install.sh | BOT_TOKEN=123:abc bash
# Повторный запуск без BOT_TOKEN обновляет код и перезапускает только процесс бота.
# Другие проекты на сервере не трогает: своя папка, свой PM2-процесс, без портов.
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/baby-sleep-bot}"
REPO="${REPO:-https://github.com/mokspp-alt/baby-sleep-bot.git}"
NAME="baby-sleep-bot"

echo "==> Python и venv"
if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-venv
fi

echo "==> Код в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> Зависимости (первый раз несколько минут)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Настройки"
mkdir -p data
if [ ! -f .env ]; then
    cp .env.example .env
    # Меньше 2 ГБ свободной памяти — берём модель распознавания полегче.
    avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    if [ "${avail_mb:-0}" -lt 2000 ]; then
        sed -i 's/^WHISPER_MODEL=.*/WHISPER_MODEL=base/' .env
        echo "    Свободно ${avail_mb} МБ памяти — модель Whisper: base"
    fi
fi
if [ -n "${BOT_TOKEN:-}" ]; then
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env
fi
chmod 600 .env
if grep -q "^BOT_TOKEN=123456:" .env; then
    echo "!! Токен не задан. Запустите ещё раз с BOT_TOKEN=..." >&2
    exit 1
fi

echo "==> PM2"
if ! command -v pm2 >/dev/null 2>&1; then
    echo "!! pm2 не найден. Установите: npm install -g pm2" >&2
    exit 1
fi
if pm2 describe "$NAME" >/dev/null 2>&1; then
    pm2 restart "$NAME" --update-env
else
    pm2 start ecosystem.config.js
fi
pm2 save
sleep 5
pm2 logs "$NAME" --lines 20 --nostream
echo "==> Готово. Напишите боту /start"
