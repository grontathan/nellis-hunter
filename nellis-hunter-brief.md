# Build Brief: Nellis Auction Deal Hunter + MCP Server

**For:** Claude Code
**Owner:** Grant (Phoenix / Mesa, AZ)
**Goal:** A scheduled pipeline that pulls Nellis Auction listings for the Phoenix + Mesa warehouses, scores each lot for resale-flip potential using a fixed cost formula, and surfaces a ranked daily shortlist. Plus an MCP server wrapping the same logic so it can be queried conversationally on demand ("anything worth flipping right now?").

**Hard boundaries (do not cross):**
- **Read-only.** This system never places bids, logs into an account, or takes any account action. It surfaces candidates; the human bids manually.
- **Polite scraping.** Nellis ToS restricts automated access. Rate-limit aggressively (see below), set a real User-Agent, and cache. The goal is a personal hunting aid, not a hammer on their servers.
- **No secrets in code.** All config (webhook URLs, tax rate, thresholds) lives in `.env`.

---

## Part A — The data feed (do this first)

Nellis's site is a **Remix app**, so the frontend fetches listing data from JSON loader endpoints rather than rendering server-side HTML you'd have to parse. **Do not scrape rendered HTML.** Instead:

1. Open a Nellis search filtered to the Mesa location in a browser.
2. DevTools → Network → filter by Fetch/XHR.
3. Find the request(s) returning listing JSON. Remix typically exposes loader data at routes with a `?_data=` query param (or a `.data` suffix). Note the full URL, query params, and any headers.
4. Identify how **location** is encoded (e.g. a `locationId`, `warehouse`, or slug param) — discover the actual Phoenix and Mesa identifiers from the network calls; do not hardcode a guess.
5. Identify how **category**, **pagination**, and **sort** are encoded.
6. Replicate the request in code (Python `httpx`). Confirm you get clean JSON back.

Document the discovered endpoints, params, and the location IDs in a `NELLIS_API.md` file in the repo so the contract is explicit and easy to fix if Nellis changes their routes.

### Fields to extract per lot
- `lot_id` / `auction_id` (stable unique key for dedup)
- `title`
- `current_bid`
- `retail_price` / MSRP (if present in payload)
- `condition` (new / open-box / returned / damaged, if present)
- `location` (Phoenix vs Mesa)
- `close_time` (UTC + local)
- `category`
- `url` (canonical listing link)
- `image_url`
- `bid_count` (if available)

### Rate limiting & politeness
- Max ~1 request / 2–3 seconds, single-threaded.
- One full sweep per scheduled run (default once daily); no continuous polling.
- Respect any 429s with exponential backoff; abort the run cleanly on repeated failures.
- Realistic `User-Agent`. Cache responses to disk so re-runs during development don't re-hit the site.

---

## Part B — Scoring engine (the core IP)

This is the part that decides what's worth flipging. Keep it in one well-tested module (`scoring.py`) so it can be reused by both the pipeline and the MCP server.

### Cost formula (exact — do not approximate)
Nellis charges a buyer's premium on the hammer, then sales tax on (hammer + premium):

```
all_in(hammer) = hammer * (1 + PREMIUM) * (1 + TAX)
```

Inverting, to find the max bid that hits a target out-the-door cost:

```
max_bid(target_all_in) = target_all_in / ((1 + PREMIUM) * (1 + TAX))
```

Defaults (all configurable in `.env`):
- `PREMIUM = 0.15` — but **read the per-lot premium from the listing if the payload exposes it**, since Nellis says it can vary by event. Fall back to 0.15.
- `TAX = 0.083` — Mesa-area combined rate; make it a config value.

### Flip math
Given a resale estimate, compute the projected economics:

```
EBAY_FEE_RATE = 0.1325        # final value fee, configurable
EBAY_FLAT_FEE = 0.40          # per-order
SHIP_COST     = <per-category, configurable; default 6.00>

net_resale     = resale_estimate * (1 - EBAY_FEE_RATE) - EBAY_FLAT_FEE - SHIP_COST
projected_margin(hammer) = net_resale - all_in(hammer)        # profit at a given bid
max_bid_for_profit(target_profit) = max_bid(net_resale - target_profit)
```

Output per lot:
- `projected_margin_at_current_bid`
- `max_bid` for a configurable `TARGET_PROFIT` (default $25)
- `margin_pct` = projected_margin_at_current_bid / all_in(current_bid)
- a `verdict`: BID / WATCH / SKIP based on thresholds (e.g. BID if max_bid > current_bid AND margin_pct ≥ 0.30)

### Resale estimate (make this pluggable)
This is the hardest input to automate. Build it as a swappable interface `ResaleEstimator.estimate(lot) -> float`:
- **v1 (ship first):** heuristic. If MSRP/retail is in the payload, `resale_estimate = retail * category_haircut` (e.g. electronics 0.55–0.70 used, configurable lookup table). If no retail, mark `resale_estimate = None` and verdict `NEEDS_MANUAL`.
- **v2 (later):** real eBay sold comps via the eBay Browse/Marketplace Insights API (or a polite scrape). Keep it behind the same interface so v1 → v2 is a drop-in swap.

Do not block v1 on v2.

---

## Part C — Pipeline + scheduling

