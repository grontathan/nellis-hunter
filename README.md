# Nellis Auction Deal Hunter + MCP Server

A read-only resale-flip scout for [Nellis Auction](https://www.nellisauction.com)'s
**Phoenix** and **Mesa** warehouses. It pulls live listings, scores each lot for
eBay-flip potential using a fixed cost formula, and surfaces a ranked daily shortlist
— plus an MCP server so you can ask Claude *"anything worth flipping right now?"* on
demand.

> **Read-only, permanently.** This system never bids, logs in, or takes any account
> action. It surfaces candidates; you bid manually. There is no bidding code and
> never will be.

---

## What it does

```
Algolia search  ─►  resale estimate  ─►  live-bid enrich  ─►  score  ─►  dedup  ─►  ranked digest
 (Part A feed)      (Part B resale)       (Part A detail)    (Part B)   (Part C)    (Discord / console)
```

- **Part A — feed** (`feed.py`): Nellis search is backed by a public Algolia index.
  We query it for bulk discovery (title, retail, location, condition, close time,
  category, photo), then fetch each lot's detail page only for the live bid + buyer's
  premium. Politeness is enforced (rate-limit, backoff, cache). Contract is documented
  in [`NELLIS_API.md`](NELLIS_API.md).
- **Part B — scoring** (`scoring.py`, `resale.py`): a pure, unit-tested cost/flip
  engine. Resale estimation is pluggable (v1 retail-haircut heuristic; v2 eBay comps
  drop in behind the same interface).
- **Part C — pipeline** (`pipeline.py`, `db.py`, `notify.py`, `run.py`): sweeps both
  warehouses, dedups against SQLite, ranks by projected margin, and posts a digest.
- **Part D — MCP server** (`mcp_server.py`): the same logic exposed as MCP tools.

---

## Setup

Requires **Python 3.11+**.

```bash
cd nellis-hunter
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

Edit `.env` — at minimum set `DISCORD_WEBHOOK_URL` if you want Discord output (omit it
to print to the console). All cost knobs (premium, tax, eBay fees, thresholds,
categories) live here; see comments in [`.env.example`](.env.example).

### Verify the feed (Part A)

```bash
python -m nellis_hunter.feed --location Mesa --category "Monitors & Monitor Stands" --limit 5
python -m nellis_hunter.feed --lot 113248338      # single lot with live bid
python -m nellis_hunter.feed --categories --location Phoenix   # list category facet values
```

### Run the tests (Part B)

```bash
python -m pytest -q
```

The scoring tests pin the cost formula to hand-checked values (e.g. a `$120.50` bid →
`$150.08` all-in at 15% premium + 8.3% tax). **Don't let these go red** — a regression
there costs real money.

---

## Run a sweep (Part C)

```bash
# Full Phoenix+Mesa sweep across .env FLIP_CATEGORIES -> Discord (or console)
python -m nellis_hunter.run

# Useful flags:
python -m nellis_hunter.run --console                       # force console output
python -m nellis_hunter.run --closing-within-hours 30       # only lots closing soon
python -m nellis_hunter.run --categories "Monitors & Monitor Stands"
python -m nellis_hunter.run --no-db                         # one-shot, skip dedup store
python -m nellis_hunter.run --dry-run                       # score + print, never POST
```

Sample digest line:

```
[🟢 BID ▸ $355 margin] LG 27-Inch StanbyME 2 with Folio Cover — Mesa
  current $79 | max bid $344 | retail $950 | closes 6:31pm
  https://www.nellisauction.com/p/x/112759408
```

**Verdicts:** `BID` (headroom over current bid *and* margin ≥ `MIN_MARGIN_PCT`),
`WATCH` (profitable but thin/bid-up), `SKIP` (filtered out), `NEEDS_MANUAL` (no retail
in payload — surfaced flagged so you can drop in a real eBay comp via the MCP
`score_lot` tool).

**Dedup:** a lot is surfaced once; it re-surfaces only if it's closing within
`RESURFACE_WITHIN_HOURS` and still under your max bid. The full scored run is persisted
to `nellis_hunter.db` for later analysis.

### Output sinks (pluggable)

`notify.py` defines a `Notifier` interface. **Discord** is the v1 sink (auto-selected
when `DISCORD_WEBHOOK_URL` is set, else console). `Telegram`, `GoogleSheet`, and
`Email` are stubbed behind the same interface for drop-in later.

**Discord setup:** in Discord, **Channel → Edit Channel → Integrations → Webhooks →
New Webhook → Copy Webhook URL**, then put it in `.env` as `DISCORD_WEBHOOK_URL=...`
(it's a secret — already gitignored). Confirm it works without running a full sweep:

```bash
python -m nellis_hunter.notify --test    # posts a test message to your channel
```

The Discord sink posts **one rich embed per lot** — a self-contained card with the
listing photo welded to its bid/pricing fields (current bid, max bid, margin, retail,
resale estimate, close time). This replaces the old plain-text digest, where Discord
auto-unfurled each bare URL into its own embed and the images drifted away from the
text describing them. Embeds are batched at Discord's 10-per-message cap and the sink
backs off on the webhook's `429` rate limit automatically.

---

## Scheduling

The entry point is a single command (`python -m nellis_hunter.run`) — there is **no
internal scheduler loop**. Pick one of these to run it (default: once daily).

### Option 1 — GitHub Actions cron (preferred: free, cloud, no always-on machine)

A workflow is included at [`.github/workflows/nellis-hunter.yml`](.github/workflows/nellis-hunter.yml).
It runs daily at 14:00 UTC (07:00 Phoenix) and can be triggered manually.

1. Push this repo to GitHub.
2. Repo **Settings → Secrets and variables → Actions** → add secret
   `DISCORD_WEBHOOK_URL`.
3. Done. Adjust the `cron:` line or the `FLIP_CATEGORIES` env in the workflow to taste.
   (The dedup DB is cached between runs so you aren't re-pinged on the same lots.)

### Option 2 — Windows Task Scheduler (runs on this PC)

```powershell
# Create a daily 7am task. Adjust paths if you move the repo.
$py  = "C:\Users\Grant Gibbons\Documents\Claude\Claude Code Projects\nellis-hunter\.venv\Scripts\python.exe"
$dir = "C:\Users\Grant Gibbons\Documents\Claude\Claude Code Projects\nellis-hunter"
$action  = New-ScheduledTaskAction -Execute $py -Argument "-m nellis_hunter.run" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 7am
Register-ScheduledTask -TaskName "NellisHunter" -Action $action -Trigger $trigger -Description "Daily Nellis flip sweep"
```

### Option 3 — macOS/Linux cron

```cron
# 7:00 AM Phoenix daily (adjust TZ to your box)
0 7 * * * cd /path/to/nellis-hunter && .venv/bin/python -m nellis_hunter.run >> sweep.log 2>&1
```

---

## MCP server (Part D)

Exposes the feed + scoring as MCP tools over **stdio** for use inside Claude Code.

### Register

```bash
claude mcp add nellis-hunter -- "C:\Users\Grant Gibbons\Documents\Claude\Claude Code Projects\nellis-hunter\.venv\Scripts\python.exe" -m nellis_hunter.mcp_server
```

> Point it at the **venv** Python (above) so it has the dependencies. On macOS/Linux
> use `.venv/bin/python`. Verify with `claude mcp list`; remove with
> `claude mcp remove nellis-hunter`.

### Tools

| Tool | What it does |
|---|---|
| `search_nellis(location, category?, keywords?, max_results=20, closing_within_hours?)` | Fast Algolia search + heuristic score (no live bid). |
| `get_lot(lot_id)` | One lot, enriched with the live current bid + premium, fully scored. |
| `score_lot(lot_id, resale_estimate, target_profit=25)` | Re-score with a **human eBay comp**, overriding the heuristic. |
| `hot_deals(location?, min_margin=20, closing_within_hours?, max_results=15)` | Ranked `BID`-verdict lots with live bids — the on-demand digest. |
| `max_bid(target_all_in, premium?, tax?)` | Cost what-if: max hammer bid for a target out-the-door cost. |
| `all_in(hammer, premium?, tax?)` | Cost what-if: out-the-door cost of winning at a hammer price. |

Example asks once registered: *"Use hot_deals for Mesa closing in the next 6 hours,"*
or *"get_lot 113248338, and if it's NEEDS_MANUAL, score_lot it with a $300 eBay comp."*

**Optional remote access from Claude.ai:** deploy the same server as a Streamable-HTTP
MCP server behind auth and add it as a connector. Not required for v1.

---

## Project layout

```
nellis_hunter/
  feed.py          # Part A — Algolia search + lot-detail client (rate-limited, cached)
  scoring.py       # Part B — pure cost + flip math (unit-tested)
  resale.py        # Part B — ResaleEstimator interface + v1 heuristic
  pipeline.py      # Part C — sweep, enrich, dedup, rank
  ebay.py          # Part B — eBay sold-comps client (v2 resale data source)
  db.py            # Part C — SQLite dedup + run history
  notify.py        # Part C — Notifier interface + Discord (+ stubs)
  run.py           # Part C — CLI entry point for the scheduled job
  mcp_server.py    # Part D — FastMCP stdio server
  config.py        # .env loading; pure ScoringConfig
  models.py        # Pydantic Lot / Scoring / ScoredLot
tests/test_scoring.py
NELLIS_API.md      # discovered Algolia + detail-page contract
.github/workflows/nellis-hunter.yml
.env.example
```

## Notes & limitations

- **Buyer's premium** is read per-lot from the detail page when present (it can vary by
  event); falls back to the `PREMIUM` default.
- **No retail in payload → `NEEDS_MANUAL`.** That's expected; surface it and drop in an
  eBay comp via `score_lot`. The heuristic resale estimate is deliberately conservative
  (better to skip than overbid on a phantom margin).
- **Resale estimator (v1 vs v2).** `RESALE_SOURCE` in `.env` selects the data source
  behind the `ResaleEstimator` seam:
  - `heuristic` (v1) — `resale ≈ retail × category_haircut × condition_multiplier`.
    No external traffic. Degrades to `NEEDS_MANUAL` when a lot has no retail.
  - `ebay` (v2) — real eBay **sold** comps: builds a search from the lot's brand +
    model, scrapes the completed/sold results, and uses a trimmed median (condition-
    adjusted) as the resale value. Falls back to the v1 heuristic per-lot when there
    are too few comps. To stay polite, eBay is queried **only for the enriched
    shortlist** (the `MAX_DETAIL_FETCHES` lots that get a live-bid check), never for
    every discovered candidate; results cache for `EBAY_CACHE_TTL` (default 1 day).
  - **Caveat:** eBay's `/sch/` search sits behind Akamai bot management and reliably
    returns `403/503` from **datacenter IPs** — including GitHub Actions runners. From
    a residential IP (e.g. running the sweep on your own PC) it generally works; in CI
    it will usually be blocked, at which point v2 silently falls back to the v1
    heuristic. For robust automated sold-comps you'd need a headless browser, a
    scraping proxy, or eBay's gated Marketplace Insights API — all behind the same
    `ResaleEstimator` interface, so any of them is a drop-in replacement later.
- **Politeness:** detail fetches to nellisauction.com are single-threaded, ≥2.5s apart,
  backed off on 429/5xx, and cached to `cache/`. A daily sweep makes tens — not
  thousands — of detail requests. Respect Nellis's ToS; this is a personal hunting aid.
