#!/usr/bin/env python3
"""
fill_sheet.py — Auto-fill missing valueId and fix sizeAndCount in Google Sheet

Reads the Google Sheet, finds rows with:
  - Empty column D (valueId)  → fetches Coupang page, maps vendorItemId → valueId
  - Bare 'n개' in column B (sizeAndCount) → prepends size from product name

Usage:
    python3 fill_sheet.py             # apply updates
    python3 fill_sheet.py --dry-run   # preview without writing

One-time setup:
    pip install gspread google-auth-oauthlib google-auth-httplib2 requests

    Google Sheets API auth (pick one):
    A) OAuth (easiest for personal use):
       1. console.cloud.google.com → select/create project
       2. APIs & Services → Library → Google Sheets API → Enable
       3. APIs & Services → Credentials → + Create Credentials → OAuth client ID
          → Application type: Desktop app → Create → Download JSON
       4. Save downloaded JSON as  credentials.json  in this directory
       First run opens a browser for consent; subsequent runs reuse token.json.

    B) Service account (good for automation):
       1. Same project → Credentials → + Create Credentials → Service account
       2. Download the key JSON → save as  service_account.json  here
       3. Share the Google Sheet with the service account's email (Editor role)
       Set USE_SERVICE_ACCOUNT = True below.

    Coupang scraping (pick one):
    - Set BRIGHT_DATA_API_KEY in .env  (preferred — bypasses Akamai reliably)
    - OR open coupang.com in Chrome (~30s) then run without the key
      (uses curl-cffi + Chrome cookies; requires: pip install curl-cffi browser_cookie3)
"""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SPREADSHEET_ID = "1DrPiq_WQ-Hkw17PzrXGmF2YsUkw2hfdJf1Rv4FPupq0"
SHEET_GID = 873878318

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
SERVICE_ACCOUNT_FILE = BASE_DIR / "service_account.json"
TOKEN_FILE = BASE_DIR / "token.json"

USE_SERVICE_ACCOUNT = SERVICE_ACCOUNT_FILE.exists()  # auto-detect

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MIN_DELAY = 2   # seconds between Coupang page fetches
MAX_DELAY = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
BRIGHT_DATA_API_KEY = ""
BRIGHT_DATA_ZONE = "web_unlocker1"


def load_env():
    global BRIGHT_DATA_API_KEY, BRIGHT_DATA_ZONE
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    BRIGHT_DATA_API_KEY = os.environ.get("BRIGHT_DATA_API_KEY", "")
    BRIGHT_DATA_ZONE = os.environ.get("BRIGHT_DATA_ZONE", "web_unlocker1")


load_env()

