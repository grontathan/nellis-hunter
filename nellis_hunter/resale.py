"""Resale estimation — the pluggable, hardest-to-automate input.

`ResaleEstimator.estimate(lot) -> float | None` is the seam. v1 ships a retail-haircut
heuristic; a future v2 (real eBay sold comps via the Browse/Marketplace Insights API)
drops in behind the same interface with no change to scoring/pipeline/MCP.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Lot

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
