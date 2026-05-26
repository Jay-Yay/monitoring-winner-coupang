#!/usr/bin/env python3
"""
Quick test — resolve itemId/vendorItemId for a valueId, then check seller if needed.

Usage:
    python3 test_single.py
    python3 test_single.py <productId> <valueId> [target_vendorItemId]
    python3 test_single.py "https://www.coupang.com/vp/products/...?ItemId=..."
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from monitor import create_scraper, resolve_option_from_html

# Default: ampoule 60ml × 1개
DEFAULT_PRODUCT_ID = "8227687108"
DEFAULT_VALUE_ID = "1006101947,1626145991"
DEFAULT_TARGET_VID = "93810644260"   # 주식회사 웨이브메트릭's vendorItemId for this option
DEBUG_HTML = Path(__file__).parent / "debug_page.html"


def test_by_value_id(product_id: str, value_id: str, target_vid: str):
    scraper = create_scraper()
    scraper.start()
    try:
        print(f"Product ID:          {product_id}")
        print(f"Value ID:            {value_id}")
        print(f"Target vendorItemId: {target_vid}")
        print("-" * 55)

        # Step 1: fetch product page and resolve option
        print("Step 1: fetching product page...")
        base_url = f"https://www.coupang.com/vp/products/{product_id}"
        html, err = scraper.fetch_html(base_url)
        if err:
            print(f"FAIL — could not fetch product page: {err}")
            return

        option = resolve_option_from_html(html, value_id)
        if not option:
            print(f"FAIL — valueId {value_id} not found in product options")
            print("Available options:")
            import re
            for m in re.finditer(r'valueId\\":\\"([^"\\]+)\\",\\"name\\":\\"([^"\\]+)\\"', html):
                print(f"  valueId={m.group(1)}  name={m.group(2)}")
            return

        current_vid = option["vendor_item_id"]
        item_id = option["item_id"]
        print(f"  itemId resolved:         {item_id}")
        print(f"  current vendorItemId:    {current_vid}")

        # Step 2: compare
        if current_vid == target_vid:
            print(f"\nBuy box is YOURS (vendorItemId matches)")
        else:
            print(f"\nBuy box LOST — fetching reseller name...")
            item_url = f"https://www.coupang.com/vp/products/{product_id}?ItemId={item_id}"
            result = scraper.check_product(item_url)
            print(f"  Reseller: {result['seller']}")
            print(f"  Price:    {result['price']}")
            if result["error"]:
                print(f"  Error:    {result['error']}")
            else:
                print(f"\nBuy box held by: {result['seller']}")
            if not result["seller"] and result.get("page_source"):
                DEBUG_HTML.write_text(result["page_source"], encoding="utf-8")
                print(f"Page source saved to: {DEBUG_HTML}")
    finally:
        scraper.stop()


def test_by_url(url: str):
    """Legacy: test with a direct ItemId URL (seller name extraction only)."""
    scraper = create_scraper()
    scraper.start()
    try:
        print(f"URL: {url}")
        print("-" * 55)
        result = scraper.check_product(url)
        print(f"Seller: {result['seller']}")
        print(f"Price:  {result['price']}")
        print(f"Error:  {result['error']}")
        if not result["seller"] and result.get("page_source"):
            DEBUG_HTML.write_text(result["page_source"], encoding="utf-8")
            print(f"Page source saved to: {DEBUG_HTML}")
    finally:
        scraper.stop()


def main():
    args = sys.argv[1:]

    if args and args[0].startswith("http"):
        test_by_url(args[0])
    elif len(args) >= 2:
        product_id = args[0]
        value_id = args[1]
        target_vid = args[2] if len(args) > 2 else ""
        test_by_value_id(product_id, value_id, target_vid)
    else:
        test_by_value_id(DEFAULT_PRODUCT_ID, DEFAULT_VALUE_ID, DEFAULT_TARGET_VID)


if __name__ == "__main__":
    main()
