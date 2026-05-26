#!/bin/bash
# ============================================================
# Coupang Buy Box (Product Seller) Monitor — Local PC Setup
# ============================================================
set -e

echo "=== Coupang Buy Box (Product Seller) Monitor Setup ==="

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$PROJECT_DIR/logs"

echo "[1/3] Installing Python packages..."
pip3 install playwright requests

echo "[2/3] Installing Chromium..."
playwright install chromium

echo "[3/3] Creating .env template..."
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
GOOGLE_SHEET_CSV_URL=https://docs.google.com/spreadsheets/d/1DrPiq_WQ-Hkw17PzrXGmF2YsUkw2hfdJf1Rv4FPupq0/export?format=csv
EOF
    echo ""
    echo ">>> Edit .env with your Slack webhook URL:"
    echo "    $ENV_FILE"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Usage:"
echo "  Single check:   python3 monitor.py"
echo "  Daemon (10min):  python3 monitor.py --daemon"
echo ""
echo "Before running, edit .env with your Slack webhook URL."
