# Nellis Auction — discovered API contract

> The Nellis storefront (`https://www.nellisauction.com`) is a **Remix** app whose
> search is powered by a public **Algolia** index. We do **not** scrape rendered HTML
> for listings — we query Algolia for discovery, and fetch the Remix-rendered lot
> page only to read the live bid/premium that Algolia does not carry.
>
> This file is the contract. If Nellis changes their setup, fix it here and in
> `nellis_hunter/feed.py` (the constants live at the top of that module).

Last verified: **2026-06-19**.

---

## Stage 1 — Algolia search (bulk discovery)

The browser embeds public, search-only Algolia credentials in `window.ENV` on every
page (`APP_PUBLIC_ALGOLIA_*`). These are meant for client-side search.

| Thing | Value |
|---|---|
| Application ID | `GL1QVP8R29` |
| Search API key | `d22f83c614aa8eda28fa9eadda0d07b9` (public, search-only) |
| Index | `nellisauction-prd` |
| Endpoint | `POST https://GL1QVP8R29-dsn.algolia.net/1/indexes/nellisauction-prd/query` |

**Headers**

```
X-Algolia-API-Key: d22f83c614aa8eda28fa9eadda0d07b9
X-Algolia-Application-Id: GL1QVP8R29
Content-Type: application/json
```

**Body** — Algolia takes a single URL-encoded `params` string:

```json
{ "params": "query=<text>&hitsPerPage=100&page=0&attributesToRetrieve=%5B%22*%22%5D&facetFilters=...&numericFilters=..." }
```

### Params we use

| Param | Purpose | Notes |
|---|---|---|
| `query` | free-text keyword search | empty string = browse all |
| `hitsPerPage` | page size | max 1000; we use 100 |
| `page` | 0-indexed pagination | response carries `nbPages`, `nbHits` |
| `attributesToRetrieve` | `["*"]` | **required** — the index default omits retail & taxonomy |
| `facetFilters` | location + category filters | array-of-arrays (AND of ORs); see below |
| `numericFilters` | closing-soon window | `["Date Closed>=<now>","Date Closed<=<cutoff>"]` (unix epoch) |
| `facets` + `maxValuesPerFacet` | enumerate facet values | for discovery / introspection |

### Location encoding — **`Location Name` facet** (this is the warehouse)

There is no numeric location id in the search payload. Location is a **string facet**
`Location Name`. The two AZ warehouses Grant cares about are distinct values:

| `Location Name` | open lots (2026-06-19) |
|---|---|
| **Phoenix** | ~28,000 |
| **Mesa** | ~28,000 |
| North Las Vegas, Dean Martin, Katy, Delran, SW Houston, Denton, Dallas, Denver, … | (other metros) |

Filter: `facetFilters=[["Location Name:Mesa"]]`.

> Note: a *separate* "shopping location" concept exists in the page bootstrap
> (`shoppingLocations`: Las Vegas=1, **Phoenix=2**, Houston=5, Philadelphia=6,
> Denver=7, Dallas=8). That is the metro selector, **not** the per-lot warehouse.
> The lot **detail** payload also has its own `location.id` (e.g. Phoenix=10). For
> hunting, the Algolia `Location Name` facet ("Phoenix" / "Mesa") is the right knob.

### Category encoding — taxonomy facets

Three levels: `Taxonomy Level 1` / `Level 2` / `Level 3`. We filter on whichever level
a configured category string belongs to (`facetFilters=[["Taxonomy Level 2:<cat>","Taxonomy Level 1:<cat>"]]`).

`Taxonomy Level 1` values include: `Electronics`, `Home & Household Essentials`,
`Home Improvement`, `Automotive`, `Furniture & Appliances`, …

`Taxonomy Level 2` under **Electronics** (the flip-relevant set):

