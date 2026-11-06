#!/usr/bin/env python3
"""Watch an Amazon listing for a specific variant to appear and come into stock.

Each run does one check and exits. A scheduler (Windows Task Scheduler, cron,
GitHub Actions, ...) is responsible for running it repeatedly.

How a check works:
  1. Fetch the parent listing page and read its variant table
     (`dimensionValuesDisplayData`), which maps each variant ASIN to its
     labels, e.g. "B0DSBTKP5Q": ["12GB + 512GB", "...", "Titanium Silverblue"].
  2. Find a variant whose labels contain every --match term (case-insensitive).
  3. If one exists, fetch that variant's own page and decide whether it is
     buyable (has an Add to Cart button and is not "Currently unavailable").
  4. Compare with the previous run's result (state.json) and notify only on a
     change, so you get one alert per event rather than one every run.

If Amazon serves a CAPTCHA or a page without the expected markers, the run is
recorded as "blocked" instead of being misread as "not available". After
several blocked runs in a row you get a single warning.

Standard library only, so it runs anywhere Python 3.9+ is installed.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watch.log"
NTFY_TOPIC_FILE = HERE / "ntfy_topic.txt"  # git-ignored; see notify_ntfy

DEFAULT_HOST = "www.amazon.in"
DEFAULT_LISTING = "B0DSKL9MQ8"  # Galaxy S25 Ultra listing the user is watching
DEFAULT_MATCH = ["512GB", "Whitesilver"]
BLOCKED_ALERT_AFTER = 6  # consecutive blocked runs before warning
LAYOUT_ALERT_AFTER = 2   # consecutive "page redesigned" runs before warning

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

log = logging.getLogger("watch")


class Blocked(Exception):
    """Amazon refused the request (CAPTCHA, bot check, error status)."""


class LayoutChanged(Blocked):
    """The page loaded but the markers we parse are missing: Amazon redesigned it."""


@dataclass
class Result:
    status: str  # "not_listed" | "out_of_stock" | "in_stock"
    asin: str | None = None
    title: str | None = None
    price: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Fetching and parsing
# --------------------------------------------------------------------------- #

def fetch(url: str, attempts: int = 3) -> str:
    """Fetch a product page, retrying when Amazon blocks the request.

    Blocks are intermittent (on GitHub's runners roughly every other request),
    so a short randomised wait and another try usually gets through.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fetch_once(url)
        except LayoutChanged:
            raise  # retrying won't fix a redesign
        except Blocked as e:
            if attempt == attempts:
                raise
            wait = random.uniform(20, 40)
            log.info("attempt %d/%d blocked (%s); retrying in %.0fs",
                     attempt, attempts, e, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_once(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # Amazon answers bots with 503 + a CAPTCHA page.
        raise Blocked(f"HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise Blocked(f"network error for {url}: {e}") from e

    if 'id="productTitle"' in body:
        return body
    lowered = body.lower()
    if "captcha" in lowered:
        raise Blocked(f"CAPTCHA page for {url}")
    if "bm-verify" in lowered or "/_sec/verify" in lowered:
        raise Blocked(f"bot-check challenge page for {url}")
    raise LayoutChanged(f"no product title on {url}")


def parse_variants(page: str) -> dict[str, list[str]]:
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', page)
    if not m:
        raise LayoutChanged("variant table (dimensionValuesDisplayData) not found")
    return json.loads(m.group(1))


def normalise(text: str) -> str:
    """Lower-case and drop spaces so '512 GB' and '512GB' compare equal."""
    return re.sub(r"\s+", "", text).lower()


def find_variant(variants: dict[str, list[str]], terms: list[str]) -> str | None:
    wanted = [normalise(t) for t in terms]
    for asin, labels in sorted(variants.items()):  # sorted: don't rely on page order
        haystack = normalise(" ".join(labels))
        if all(w in haystack for w in wanted):
            return asin
    return None


def text_of(page: str, element_id: str, limit: int = 2000) -> str:
    # Skip the rest of the opening tag, then strip tags and inline JSON blobs.
    m = re.search(rf'id="{element_id}"[^>]*>(.{{0,{limit}}})', page, re.S)
    if not m:
        return ""
    stripped = re.sub(r"<[^>]+>|\{.*", " ", m.group(1), flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", stripped)).strip()


def parse_product(page: str) -> tuple[str, str | None, bool, str]:
    title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1))).strip() if title_m else "?"

    price_m = re.search(r'a-price-whole">([\d,]+)', page)
    price = f"₹{price_m.group(1)}" if price_m else None

    availability = text_of(page, "availability", 400)[:120]
    unavailable = "currently unavailable" in availability.lower()
    buyable = 'id="add-to-cart-button"' in page and not unavailable
    return title, price, buyable, availability


def check(host: str, listing: str, terms: list[str]) -> Result:
    listing_page = fetch(f"https://{host}/dp/{listing}?th=1")
    variants = parse_variants(listing_page)
    asin = find_variant(variants, terms)
    if asin is None:
        return Result("not_listed", detail=f"{len(variants)} variants listed, none match")

    product_page = fetch(f"https://{host}/dp/{asin}?th=1&psc=1")
    title, price, buyable, availability = parse_product(product_page)
    return Result(
        "in_stock" if buyable else "out_of_stock",
        asin=asin,
        title=title,
        price=price,
        detail=availability or None,
    )


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify_windows(title: str, body: str, url: str | None) -> bool:
    """Show a Windows toast from WSL. Stays on screen until dismissed."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return False
    actions = ""
    launch = ""
    if url:
        u = xml_escape(url, {'"': "&quot;"})
        launch = f' activationType="protocol" launch="{u}"'
        actions = (
            f'<actions><action content="Open Amazon" activationType="protocol" '
            f'arguments="{u}"/><action content="Dismiss" activationType="system" '
            f'arguments="dismiss"/></actions>'
        )
    xml = (
        f'<toast scenario="reminder"{launch}><visual><binding template="ToastGeneric">'
        f"<text>{xml_escape(title)}</text><text>{xml_escape(body)}</text>"
        f'</binding></visual><audio src="ms-winsoundevent:Notification.Looping.Alarm"/>'
        f"{actions}</toast>"
    )
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{xml}
'@)
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
    [Windows.UI.Notifications.ToastNotification]::new($doc))
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        log.warning("Windows toast failed: %s", proc.stderr.strip()[:300])
        return False
    return True


def notify_ntfy(title: str, body: str, url: str | None, priority: str = "urgent") -> bool:
    """Push to a phone via ntfy.sh.

    The topic name acts as the password for the channel, so it comes from the
    NTFY_TOPIC env var (a GitHub secret in CI) or a git-ignored local file,
    never from the source.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic and NTFY_TOPIC_FILE.exists():
        topic = NTFY_TOPIC_FILE.read_text().strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("utf-8"), "Priority": priority,
               "Tags": "rotating_light" if priority == "urgent" else "white_check_mark"}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.URLError as e:
        log.warning("ntfy push failed: %s", e)
        return False


def notify(title: str, body: str, url: str | None = None) -> None:
    sent = [name for name, fn in (("windows", notify_windows), ("ntfy", notify_ntfy))
            if fn(title, body, url)]
    log.info("NOTIFY [%s] %s | %s", ",".join(sent) or "none delivered", title, body)


# --------------------------------------------------------------------------- #
# State and main
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)  # atomic, so an interrupted run can't corrupt it


