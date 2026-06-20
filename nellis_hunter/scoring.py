"""Part B — the scoring engine (the core IP).

PURE module: no I/O, no network, no globals. Everything is a function of its
inputs so it can be unit-tested hard and reused identically by the pipeline and
the MCP server. A regression here costs real money, so the cost formula is locked
by tests in tests/test_scoring.py.

Cost model (Nellis):
    Nellis charges a buyer's premium on the hammer, then sales tax on
    (hammer + premium):

        all_in(hammer) = hammer * (1 + PREMIUM) * (1 + TAX)

    Inverting to find the max bid that lands on a target out-the-door cost:

        max_bid(target_all_in) = target_all_in / ((1 + PREMIUM) * (1 + TAX))

Flip model (eBay resale):
        net_resale = resale * (1 - EBAY_FEE_RATE) - EBAY_FLAT_FEE - SHIP_COST
        projected_margin(hammer) = net_resale - all_in(hammer)
        max_bid_for_profit(target) = max_bid(net_resale - target)
"""

from __future__ import annotations

from .config import ScoringConfig
from .models import Lot, Scoring, Verdict

# ---- Core cost math (the part tests pin to hand-checked values) ----


def all_in(hammer: float, premium: float, tax: float) -> float:
    """Out-the-door cost for a winning hammer bid."""
    return hammer * (1.0 + premium) * (1.0 + tax)


def max_bid(target_all_in: float, premium: float, tax: float) -> float:
    """Largest hammer bid whose all-in cost stays at/under `target_all_in`."""
    return target_all_in / ((1.0 + premium) * (1.0 + tax))


def net_resale(
    resale_estimate: float,
    *,
    ebay_fee_rate: float,
    ebay_flat_fee: float,
    ship_cost: float,
) -> float:
    """What lands in your pocket after eBay fees + shipping on a sale."""
    return resale_estimate * (1.0 - ebay_fee_rate) - ebay_flat_fee - ship_cost


def projected_margin(
    hammer: float,
    resale_estimate: float,
    *,
    premium: float,
    tax: float,
    ebay_fee_rate: float,
    ebay_flat_fee: float,
    ship_cost: float,
) -> float:
    """Profit if you win at `hammer` and resell at `resale_estimate`."""
    nr = net_resale(
        resale_estimate,
        ebay_fee_rate=ebay_fee_rate,
        ebay_flat_fee=ebay_flat_fee,
        ship_cost=ship_cost,
    )
    return nr - all_in(hammer, premium, tax)


def max_bid_for_profit(
    target_profit: float,
    resale_estimate: float,
    *,
    premium: float,
    tax: float,
    ebay_fee_rate: float,
    ebay_flat_fee: float,
    ship_cost: float,
) -> float:
    """Highest hammer bid that still nets `target_profit` after all costs.

    Can be negative if the item can't clear the target at any bid — callers
    should treat <= 0 as 'never bid'."""
    nr = net_resale(
        resale_estimate,
        ebay_fee_rate=ebay_fee_rate,
        ebay_flat_fee=ebay_flat_fee,
        ship_cost=ship_cost,
    )
    return max_bid(nr - target_profit, premium, tax)


# ---- Lot-level scoring (composes the math + a verdict) ----


def score(
    lot: Lot,
    resale_estimate: float | None,
    config: ScoringConfig,
    *,
    ship_cost: float | None = None,
    target_profit: float | None = None,
) -> Scoring:
    """Score one lot. `resale_estimate` is the pluggable input (heuristic or a
    human/eBay comp). Premium prefers the lot's own value, else config default."""

    premium = lot.premium if lot.premium is not None else config.premium
    tax = config.tax
    ship = ship_cost if ship_cost is not None else config.default_ship_cost
    target = target_profit if target_profit is not None else config.target_profit

    sc = Scoring(
        resale_estimate=resale_estimate,
        premium_used=premium,
        target_profit=target,
    )

    # No resale estimate → can't do flip math. Surface for a manual eBay comp.
    if resale_estimate is None:
        sc.verdict = Verdict.NEEDS_MANUAL
        sc.reason = "No retail/resale estimate available; supply an eBay comp via score_lot."
        return sc

    nr = net_resale(
        resale_estimate,
        ebay_fee_rate=config.ebay_fee_rate,
        ebay_flat_fee=config.ebay_flat_fee,
        ship_cost=ship,
    )
    sc.net_resale = nr
    sc.max_bid = max_bid_for_profit(
        target,
        resale_estimate,
        premium=premium,
        tax=tax,
        ebay_fee_rate=config.ebay_fee_rate,
        ebay_flat_fee=config.ebay_flat_fee,
        ship_cost=ship,
    )

    # Current-bid economics need a known current bid.
    if lot.current_bid is None:
        sc.verdict = Verdict.WATCH
        sc.reason = "Resale known but live bid not yet fetched."
        return sc

    cb = lot.current_bid
    sc.all_in_at_current_bid = all_in(cb, premium, tax)
    sc.projected_margin_at_current_bid = nr - sc.all_in_at_current_bid
    # margin_pct is profit relative to your all-in cost. Guard divide-by-zero.
    if sc.all_in_at_current_bid > 0:
        sc.margin_pct = sc.projected_margin_at_current_bid / sc.all_in_at_current_bid
    else:
        sc.margin_pct = None

    sc.verdict, sc.reason = _verdict(cb, sc, config)
    return sc


def _verdict(current_bid: float, sc: Scoring, config: ScoringConfig) -> tuple[Verdict, str]:
    """BID if there's still headroom over the current bid AND the margin clears
    the threshold; WATCH if it's profitable but thin or already bid up; else SKIP."""
    headroom = (sc.max_bid is not None) and (sc.max_bid > current_bid)
    margin_ok = (sc.margin_pct is not None) and (sc.margin_pct >= config.min_margin_pct)
    profitable = (sc.projected_margin_at_current_bid or 0) > 0

    if headroom and margin_ok:
        return Verdict.BID, (
            f"max_bid ${sc.max_bid:.0f} > current ${current_bid:.0f}, "
            f"margin {sc.margin_pct:.0%} ≥ {config.min_margin_pct:.0%}."
        )
    if profitable and (headroom or margin_ok):
        # margin_pct is None when current_bid is $0 (all-in = 0, undefined ratio);
        # guard the format the same way the SKIP branch does.
        margin_str = f"{sc.margin_pct:.0%}" if sc.margin_pct is not None else "n/a"
        return Verdict.WATCH, (
            f"Profitable but borderline (margin {margin_str}, "
            f"max_bid ${sc.max_bid:.0f} vs current ${current_bid:.0f})."
        )
    return Verdict.SKIP, (
        f"Current ${current_bid:.0f} leaves too little: margin "
        f"{(sc.margin_pct or 0):.0%}, max_bid ${(sc.max_bid or 0):.0f}."
    )
