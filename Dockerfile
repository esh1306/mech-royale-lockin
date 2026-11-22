# Mech Royale bot image. Pure stdlib Python, no packages to install.
# The match runs it with no network, read-only root (writable /tmp), 1 CPU, 512 MB, map at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
