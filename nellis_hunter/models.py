"""Pydantic models shared across the feed, scoring, pipeline, and MCP server."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

LOCAL_TZ = ZoneInfo("America/Phoenix")  # Phoenix + Mesa share this tz (no DST)


class Verdict(str, Enum):
    BID = "BID"
    WATCH = "WATCH"
    SKIP = "SKIP"
    NEEDS_MANUAL = "NEEDS_MANUAL"  # no resale estimate available


class Lot(BaseModel):
    """A single auction lot. Fields marked (detail) require a per-lot fetch and
    may be None when the lot came only from the Algolia search stage."""

    lot_id: str
    title: str
    location: str  # "Phoenix" or "Mesa" (Algolia Location Name)
    url: str

    retail_price: float | None = None
    current_bid: float | None = None  # (detail) Algolia does not expose live price
    bid_count: int | None = None  # (detail)
    premium: float | None = None  # (detail) per-lot buyer's premium, e.g. 0.15

    condition: str | None = None
    close_time: datetime | None = None  # UTC
    category: str | None = None  # taxonomy level 2 (or level 1 fallback)
    category_l1: str | None = None
    image_url: str | None = None

    auction_event_name: str | None = None
    auction_event_type: str | None = None
    brand: str | None = None
    market_status: str | None = None  # (detail) e.g. "open"

    @property
    def close_time_local(self) -> datetime | None:
        if self.close_time is None:
            return None
        return self.close_time.astimezone(LOCAL_TZ)

    @property
    def hours_until_close(self) -> float | None:
        if self.close_time is None:
            return None
        delta = self.close_time - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0


class Scoring(BaseModel):
    """Output of the scoring engine for a lot. Pure derived numbers."""

    resale_estimate: float | None = None
    premium_used: float = 0.15
    all_in_at_current_bid: float | None = None
    net_resale: float | None = None
    projected_margin_at_current_bid: float | None = None
    margin_pct: float | None = None
    max_bid: float | None = None  # max bid to hit TARGET_PROFIT
    target_profit: float = 25.0
    verdict: Verdict = Verdict.NEEDS_MANUAL
    reason: str = ""


class ScoredLot(BaseModel):
    lot: Lot
    scoring: Scoring

    @property
    def lot_id(self) -> str:
        return self.lot.lot_id

    def summary(self) -> dict:
        """Compact dict for MCP tool output — small and structured on purpose."""
        lot, sc = self.lot, self.scoring
        local = lot.close_time_local
        return {
            "lot_id": lot.lot_id,
            "title": lot.title,
            "location": lot.location,
            "verdict": sc.verdict.value,
            "current_bid": lot.current_bid,
            "max_bid": round(sc.max_bid, 2) if sc.max_bid is not None else None,
            "retail_price": lot.retail_price,
            "resale_estimate": (
                round(sc.resale_estimate, 2) if sc.resale_estimate is not None else None
            ),
            "projected_margin": (
                round(sc.projected_margin_at_current_bid, 2)
                if sc.projected_margin_at_current_bid is not None
                else None
            ),
            "margin_pct": round(sc.margin_pct, 3) if sc.margin_pct is not None else None,
            "condition": lot.condition,
            "category": lot.category,
            "closes_local": local.strftime("%Y-%m-%d %I:%M%p %Z") if local else None,
            "hours_until_close": (
                round(lot.hours_until_close, 1) if lot.hours_until_close is not None else None
            ),
            "url": lot.url,
        }