def run(args: argparse.Namespace) -> int:
    state = load_state()
    label = " ".join(args.match)

    try:
        result = check(args.host, args.listing, args.match)
    except Blocked as e:
        state["blocked_streak"] = state.get("blocked_streak", 0) + 1
        state["blocked_since_heartbeat"] = state.get("blocked_since_heartbeat", 0) + 1
        log.warning("blocked (%d in a row): %s", state["blocked_streak"], e)

        # A redesign won't fix itself, so it alerts sooner (2 runs in a row,
        # to ride out a one-off odd page) than ordinary blocks do.
        if isinstance(e, LayoutChanged):
            state["layout_streak"] = state.get("layout_streak", 0) + 1
            if state["layout_streak"] == LAYOUT_ALERT_AFTER:
                notify("Amazon watcher needs updating",
                       f"Amazon's page no longer looks the way watch.py expects ({e}). "
                       f"Checks for {label} are not working until the parser is fixed.")
        else:
            state["layout_streak"] = 0
            if state["blocked_streak"] == BLOCKED_ALERT_AFTER:
                notify("Amazon watcher can't see the page",
                       f"{BLOCKED_ALERT_AFTER} checks in a row were blocked ({e}). "
                       f"Alerts for {label} may be missed; see watch.log.")
        finish(args, state)
        return 2

    if state.get("blocked_streak", 0) >= BLOCKED_ALERT_AFTER:
        log.info("page readable again after %d blocked runs", state["blocked_streak"])
    state["blocked_streak"] = 0
    state["layout_streak"] = 0
    state["ok_since_heartbeat"] = state.get("ok_since_heartbeat", 0) + 1
    state["last_ok"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    previous = state.get("status")
    log.info("status=%s asin=%s price=%s detail=%s",
             result.status, result.asin, result.price, result.detail)

    url = f"https://{args.host}/dp/{result.asin}?th=1&psc=1" if result.asin else None
    if result.status != previous:
        if result.status == "in_stock":
            notify(f"IN STOCK: {label}",
                   f"{result.price or 'price not shown'} - {result.title}", url)
        elif result.status == "out_of_stock" and previous in (None, "not_listed"):
            notify(f"Listed, not buyable yet: {label}",
                   f"{result.detail or 'Currently unavailable'} - {result.title}", url)
        elif previous == "in_stock":
            notify(f"Sold out again: {label}", result.detail or "No longer buyable", url)

    state.update(status=result.status, asin=result.asin, price=result.price,
                 last_check=time.strftime("%Y-%m-%d %H:%M:%S"))
    finish(args, state)
    return 0


def finish(args: argparse.Namespace, state: dict) -> None:
    """Send the daily heartbeat if requested, then persist state."""
    if args.heartbeat:
        ok = state.get("ok_since_heartbeat", 0)
        blocked = state.get("blocked_since_heartbeat", 0)
        status = {"not_listed": "not listed yet", "out_of_stock": "listed, not buyable",
                  "in_stock": "IN STOCK"}.get(state.get("status"), "unknown")
        notify_ntfy(  # phone only: this is a quiet daily signal, not an alert
            "Amazon watcher: still running",
            f"{' '.join(args.match)}: {status}. Since last heartbeat: {ok} checks ok, "
            f"{blocked} blocked. Last good check {state.get('last_ok', 'never')}.",
            None, priority="low")
        log.info("heartbeat sent (%d ok, %d blocked)", ok, blocked)
        state["ok_since_heartbeat"] = state["blocked_since_heartbeat"] = 0
    save_state(state)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--listing", default=DEFAULT_LISTING,
                   help="any ASIN from the product's listing page")
    p.add_argument("--match", nargs="+", default=DEFAULT_MATCH,
                   help="terms that must all appear in the variant's labels")
    p.add_argument("--jitter", type=int, default=0,
                   help="sleep a random 0..N seconds first, so checks aren't clockwork")
    p.add_argument("--heartbeat", action="store_true",
                   help="after checking, push a low-priority 'still running' summary")
    p.add_argument("--test-notify", action="store_true",
                   help="send a test notification and exit")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )

    if args.test_notify:
        notify("Amazon watcher test", "Notifications are working.",
               f"https://{args.host}/dp/{args.listing}")
        return 0

    if args.jitter:
        time.sleep(random.uniform(0, args.jitter))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
