# Container image untuk mode webhook Telegram bot.
# Server HTTP menerima update Telegram melalui webhook.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The bot writes temporary downloads and logs at runtime. Keep the container
# non-root while giving its dedicated user ownership of the application files.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/downloads /app/logs \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "webhook_server.py"]
