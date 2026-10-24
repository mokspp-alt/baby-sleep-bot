#!/bin/sh
# Короткая ссылка на установку, чтобы набрать её руками в веб-консоли:
#   curl -L raw.githubusercontent.com/mokspp-alt/baby-sleep-bot/main/i | sh
set -e
curl -fsSL https://raw.githubusercontent.com/mokspp-alt/baby-sleep-bot/main/deploy/install.sh -o /tmp/baby-sleep-bot-install.sh
bash /tmp/baby-sleep-bot-install.sh
