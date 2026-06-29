#!/bin/bash
# Deployment script for VPS (Ubuntu 22.04+)
# Usage: scp this to VPS, then run: bash setup_vps.sh

set -e

echo "=== Job Hunter VPS Setup ==="

# Update system
apt update && apt upgrade -y

# Install Python 3.12
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3.12-dev

# Install system deps for Playwright
apt install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    fonts-liberation fonts-noto-color-emoji \
    xvfb dbus-x11

# Install git
apt install -y git

# Create app user
useradd -m -s /bin/bash jobhunter || true

# Clone repo
cd /opt
if [ ! -d "job-hunter" ]; then
    git clone https://github.com/egorov8080/hh-avtootkliki.git
fi
cd job-hunter

# Create venv
python3.12 -m venv .venv
source .venv/bin/activate

# Install deps
pip install --upgrade pip
pip install -e .

# Install Playwright browsers
playwright install chromium
playwright install-deps chromium

# Create data directory
mkdir -p data/browser_sessions
chown -R jobhunter:jobhunter /opt/job-hunter

# Create .env file template
if [ ! -f .env ]; then
    cat > .env << 'EOF'
TG_BOT_TOKEN=
TG_ADMIN_CHAT_ID=
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://waveapi.tonvarex.ru
DATABASE_URL=sqlite+aiosqlite:///data/jobhunter.db
DESIRED_POSITION=Бизнес/Системный аналитик (Middle)
DESIRED_SALARY_MIN=200000
DESIRED_SALARY_MAX=400000
CHECK_INTERVAL_SEC=300
MAX_APPLIES_PER_DAY=30
HH_LOGIN=
HH_PASSWORD=
BROWSER_HEADLESS=true
EOF
    echo ">>> Created .env template. Fill in your credentials!"
fi

# Create systemd service
cat > /etc/systemd/system/job-hunter.service << 'EOF'
[Unit]
Description=Job Hunter Bot
After=network.target

[Service]
Type=simple
User=jobhunter
WorkingDirectory=/opt/job-hunter
Environment=PATH=/opt/job-hunter/.venv/bin:/usr/bin
ExecStart=/opt/job-hunter/.venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable job-hunter

echo ""
echo "=== Setup complete! ==="
echo "1. Edit /opt/job-hunter/.env with your credentials"
echo "2. Start: systemctl start job-hunter"
echo "3. Logs:  journalctl -u job-hunter -f"
echo ""
