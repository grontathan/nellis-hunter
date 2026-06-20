"""Part D — MCP server (FastMCP, stdio).

Wraps the same feed + scoring used by the pipeline so the hunt is queryable
conversationally from Claude Code:

    claude mcp add nellis-hunter -- python -m nellis_hunter.mcp_server

Tool outputs are deliberately small and structured (`ScoredLot.summary()`) — the
model weighs every tool every turn, so a tight schema keeps it fast and accurate.

Read-only, always: these tools never bid or take any account action.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .feed import NellisFeed
from .models import ScoredLot, Verdict
from .resale import FixedResaleEstimator, HeuristicResaleEstimator, build_estimator
from .scoring import all_in as all_in_calc
from .scoring import max_bid as max_bid_calc
from .scoring import score

mcp = FastMCP("nellis-hunter")

_config = load_config()
# Fast heuristic for the bulk, Algolia-only `search_nellis`; the configured
# estimator (eBay sold comps when RESALE_SOURCE=ebay) for single-lot lookups.
_estimator = HeuristicResaleEstimator()
_lot_estimator = build_estimator(_config)


def _feed() -> NellisFeed:
    return NellisFeed(_config)


@mcp.tool()
def search_nellis(
    location: str = "Mesa",
    category: str | None = None,
    keywords: str | None = None,
    max_results: int = 20,
    closing_within_hours: float | None = None,
) -> list[dict]:
    """Search Nellis lots for a location ('Phoenix' or 'Mesa') with heuristic
    scoring. Fast: uses Algolia metadata only (no live-bid fetch), so verdicts are
    WATCH/NEEDS_MANUAL. Use get_lot for the live bid + a final BID/SKIP call."""
    with _feed() as feed:
        lots = feed.search(
            location=location,
            category=category,
            keywords=keywords,
            max_results=min(max_results, 50),
            closing_within_hours=closing_within_hours,
        )
        out = []
        for lot in lots:
            est = _estimator.estimate(lot)
            out.append(ScoredLot(lot=lot, scoring=score(lot, est, _config.scoring)).summary())
        return out


@mcp.tool()
def get_lot(lot_id: str) -> dict | None:
    """Fetch a single lot fully enriched with the live current bid + per-lot
    buyer's premium, then score it. Returns null if the lot can't be found."""
    with _feed() as feed:
        lot = feed.get_lot(lot_id)
        if lot is None:
            return None
        est = _lot_estimator.estimate(lot)
        return ScoredLot(lot=lot, scoring=score(lot, est, _config.scoring)).summary()


@mcp.tool()
def score_lot(lot_id: str, resale_estimate: float, target_profit: float = 25.0) -> dict | None:
    """Re-score a lot using a HUMAN-SUPPLIED resale value (e.g. a real eBay sold
    comp), overriding the heuristic. This is how to turn a NEEDS_MANUAL lot into a
    real BID/SKIP call. Fetches the live bid first."""
    with _feed() as feed:
        lot = feed.get_lot(lot_id)
        if lot is None:
            return None
        est = FixedResaleEstimator(resale_estimate).estimate(lot)
        sc = score(lot, est, _config.scoring, target_profit=target_profit)
        return ScoredLot(lot=lot, scoring=sc).summary()


@mcp.tool()
def hot_deals(
    location: str | None = None,
    min_margin: float = 20.0,
    closing_within_hours: float | None = None,
    max_results: int = 15,
) -> list[dict]:
    """Ranked BID-verdict lots across the configured flip categories, enriched with
    live bids. `location` null = both Phoenix + Mesa. Returns the best margins first.
    This is the on-demand version of the daily digest."""
    from .pipeline import Pipeline

    locations = [location] if location else None
    pipeline = Pipeline(_config, feed=_feed())  # uses the configured estimator
    try:
        result = pipeline.sweep(
            locations=locations,
            closing_within_hours=closing_within_hours,
            db=None,
        )
    finally:
        pipeline.close()

    deals = [
        s
        for s in result.scored
        if s.scoring.verdict == Verdict.BID
        and (s.scoring.projected_margin_at_current_bid or 0) >= min_margin
    ]
    deals.sort(key=lambda s: s.scoring.projected_margin_at_current_bid or 0, reverse=True)
    return [s.summary() for s in deals[:max_results]]


@mcp.tool()
def max_bid(target_all_in: float, premium: float | None = None, tax: float | None = None) -> dict:
    """Cost what-if: the largest hammer bid whose out-the-door cost stays at/under
    `target_all_in`, given the buyer's premium + sales tax."""
    p = _config.scoring.premium if premium is None else premium
    t = _config.scoring.tax if tax is None else tax
    return {
        "target_all_in": target_all_in,
        "premium": p,
        "tax": t,
        "max_bid": round(max_bid_calc(target_all_in, p, t), 2),
    }


@mcp.tool()
def all_in(hammer: float, premium: float | None = None, tax: float | None = None) -> dict:
    """Cost what-if: the out-the-door cost of winning at `hammer`, including the
    buyer's premium and sales tax."""
    p = _config.scoring.premium if premium is None else premium
    t = _config.scoring.tax if tax is None else tax
    return {
        "hammer": hammer,
        "premium": p,
        "tax": t,
        "all_in": round(all_in_calc(hammer, p, t), 2),
    }


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
