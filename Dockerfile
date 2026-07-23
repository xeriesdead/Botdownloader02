# Container image untuk mode polling Telegram bot.
# Proses ini harus tetap hidup, sehingga cocok untuk Railway atau hosting
# always-on lainnya.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
