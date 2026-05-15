FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg2 && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

RUN playwright install chromium && playwright install-deps

COPY . .

RUN mkdir -p data/browser_sessions logs

CMD ["python", "-m", "app.main"]
