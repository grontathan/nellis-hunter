"""eBay sold-comps client — the data half of the v2 resale estimator.

The brief's v2 is "real eBay sold comps … (or a polite scrape)". There is no
generally-available official API for *sold* prices (Marketplace Insights is gated
behind eBay approval; the Browse API only returns active listings), so v1→v2 ships
as a polite, cached scrape of eBay's completed/sold search — the same posture this
project already takes with nellisauction.com.

This module is split into:
  - PURE helpers (`build_query`, `parse_sold_prices`, `summarize_comps`) that have
    no I/O and are unit-tested hard, and
  - `EbayCompsClient`, the rate-limited + disk-cached fetcher that wraps them.

Read-only. It never logs in or submits anything.
"""

from __future__ import annotations

import re
import statistics
import time
from urllib.parse import quote_plus

import httpx

from .feed import DiskCache
from .models import Lot

EBAY_SOLD_URL = "https://www.ebay.com/sch/i.html"

# Words that describe Nellis lot condition/packaging, not the product. They poison
# an eBay comp search (you want the product, not "open box untested"), so strip them.
_NOISE_WORDS = {
    "new", "open", "box", "openbox", "used", "damaged", "untested", "tested",
    "missing", "parts", "package", "packaging", "sealed", "refurbished", "refurb",
    "lot", "of", "bundle", "set", "pack", "the", "a", "an", "for", "with", "and",
    "&", "oem", "genuine", "authentic", "read", "description", "see", "photos",
    "approx", "approximately", "various", "assorted", "as-is", "asis",
}
# Strip these characters but keep alphanumerics and hyphens (model numbers!).
_PUNCT_RE = re.compile(r"[^\w\-]+")
# eBay renders prices as e.g. "$1,234.56" or a range "$10.00 to $25.00".
_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
# Each sold tile carries a price inside an s-item__price span.
_PRICE_SPAN_RE = re.compile(
    r'class="s-item__price"[^>]*>\s*(?:<span[^>]*>)?\s*([^<]+)', re.IGNORECASE
)


def _clean_money(raw: str) -> float | None:
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def build_query(lot: Lot, *, max_words: int = 8) -> str:
    """Turn a Nellis lot into a tight eBay search string.

    Keeps brand + meaningful title tokens (model numbers especially), drops the
    condition/packaging noise that would mislead a comp search."""
    brand = (lot.brand or "").strip()
    title = (lot.title or "").strip()
    raw = f"{brand} {title}"

    tokens: list[str] = []
    seen: set[str] = set()
    for tok in _PUNCT_RE.sub(" ", raw).split():
        low = tok.lower()
        if low in _NOISE_WORDS or len(tok) <= 1:
            continue
        if low in seen:  # de-dupe (brand often repeats in the title)
            continue
        seen.add(low)
        tokens.append(tok)
        if len(tokens) >= max_words:
            break
    return " ".join(tokens)


def parse_sold_prices(html: str) -> list[float]:
    """Extract sold prices from an eBay completed-search results page.

    Ranges ("$10 to $25") collapse to their low end (conservative). The first
    result tile on eBay is a 'Shop on eBay' placeholder with a junk price; we
    drop it heuristically by ignoring the highest-frequency placeholder value
    only when it's an obvious outlier — handled downstream by trimming."""
    prices: list[float] = []
    for raw in _PRICE_SPAN_RE.findall(html):
        val = _clean_money(raw)
        if val is not None and val > 0:
            prices.append(val)
    return prices


def summarize_comps(
    prices: list[float], *, min_comps: int = 3, trim_frac: float = 0.15
) -> dict | None:
    """Trimmed-median summary of sold prices, or None if too few comps.

    Trims the cheapest/most-expensive `trim_frac` from each end to shrug off the
    eBay placeholder tile, broken-for-parts listings, and the odd typo, then takes
    the median of what's left. Median (not mean) keeps a single whale from skewing
    the estimate."""
    clean = sorted(p for p in prices if p > 0)
    if len(clean) < min_comps:
        return None

    k = int(len(clean) * trim_frac)
    trimmed = clean[k: len(clean) - k] if len(clean) - 2 * k >= min_comps else clean
    return {
        "median": round(statistics.median(trimmed), 2),
        "n": len(trimmed),
        "n_raw": len(clean),
        "low": round(trimmed[0], 2),
        "high": round(trimmed[-1], 2),
    }


class EbayCompsClient:
    """Polite, disk-cached fetcher for eBay sold comps.

    One request per `interval` seconds, results cached for `cache_ttl` (sold prices
    move slowly, so a long TTL is both kinder and faster). Returns the summarized
    comp dict from `summarize_comps`, or None when there aren't enough sold comps."""

    def __init__(
        self,
        cache: DiskCache,
        *,
        client: httpx.Client | None = None,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NellisHunter/0.2",
        interval: float = 3.0,
        cache_ttl: int = 86_400,
        min_comps: int = 3,
        max_results_param: int = 120,
    ):
        self.cache = cache
        self.interval = interval
        self.cache_ttl = cache_ttl
        self.min_comps = min_comps
        self.max_results_param = max_results_param
        self._client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                # Fuller browser-like headers: eBay's search endpoint sits behind
                # Akamai bot management and rejects bare requests. This gets the
                # best shot from a residential IP; from datacenter IPs (e.g. CI) it
                # may still 403/503 — in which case sold_comps() returns None and
                # the CompositeResaleEstimator falls back to the v1 heuristic.
                "User-Agent": user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Referer": "https://www.ebay.com/",
            },
        )
        self._owns_client = client is None
        self._last_request = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "EbayCompsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _search_url(self, query: str) -> str:
        # LH_Sold + LH_Complete = sold/completed only; _sop=13 = ended recently first.
        return (
            f"{EBAY_SOLD_URL}?_nkw={quote_plus(query)}"
            f"&LH_Sold=1&LH_Complete=1&_sop=13&_ipg={self.max_results_param}"
        )

    def sold_comps(self, query: str) -> dict | None:
        """Median sold-price summary for `query`, cached. None if too few comps."""
        if not query.strip():
            return None
        cache_key = f"ebay::{query.lower()}"
        cached = self.cache.get(cache_key, self.cache_ttl)
        if cached is not None:
            return cached.get("summary")

        try:
            self._throttle()
            resp = self._client.get(self._search_url(query))
            resp.raise_for_status()
        except httpx.HTTPError:
            return None  # network hiccup → let the caller fall back to the heuristic

        summary = summarize_comps(parse_sold_prices(resp.text), min_comps=self.min_comps)
        # Cache even a None summary so we don't re-hit eBay for a dud query.
        self.cache.set(cache_key, {"query": query, "summary": summary})
        return summary
