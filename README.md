# Coupang Buy Box Monitor

쿠팡 상품 페이지에서 메인 판매자(Buy Box)가 우리 브랜드(`주식회사 웨이브메트릭`)인지 10분마다 확인하고, 리셀러에게 빼앗겼을 때 Slack으로 알림을 보냅니다.

## Why local PC (not cloud)?

쿠팡은 AWS/GCP 등 클라우드 서버 IP를 403으로 차단합니다. 가정용(주거용) IP에서만 접근 가능하므로, 이 스크립트는 **로컬 PC에서 실행**해야 합니다.

## Setup

```bash
# 1. Clone/copy this folder to your PC
# 2. Run setup
bash setup.sh

# 3. Edit .env with your Slack webhook URL
#    Get one from: https://api.slack.com/messaging/webhooks

# 4. Make sure your Google Sheet is shared (viewable by link)
```

## Usage

```bash
# Test with a single product first
python3 test_single.py

# Run once (check all products)
python3 monitor.py

# Run as daemon (every 10 min, keeps browser open)
python3 monitor.py --daemon
```

## How it works

1. Reads product URLs from Google Sheet (CSV export)
2. Opens each URL in Chromium via Playwright
3. Extracts the current main seller name and price
4. Compares seller against "주식회사 웨이브메트릭"
5. If different → sends Slack alert with reseller name + price
6. When recovered → sends recovery notification
7. Uses `state.json` to avoid duplicate alerts

## Files

```
monitor.py       — Main script (single run or daemon mode)
test_single.py   — Test scraping on one product
setup.sh         — Install dependencies
.env             — Slack webhook URL (create this)
state.json       — Alert state (auto-created)
logs/            — Daily log files
```

## Google Sheet format

| product_name | size(ml) | URL_coupang |
|---|---|---|
| 상품명 | 60 | https://www.coupang.com/vp/products/... |

## Keeping it running

**Option A: Terminal (simple)**
```bash
# Use tmux or screen to keep it running
tmux new -s coupang
python3 monitor.py --daemon
# Ctrl+B, D to detach
```

**Option B: macOS launchd**
Create `~/Library/LaunchAgents/com.coupang.monitor.plist` for auto-start on login.

**Option C: Windows Task Scheduler**
Create a task that runs `python3 monitor.py` every 10 minutes.

## Troubleshooting

- **Seller not detected**: Run `test_single.py` — the browser opens visually. Inspect the page to check if Coupang changed their HTML structure. Update CSS selectors in `monitor.py > check_product()`.
- **403 error**: Your IP may be temporarily blocked. Wait 30 min and retry. If persistent, try from a different network.
- **No Slack alerts**: Check `.env` file has the correct webhook URL. Check `logs/` for errors.

## Maintenance

Coupang periodically changes their page structure. When seller detection stops working:
1. Open the product page in a normal browser
2. Right-click the seller name → Inspect Element
3. Note the CSS class/selector
4. Update the `seller_selectors` list in `check_product()`
