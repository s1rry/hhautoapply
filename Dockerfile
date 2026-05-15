FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg2 && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir . && pip install --no-cache-dir aiosqlite

RUN playwright install chromium && playwright install-deps

RUN mkdir -p data/browser_sessions logs

CMD ["python", "-m", "app.main"]
