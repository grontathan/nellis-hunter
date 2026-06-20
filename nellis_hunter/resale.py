"""Resale estimation — the pluggable, hardest-to-automate input.

`ResaleEstimator.estimate(lot) -> float | None` is the seam. v1 ships a retail-haircut
heuristic; a future v2 (real eBay sold comps via the Browse/Marketplace Insights API)
drops in behind the same interface with no change to scoring/pipeline/MCP.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .models import Lot

if TYPE_CHECKING:
    from .ebay import EbayCompsClient

# Fraction of MSRP a used/returned item realistically resells for on eBay,
# keyed by a substring of the lot's category (taxonomy). Configurable lookup.
# Conservative on purpose — better to skip a deal than overbid on a phantom margin.
CATEGORY_HAIRCUTS: dict[str, float] = {
    "computers, laptops": 0.60,
    "monitors": 0.62,
    "networking & drives": 0.65,  # RAM/SSD/networking hold value well
    "cell phones": 0.55,
    "cameras": 0.60,
    "headphones": 0.55,
    "speakers": 0.55,
    "tvs": 0.50,
    "wearable technology": 0.50,
    "printers": 0.45,
    "keyboards": 0.55,
    "computer mice": 0.55,
    "electronics": 0.55,  # generic electronics fallback
}
DEFAULT_HAIRCUT = 0.45  # non-electronics / unknown category

# Condition multipliers applied on top of the category haircut.
CONDITION_MULTIPLIERS: list[tuple[str, float]] = [
    ("damaged", 0.45),
    ("missing parts", 0.55),
    ("untested", 0.70),
    ("used", 0.90),
    ("open box", 0.92),
    ("new", 1.00),
]


def category_haircut(category: str | None) -> float:
    if not category:
        return DEFAULT_HAIRCUT
    c = category.lower()
    for key, frac in CATEGORY_HAIRCUTS.items():
        if key in c:
            return frac
    return DEFAULT_HAIRCUT


def condition_multiplier(condition: str | None) -> float:
    if not condition:
        return 0.85  # unknown condition — assume used-ish
    c = condition.lower()
    mult = 1.0
    # Worst applicable flag wins (take the minimum match).
    matched = [m for key, m in CONDITION_MULTIPLIERS if key in c]
    if matched:
        mult = min(matched)
    return mult


class ResaleEstimator(ABC):
    """Swappable resale-value estimator. Returns None when it can't estimate."""

    @abstractmethod
    def estimate(self, lot: Lot) -> float | None: ...


class HeuristicResaleEstimator(ResaleEstimator):
    """v1: resale ≈ retail * category_haircut * condition_multiplier.

    Returns None when the payload has no retail price → the lot is surfaced as
    NEEDS_MANUAL so a human can drop in a real eBay comp via `score_lot`."""

    def __init__(
        self,
        category_haircuts: dict[str, float] | None = None,
        default_haircut: float = DEFAULT_HAIRCUT,
    ):
        self.category_haircuts = category_haircuts or CATEGORY_HAIRCUTS
        self.default_haircut = default_haircut

    def _haircut(self, category: str | None) -> float:
        if not category:
            return self.default_haircut
        c = category.lower()
        for key, frac in self.category_haircuts.items():
            if key in c:
                return frac
        return self.default_haircut

    def estimate(self, lot: Lot) -> float | None:
        if lot.retail_price is None or lot.retail_price <= 0:
            return None
        haircut = self._haircut(lot.category or lot.category_l1)
        mult = condition_multiplier(lot.condition)
        return round(lot.retail_price * haircut * mult, 2)


class FixedResaleEstimator(ResaleEstimator):
    """Returns a human-supplied number regardless of lot. Used by the MCP
    `score_lot` tool to override the heuristic with a real eBay comp."""

    def __init__(self, value: float):
        self.value = value

    def estimate(self, lot: Lot) -> float | None:
        return self.value


class EbayResaleEstimator(ResaleEstimator):
    """v2: resale ≈ median eBay sold-comp price, condition-adjusted.

    Pulls real sold comps for the lot via `EbayCompsClient`, then applies the same
    condition multiplier the heuristic uses — a damaged/untested Nellis item sells
    below the median of its sold comps. Returns None when there aren't enough comps,
    so a caller can fall back to the heuristic (see `CompositeResaleEstimator`)."""

    def __init__(self, client: "EbayCompsClient", *, apply_condition: bool = True):
        self.client = client
        self.apply_condition = apply_condition

    def estimate(self, lot: Lot) -> float | None:
        from .ebay import build_query  # local import: keep resale.py I/O-free to import

        summary = self.client.sold_comps(build_query(lot))
        if not summary:
            return None
        median = summary["median"]
        if self.apply_condition:
            median *= condition_multiplier(lot.condition)
        return round(median, 2)


class CompositeResaleEstimator(ResaleEstimator):
    """Try each estimator in order; first non-None wins. Lets v2 (eBay comps)
    lead, then degrade gracefully to the v1 heuristic, then to None/NEEDS_MANUAL."""

    def __init__(self, estimators: list[ResaleEstimator]):
        self.estimators = estimators

    def estimate(self, lot: Lot) -> float | None:
        for est in self.estimators:
            value = est.estimate(lot)
            if value is not None:
                return value
        return None


def build_ebay_client(config) -> "EbayCompsClient":
    """Construct an `EbayCompsClient` wired to the app's disk cache + politeness
    settings. Kept here so callers don't have to import the feed/ebay internals."""
    from .ebay import EbayCompsClient
    from .feed import DiskCache

    return EbayCompsClient(
        DiskCache(config.cache_dir),
        user_agent=config.user_agent,
        interval=config.ebay_request_interval_seconds,
        cache_ttl=config.ebay_cache_ttl,
        min_comps=config.ebay_min_comps,
    )


def build_estimator(config, *, ebay_client: "EbayCompsClient | None" = None) -> ResaleEstimator:
    """Pick the resale estimator from config.

    RESALE_SOURCE=ebay  → eBay sold comps, falling back to the retail heuristic when
                          a lot has too few comps (best of both; the v2 default).
    RESALE_SOURCE=heuristic → retail-haircut only (v1; no eBay traffic).

    The returned estimator may hit the network, so the pipeline only runs it on the
    small enriched shortlist — never across every discovered candidate."""
    heuristic = HeuristicResaleEstimator()
    if getattr(config, "resale_source", "heuristic").lower() != "ebay":
        return heuristic
    client = ebay_client or build_ebay_client(config)
    return CompositeResaleEstimator([EbayResaleEstimator(client), heuristic])
