"""Part A — the data feed.

Nellis's storefront is a Remix app whose search is backed by Algolia. Two stages:

  1. Algolia query  -> bulk discovery. One request returns many lots with title,
     retail, location, condition, close time, category, photo. Cheap, paginated.
  2. Per-lot detail -> the live `currentPrice` (current bid), `bidCount`, and the
     per-lot buyer's premium, which Algolia does NOT expose. This hits
     nellisauction.com directly, so it is rate-limited and cached aggressively.

See NELLIS_API.md for the discovered contract (endpoints, params, field map).

This module is read-only. It never authenticates or submits anything.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Config, load_config
from .models import Lot

# ---- Discovered Algolia contract (public, browser-embedded search creds) ----
ALGOLIA_APP_ID = "GL1QVP8R29"
ALGOLIA_SEARCH_KEY = "d22f83c614aa8eda28fa9eadda0d07b9"
ALGOLIA_INDEX = "nellisauction-prd"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

NELLIS_BASE = "https://www.nellisauction.com"
LOCATION_FACET = "Location Name"
CATEGORY_L1_FACET = "Taxonomy Level 1"
CATEGORY_L2_FACET = "Taxonomy Level 2"

_PREMIUM_RE = re.compile(r"Buyers Premium</p><p>(\d+(?:\.\d+)?)%")


class FeedError(RuntimeError):
    pass


def _epoch_to_utc(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class DiskCache:
    """Tiny JSON file cache keyed by a string, with per-entry TTL."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        import hashlib

        h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.dir / f"{h}.json"

    def get(self, key: str, ttl: int) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        if ttl >= 0 and (time.time() - path.stat().st_mtime) > ttl:
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, value: dict) -> None:
        try:
            self._path(key).write_text(json.dumps(value), "utf-8")
        except OSError:
            pass


