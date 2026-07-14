# Container image untuk mode webhook (serverless), dipakai saat deploy ke
# Google Cloud Run. Lihat DEPLOY.md untuk instruksi lengkap.
#
# Mode polling (main.py) TIDAK dimaksudkan untuk image ini — Cloud Run
# mematikan instance yang tidak menerima request, sehingga proses polling
# yang harus hidup selamanya tidak cocok di sini.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "webhook_server.py"]
