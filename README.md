# amazon-stock-watch

Alerts me when a specific Amazon variant that isn't sold yet gets listed and
comes into stock. Price trackers like Keepa can't do this, because a variant
that isn't listed has no ASIN to track.

Currently watching: **Samsung Galaxy S25 Ultra, Titanium Whitesilver, 12GB/512GB**
on amazon.in (listing `B0DSKL9MQ8`).

## How it works

`watch.py` does one check per run:

1. Reads the listing page's variant table and looks for a variant whose labels
   contain every `--match` term (`512GB`, `Whitesilver`).
2. If found, opens that variant's page and checks it can be added to the cart.
3. Notifies only when the status changes (listed / in stock / sold out again),
   using `state.json` to remember the last status.

A CAPTCHA or unexpected page never counts as "unavailable". Instead:

| Alert | Meaning |
|---|---|
| **IN STOCK** / **Listed, not buyable yet** / **Sold out again** | the variant's status changed |
| **Amazon watcher can't see the page** | 6 runs in a row hit a CAPTCHA or bot check (blocked fetches are retried first) |
| **Amazon watcher needs updating** | 2 runs in a row got a page without the expected markers: Amazon redesigned it and the parser needs fixing |
| **Amazon watcher: still running** (low priority, 08:00 IST daily, GitHub only) | the schedule is alive; includes how many checks got through. If it stops arriving, check the Actions tab |

## Known limits

- Only variants on this listing are seen. A separate listing for the same
  phone wouldn't be detected. Searching Amazon would catch it, but search is
  behind a JavaScript bot challenge, and getting past that would mean
  defeating Amazon's bot protection, which this project deliberately doesn't do.
- GitHub disables schedules in repos with no commits for 60 days. The missing
  daily heartbeat is the signal; re-enable it from the email or Actions tab.

## Where it runs

| Runner | When | Alerts |
|---|---|---|
| Windows Task Scheduler (`install-windows-task.ps1`) | every 10 min while the laptop is on | Windows toast + ntfy |
| GitHub Actions (`.github/workflows/watch.yml`) | every 10 min, always | ntfy |

Amazon serves a CAPTCHA to roughly 2 of 3 GitHub runs, since they come from
data-centre IPs, so GitHub is the backup and the laptop is the main watcher.
Each runner keeps its own state, so an event can alert once from each.

## Setup

- Phone alerts: install the [ntfy](https://ntfy.sh) app and subscribe to the
  topic in `ntfy_topic.txt` (git-ignored). The same value is the `NTFY_TOPIC`
  repo secret.
- Local schedule, from WSL:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w install-windows-task.ps1)"`
- Test notifications: `python3 watch.py --test-notify`
- Tests (offline): `python3 -m unittest -v`
- Watch something else: `python3 watch.py --listing <ASIN> --match <term> <term>`

## Stopping it

- Local: `Unregister-ScheduledTask -TaskName 'Amazon Stock Watch' -Confirm:$false` (PowerShell)
- GitHub: `gh workflow disable watch`