class NellisFeed:
    """Polite, cached client for the Nellis Algolia index + lot detail pages."""

    def __init__(self, config: Config | None = None, client: httpx.Client | None = None):
        self.config = config or load_config()
        self.cache = DiskCache(self.config.cache_dir)
        self._client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": self.config.user_agent},
        )
        self._owns_client = client is None
        self._last_site_request = 0.0  # monotonic; throttles nellisauction.com only

    # -- lifecycle --
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "NellisFeed":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- rate limiting (applies to nellisauction.com, the ToS-sensitive host) --
    def _throttle(self) -> None:
        wait = self.config.request_interval_seconds - (time.monotonic() - self._last_site_request)
        if wait > 0:
            time.sleep(wait)
        self._last_site_request = time.monotonic()

    def _get_site(self, url: str, *, max_retries: int = 4) -> httpx.Response:
        """GET nellisauction.com with throttling + exponential backoff on 429/5xx."""
        backoff = 2.0
        for attempt in range(max_retries):
            self._throttle()
            resp = self._client.get(url)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == max_retries - 1:
                    raise FeedError(f"{url} failed after {max_retries} tries: {resp.status_code}")
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        raise FeedError(f"{url}: exhausted retries")

    # -- Algolia search stage --
    def _algolia_query(self, params: str, *, ttl: int | None = None) -> dict:
        ttl = self.config.search_cache_ttl if ttl is None else ttl
        cache_key = f"algolia::{params}"
        cached = self.cache.get(cache_key, ttl)
        if cached is not None:
            return cached
        body = {"params": params}
        resp = self._client.post(
            ALGOLIA_URL,
            headers={
                "X-Algolia-API-Key": ALGOLIA_SEARCH_KEY,
                "X-Algolia-Application-Id": ALGOLIA_APP_ID,
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code == 429:
            raise FeedError("Algolia rate limited (429)")
        resp.raise_for_status()
        data = resp.json()
        self.cache.set(cache_key, data)
        return data

    @staticmethod
    def _build_params(
        *,
        query: str = "",
        location: str | None = None,
        category: str | None = None,
        hits_per_page: int = 100,
        page: int = 0,
        closing_within_hours: float | None = None,
    ) -> str:
        facet_filters: list[list[str]] = []
        if location:
            facet_filters.append([f"{LOCATION_FACET}:{location}"])
        if category:
            # Match either taxonomy level; whichever the category string belongs to.
            facet_filters.append(
                [f"{CATEGORY_L2_FACET}:{category}", f"{CATEGORY_L1_FACET}:{category}"]
            )

        parts = [
            f"query={quote(query)}",
            f"hitsPerPage={hits_per_page}",
            f"page={page}",
            # The index's default attributesToRetrieve omits retail/taxonomy;
            # ask for everything so each hit is self-contained.
            f"attributesToRetrieve={quote(json.dumps(['*']))}",
        ]
        if facet_filters:
            parts.append(f"facetFilters={quote(json.dumps(facet_filters))}")
        if closing_within_hours is not None:
            cutoff = int(time.time() + closing_within_hours * 3600)
            now = int(time.time())
            # "Date Closed" is a unix-epoch numeric attribute on each hit.
            parts.append(
                f"numericFilters={quote(json.dumps([f'Date Closed>={now}', f'Date Closed<={cutoff}']))}"
            )
        return "&".join(parts)

    @staticmethod
    def _hit_to_lot(hit: dict) -> Lot:
        lot_id = str(hit.get("objectID"))
        cond_bits = []
        if hit.get("Item Condition"):
            cond_bits.append(str(hit["Item Condition"]))
        if hit.get("Is Damaged") == "Yes":
            cond_bits.append("Damaged")
        if hit.get("Missing Parts") == "Yes":
            cond_bits.append("Missing Parts")
        if hit.get("In Package") == "Yes":
            cond_bits.append("In Package")
        condition = ", ".join(cond_bits) or None

        return Lot(
            lot_id=lot_id,
            title=hit.get("Lead Description") or f"Lot {lot_id}",
            location=hit.get("Location Name") or "",
            url=f"{NELLIS_BASE}/p/x/{lot_id}",
            retail_price=hit.get("Suggested Retail"),
            condition=condition,
            close_time=_epoch_to_utc(hit.get("Date Closed") or hit.get("Time Remaining")),
            category=hit.get("Taxonomy Level 2") or hit.get("Taxonomy Level 1"),
            category_l1=hit.get("Taxonomy Level 1"),
            image_url=hit.get("Photo"),
            auction_event_name=hit.get("Auction Event Name"),
            auction_event_type=hit.get("Auction Event Type"),
            brand=hit.get("Brand"),
        )

    def search(
        self,
        location: str | None = None,
        category: str | None = None,
        keywords: str | None = None,
        max_results: int = 100,
        closing_within_hours: float | None = None,
    ) -> list[Lot]:
        """Search the Algolia index. Returns Lots WITHOUT live bid data (use
        `enrich_detail`/`get_lot` to add current_bid + bid_count + premium)."""
        lots: list[Lot] = []
        page = 0
        per_page = min(max_results, 100)
        while len(lots) < max_results:
            params = self._build_params(
                query=keywords or "",
                location=location,
                category=category,
                hits_per_page=per_page,
                page=page,
                closing_within_hours=closing_within_hours,
            )
            data = self._algolia_query(params)
            hits = data.get("hits", [])
            if not hits:
                break
            lots.extend(self._hit_to_lot(h) for h in hits)
            if page >= data.get("nbPages", 1) - 1:
                break
            page += 1
        return lots[:max_results]

    # -- per-lot detail stage (current bid, bid count, premium) --
    def enrich_detail(self, lot: Lot, *, ttl: int | None = None) -> Lot:
        """Fetch the live detail page and fill in current_bid, bid_count,
        premium, market_status. Mutates and returns the same Lot."""
        ttl = self.config.detail_cache_ttl if ttl is None else ttl
        cache_key = f"detail::{lot.lot_id}"
        detail = self.cache.get(cache_key, ttl)
        if detail is None:
            detail = self._fetch_detail_raw(lot.lot_id)
            self.cache.set(cache_key, detail)
        lot.current_bid = detail.get("current_bid")
        lot.bid_count = detail.get("bid_count")
        lot.premium = detail.get("premium")
        lot.market_status = detail.get("market_status")
        if detail.get("retail_price") is not None and lot.retail_price is None:
            lot.retail_price = detail["retail_price"]
        if detail.get("close_time") is not None:
            lot.close_time = _epoch_to_utc(detail["close_time"])
        return lot

    def _fetch_detail_raw(self, lot_id: str) -> dict:
        """Pull bid/premium fields out of the Remix-rendered detail page."""
        resp = self._get_site(f"{NELLIS_BASE}/p/x/{lot_id}")
        html = resp.text
        out: dict = {"lot_id": lot_id}

        # The lot object is embedded in the Remix context as JSON. Grab the
        # numeric fields directly rather than parsing the whole blob.
        def _num(pattern: str) -> float | None:
            m = re.search(pattern, html)
            return float(m.group(1)) if m else None

        out["current_bid"] = _num(r'"currentPrice":\s*(-?\d+(?:\.\d+)?)')
        bc = _num(r'"bidCount":\s*(\d+)')
        out["bid_count"] = int(bc) if bc is not None else None
        out["retail_price"] = _num(r'"retailPrice":\s*(\d+(?:\.\d+)?)')

        m_status = re.search(r'"marketStatus":\s*"([^"]+)"', html)
        out["market_status"] = m_status.group(1) if m_status else None

        m_close = re.search(r'"closeTime":\{"__type":"Date","value":"([^"]+)"', html)
        if m_close:
            try:
                dt = datetime.fromisoformat(m_close.group(1).replace("Z", "+00:00"))
                out["close_time"] = dt.timestamp()
            except ValueError:
                pass

        m_prem = _PREMIUM_RE.search(html)
        out["premium"] = (float(m_prem.group(1)) / 100.0) if m_prem else None
        return out

    def get_lot(self, lot_id: str) -> Lot | None:
        """Fetch a single lot fully scored-ready: Algolia metadata + live detail.

        Algolia is filtered by objectID to recover title/retail/category, then the
        detail page adds the live bid. Falls back to detail-only if not in index."""
        data = self._algolia_query(
            self._build_params(query="", hits_per_page=1)
            + f"&filters={quote(f'objectID:{lot_id}')}",
            ttl=self.config.detail_cache_ttl,
        )
        hits = data.get("hits", [])
        if hits:
            lot = self._hit_to_lot(hits[0])
        else:
            lot = Lot(lot_id=lot_id, title=f"Lot {lot_id}", location="", url=f"{NELLIS_BASE}/p/x/{lot_id}")
        try:
            self.enrich_detail(lot)
        except (FeedError, httpx.HTTPError):
            return lot if hits else None
        return lot

    # -- introspection helpers --
    def category_facets(self, location: str | None = None) -> dict[str, dict]:
        facets = quote(json.dumps([CATEGORY_L1_FACET, CATEGORY_L2_FACET]))
        params = (
            self._build_params(location=location, hits_per_page=0)
            + f"&facets={facets}&maxValuesPerFacet=200"
        )
        data = self._algolia_query(params, ttl=self.config.search_cache_ttl)
        return data.get("facets", {})


def _cli() -> None:
    """Small CLI for Part A verification: confirm clean JSON comes back."""
    import argparse

    ap = argparse.ArgumentParser(description="Nellis feed probe (Part A verification)")
    ap.add_argument("--location", default="Mesa")
    ap.add_argument("--category", default=None)
    ap.add_argument("--keywords", default=None)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--lot", default=None, help="Fetch a single lot by id (with live bid)")
    ap.add_argument("--categories", action="store_true", help="List category facet values")
    args = ap.parse_args()

    with NellisFeed() as feed:
        if args.categories:
            facets = feed.category_facets(args.location)
            print(json.dumps(facets.get(CATEGORY_L2_FACET, {}), indent=2))
            return
        if args.lot:
            lot = feed.get_lot(args.lot)
            print(lot.model_dump_json(indent=2) if lot else "not found")
            return
        lots = feed.search(
            location=args.location,
            category=args.category,
            keywords=args.keywords,
            max_results=args.limit,
        )
        print(f"# {len(lots)} lots from {args.location}")
        for lot in lots:
            print(json.dumps(lot.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    _cli()