1. Sweep Phoenix + Mesa across the configured flip categories (electronics, monitors, computer components, RAM/SSD, networking, small appliances — make the list config).
2. Run each lot through the scoring engine.
3. **Dedup** against a persistent store (SQLite preferred, `seen` table keyed on `lot_id`). Only surface lots not seen before, OR seen lots whose `current_bid` is still under `max_bid` and close within N hours (configurable "closing soon" re-surface).
4. Rank surviving lots by `projected_margin_at_current_bid` descending.
5. Push the shortlist out (see Output).
6. Persist the full scored run to SQLite for later analysis.

**Scheduling:** make the entry point a single CLI command (`python -m nellis_hunter.run`). Do NOT build an internal scheduler loop. Instead document three ways to schedule it and pick one:
- GitHub Actions cron (free, runs in the cloud, good if no always-on machine) — preferred.
- Windows Task Scheduler (if running on Grant's PC).
- macOS/Linux cron.

### Output format (default: Discord webhook)
Post a compact daily digest. Per lot, one line/embed:
```
[BID ▸ $34 margin] Dell OptiPlex 3080 Micro — Mesa
  current $40 | max bid $120 | retail $260 | closes 9:14p
  https://nellisauction.com/...
```
Make the sink pluggable (`Notifier` interface): Discord webhook (v1), with stubs for Telegram, a Google Sheet append, and email. Reference pattern: aaronata/AuctionChecker (keyword monitor → Discord webhook, scheduled, ignore-list dedup) — same shape, adapt it.

---

## Part D — MCP server

Wrap the scoring + feed in an MCP server so it's queryable on demand from Claude Code (and optionally Claude.ai).

**Stack:** Python MCP SDK (FastMCP). Transport: **stdio** for local use with Claude Code. Register with:
```
claude mcp add nellis-hunter -- python -m nellis_hunter.mcp_server
```

**Tools to expose:**
- `search_nellis(location, category=None, keywords=None, max_results=20)` → list of lots with full scoring fields.
- `get_lot(lot_id)` → single lot, fully scored.
- `score_lot(lot_id, resale_estimate, target_profit=25)` → run the math with a human-supplied resale comp (lets Claude/Grant override the heuristic with a real eBay number).
- `hot_deals(location=None, min_margin=20, closing_within_hours=None)` → ranked BID-verdict lots.
- `max_bid(target_all_in)` and `all_in(hammer)` → expose the cost math as standalone tools for quick what-ifs.

Keep tool outputs small and structured (the model considers every tool on every turn; a tight schema keeps it fast and accurate).

**Optional — make it reachable from Claude.ai chat:** if Grant wants to query it conversationally outside Claude Code, deploy the same server as a remote **Streamable HTTP** MCP server (behind auth) and add it as a connector in Claude.ai. Not required for v1.

---

## Tech stack
- **Language:** Python 3.11+
- **HTTP:** `httpx`
- **Models/validation:** `pydantic`
- **Storage:** SQLite (`sqlite3` or SQLModel)
- **MCP:** official Python SDK / FastMCP
- **Config:** `.env` via `python-dotenv`
- **Tests:** `pytest` — the scoring module especially (lock the cost formula with unit tests; a regression there costs real money).

## Repo layout
```
nellis-hunter/
  nellis_hunter/
    __init__.py
    feed.py          # endpoint client (Part A)
    scoring.py       # cost + flip math (Part B)
    resale.py        # ResaleEstimator interface + v1 heuristic
    pipeline.py      # sweep, dedup, rank (Part C)
    notify.py        # Notifier interface + Discord
    run.py           # CLI entry for the scheduled job
    mcp_server.py    # MCP server (Part D)
    db.py            # SQLite
  tests/
    test_scoring.py
  NELLIS_API.md      # discovered endpoints, params, location IDs
  .env.example
  README.md          # setup, scheduling options, MCP registration
```

## .env.example
```
DISCORD_WEBHOOK_URL=
PREMIUM=0.15
TAX=0.083
EBAY_FEE_RATE=0.1325
EBAY_FLAT_FEE=0.40
DEFAULT_SHIP_COST=6.00
TARGET_PROFIT=25
MIN_MARGIN_PCT=0.30
FLIP_CATEGORIES=electronics,monitors,computer-components,networking
PHOENIX_LOCATION_ID=    # discover from network tab
MESA_LOCATION_ID=       # discover from network tab
```

## Acceptance criteria (milestones, in order)
1. `feed.py` returns clean JSON for a Mesa search; `NELLIS_API.md` documents the contract.
2. `scoring.py` passes unit tests for `all_in`, `max_bid`, and `projected_margin` against hand-checked values (e.g. $120.50 bid → ~$150 all-in at 15% + 8.3%).
3. `python -m nellis_hunter.run` does a full Phoenix+Mesa sweep, scores, dedups, and posts a ranked Discord digest.
4. Scheduling documented and one method wired up.
5. MCP server runs over stdio, registers in Claude Code, and `hot_deals` / `search_nellis` return scored lots.
6. README covers setup + scheduling + MCP registration end to end.

## Notes for the implementer
- Build Parts A → B → C → D in order; each is usable on its own.
- The scoring module is the asset — test it hard, keep it pure (no I/O), reuse it everywhere.
- If the Nellis payload doesn't expose retail price, the v1 heuristic degrades to `NEEDS_MANUAL` — that's fine, surface those lots flagged so the human can drop in an eBay comp via `score_lot`.
- Never add a bidding capability, even if it seems convenient. Out of scope, permanently.
