"""Configuration loading. All tunables live in .env (see .env.example).

`ScoringConfig` is intentionally a small, pure value object so `scoring.py` can
stay I/O-free and be tested in isolation. `Config` is the full app config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root once, on import. Real env vars win over .env.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _s(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw not in (None, "") else default


def _list(name: str, default: list[str]) -> list[str]:
    """Pipe-separated list (categories contain commas, so '|' is the delimiter)."""
    raw = os.getenv(name)
    if raw in (None, ""):
        return list(default)
    return [item.strip() for item in raw.split("|") if item.strip()]


@dataclass(frozen=True)
class ScoringConfig:
    """Pure numeric inputs to the cost/flip math. No I/O, fully unit-testable."""

    premium: float = 0.15
    tax: float = 0.083
    ebay_fee_rate: float = 0.1325
    ebay_flat_fee: float = 0.40
    default_ship_cost: float = 6.00
    target_profit: float = 25.0
    min_margin_pct: float = 0.30


@dataclass(frozen=True)
class Config:
    scoring: ScoringConfig

    # Sweep
    flip_categories: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)

    # Output
    discord_webhook_url: str = ""

    # Resale estimation (v2). "ebay" = real sold comps w/ heuristic fallback; "heuristic" = v1.
    resale_source: str = "heuristic"
    ebay_request_interval_seconds: float = 3.0
    ebay_cache_ttl: int = 86_400  # sold comps move slowly; cache a day
    ebay_min_comps: int = 3       # need at least this many sold comps to trust the median

    # Politeness
    request_interval_seconds: float = 2.5
    search_cache_ttl: int = 3600
    detail_cache_ttl: int = 1800
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NellisHunter/0.1 (personal research)"
    )

    # Pipeline tuning
    resurface_within_hours: float = 6.0
    max_detail_fetches: int = 60
    max_digest_lots: int = 25

    # Paths
    repo_root: Path = _REPO_ROOT
    cache_dir: Path = _REPO_ROOT / "cache"
    db_path: Path = _REPO_ROOT / "nellis_hunter.db"


def load_config() -> Config:
    scoring = ScoringConfig(
        premium=_f("PREMIUM", 0.15),
        tax=_f("TAX", 0.083),
        ebay_fee_rate=_f("EBAY_FEE_RATE", 0.1325),
        ebay_flat_fee=_f("EBAY_FLAT_FEE", 0.40),
        default_ship_cost=_f("DEFAULT_SHIP_COST", 6.00),
        target_profit=_f("TARGET_PROFIT", 25.0),
        min_margin_pct=_f("MIN_MARGIN_PCT", 0.30),
    )
    return Config(
        scoring=scoring,
        flip_categories=_list(
            "FLIP_CATEGORIES",
            [
                "Computers, Laptops, Tablets & Accessories",
                "Monitors & Monitor Stands",
                "Networking & Drives",
            ],
        ),
        locations=_list("LOCATIONS", ["Phoenix", "Mesa"]),
        discord_webhook_url=_s("DISCORD_WEBHOOK_URL", ""),
        resale_source=_s("RESALE_SOURCE", "heuristic"),
        ebay_request_interval_seconds=_f("EBAY_REQUEST_INTERVAL_SECONDS", 3.0),
        ebay_cache_ttl=_i("EBAY_CACHE_TTL", 86_400),
        ebay_min_comps=_i("EBAY_MIN_COMPS", 3),
        request_interval_seconds=_f("REQUEST_INTERVAL_SECONDS", 2.5),
        search_cache_ttl=_i("SEARCH_CACHE_TTL", 3600),
        detail_cache_ttl=_i("DETAIL_CACHE_TTL", 1800),
        user_agent=_s(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NellisHunter/0.1 (personal research)",
        ),
        resurface_within_hours=_f("RESURFACE_WITHIN_HOURS", 6.0),
        max_detail_fetches=_i("MAX_DETAIL_FETCHES", 60),
        max_digest_lots=_i("MAX_DIGEST_LOTS", 25),
    )
