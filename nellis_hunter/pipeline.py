"""Part C — the sweep pipeline: discover -> estimate -> enrich -> score -> dedup -> rank.

Reused verbatim by both the scheduled run (`run.py`) and the MCP server (`mcp_server.py`).

Efficiency/politeness design: Algolia discovery is cheap, so we pull all configured
location x category candidates up front and pre-score them on retail alone. We then
enrich only the most promising `MAX_DETAIL_FETCHES` with a per-lot detail fetch (the
rate-limited, ToS-sensitive call) to get the live bid before final scoring + ranking.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Config, load_config
from .db import NellisDB
from .feed import FeedError, NellisFeed
from .models import Lot, ScoredLot, Verdict
from .resale import HeuristicResaleEstimator, ResaleEstimator, build_estimator
from .scoring import score


@dataclass
class SweepResult:
    scored: list[ScoredLot]          # everything scored this run
    surfaced: list[ScoredLot]        # ranked shortlist that passed dedup
    candidates_seen: int
    detail_fetches: int


def _dedup_lots(lots: list[Lot]) -> list[Lot]:
    seen: dict[str, Lot] = {}
    for lot in lots:
        # Prefer the richer record if a lot appears under multiple categories.
        if lot.lot_id not in seen or (lot.retail_price and not seen[lot.lot_id].retail_price):
            seen[lot.lot_id] = lot
    return list(seen.values())


class Pipeline:
    def __init__(
        self,
        config: Config | None = None,
        feed: NellisFeed | None = None,
        estimator: ResaleEstimator | None = None,
    ):
        self.config = config or load_config()
        self.feed = feed or NellisFeed(self.config)
        # Two estimators on purpose: the prescore one ranks EVERY discovered
        # candidate, so it must be cheap (heuristic, no network). The final one
        # re-scores only the small enriched shortlist, so it can be the v2 eBay
        # estimator (one polite eBay request per shortlisted lot, bounded by
        # MAX_DETAIL_FETCHES). Both default from config; a passed estimator wins.
        self.prescore_estimator = HeuristicResaleEstimator()
        self.estimator = estimator or build_estimator(self.config)

    # -- discovery + pre-scoring (Algolia only) --
    def discover(
        self,
        locations: list[str] | None = None,
        categories: list[str] | None = None,
        keywords: str | None = None,
        per_query: int = 100,
        closing_within_hours: float | None = None,
    ) -> list[Lot]:
        locations = locations or self.config.locations
        categories = categories if categories is not None else self.config.flip_categories
        cat_list: list[str | None] = list(categories) if categories else [None]
        out: list[Lot] = []
        for loc in locations:
            for cat in cat_list:
                out.extend(
                    self.feed.search(
                        location=loc,
                        category=cat,
                        keywords=keywords,
                        max_results=per_query,
                        closing_within_hours=closing_within_hours,
                    )
                )
        return _dedup_lots(out)

    def _prescore(self, lot: Lot) -> ScoredLot:
        # Cheap, network-free pre-score to decide which lots merit a detail fetch.
        est = self.prescore_estimator.estimate(lot)
        return ScoredLot(lot=lot, scoring=score(lot, est, self.config.scoring))

    # -- full sweep --
    def sweep(
        self,
        locations: list[str] | None = None,
        categories: list[str] | None = None,
        keywords: str | None = None,
        closing_within_hours: float | None = None,
        max_detail_fetches: int | None = None,
        db: NellisDB | None = None,
    ) -> SweepResult:
        max_details = (
            max_detail_fetches if max_detail_fetches is not None else self.config.max_detail_fetches
        )

        lots = self.discover(
            locations=locations,
            categories=categories,
            keywords=keywords,
            closing_within_hours=closing_within_hours,
        )

        # Pre-score on retail to pick which lots merit a live-bid fetch. A lot with
        # a big max_bid headroom is worth the polite detail request; junk isn't.
        prescored = [self._prescore(lot) for lot in lots]
        worth_enriching = [s for s in prescored if s.scoring.max_bid is not None]
        worth_enriching.sort(key=lambda s: s.scoring.max_bid or 0, reverse=True)

        detail_fetches = 0
        consecutive_failures = 0
        enriched_ids: set[str] = set()
        for s in worth_enriching[:max_details]:
            try:
                self.feed.enrich_detail(s.lot)
            except (FeedError, httpx.HTTPError):
                consecutive_failures += 1
                # Tolerate the odd hiccup, but abort the stage cleanly if the
                # site is repeatedly failing rather than hammering it.
                if consecutive_failures >= 3:
                    break
                continue
            consecutive_failures = 0
            detail_fetches += 1
            # Re-score now that we know the live bid + per-lot premium.
            est = self.estimator.estimate(s.lot)
            s.scoring = score(s.lot, est, self.config.scoring)
            enriched_ids.add(s.lot.lot_id)

        # Rank surviving lots by projected margin at current bid (desc). Lots we
        # couldn't enrich (no live bid) sort last via the WATCH/None fallback.
        def rank_key(s: ScoredLot) -> float:
            m = s.scoring.projected_margin_at_current_bid
            return m if m is not None else float("-inf")

        # Dedup against persistent store + apply surface rules.
        surfaced: list[ScoredLot] = []
        for s in sorted(prescored, key=rank_key, reverse=True):
            if s.scoring.verdict in (Verdict.SKIP,):
                continue
            should = True
            if db is not None:
                should = db.should_surface(s, self.config.resurface_within_hours)
            if should and s.lot.lot_id in enriched_ids and s.scoring.verdict == Verdict.BID:
                surfaced.append(s)
            elif should and s.scoring.verdict == Verdict.NEEDS_MANUAL:
                surfaced.append(s)  # flag for manual eBay comp
            elif should and s.scoring.verdict == Verdict.WATCH and s.lot.lot_id in enriched_ids:
                surfaced.append(s)

        surfaced = surfaced[: self.config.max_digest_lots]

        if db is not None:
            db.record_run(prescored, surfaced=surfaced)
            for s in prescored:
                db.mark_seen(s, surfaced=s in surfaced)

        return SweepResult(
            scored=prescored,
            surfaced=surfaced,
            candidates_seen=len(lots),
            detail_fetches=detail_fetches,
        )

    def close(self) -> None:
        self.feed.close()
        _close_estimator(self.estimator)


def _close_estimator(estimator: ResaleEstimator) -> None:
    """Best-effort close of any eBay HTTP client held by the estimator tree."""
    from .resale import CompositeResaleEstimator, EbayResaleEstimator

    if isinstance(estimator, CompositeResaleEstimator):
        for sub in estimator.estimators:
            _close_estimator(sub)
    elif isinstance(estimator, EbayResaleEstimator):
        estimator.client.close()
