"""
GoWholesale $15 Lowest-Ask Monitor — Pushover Edition
-------------------------------------------------------
Monitors GoWholesale's new listings API and sends a
Pushover push notification when any item's lowest ask
is exactly $15.00.

Environment variables needed in Render:
  PUSHOVER_USER_KEY    - your Pushover User Key
  PUSHOVER_API_TOKEN   - your Pushover Application API Token
  CHECK_INTERVAL       - seconds between checks (default: 10)
  TARGET_PRICE         - the exact lowest ask to flag (default: 15.00)
  ALERT_ON_FIRST_RUN   - "1" (default) alerts on $15 items already on the
                         page at startup; "0" seeds them silently
"""

import os
import time
import random
import logging
import hashlib

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
PUSHOVER_USER      = os.environ["PUSHOVER_USER_KEY"]
PUSHOVER_TOKEN     = os.environ["PUSHOVER_API_TOKEN"]
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "10"))
TARGET_PRICE       = float(os.environ.get("TARGET_PRICE", "15.00"))
ALERT_ON_FIRST_RUN = os.environ.get("ALERT_ON_FIRST_RUN", "1") == "1"

API_URL      = "https://gowholesale.com/api/catalog/product"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://gowholesale.com",
    "Referer": "https://gowholesale.com/search?in-stock=0&sort=newestListings",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

PAYLOAD = {
    "from": 0,
    "size": 48,
    "sort": [{"created_at": {"order": "desc"}}],
    "query": {
        "bool": {
            "filter": {
                "bool": {
                    "must": [{"range": {"stock.qty": {"gte": 1}}}],
                    "must_not": [{"exists": {"field": "master_product_sku"}}]
                }
            }
        }
    },
    "_source": ["name", "sku", "url_key", "stock", "created_at", "price", "msrp"],
}

# Alert key = sku + price, so a repriced item can trigger again
alerted: set[str] = set()
first_run: bool = True


def alert_key(sku: str, price: float) -> str:
    return hashlib.md5(f"{sku.strip().lower()}|{price:.2f}".encode()).hexdigest()


def country_amount(source: dict, field: str) -> float | None:
    """
    Extract a dollar amount from a country-keyed field.
    GoWholesale's API returns e.g. price={"US": 15} and
    msrp={"US": 29.99}, sometimes with other countries like
    {"US": 119, "CA": 164.27}. Prefer the US value; fall back to
    the lowest of any country's value. Returns None if absent.
    """
    value = source.get(field) or {}
    if not isinstance(value, dict):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    us = value.get("US")
    if us is not None:
        try:
            return float(us)
        except (TypeError, ValueError):
            pass
    candidates = []
    for val in value.values():
        try:
            candidates.append(float(val))
        except (TypeError, ValueError):
            continue
    return min(candidates) if candidates else None


def lowest_ask(source: dict) -> float | None:
    return country_amount(source, "price")


def is_target_price(price: float) -> bool:
    """Exactly $15.00 (penny-safe float comparison)."""
    return abs(price - TARGET_PRICE) < 0.005


def send_notification(name: str, price: float, url: str, qty, msrp: float | None) -> None:
    msg = f"{name}\nLowest ask: ${price:.2f}"
    if msrp:
        msg += f"\nMSRP: ${msrp:,.2f} ({price / msrp * 100:.0f}% of MSRP)"
    if qty not in (None, ""):
        msg += f"\nQty: {qty}"
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": f"💲 ${TARGET_PRICE:.2f} Lowest Ask on GoWholesale!",
        "message": msg,
        "url": url,
        "url_title": "View on GoWholesale",
        "priority": 1,
        "sound": "cashregister",
    }
    resp = requests.post(PUSHOVER_URL, data=data, timeout=10)
    resp.raise_for_status()
    log.info(f"Pushover sent: {name} @ ${price:.2f}")


def fetch_listings() -> list[dict]:
    try:
        resp = requests.post(API_URL, headers=API_HEADERS, json=PAYLOAD, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        listings = []
        for hit in hits:
            source  = hit.get("_source", {})
            sku     = hit.get("_id") or source.get("sku", "")
            name    = source.get("name", "")
            url_key = source.get("url_key", "")
            url     = f"https://gowholesale.com/p/{url_key}" if url_key else "https://gowholesale.com/search?in-stock=0&sort=newestListings"
            qty     = (source.get("stock") or {}).get("qty", "")
            price   = lowest_ask(source)
            msrp    = country_amount(source, "msrp")
            listings.append({"sku": sku, "name": name, "url": url, "qty": qty, "price": price, "msrp": msrp})
        return listings
    except Exception as e:
        log.error(f"API error: {e}")
        return []


def check_once() -> None:
    global first_run
    log.info("Fetching listings...")
    listings = fetch_listings()
    log.info(f"Got {len(listings)} listings")

    seeding_quietly = first_run and not ALERT_ON_FIRST_RUN

    for item in listings:
        price = item["price"]
        if price is None or not is_target_price(price):
            continue

        key = alert_key(item["sku"], price)
        if key in alerted:
            continue

        if seeding_quietly:
            alerted.add(key)
            log.info(f"Seeded (no alert): {item['name']} @ ${price:.2f}")
            continue

        try:
            send_notification(item["name"], price, item["url"], item["qty"], item["msrp"])
            alerted.add(key)
        except Exception as e:
            log.error(f"Notification failed: {e}")

    first_run = False


def main() -> None:
    log.info(f"GoWholesale ${TARGET_PRICE:.2f} Lowest-Ask Monitor started (Pushover).")
    log.info(f"Checking every {CHECK_INTERVAL}s | Alert on first run: {ALERT_ON_FIRST_RUN}")
    while True:
        try:
            check_once()
        except Exception as e:
            log.error(f"Error: {e}")
        sleep = CHECK_INTERVAL + random.uniform(1.5, 4.0)
        log.info(f"Next check in {sleep:.1f}s...")
        time.sleep(sleep)


if __name__ == "__main__":
    main()
