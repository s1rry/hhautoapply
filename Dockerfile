FROM python:3.12-slim

WORKDIR /app

# Install system deps for Playwright (optional, only for VPS)
ARG INSTALL_PLAYWRIGHT=false
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    fonts-liberation && \
    rm -rf /var/lib/apt/lists/*; \
    fi

COPY pyproject.toml .
COPY app ./app
COPY configs ./configs
RUN pip install --no-cache-dir -e .

# Install Playwright browser if enabled
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
    playwright install chromium && \
    playwright install-deps chromium; \
    fi

RUN mkdir -p data/browser_sessions logs

CMD ["python", "-m", "app.main"]
