#!/usr/bin/env python3
"""
Coupang Product Seller Monitor
=======================
Two scraping backends — pick one via .env:

  [Bright Data — recommended, stable]
  BRIGHT_DATA_API_KEY=your_api_key
  BRIGHT_DATA_ZONE=web_unlocker1   # name of your Web Unlocker zone

  [Chrome/local — fallback, requires Mac + Chrome running]
  # Leave BRIGHT_DATA_API_KEY unset.
  # Visit https://www.coupang.com in Chrome for ~30s before each run.

Google Sheet columns (required):
  productId     — Coupang product ID (from product URL)
  valueId       — Option identifier (stable, comma-separated type IDs)
  Target_Seller — Seller name to watch (Korean company name)
  product_name  — Human-readable product name
  size(ml)      — Size/quantity label

Run:
  python3 test_single.py          # test one product
  python3 monitor.py              # single check of all products
  python3 monitor.py --daemon     # run every 30-40 min (keep terminal open)
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
import signal
import sys
import time
import logging
from datetime import datetime
from io import StringIO
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SLACK_WEBHOOK_URL_OLIVE = os.environ.get("SLACK_WEBHOOK_URL_OLIVE", "")
COUPANG_ID = os.environ.get("COUPANG_ID", "")
COUPANG_PW = os.environ.get("COUPANG_PW", "")

BRIGHT_DATA_API_KEY = os.environ.get("BRIGHT_DATA_API_KEY", "")
BRIGHT_DATA_ZONE = os.environ.get("BRIGHT_DATA_ZONE", "unblocker")

GOOGLE_SHEET_CSV_URL = os.environ.get(
    "GOOGLE_SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1DrPiq_WQ-Hkw17PzrXGmF2YsUkw2hfdJf1Rv4FPupq0/export?format=csv",
)

CHECK_INTERVAL_MIN = (30, 40)  # random range in minutes between checks in daemon mode
MIN_DELAY = 3            # min seconds between product checks
MAX_DELAY = 6            # max seconds between product checks

# Paths
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
SESSION_FILE = BASE_DIR / "session.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
ENV_FILE = BASE_DIR / ".env"

# ---------------------------------------------------------------------------
# Load .env file if exists
# ---------------------------------------------------------------------------
def load_env():
    global SLACK_WEBHOOK_URL_OLIVE, COUPANG_ID, COUPANG_PW
    global BRIGHT_DATA_API_KEY, BRIGHT_DATA_ZONE
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    SLACK_WEBHOOK_URL_OLIVE = os.environ.get("SLACK_WEBHOOK_URL_OLIVE", SLACK_WEBHOOK_URL_OLIVE)
    COUPANG_ID = os.environ.get("COUPANG_ID", COUPANG_ID)
    COUPANG_PW = os.environ.get("COUPANG_PW", COUPANG_PW)
    BRIGHT_DATA_API_KEY = os.environ.get("BRIGHT_DATA_API_KEY", BRIGHT_DATA_API_KEY)
    BRIGHT_DATA_ZONE = os.environ.get("BRIGHT_DATA_ZONE", BRIGHT_DATA_ZONE)

load_env()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"monitor_{datetime.now():%Y%m%d}.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Sheet reader
# ---------------------------------------------------------------------------
def fetch_product_list() -> list[dict]:
    resp = requests.get(GOOGLE_SHEET_CSV_URL, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    reader = csv.DictReader(StringIO(resp.text))
    products = []
    for row in reader:
        product_id = row.get("productId", "").strip()
        value_id = row.get("valueId", "").strip()
        # Support both column name variants (sheet uses "vendorItemIdMy " with trailing space)
        target_vid = (
            row.get("Target_VendorItemId", "")
            or row.get("vendorItemIdMy", "")
            or row.get("vendorItemIdMy ", "")
        ).strip()
        if product_id and value_id and target_vid:
            products.append({
                "product_id": product_id,
                "value_id": value_id,
                "target_vendor_item_id": target_vid,
                "target_seller": row.get("Target_Seller", "").strip(),
                "name": (
                    row.get("product_name", "")
                    or row.get("productName", "")
                ).strip(),
                "size": (
                    row.get("size(ml)", "")
                    or row.get("sizeAndCount", "")
                ).strip(),
            })
    log.info(f"Loaded {len(products)} products from Google Sheet")
    return products

# ---------------------------------------------------------------------------
# Cookie extraction from Chrome
# ---------------------------------------------------------------------------
def get_chrome_cookies_for_coupang() -> dict:
    import browser_cookie3
    chrome_base = Path.home() / "Library/Application Support/Google/Chrome"
    if not chrome_base.exists():
        log.warning("Chrome not found")
        return {}
    best_cookies: dict = {}
    for profile_dir in sorted(chrome_base.iterdir()):
        cookie_file = profile_dir / "Cookies"
        if not cookie_file.exists():
            continue
        try:
            jar = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                domain_name="coupang.com",
            )
            cookies = {c.name: c.value for c in jar if c.value}
            if len(cookies) > len(best_cookies):
                best_cookies = cookies
                log.info(f"Found {len(cookies)} Coupang cookies in profile '{profile_dir.name}'")
        except Exception:
            continue
    if not best_cookies:
        log.warning("No Coupang cookies found in any Chrome profile")
    return best_cookies

def refresh_chrome_cookies() -> dict:
    """Open Coupang in Chrome via AppleScript to get fresh Akamai cookies, then re-read them."""
    import subprocess
    log.info("Refreshing cookies: opening Coupang in Chrome via AppleScript...")
    subprocess.run([
        "osascript", "-e",
        'tell application "Google Chrome" to open location "https://www.coupang.com"'
    ], check=False)
    time.sleep(8)
    return get_chrome_cookies_for_coupang()

def fetch_via_chrome(url: str) -> dict:
    """Open URL in Chrome and extract seller+price via JavaScript.
    Returns dict with 'seller' and 'price' keys (price as raw digits string).
    Requires: Chrome > View > Developer > Allow JavaScript from Apple Events
    """
    import subprocess
    log.info(f"Fetching via Chrome AppleScript: {url}")

    # JS rules: only single-quoted strings, no backslash sequences
    # (AppleScript interprets \s, \n etc. inside "..." as escape sequences).
    # Returns "SELLER||PRICE" to avoid double quotes in the output (JSON.stringify would add them).
    # String.fromCharCode(10) = newline without backslash.
    # Walks DOM parents to skip review-section vendor links.
    js = (
        "(function(){"
        "var seller='';"
        "var price='';"
        "var ss=document.querySelectorAll('script');"
        "for(var i=0;i<ss.length;i++){"
          "if(ss[i].type!=='application/ld+json')continue;"
          "try{var d=JSON.parse(ss[i].textContent);"
          "if(d.offers&&d.offers.price){price=String(d.offers.price);break;}}"
          "catch(e){}"
        "}"
        "var ls=document.querySelectorAll('a');"
        "for(var i=0;i<ls.length;i++){"
          "var el=ls[i];"
          "if((el.href||'').indexOf('shop.coupang.com/vid')<0)continue;"
          "var p=el.parentElement;var skip=false;"
          "while(p){"
            "var cn=p.className||'';"
            "if(cn.indexOf('review')>=0||cn.indexOf('rc-table')>=0){skip=true;break;}"
            "p=p.parentElement;"
          "}"
          "if(skip)continue;"
          "var t=(el.innerText||'').trim().split(String.fromCharCode(10))[0].trim();"
          "if(t&&t.length>=2&&t.length<=80){seller=t;break;}"
        "}"
        "return seller+'||'+price;"
        "})()"
    )

    script = f'''
tell application "Google Chrome"
    activate
    open location "{url}"
    delay 6
    set jsOut to execute front window's active tab javascript "{js}"
    close (front window's active tab)
    return jsOut
end tell
'''
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        log.warning("AppleScript fetch timed out after 60s")
        return {}
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "Allow JavaScript from Apple Events" in stderr or "turned off" in stderr:
            log.error(
                "Chrome is blocking AppleScript JavaScript. Fix once:\n"
                "  Chrome 메뉴 > View > Developer > Allow JavaScript from Apple Events 체크"
            )
        else:
            log.warning(f"AppleScript fetch failed: {stderr}")
        return {}
    out = proc.stdout.strip()
    if "||" in out:
        seller, _, price = out.partition("||")
        return {"seller": seller.strip(), "price": price.strip()}
    log.warning(f"Chrome JS returned unexpected output: {out[:100]}")
    return {}

# ---------------------------------------------------------------------------
# Shared HTML parsing — used by both scrapers
# ---------------------------------------------------------------------------
def extract_seller_price(content: str, result: dict):
    """Parse seller name and price from Coupang product page HTML."""

    # Seller: structured sellerInfo block — highest priority.
    # This is specifically tagged PRODUCT_DETAIL_SELLER_INFO in the Next.js payload
    # and corresponds to the selected item's seller, unlike the anchor pattern which
    # can match sellers from the option comparison table for OTHER options on the page.
    if not result["seller"]:
        m = re.search(
            r'\\"sellerName\\":\\"([^\\]+)\\",\\"sellerNameLabel\\":\\"판매자',
            content,
        )
        if m:
            name = m.group(1).strip()
            if len(name) > 1:
                result["seller"] = name
                log.info(f"Seller found via sellerInfo: {name}")

    # Seller: HTML anchor (e.g. marketplace sellers with a store page)
    if not result["seller"]:
        for pattern in [
            r'판매자:<a[^>]*>([^<]{2,80})</a>',
            r'판매자:\s*<a[^>]*>([^<]{2,80})</a>',
        ]:
            m = re.search(pattern, content)
            if m:
                name = m.group(1).strip()
                if len(name) > 1:
                    result["seller"] = name
                    log.info(f"Seller found via anchor: {name}")
                    break

    # Seller: React comment-node pattern — 판매자:<!-- -->판매자명 (no anchor, no store page)
    if not result["seller"]:
        m = re.search(r'판매자:(?:<!-- -->)([^<]{2,80})</div>', content)
        if m:
            name = m.group(1).strip()
            if len(name) > 1:
                result["seller"] = name
                log.info(f"Seller found via comment-node: {name}")

    # Seller: escaped JSON blob (Next.js streaming payload)
    if not result["seller"]:
        for pattern in [
            r'\\"sellerName\\":\\"([^\\]{2,80})\\"',
            r'\\"vendorName\\":\\"([^\\]{2,80})\\"',
            r'\\"storeName\\":\\"([^\\]{2,80})\\"',
        ]:
            m = re.search(pattern, content)
            if m:
                name = m.group(1).strip()
                if len(name) > 1 and not name.startswith("{"):
                    result["seller"] = name
                    log.info(f"Seller found via escaped JSON: {name}")
                    break

    # Seller: unescaped JSON blobs
    if not result["seller"]:
        for pattern in [
            r'"vendorName"\s*:\s*"([^"]{2,80})"',
            r'"partnerName"\s*:\s*"([^"]{2,80})"',
            r'"sellerDisplayName"\s*:\s*"([^"]{2,80})"',
            r'"sellerDispName"\s*:\s*"([^"]{2,80})"',
            r'"sellerName"\s*:\s*"([^"]{2,80})"',
        ]:
            m = re.search(pattern, content)
            if m:
                name = m.group(1).strip()
                if len(name) > 1 and not name.startswith("{"):
                    result["seller"] = name
                    log.info(f"Seller found via JSON regex: {name}")
                    break

    # Seller: JSON-LD
    if not result["seller"]:
        for ld_match in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            content, re.DOTALL
        ):
            try:
                data = json.loads(ld_match.group(1))
                if isinstance(data, dict) and "offers" in data:
                    offers = data["offers"]
                    if isinstance(offers, dict) and "seller" in offers:
                        seller_info = offers["seller"]
                        name = seller_info.get("name", "") if isinstance(seller_info, dict) else str(seller_info)
                        if name:
                            result["seller"] = name
                            log.info(f"Seller found via JSON-LD: {name}")
                            break
            except Exception:
                continue

    # Seller: Rocket Delivery fallback — only when no third-party seller found
    # ROCKET_MERCHANT = seller using Coupang logistics (still a third-party seller)
    # ROCKET          = Coupang itself selling the product
    if not result["seller"]:
        has_rocket = 'data-badge-id="ROCKET"' in content
        has_rocket_merchant = 'data-badge-id="ROCKET_MERCHANT"' in content
        if has_rocket and not has_rocket_merchant:
            result["seller"] = "쿠팡 로켓배송"
            log.info("Seller: 쿠팡 로켓배송 (Rocket badge only, no third-party seller found)")

    # Price: JSON-LD
    for ld_match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        content, re.DOTALL
    ):
        try:
            data = json.loads(ld_match.group(1))
            if isinstance(data, dict) and "offers" in data:
                raw = str(data["offers"].get("price", "")).replace(",", "")
                if raw.isdigit() and int(raw) > 100:
                    result["price"] = f"{int(raw):,}원"
                    break
        except Exception:
            continue

    # Price: escaped JSON blob
    if not result["price"]:
        m = re.search(r'\\"finalPrice\\":\\"([\d,]+)원\\"', content)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.isdigit() and int(raw) > 100:
                result["price"] = f"{int(raw):,}원"

    # Price: HTML element
    if not result["price"]:
        m = re.search(r'twc-text-bluegray-900[^>]*>([\d,]+)원', content)
        if m:
            raw = m.group(1).replace(",", "")
            if raw.isdigit() and int(raw) > 100:
                result["price"] = f"{int(raw):,}원"


# ---------------------------------------------------------------------------
# valueId → {item_id, vendor_item_id} resolver
# ---------------------------------------------------------------------------
def resolve_option_from_html(html: str, value_id: str) -> dict | None:
    """Parse product page HTML to find itemId and vendorItemId for a given valueId.

    Coupang's Next.js payload embeds two complementary structures:
      - attributeVendorItemMap: {valueId → {itemId, vendorItemId}} (newer format)
      - optionRows.attributes:  {valueId → option display name} + item list (older format)
    The first structure is tried first as it directly maps valueId to the IDs we need.

    value_id may be a comma-separated list of individual type IDs (1, 2, or 3+).
    Returns {"item_id": "...", "vendor_item_id": "..."} or None.
    """
    # Primary: attributeVendorItemMap keyed by value_id — itemId and vendorItemId
    # are direct fields in the entry, not adjacent to itemName.
    m_key = re.search(r'\\"' + re.escape(value_id) + r'\\":\{', html)
    if m_key:
        window = html[m_key.start():m_key.start() + 3000]
        m_ids = re.search(r'\\"itemId\\":(\d+),\\"vendorItemId\\":(\d+)', window)
        if m_ids:
            return {"item_id": m_ids.group(1), "vendor_item_id": m_ids.group(2)}

    # Fallback: value_to_name → itemName matching (older page format where
    # itemId, itemName, and vendorItemId appear in sequence in optionRows).
    value_to_name: dict[str, str] = {}
    for m in re.finditer(r'valueId\\":\\"([^"\\]+)\\",\\"name\\":\\"([^"\\]+)\\"', html):
        value_to_name[m.group(1)] = m.group(2)

    option_name = value_to_name.get(value_id)

    if option_name is None and "," in value_id:
        parts = value_id.split(",")
        part_names = [value_to_name.get(p) for p in parts]
        if all(part_names):
            for m in re.finditer(
                r'itemId\\":(\d+),\\"itemName\\":\\"([^\\]+)\\",\\"vendorItemId\\":(\d+)', html
            ):
                item_name = m.group(2)
                if all(n in item_name for n in part_names):
                    return {"item_id": m.group(1), "vendor_item_id": m.group(3)}
        return None

    if not option_name:
        return None

    for m in re.finditer(
        r'itemId\\":(\d+),\\"itemName\\":\\"([^\\]+)\\",\\"vendorItemId\\":(\d+)', html
    ):
        if m.group(2) == option_name:
            return {"item_id": m.group(1), "vendor_item_id": m.group(3)}

    return None


# Keep backward-compatible alias used in test_single.py
def resolve_item_id_from_html(html: str, value_id: str) -> str | None:
    opt = resolve_option_from_html(html, value_id)
    return opt["item_id"] if opt else None


def find_highest_qty_item_id(html: str) -> str | None:
    """Return the itemId of the highest-quantity option visible in the page HTML.

    Coupang only renders options up to the currently-selected quantity in the
    optionRows section, but itemBasicInfo often includes higher options.
    Probing with that itemId on the next run expands the visible option list.
    De-duplicates by itemId so repeated entries don't skew the result.
    """
    max_qty = -1
    best_item_id = None
    seen: set[str] = set()
    for m in re.finditer(
        r'itemId\\":(\d+),\\"itemName\\":\\"([^\\]+)\\",\\"vendorItemId\\":(\d+)', html
    ):
        item_id = m.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        qty_m = re.search(r'(\d+)개', m.group(2))
        if qty_m:
            qty = int(qty_m.group(1))
            if qty > max_qty:
                max_qty = qty
                best_item_id = item_id
    return best_item_id


# ---------------------------------------------------------------------------
# Bright Data Web Unlocker scraper (recommended — bypasses Akamai reliably)
# ---------------------------------------------------------------------------
class BrightDataScraper:
    """Fetches pages via Bright Data Web Unlocker API (API key auth).
    Bright Data handles Akamai bypass, cookie management, and residential IPs.

    Setup:
      Add to .env:
        BRIGHT_DATA_API_KEY=your_api_key
        BRIGHT_DATA_ZONE=unblocker   # name of your Web Unlocker zone
    """

    API_URL = "https://api.brightdata.com/request"

    def __init__(self):
        self._session = None

    def start(self):
        if not BRIGHT_DATA_API_KEY:
            raise ValueError(
                "Bright Data API key missing. Add to .env:\n"
                "  BRIGHT_DATA_API_KEY=your_api_key\n"
                "  BRIGHT_DATA_ZONE=unblocker"
            )
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {BRIGHT_DATA_API_KEY}",
            "Content-Type": "application/json",
        })
        log.info(f"Bright Data scraper ready (API, zone={BRIGHT_DATA_ZONE})")

    def stop(self):
        if self._session:
            self._session.close()
            self._session = None
        log.info("Bright Data scraper stopped")

    def reload_cookies(self):
        pass

    def reset_cycle(self):
        pass

    @staticmethod
    def _brd_error(resp) -> str | None:
        """Return the real Bright Data error if the API failed, else None.

        Bright Data signals proxy/account failures in response headers while
        still returning HTTP 200 with an empty body (e.g. a suspended account
        returns x-brd-err-code=client_10020). Without this check those failures
        get mislabeled as an "Akamai challenge" by the body-length heuristic.
        """
        msg = resp.headers.get("x-brd-err-msg") or resp.headers.get("x-brd-error")
        code = resp.headers.get("x-brd-err-code")
        if msg:
            return f"Bright Data error{f' [{code}]' if code else ''}: {msg}"
        # Empty 200 with no recognizable page is an API failure, not a challenge.
        if resp.status_code == 200 and not resp.text.strip():
            brd_status = resp.headers.get("x-brd-status-code")
            return (
                f"Bright Data returned empty response"
                f"{f' (proxy status {brd_status})' if brd_status else ''} "
                "— check account/zone status in Bright Data dashboard"
            )
        return None

    def fetch_html(self, url: str) -> tuple[str | None, str | None]:
        """Fetch a page and return (html, error). Used for itemId resolution."""
        try:
            resp = self._session.post(
                self.API_URL,
                json={"zone": BRIGHT_DATA_ZONE, "url": url, "format": "raw", "country": "kr"},
                timeout=60,
            )
            brd_err = self._brd_error(resp)
            if brd_err:
                return None, brd_err
            if resp.status_code == 403:
                return None, "403 Forbidden"
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            content = resp.text
            if "access denied" in content.lower():
                return None, "Access Denied"
            is_challenged = (
                "sec-if-cpt-container" in content or
                len(content) < 5000
            )
            if is_challenged:
                return None, "Akamai challenge"
            return content, None
        except Exception as e:
            log.error(f"Bright Data fetch_html error: {e}")
            return None, str(e)

    def check_product(self, url: str) -> dict:
        result = {"seller": None, "price": None, "error": None, "page_source": None}
        try:
            resp = self._session.post(
                self.API_URL,
                json={"zone": BRIGHT_DATA_ZONE, "url": url, "format": "raw", "country": "kr"},
                timeout=60,
            )
            brd_err = self._brd_error(resp)
            if brd_err:
                result["error"] = brd_err
                return result
            if resp.status_code == 403:
                result["error"] = "403 Forbidden — Bright Data API 접근 거부됨"
                return result
            if resp.status_code != 200:
                result["error"] = f"Bright Data API error: HTTP {resp.status_code} — {resp.text[:200]}"
                return result

            content = resp.text
            result["page_source"] = content

            if "access denied" in content.lower():
                result["error"] = "Access Denied"
                return result

            is_challenged = (
                "sec-if-cpt-container" in content or
                (len(content) < 5000 and "판매자:" not in content)
            )
            if is_challenged:
                result["error"] = "Akamai challenge returned — check zone settings in Bright Data dashboard"
                return result

            extract_seller_price(content, result)

        except Exception as e:
            result["error"] = str(e)
            log.error(f"Bright Data scrape error: {e}")

        return result


# ---------------------------------------------------------------------------
# Scraper using curl-cffi + auto cookie refresh via Chrome AppleScript
# ---------------------------------------------------------------------------
class CoupangScraper:
    def __init__(self):
        self._session = None
        self._refreshed = False  # only auto-refresh once per run

    def start(self):
        from curl_cffi import requests as cffi_requests, CurlHttpVersion
        self._session = cffi_requests.Session(
            impersonate="chrome",
            http_version=CurlHttpVersion.V1_1,
        )
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
        self._load_cookies(get_chrome_cookies_for_coupang())
        log.info("Scraper ready (curl-cffi + Chrome cookies)")

    def _load_cookies(self, cookies: dict):
        self._session.cookies.clear()
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain=".coupang.com")

    def reload_cookies(self):
        """Re-read Chrome cookie DB without opening Chrome (cheap, no browser needed)."""
        fresh = get_chrome_cookies_for_coupang()
        if fresh:
            self._load_cookies(fresh)

    def _auto_refresh(self):
        """Open Coupang in Chrome via AppleScript to get fresh Akamai cookies (once per cycle)."""
        if self._refreshed:
            return False
        self._refreshed = True
        fresh = refresh_chrome_cookies()
        if fresh:
            self._load_cookies(fresh)
            log.info(f"Reloaded {len(fresh)} fresh cookies from Chrome")
            return True
        return False

    def stop(self):
        if self._session:
            self._session.close()
            self._session = None
        log.info("Scraper stopped")

    def reset_cycle(self):
        self._refreshed = False

    def fetch_html(self, url: str) -> tuple[str | None, str | None]:
        """Fetch a page and return (html, error). Used for itemId resolution."""
        try:
            resp = self._session.get(url, headers={"Referer": "https://www.coupang.com/"}, timeout=20)
            if resp.status_code == 403:
                return None, "403 Forbidden"
            content = resp.text
            if "access denied" in content.lower():
                return None, "Access Denied"
            is_challenged = (
                "sec-if-cpt-container" in content or
                len(content) < 5000
            )
            if is_challenged:
                return None, "Akamai challenge"
            return content, None
        except Exception as e:
            return None, str(e)

    def check_product(self, url: str) -> dict:
        """Fetch product page and extract seller/price info."""
        result = {"seller": None, "price": None, "error": None, "page_source": None}
        try:
            resp = self._session.get(url, headers={"Referer": "https://www.coupang.com/"}, timeout=20)

            if resp.status_code == 403:
                if self._auto_refresh():
                    resp = self._session.get(url, headers={"Referer": "https://www.coupang.com/"}, timeout=20)
                if resp.status_code == 403:
                    result["error"] = "403 Forbidden — 쿠키 갱신 후에도 접근 거부됨"
                    return result

            content = resp.text
            result["page_source"] = content

            if "access denied" in content.lower():
                result["error"] = "Access Denied — 쿠키 갱신 필요"
                return result

            # Detect Akamai JS challenge page or bot-detection silent failure
            is_challenged = (
                "sec-if-cpt-container" in content or
                (len(content) < 5000 and "판매자:" not in content)
            )
            if is_challenged:
                log.warning("Akamai challenge detected — falling back to Chrome")
                chrome_data = fetch_via_chrome(url)
                if chrome_data:
                    if chrome_data.get("seller"):
                        result["seller"] = chrome_data["seller"]
                        log.info(f"Seller found via Chrome JS: {result['seller']}")
                    price_raw = str(chrome_data.get("price", "")).replace(",", "")
                    if price_raw.isdigit() and int(price_raw) > 100:
                        result["price"] = f"{int(price_raw):,}원"
                    self._auto_refresh()  # refresh cookies for subsequent products
                    return result
                else:
                    result["error"] = "Chrome AppleScript fetch 실패"
                    return result

            extract_seller_price(content, result)

        except Exception as e:
            result["error"] = str(e)
            log.error(f"Scrape error: {e}")

        return result

# ---------------------------------------------------------------------------
# Scraper factory
# ---------------------------------------------------------------------------
def create_scraper():
    """Return BrightDataScraper if API key is configured, else CoupangScraper."""
    if BRIGHT_DATA_API_KEY:
        log.info("Using Bright Data Web Unlocker API")
        return BrightDataScraper()
    log.info("Using local Chrome scraper (add BRIGHT_DATA_API_KEY to .env to switch to Bright Data)")
    return CoupangScraper()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def send_slack_summary(total: int, failed: int, mine: int, others: list, failed_items: list = None, link_update_items: list = None):
    """Send one summary message per check cycle."""
    if not SLACK_WEBHOOK_URL_OLIVE:
        log.warning("Slack webhook not configured — summary skipped")
        return

    failed_items = failed_items or []
    link_update_items = link_update_items or []
    checked = total - failed
    header_emoji = "🚨" if others else "✅"
    header_text = f"{header_emoji} 쿠팡 제품 셀러 점검 결과"

    stats_line = (
        f"*전체 상품:* {total}개  |  "
        f"*확인 성공:* {checked}개  |  "
        f"*확인 실패:* {failed}개\n"
        f"*내 브랜드:* {mine}개  |  "
        f"*타 판매자:* {len(others)}개"
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": stats_line}},
    ]

    if others:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "<!here> *🚨 타 판매자 점유 상품*"},
        })
        for item in others:
            product_url = f"https://www.coupang.com/vp/products/{item['product_id']}"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*<{product_url}|{item['name']} ({item['size']})>*\n"
                        f"현재 판매자: `{item['seller']}`  |  가격: {item['price'] or '확인 불가'}\n"
                        f"현재 vendorItemId: `{item.get('current_vendor_item_id', '?')}`  |  "
                        f"목표: `{item['target_vendor_item_id']}`"
                    ),
                },
            })

    if link_update_items:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*❌ 확인 실패 상품 - 제품 링크 확인/업데이트 필요*"},
        })
        for item in link_update_items:
            product_url = f"https://www.coupang.com/vp/products/{item['product_id']}"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*<{product_url}|{item['name']} ({item['size']})>*\n"
                        f"valueId `{item['value_id']}` 를 찾을 수 없음 — 제품 URL 또는 옵션이 변경되었을 수 있습니다"
                    ),
                },
            })

    if failed_items:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*❌ 확인 실패 상품 - 차단*"},
        })
        for item in failed_items:
            product_url = f"https://www.coupang.com/vp/products/{item['product_id']}"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*<{product_url}|{item['name']} ({item['size']})>*\n"
                        f"이유: {item['reason']}"
                    ),
                },
            })

    sheet_id = re.search(r'/spreadsheets/d/([^/]+)', GOOGLE_SHEET_CSV_URL)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id.group(1)}/edit" if sheet_id else ""
    context_text = f"점검 시각: {datetime.now():%Y-%m-%d %H:%M:%S KST}"
    if sheet_url:
        context_text += f"  |  <{sheet_url}|모니터링 제품 리스트 확인>"
    context_text += "  |  <https://wing.coupang.com/vendor-inventory/list?searchKeywordType=ALL&searchKeywords=&salesMethod=ALL&productStatus=ALL&stockSearchType=ALL&shippingFeeSearchType=ALL&displayCategoryCodes=&listingStartTime=null&listingEndTime=null&saleEndDateSearchType=ALL&bundledShippingSearchType=ALL&upBundling=ALL&displayDeletedProduct=false&shippingMethod=ALL&exposureStatus=NON_ITEM_WINNER&locale=ko_KR&sortMethod=SORT_BY_ITEM_LEVEL_UNIT_SOLD&countPerPage=50&page=1|👉 쿠팡 위너 누락 제품 전체 확인 (쿠팡 센터)>"
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": context_text}],
    })

    payload = {"blocks": blocks}
    urls = [u for u in [SLACK_WEBHOOK_URL_OLIVE] if u]
    for url in urls:
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            log.info(f"Slack summary sent → {url[:60]}...")
        except Exception as e:
            log.error(f"Slack send failed ({url[:60]}...): {e}")

# ---------------------------------------------------------------------------
# Single check cycle
# ---------------------------------------------------------------------------
def run_check(scraper):
    log.info("=" * 50)
    log.info(f"Check started at {datetime.now():%Y-%m-%d %H:%M:%S}")

    try:
        products = fetch_product_list()
    except Exception as e:
        log.error(f"Failed to fetch product list: {e}")
        return

    if not products:
        log.warning("No products to check")
        return

    scraper.reset_cycle()
    scraper.reload_cookies()
    state = load_state()
    failed_count = 0
    mine_count = 0
    others = []
    failed_items = []
    link_update_items = []

    # Group by productId so we fetch each product page only once
    from collections import defaultdict
    by_pid: dict[str, list] = defaultdict(list)
    for p in products:
        by_pid[p["product_id"]].append(p)

    total = len(products)
    idx = 0
    first_call = True

    for product_id, pid_products in by_pid.items():
        # ---------- Step 1: fetch product page once per productId ----------
        if not first_call:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        first_call = False

        # Coupang shows an expanded option list only when a higher-quantity itemId
        # is in the URL (e.g. 5개/6개 options only appear when 4개 is selected).
        # Use a cached probe itemId so later runs get the full list.
        probe_key = f"_probe_{product_id}"
        probe_item_id = state.get(probe_key, {}).get("item_id")
        if probe_item_id:
            base_url = f"https://www.coupang.com/vp/products/{product_id}?itemId={probe_item_id}"
        else:
            base_url = f"https://www.coupang.com/vp/products/{product_id}"

        def _is_transient(e: str | None) -> bool:
            if not e:
                return False
            el = e.lower()
            return "timed out" in el or "akamai" in el or "captcha" in el or "protection" in el

        log.info(f"Fetching product page for productId={product_id} ({len(pid_products)} variant(s))...")
        html, err = scraper.fetch_html(base_url)
        if _is_transient(err):
            log.warning(f"  {err} — retrying in 10s...")
            time.sleep(10)
            html, err = scraper.fetch_html(base_url)

        # Stale probe itemIds can cause 403s from Coupang. If the probe-URL fetch
        # failed for any reason, drop the probe and retry with the bare product URL.
        if err and probe_item_id:
            log.warning(f"  Fetch with probe itemId={probe_item_id} failed ({err}), retrying without probe...")
            state.pop(probe_key, None)
            probe_item_id = None
            base_url = f"https://www.coupang.com/vp/products/{product_id}"
            html, err = scraper.fetch_html(base_url)
            if _is_transient(err):
                log.warning(f"  {err} — retrying in 10s...")
                time.sleep(10)
                html, err = scraper.fetch_html(base_url)

        # Verify the returned page actually belongs to our expected productId.
        # A probe itemId can become stale when Coupang reassigns the catalog item
        # to a new productId — the fetch silently serves a different product's page.
        # Also refetch if no productId is found at all (error/challenge page slipped through).
        if html and probe_item_id:
            m = re.search(r'\\"productId\\":(\d+)', html) or re.search(r'"productId"\s*:\s*(\d+)', html)
            actual_pid = m.group(1) if m else None
            if not actual_pid or actual_pid != product_id:
                if actual_pid:
                    log.warning(
                        f"  Stale probe itemId={probe_item_id}: page returned productId={actual_pid}, "
                        f"expected={product_id} — clearing probe and re-fetching"
                    )
                else:
                    log.warning(
                        f"  Probe itemId={probe_item_id}: page has no productId (error/challenge page) — "
                        f"clearing probe and re-fetching"
                    )
                state.pop(probe_key, None)
                probe_item_id = None
                base_url = f"https://www.coupang.com/vp/products/{product_id}"
                html, err = scraper.fetch_html(base_url)
                if _is_transient(err):
                    log.warning(f"  {err} — retrying in 10s...")
                    time.sleep(10)
                    html, err = scraper.fetch_html(base_url)

        # Track the last successfully resolved itemId to use as probe next run
        last_resolved_item_id = None

        for product in pid_products:
            idx += 1
            value_id = product["value_id"]
            target_vid = product["target_vendor_item_id"]
            key = f"{product_id}_{value_id}"
            prev = state.get(key, {})

            log.info(f"[{idx}/{total}] {product['name']} ({product['size']})")

            if err:
                log.warning(f"  Product page load failed: {err}")
                failed_count += 1
                failed_items.append({**product, "reason": f"상품 페이지 로드 실패: {err}"})
                state[key] = prev
                continue

            # ---------- Step 2: resolve option (vendorItemId) ----------
            option = resolve_option_from_html(html, value_id)
            if not option:
                log.warning(f"  valueId {value_id} not found in product options")
                available = [
                    f"{m.group(1)}={m.group(2)}"
                    for m in re.finditer(
                        r'valueId\\":\\"([^"\\]+)\\",\\"name\\":\\"([^"\\]+)\\"', html
                    )
                ]
                if available:
                    log.warning(f"  Available valueIds: {', '.join(available[:12])}")
                failed_count += 1
                link_update_items.append({**product, "value_id": value_id})
                state[key] = prev
                continue

            current_vid = option["vendor_item_id"]
            item_id = option["item_id"]
            last_resolved_item_id = item_id  # update probe for next run
            log.info(f"  현재 vendorItemId: {current_vid} | 목표: {target_vid}")

            was_lost = prev.get("lost", False)

            if current_vid == target_vid:
                # ---------- Buy box is ours ----------
                mine_count += 1
                if was_lost:
                    log.info(f"  RECOVERED")
                    prev = {"lost": False}
                else:
                    log.info(f"  OK")
                    prev["lost"] = False
            else:
                # ---------- Buy box lost — fetch item page to get reseller name ----------
                log.warning(f"  BUY BOX LOST (vendorItemId mismatch)")
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                # Use lowercase itemId + vendorItemId so Bright Data renders the correct option
                item_url = (
                    f"https://www.coupang.com/vp/products/{product_id}"
                    f"?itemId={item_id}&vendorItemId={current_vid}"
                )
                info = scraper.check_product(item_url)

                if info["error"]:
                    log.warning(f"  Could not get reseller info: {info['error']}")
                    reseller = "확인 불가"
                    price = None
                else:
                    reseller = info["seller"] or "확인 불가"
                    price = info["price"]
                    log.warning(f"  현재 판매자: {reseller} | 가격: {price}")

                others.append({**product, "seller": reseller, "price": price,
                               "current_vendor_item_id": current_vid})
                prev.update({"lost": True, "seller": reseller, "price": price,
                             "since": datetime.now().isoformat()})

            state[key] = prev

        # Save the highest-qty visible item as probe for the next run.
        # find_highest_qty_item_id scans itemBasicInfo (not just optionRows),
        # so it can find 5개/6개 even when they don't appear in the option dropdown.
        if html:
            best_probe = find_highest_qty_item_id(html) or last_resolved_item_id
            if best_probe:
                old_probe = state.get(probe_key, {}).get("item_id")
                if best_probe != old_probe:
                    log.info(f"  Probe updated: {old_probe} → {best_probe}")
                state[probe_key] = {"item_id": best_probe}
        elif last_resolved_item_id:
            state[probe_key] = {"item_id": last_resolved_item_id}

    save_state(state)
    send_slack_summary(total, failed_count, mine_count, others, failed_items, link_update_items)
    log.info(f"Check complete at {datetime.now():%H:%M:%S}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daemon_mode = "--daemon" in sys.argv

    if not SLACK_WEBHOOK_URL_OLIVE:
        log.error("No Slack webhook configured — add SLACK_WEBHOOK_URL_OLIVE to .env")
        sys.exit(1)

    scraper = create_scraper()

    def graceful_shutdown(sig, frame):
        log.info("Shutting down...")
        scraper.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    scraper.start()

    if daemon_mode:
        log.info(f"Daemon mode — checking every {CHECK_INTERVAL_MIN[0]}–{CHECK_INTERVAL_MIN[1]} min (randomized). Ctrl+C to stop.")
        while True:
            try:
                run_check(scraper)
            except Exception as e:
                log.error(f"Check cycle error: {e}")
            wait_min = random.uniform(*CHECK_INTERVAL_MIN)
            next_run = datetime.now().timestamp() + wait_min * 60
            log.info(f"Next check in {wait_min:.1f} min (at {datetime.fromtimestamp(next_run):%H:%M:%S})")
            time.sleep(wait_min * 60)
    else:
        try:
            run_check(scraper)
        finally:
            scraper.stop()


if __name__ == "__main__":
    main()
