# price-watch

Alerts me when a product I'm waiting for becomes worth buying, on Amazon and
Flipkart (India). What to watch lives in [`targets.json`](targets.json).

Currently watching the **Samsung Galaxy S25 Ultra, Titanium Whitesilver, 12GB/512GB**:

| Site | Waiting for | Why a custom watcher |
|---|---|---|
| Amazon.in (listing `B0DSKL9MQ8`) | the variant to be **listed and buyable** | it isn't listed yet, so it has no ASIN for trackers like Keepa to follow |
| Flipkart | the price to drop to **₹1,09,999 or less** (no card offers) while in stock | one config entry, same alerts and phone channel as Amazon |

## How it works

Each run of `watch.py` checks every target once:

- **amazon**: reads the listing's variant table, looks for a variant whose
  labels contain every `match` term (`512GB`, `Whitesilver`), then checks
  whether that variant's page has an Add to Cart button.
- **flipkart**: reads the page's schema.org Product data (the structured block
  published for search engines, which is far more stable than the visual
  layout) for price and stock, and compares with `max_price`.

Each target remembers its last status in `state.json` and alerts only when the
status changes. A CAPTCHA or unexpected page never counts as "unavailable":

| Alert | Meaning |
|---|---|
| **Amazon: … is in stock!** / **Flipkart: … is at ₹X (target ₹Y)!** | what you're waiting for has happened |
| **… is listed, not buyable yet** | the Amazon variant appeared but can't be bought yet |
| **… is now sold out** / **… is now above your ₹Y target** | it stopped being worth buying |
| **\<Site\> watcher can't see the page** | 6 runs in a row hit a CAPTCHA or bot check (blocked fetches are retried first) |
| **\<Site\> watcher needs updating** | 2 runs in a row got a page without the expected markers: the site redesigned it and the parser needs fixing |
| **Watcher: still running** (low priority, 08:00 IST daily, GitHub only) | the schedule is alive; shows each target's status, price, and how many checks got through. If it stops arriving, check the Actions tab |

## Where it runs

| Runner | When | Alerts |
|---|---|---|
| Windows Task Scheduler (`install-windows-task.ps1`) | every 10 min while the laptop is on | Windows toast + ntfy |
| GitHub Actions (`.github/workflows/watch.yml`) | every 10 min, always | ntfy |

From GitHub's data-centre IPs, Amazon serves a CAPTCHA on roughly 2 of 3 runs;
Flipkart got through 5 of 5 in testing. So for Amazon the laptop is the main
watcher and GitHub the backup; for Flipkart, GitHub alone is reliable. Each
runner keeps its own state, so an event can alert once from each.

## Watching something else

Add an entry to `targets.json`. `key` must be unique and stable, since it names
the target's saved state.

```json
{"key": "amazon-…", "site": "amazon", "label": "…",
 "host": "www.amazon.in", "listing": "<any ASIN on the listing>", "match": ["term", "term"]}

{"key": "flipkart-…", "site": "flipkart", "label": "…",
 "url": "https://www.flipkart.com/…/p/itm…?pid=<variant pid>", "max_price": 109999}
```

Check one target by itself with `python3 watch.py --only <key>`.

## Known limits

- Amazon: only variants on this listing are seen. A separate listing for the
  same phone wouldn't be detected. Searching Amazon would catch it, but search
  is behind a JavaScript bot challenge, and getting past that would mean
  defeating Amazon's bot protection, which this project deliberately doesn't do.
- Flipkart prices exclude bank-card offers, which differ per card.
- GitHub disables schedules in repos with no commits for 60 days. The missing
  daily heartbeat is the signal; re-enable it from the email or Actions tab.

## Setup

- Phone alerts: install the [ntfy](https://ntfy.sh) app and subscribe to the
  topic in `ntfy_topic.txt` (git-ignored). The same value is the `NTFY_TOPIC`
  repo secret.
- Local schedule, from WSL:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w install-windows-task.ps1)"`
- Test notifications: `python3 watch.py --test-notify`
- Tests (offline): `python3 -m unittest -v`

## Stopping it

- Local: `Unregister-ScheduledTask -TaskName 'Price Watch' -Confirm:$false` (PowerShell)
- GitHub: `gh workflow disable watch`