| Level 2 value | ~count |
|---|---|
| Computers, Laptops, Tablets & Accessories | 5,838 |
| Cell Phones, Chargers & Accessories | 5,644 |
| Cameras & Photography Equipment | 2,484 |
| TVs | 1,386 |
| Headphones | 1,367 |
| Speakers | 951 |
| Wearable Technology | 499 |
| Printers & Printer Accessories | 480 |
| Networking & Drives | 394 |
| Monitors & Monitor Stands | 90 |
| Computer Mice & Mouse Pads / Keyboards / Software | a few each |

> List current values any time: `python -m nellis_hunter.feed --categories --location Mesa`.

### Sort

The default index is relevance-sorted and **includes already-closed lots**. We restrict
to live lots with the `Date Closed` numeric filter (epoch ≥ now). For "closing soon"
we add an upper bound. There may be replica indexes for server-side sort by close time,
but client-side ranking on our small filtered set is sufficient.

### Fields returned per hit (with `attributesToRetrieve=["*"]`)

| Algolia field | Our `Lot` field | Notes |
|---|---|---|
| `objectID` | `lot_id` | stable unique key (dedup key) |
| `Lead Description` | `title` | |
| `Suggested Retail` | `retail_price` | MSRP; **may be null** → `NEEDS_MANUAL` |
| `Location Name` | `location` | "Phoenix" / "Mesa" |
| `Item Condition`, `Is Damaged`, `Missing Parts`, `In Package`, `Is Functional` | `condition` | composed into one string |
| `Date Closed` / `Time Remaining` | `close_time` | unix epoch → UTC |
| `Taxonomy Level 2` (fallback `Level 1`) | `category` / `category_l1` | |
| `Photo` | `image_url` | |
| `Auction Event Name` / `Auction Event Type` | `auction_event_*` | e.g. "Retail Returns" |
| `Brand` | `brand` | |

**Not in Algolia:** live `current_bid`, `bid_count`, buyer's premium → Stage 2.

---

## Stage 2 — Lot detail page (live bid + premium)

`GET https://www.nellisauction.com/p/x/<lot_id>` → 200, full Remix HTML page. (The
`/p/x/<id>` form resolves without the slug.) The lot object is embedded in the Remix
context as JSON; we extract just the fields we need by regex rather than parsing the
whole bundle.

| Source in page | Our `Lot` field |
|---|---|
| `"currentPrice": <n>` | `current_bid` |
| `"bidCount": <n>` | `bid_count` |
| `"retailPrice": <n>` | `retail_price` (confirms/fills Algolia) |
| `"marketStatus": "open"` | `market_status` |
| `"closeTime":{"__type":"Date","value":"<iso>"}` | `close_time` (authoritative, post-extension) |
| `Buyers Premium</p><p>15%` (rendered) | `premium` → `0.15` |

> **Buyer's premium** is rendered as text ("15%"), not a clean JSON field we located.
> We parse it; if absent we fall back to the configured `PREMIUM` default. Premium can
> vary by event, so the per-lot value (when found) wins.

This is the **ToS-sensitive host**, so Stage 2 is where politeness matters:
- single-threaded, **≥ `REQUEST_INTERVAL_SECONDS` (default 2.5s) between requests**,
- exponential backoff on `429`/`5xx`, abort the run after repeated failures,
- realistic `User-Agent`,
- responses cached to `cache/` (`DETAIL_CACHE_TTL`, default 30 min) so dev re-runs
  don't re-hit the site,
- the pipeline only enriches the top `MAX_DETAIL_FETCHES` Algolia candidates, so a
  daily sweep makes tens — not thousands — of detail requests.

---

## Quick manual repro

```bash
# Search (Algolia only — no live bid)
python -m nellis_hunter.feed --location Mesa --category "Monitors & Monitor Stands" --limit 5

# Single lot, fully enriched with live bid + premium
python -m nellis_hunter.feed --lot 113248338

# Enumerate category facet values for a location
python -m nellis_hunter.feed --categories --location Phoenix
```
