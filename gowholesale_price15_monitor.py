"""
GoWholesale Lowest-Ask Monitor — Pushover Edition (multi-price, hardened)
--------------------------------------------------------------------------
Monitors GoWholesale's new listings API and sends a Pushover push
notification when any item's lowest ask exactly matches one of
TARGET_PRICES (default $13, $14, $15).

Environment variables in Render:
  PUSHOVER_USER_KEY    - your Pushover User Key (required)
  PUSHOVER_API_TOKEN   - your Pushover Application API Token (required)
  TARGET_PRICES        - comma-separated prices to flag (default: 13,14,15)
  TARGET_PRICE         - old single-price setting, used only if TARGET_PRICES is not set
  CHECK_INTERVAL       - seconds between checks (default: 10, minimum 1)
  ALERT_ON_FIRST_RUN   - "1" (default) alerts on matching items already on the
                         page at startup; "0" seeds them silently
  LOG_PRICES           - "1" logs every listing's price each check (for verifying)
"""

import os
import sys
import time
import random
import signal
import logging
import hashlib
import traceback
from collections import OrderedDict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Safe parsing helpers ──────────────────────────────────────────────────────
def to_money(value) -> float | None:
    """15, '15', '$15.00', '1,015.00' -> float. None if it can't be read."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if num != num or num < 0:  # NaN or negative
        return None
    return round(num, 2)


def parse_prices(raw: str) -> set[float]:
    prices = set()
    for part in raw.split(","):
        p = to_money(part)
        if p is not None:
            prices.add(p)
        elif part.strip():
            log.warning(f"Ignoring bad value {part!r} in TARGET_PRICES")
    return prices


def env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(float(raw)) if raw else default
    except ValueError:
        log.warning(f"{name}={raw!r} isn't a number, using {default}")
        value = default
    return max(value, minimum)


def env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Config ────────────────────────────────────────────────────────────────────
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "").strip()

_prices_raw = os.environ.get("TARGET_PRICES") or os.environ.get("TARGET_PRICE") or "13,14,15"
TARGET_PRICES = parse_prices(_prices_raw)
if not TARGET_PRICES:
    log.warning("No valid target prices found, falling back to 13,14,15")
    TARGET_PRICES = {13.0, 14.0, 15.0}

CHECK_INTERVAL     = env_int("CHECK_INTERVAL", 10, 1)
ALERT_ON_FIRST_RUN = env_flag("ALERT_ON_FIRST_RUN", "1")
LOG_PRICES         = env_flag("LOG_PRICES")

MAX_REMEMBERED       = 5000  # cap on remembered alerts so memory never grows forever
MAX_ALERTS_PER_CHECK = 5     # more than this at once = send one summary instead
MAX_SEND_ATTEMPTS    = 3     # give up on one alert after this many failed sends
FAIL_ALERT_AFTER     = 30    # warn you after this many failed checks in a row
MAX_BACKOFF          = 300   # longest wait (seconds) while GoWholesale is failing

API_URL      = "https://gowholesale.com/api/catalog/product"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
NEW_LISTINGS = "https://gowholesale.com/search?in-stock=0&sort=newestListings"

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://gowholesale.com",
    "Referer": NEW_LISTINGS,
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


# ── HTTP session with automatic retries ───────────────────────────────────────
def make_session() -> requests.Session:
    retry = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


session = make_session()

# ── State ─────────────────────────────────────────────────────────────────────
# Alert key = sku + price, so a repriced item can trigger again
alerted: "OrderedDict[str, bool]" = OrderedDict()
send_attempts: dict[str, int] = {}
first_run = True
fail_streak = 0
running = True


def alert_key(sku: str, price: float) -> str:
    return hashlib.md5(f"{sku.strip().lower()}|{price:.2f}".encode()).hexdigest()


def remember(key: str) -> None:
    alerted[key] = True
    alerted.move_to_end(key)
    send_attempts.pop(key, None)
    while len(alerted) > MAX_REMEMBERED:
        alerted.popitem(last=False)


def country_amount(source: dict, field: str) -> float | None:
    """
    Extract a dollar amount from a country-keyed field.
    GoWholesale's API returns e.g. price={"US": 15} and msrp={"US": 29.99},
    sometimes with other countries like {"US": 119, "CA": 164.27}.
    Prefer the US value; fall back to the lowest of any country's value.
    """
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        return to_money(value)
    us = to_money(value.get("US"))
    if us is not None:
        return us
    candidates = [v for v in (to_money(x) for x in value.values()) if v is not None]
    return min(candidates) if candidates else None


def lowest_ask(source: dict) -> float | None:
    return country_amount(source, "price")


def is_target_price(price: float | None) -> bool:
    return price is not None and any(abs(price - t) < 0.005 for t in TARGET_PRICES)


# ── Pushover ──────────────────────────────────────────────────────────────────
def pushover(title: str, message: str, url: str | None = None,
             priority: int = 1, sound: str = "cashregister") -> bool:
    """Send a Pushover message. Returns True on success, never raises."""
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title[:250],
        "message": message[:1000] or "(empty)",
        "priority": priority,
        "sound": sound,
    }
    if url:
        data["url"] = url[:512]
        data["url_title"] = "View on GoWholesale"
    try:
        resp = session.post(PUSHOVER_URL, data=data, timeout=(10, 15))
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and body.get("status") == 1:
            return True
        log.error(f"Pushover rejected message ({resp.status_code}): {body or resp.text[:200]}")
    except Exception as e:
        log.error(f"Pushover send failed: {e}")
    return False


def send_notification(item: dict) -> bool:
    price, msrp, qty = item["price"], item["msrp"], item["qty"]
    msg = f"{item['name']}\nLowest ask: ${price:.2f}"
    if msrp:
        msg += f"\nMSRP: ${msrp:,.2f} ({price / msrp * 100:.0f}% of MSRP)"
    if qty not in (None, ""):
        msg += f"\nQty: {qty}"
    ok = pushover(f"💲 ${price:.2f} Lowest Ask on GoWholesale!", msg, item["url"])
    if ok:
        log.info(f"Pushover sent: {item['name']} @ ${price:.2f}")
    return ok


# ── GoWholesale fetch ─────────────────────────────────────────────────────────
def fetch_listings() -> list[dict] | None:
    """Returns the listings, or None if this check failed."""
    try:
        resp = session.post(API_URL, headers=API_HEADERS, json=PAYLOAD, timeout=(10, 20))
    except requests.RequestException as e:
        log.warning(f"Network error: {e}")
        return None
    if resp.status_code != 200:
        log.warning(f"GoWholesale API returned status {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("GoWholesale returned something that isn't JSON (site down or blocking?)")
        return None

    hits = (data.get("hits") or {}).get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        log.warning("GoWholesale response wasn't in the expected format")
        return None

    listings = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source")
        if not isinstance(source, dict):
            continue
        sku = str(hit.get("_id") or source.get("sku") or "").strip()
        if not sku:
            continue
        url_key = str(source.get("url_key") or "").strip()
        stock = source.get("stock")
        listings.append({
            "sku": sku,
            "name": str(source.get("name") or "(no name)").strip(),
            "url": f"https://gowholesale.com/p/{url_key}" if url_key else NEW_LISTINGS,
            "qty": stock.get("qty", "") if isinstance(stock, dict) else "",
            "price": lowest_ask(source),
            "msrp": country_amount(source, "msrp"),
            "raw_price": source.get("price"),
        })
    return listings


# ── One check ─────────────────────────────────────────────────────────────────
def check_once() -> None:
    global first_run, fail_streak

    listings = fetch_listings()

    if listings is None:
        fail_streak += 1
        if fail_streak == FAIL_ALERT_AFTER:
            pushover("⚠️ Price bot can't reach GoWholesale",
                     f"{fail_streak} failed checks in a row. Check the Render logs.",
                     priority=0, sound="falling")
        return

    if fail_streak >= FAIL_ALERT_AFTER:
        pushover("✅ Price bot is back", "GoWholesale is responding again.",
                 priority=0, sound="magic")
    fail_streak = 0

    if LOG_PRICES:
        for item in listings:
            log.info(f"PRICE {item['price']} | raw={item['raw_price']} | {item['name'][:60]}")

    if not listings:
        log.info("Got 0 listings this round")
        return

    seeding_quietly = first_run and not ALERT_ON_FIRST_RUN
    first_run = False

    matches = []
    for item in listings:
        if not is_target_price(item["price"]):
            continue
        key = alert_key(item["sku"], item["price"])
        if key in alerted:
            continue
        if seeding_quietly:
            remember(key)
            log.info(f"Seeded (no alert): {item['name']} @ ${item['price']:.2f}")
            continue
        matches.append((key, item))

    if not matches:
        return

    if len(matches) > MAX_ALERTS_PER_CHECK:
        lines = [f"${i['price']:.2f} | {i['name'][:60]}" for _, i in matches[:10]]
        if pushover(f"💲 {len(matches)} price matches on GoWholesale", "\n".join(lines), NEW_LISTINGS):
            for key, _ in matches:
                remember(key)
            log.info(f"Sent summary alert for {len(matches)} matches")
        return

    for key, item in matches:
        if send_notification(item):
            remember(key)
        else:
            tries = send_attempts.get(key, 0) + 1
            send_attempts[key] = tries
            if tries >= MAX_SEND_ATTEMPTS:
                log.error(f"Giving up on alert after {tries} tries: {item['name'][:80]}")
                remember(key)


# ── Main loop ─────────────────────────────────────────────────────────────────
def handle_stop(signum, frame) -> None:
    global running
    log.info("Stop signal received, shutting down cleanly...")
    running = False


def main() -> None:
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        log.error("PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN must be set in Render's Environment tab.")
        sys.exit(1)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    prices_text = ", ".join(f"${p:.2f}" for p in sorted(TARGET_PRICES))
    log.info("GoWholesale Lowest-Ask Monitor started (Pushover, hardened).")
    log.info(f"Target prices: {prices_text}")
    log.info(f"Checking every {CHECK_INTERVAL}s | Alert on first run: {ALERT_ON_FIRST_RUN}")

    while running:
        try:
            check_once()
        except Exception:
            log.error("Unexpected error (bot keeps running):\n" + traceback.format_exc())

        if fail_streak:
            wait = min(CHECK_INTERVAL * (2 ** min(fail_streak, 6)), MAX_BACKOFF)
        else:
            wait = CHECK_INTERVAL
        wait += random.uniform(0.1, 0.3) * CHECK_INTERVAL

        end = time.time() + wait
        while running and time.time() < end:
            time.sleep(min(0.25, max(end - time.time(), 0)))

    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