# ---------------------------------------------------------------------------
# Google Sheets auth
# ---------------------------------------------------------------------------
def get_worksheet():
    try:
        import gspread
    except ImportError:
        log.error("gspread not installed. Run: pip install gspread google-auth-oauthlib")
        sys.exit(1)

    if USE_SERVICE_ACCOUNT:
        try:
            from google.oauth2.service_account import Credentials as SACredentials
        except ImportError:
            log.error("google-auth not installed. Run: pip install google-auth")
            sys.exit(1)
        creds = SACredentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
        )
        log.info("Authenticated via service account")
    else:
        # OAuth flow
        try:
            import pickle
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            log.error(
                "google-auth-oauthlib not installed.\n"
                "Run: pip install google-auth-oauthlib google-auth-httplib2"
            )
            sys.exit(1)

        creds = None
        if TOKEN_FILE.exists():
            with open(TOKEN_FILE, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not CREDENTIALS_FILE.exists():
                    log.error(
                        "credentials.json not found.\n"
                        "See setup instructions at the top of this file."
                    )
                    sys.exit(1)
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        log.info("Authenticated via OAuth")

    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.get_worksheet_by_id(SHEET_GID)
    log.info(f"Sheet: '{spreadsheet.title}' / tab: '{worksheet.title}'")
    return worksheet


# ---------------------------------------------------------------------------
# Coupang page fetch
# ---------------------------------------------------------------------------
def fetch_html_brightdata(url: str) -> tuple[str | None, str | None]:
    try:
        resp = requests.post(
            "https://api.brightdata.com/request",
            headers={
                "Authorization": f"Bearer {BRIGHT_DATA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"zone": BRIGHT_DATA_ZONE, "url": url, "format": "raw", "country": "kr"},
            timeout=60,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        content = resp.text
        if len(content) < 5000:
            err_code = resp.headers.get("x-brd-err-code", "")
            err_msg = resp.headers.get("x-brd-err-msg", "empty response")
            return None, f"Bright Data: {err_code} {err_msg}".strip()
        return content, None
    except Exception as e:
        return None, str(e)


def make_cffi_session():
    try:
        from curl_cffi import requests as cffi_requests, CurlHttpVersion
    except ImportError:
        log.error("curl-cffi not installed. Run: pip install curl-cffi browser_cookie3")
        return None

    session = cffi_requests.Session(
        impersonate="chrome", http_version=CurlHttpVersion.V1_1
    )
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    # Load Chrome cookies
    try:
        import browser_cookie3
        chrome_base = Path.home() / "Library/Application Support/Google/Chrome"
        best_cookies: dict = {}
        for profile_dir in sorted(chrome_base.iterdir()):
            cookie_file = profile_dir / "Cookies"
            if not cookie_file.exists():
                continue
            try:
                jar = browser_cookie3.chrome(
                    cookie_file=str(cookie_file), domain_name="coupang.com"
                )
                cookies = {c.name: c.value for c in jar if c.value}
                if len(cookies) > len(best_cookies):
                    best_cookies = cookies
            except Exception:
                continue
        if best_cookies:
            for name, value in best_cookies.items():
                session.cookies.set(name, value, domain=".coupang.com")
            log.info(f"Loaded {len(best_cookies)} Chrome cookies for Coupang")
        else:
            log.warning("No Coupang cookies found in Chrome — visit coupang.com in Chrome first")
    except ImportError:
        log.warning("browser_cookie3 not installed — no Chrome cookies loaded")

    return session


def fetch_html_cffi(url: str, session) -> tuple[str | None, str | None]:
    try:
        resp = session.get(url, headers={"Referer": "https://www.coupang.com/"}, timeout=20)
        if resp.status_code == 403:
            return None, "403 Forbidden (Akamai blocked)"
        content = resp.text
        if len(content) < 5000:
            return None, "Response too short — Akamai challenge?"
        return content, None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# HTML parsing: build vendorItemId → valueId map
# ---------------------------------------------------------------------------
def build_vendor_to_value_map(html: str) -> dict[str, str]:
    """
    Parses attributeVendorItemMap from Coupang product page HTML.
    Returns {vendorItemId: valueId} for all options on the page.

    The HTML contains escaped JSON like:
      \"1006017379,1626146022\":{...\"vendorItemId=95182509117\"...}
    """
    result: dict[str, str] = {}
    # Match keys like \"1006017379,1626146022\":{
    for m in re.finditer(r'\\\"([\d,]+)\\\":\{', html):
        value_id = m.group(1)
        chunk = html[m.start(): m.start() + 2000]
        vid_m = re.search(r'vendorItemId=(\d+)', chunk)
        if vid_m:
            result[vid_m.group(1)] = value_id
    return result


# ---------------------------------------------------------------------------
# sizeAndCount fixer
# ---------------------------------------------------------------------------
SIZE_UNIT_RE = re.compile(r'\d+\s*(?:ml|g|mg|kg|L|정|포|매입|팩)', re.IGNORECASE)


def fix_size_count(product_name: str, current: str) -> str:
    """
    If sizeAndCount is a bare count like '1개', '2개', extract the size
    measurement from the product name and return 'Xunit × n개'.
    Otherwise returns current unchanged.
    """
    current = current.strip()
    if not re.match(r'^\d+개$', current):
        return current  # already has correct format or different pattern (세트, etc.)

    parts = [p.strip() for p in product_name.split(',')]
    for part in parts[1:]:  # skip the base product name
        if SIZE_UNIT_RE.search(part) and part != current:
            return f"{part} × {current}"

    return current  # could not find a size part


# ---------------------------------------------------------------------------
# Column index → spreadsheet letter (A, B, ..., Z, AA, ...)
# ---------------------------------------------------------------------------
def col_letter(idx: int) -> str:
    result = ""
    n = idx + 1  # 1-based
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
HEADER_COLS = {
    "productName": None,
    "sizeAndCount": None,
    "productId": None,
    "valueId": None,
    "vendorItemIdMy": None,   # sheet may have trailing space
    "urlWithProductIdOnly": None,
}


def find_header(row: list[str]) -> dict[str, int] | None:
    """Return {col_name: index} if this row looks like our expected header."""
    indices: dict[str, int] = {}
    for i, cell in enumerate(row):
        stripped = cell.strip()
        for key in HEADER_COLS:
            if stripped == key:
                indices[key] = i
    if len(indices) >= 5:  # need at least 5 of 6 columns
        return indices
    return None


def run(dry_run: bool = False):
    worksheet = get_worksheet()

    all_values = worksheet.get_all_values()
    if not all_values:
        log.error("Sheet appears empty")
        return

    # Locate the header row (there may be a repeated header mid-sheet)
    header_indices: dict[str, int] | None = None
    for row in all_values[:5]:
        header_indices = find_header(row)
        if header_indices:
            break
    if not header_indices:
        log.error(f"Could not find expected header columns. First row: {all_values[0]}")
        return

    ci = header_indices  # short alias
    log.info(f"Column mapping: { {k: col_letter(v) for k, v in ci.items()} }")

    # Scan all rows, skip header rows, collect rows needing update
    to_update: list[dict] = []
    for row_idx, row in enumerate(all_values[1:], start=2):  # row_idx is 1-based (sheet row)
        if not row:
            continue
        # Skip any repeated header row
        if find_header(row):
            continue

        def cell(col_name: str) -> str:
            idx = ci.get(col_name, -1)
            return row[idx].strip() if idx >= 0 and idx < len(row) else ""

        product_name = cell("productName")
        size_count = cell("sizeAndCount")
        product_id = cell("productId")
        value_id = cell("valueId")
        vendor_item_id = cell("vendorItemIdMy")
        url = cell("urlWithProductIdOnly")

        if not product_name or not product_id or not url:
            continue

        fixed_size = fix_size_count(product_name, size_count)
        needs_value_id = not value_id and bool(vendor_item_id)
        needs_size_fix = fixed_size != size_count

        if needs_value_id or needs_size_fix:
            to_update.append({
                "row_idx": row_idx,
                "product_name": product_name,
                "size_count": size_count,
                "fixed_size": fixed_size,
                "product_id": product_id,
                "value_id": value_id,
                "vendor_item_id": vendor_item_id,
                "url": url,
                "needs_value_id": needs_value_id,
                "needs_size_fix": needs_size_fix,
                "resolved_value_id": None,  # filled in below
            })

    log.info(f"Rows needing update: {len(to_update)}")
    if not to_update:
        log.info("Nothing to do — all rows already complete!")
        return

    # --- Fetch Coupang pages to resolve valueId ---
    # Group rows needing valueId by URL (fetch each product page once)
    url_to_rows: dict[str, list[dict]] = {}
    for item in to_update:
        if item["needs_value_id"]:
            url_to_rows.setdefault(item["url"], []).append(item)

    if url_to_rows:
        log.info(f"Need to fetch {len(url_to_rows)} unique product page(s)")

        cffi_session = None
        if not BRIGHT_DATA_API_KEY:
            log.info("BRIGHT_DATA_API_KEY not set — using curl-cffi + Chrome cookies")
            cffi_session = make_cffi_session()

        # Build a global vendorItemId → valueId map from all pages
        global_vendor_map: dict[str, str] = {}

        for i, (url, rows) in enumerate(url_to_rows.items(), start=1):
            log.info(f"[{i}/{len(url_to_rows)}] Fetching {url}")
            if BRIGHT_DATA_API_KEY:
                html, err = fetch_html_brightdata(url)
            elif cffi_session:
                html, err = fetch_html_cffi(url, cffi_session)
            else:
                log.error("No scraper available. Set BRIGHT_DATA_API_KEY or install curl-cffi.")
                break

            if err:
                log.warning(f"  Fetch failed: {err}")
            else:
                page_map = build_vendor_to_value_map(html)
                log.info(f"  Found {len(page_map)} option mapping(s): {page_map}")
                global_vendor_map.update(page_map)

            if i < len(url_to_rows):
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                log.info(f"  Waiting {delay:.1f}s...")
                time.sleep(delay)

        if cffi_session:
            cffi_session.close()

        # Assign resolved valueId to each row
        for item in to_update:
            if item["needs_value_id"] and item["vendor_item_id"]:
                item["resolved_value_id"] = global_vendor_map.get(item["vendor_item_id"])
                if not item["resolved_value_id"]:
                    log.warning(
                        f"  Row {item['row_idx']}: vendorItemId {item['vendor_item_id']} "
                        f"not found in page ({item['product_name'][:50]})"
                    )

    # --- Build batch updates ---
    batch: list[dict] = []

    for item in to_update:
        row = item["row_idx"]

        if item["needs_size_fix"]:
            col = col_letter(ci["sizeAndCount"])
            log.info(
                f"  Row {row} [{col}] sizeAndCount: "
                f"'{item['size_count']}' → '{item['fixed_size']}'"
            )
            batch.append({"range": f"{col}{row}", "values": [[item["fixed_size"]]]})

        if item["needs_value_id"] and item["resolved_value_id"]:
            col = col_letter(ci["valueId"])
            log.info(
                f"  Row {row} [{col}] valueId: "
                f"vendorItemId={item['vendor_item_id']} → '{item['resolved_value_id']}'"
            )
            batch.append({"range": f"{col}{row}", "values": [[item["resolved_value_id"]]]})

    log.info(f"\nTotal cell updates prepared: {len(batch)}")

    if dry_run:
        log.info("DRY RUN — no changes written to sheet")
        return

    if not batch:
        log.info("No resolvable updates to write")
        return

    worksheet.batch_update(batch, value_input_option="RAW")
    log.info(f"Done — wrote {len(batch)} cell(s) to Google Sheet")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info("=== DRY RUN MODE ===")
    run(dry_run=dry_run)
