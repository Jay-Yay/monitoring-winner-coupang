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

## 오류 원인 및 해결 방법 (Troubleshooting)

> 참고: 로컬 PC(Playwright)와 별개로, 프로덕션은 GitHub Actions(`monitor.yml`)에서 cron-job.org 트리거로 매시간 실행되며 Bright Data Web Unlocker API를 사용합니다. 아래 항목 중 Bright Data/Slack 관련 내용은 GitHub Actions 실행 기준입니다.

### 1. Bright Data 계정 정지 (billing) — 가장 흔한 원인

- **증상**: GitHub Actions 로그(`Run monitor` 단계)에 아래와 같은 경고가 상품마다 반복됨.
  ```
  [WARNING] Product page load failed: Bright Data error [client_10020]: Account is suspended. Login to brightdata.com/cp/setting/billing to activate your account
  ```
  이 경우 시트의 상품이 전부 "확인 실패"로 처리되며, Slack 요약 메시지에도 전 항목이 실패로 표시됩니다.
- **원인**: Bright Data 계정의 결제(billing) 문제로 계정 또는 zone이 정지된 상태. HTTP 자체는 200으로 응답하지만 본문이 비어 있고, 실제 사유는 `x-brd-err-code`/`x-brd-err-msg` 헤더에만 담겨 옵니다.
- **해결**: https://brightdata.com/cp/setting/billing 에 로그인해 결제 정보를 확인하고 계정을 재활성화하세요. 재활성화 후 다음 실행부터 정상 동작합니다.

### 2. cron-job.org는 "Successful"인데 Slack 메시지가 안 옴

- cron-job.org의 "Successful / 204 No Content"는 **GitHub Actions 워크플로우를 트리거하는 API 호출 자체**가 성공했다는 뜻일 뿐, 실제 모니터링 스크립트(`monitor.py`)나 Slack 전송이 성공했다는 의미가 아닙니다. cron-job.org는 워크플로우 실행 결과를 기다리지 않고 즉시 응답을 받습니다.
- 실제 실행 결과는 GitHub 저장소의 **Actions 탭 → 해당 workflow run → `Run monitor` 로그**에서 확인해야 합니다.
- Slack 웹훅 URL을 재발급했다면 로컬 `.env`뿐 아니라 **GitHub 저장소 Settings → Secrets → `SLACK_WEBHOOK_URL_OLIVE`도 함께 갱신**해야 합니다. `.env`만 바꾸면 로컬 실행에만 반영되고, 실제 프로덕션(GitHub Actions)에는 반영되지 않습니다.
- 실패한 상품 수가 많으면(예: Bright Data 계정 정지로 전 상품 실패) Slack 메시지 하나에 블록(block)이 50개를 넘어 Slack API가 `400 Bad Request`로 거부할 수 있습니다. (2026-07-05 수정: `send_slack_summary`가 블록 수를 48개로 자동 제한하고 생략 안내 문구를 추가하도록 변경됨 — `monitor.py`)
- 정확한 실패 원인은 로그의 `[ERROR] Slack send failed ... body=...` 라인에서 Slack이 반환한 실제 오류 메시지를 확인하세요.

### 3. Seller not detected (로컬 Playwright 실행 시)

`test_single.py`를 실행해 브라우저를 직접 확인하세요. 쿠팡이 HTML 구조를 변경했다면 `monitor.py > check_product()`의 CSS 셀렉터를 업데이트해야 합니다.

### 4. 403 오류 (로컬 Playwright 실행 시)

IP가 일시적으로 차단되었을 수 있습니다. 30분 후 재시도하거나, TunnelBear 등 VPN이 켜져 있다면 꺼주세요 (VPN을 사용하면 거주지 IP가 아닌 것으로 인식되어 차단됩니다). 계속 발생하면 다른 네트워크에서 시도하세요.

## Maintenance

Coupang periodically changes their page structure. When seller detection stops working:
1. Open the product page in a normal browser
2. Right-click the seller name → Inspect Element
3. Note the CSS class/selector
4. Update the `seller_selectors` list in `check_product()`
