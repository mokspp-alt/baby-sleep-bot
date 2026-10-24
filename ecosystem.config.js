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
      max_memory_restart: "1500M",
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
