// PM2: pm2 start ecosystem.config.js — токен и настройки берутся из .env в корне проекта.
module.exports = {
  apps: [
    {
      name: "baby-sleep-bot",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "-m sleepbot",
      interpreter: "none",
      autorestart: true,
      max_memory_restart: "2500M",  // модель распознавания речи занимает ~1 ГБ, с запасом на пики
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
