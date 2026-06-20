"""Lock the cost formula with hand-checked values. A regression here costs money."""

from datetime import datetime, timezone

import pytest

from nellis_hunter.config import ScoringConfig
from nellis_hunter.models import Lot, Verdict
from nellis_hunter.scoring import (
    all_in,
    max_bid,
    max_bid_for_profit,
    net_resale,
    projected_margin,
    score,
)

PREMIUM = 0.15
TAX = 0.083
CFG = ScoringConfig()  # defaults: premium .15, tax .083, fee .1325, flat .40, ship 6, target 25


def _lot(**kw) -> Lot:
    base = dict(
        lot_id="1",
        title="t",
        location="Mesa",
        url="https://www.nellisauction.com/p/x/1",
        close_time=datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Lot(**base)


# ---- all_in ----


def test_all_in_brief_anchor():
    # Brief: $120.50 bid -> ~$150 all-in at 15% + 8.3%
    assert all_in(120.50, PREMIUM, TAX) == pytest.approx(150.0767, abs=1e-3)


def test_all_in_zero():
    assert all_in(0, PREMIUM, TAX) == 0.0


def test_all_in_components():
    # 100 hammer -> +15% premium = 115 -> +8.3% tax = 124.545
    assert all_in(100, PREMIUM, TAX) == pytest.approx(124.545, abs=1e-6)


# ---- max_bid is the exact inverse of all_in ----


def test_max_bid_inverts_all_in():
    for hammer in (1.0, 37.5, 120.50, 999.99):
        target = all_in(hammer, PREMIUM, TAX)
        assert max_bid(target, PREMIUM, TAX) == pytest.approx(hammer, abs=1e-9)


def test_max_bid_target_150():
    assert max_bid(150.0, PREMIUM, TAX) == pytest.approx(120.4383, abs=1e-3)


# ---- net_resale ----


def test_net_resale_hand_checked():
    # 200 * (1-0.1325) - 0.40 - 6.00 = 173.5 - 6.40 = 167.10
    nr = net_resale(200.0, ebay_fee_rate=0.1325, ebay_flat_fee=0.40, ship_cost=6.00)
    assert nr == pytest.approx(167.10, abs=1e-6)


# ---- projected_margin ----


def test_projected_margin_hand_checked():
    # net_resale(200)=167.10 ; all_in(50)=62.2725 ; margin=104.8275
    m = projected_margin(
        50.0, 200.0, premium=PREMIUM, tax=TAX, ebay_fee_rate=0.1325, ebay_flat_fee=0.40, ship_cost=6.00
    )
    assert m == pytest.approx(104.8275, abs=1e-4)


def test_max_bid_for_profit_hand_checked():
    # net_resale(200)=167.10 ; target 25 -> max_bid(142.10) = 142.10/1.24545 = 114.094...
    mb = max_bid_for_profit(
        25.0, 200.0, premium=PREMIUM, tax=TAX, ebay_fee_rate=0.1325, ebay_flat_fee=0.40, ship_cost=6.00
    )
    assert mb == pytest.approx(114.094, abs=1e-2)
    # At exactly that hammer, realized profit equals the target.
    assert projected_margin(
        mb, 200.0, premium=PREMIUM, tax=TAX, ebay_fee_rate=0.1325, ebay_flat_fee=0.40, ship_cost=6.00
    ) == pytest.approx(25.0, abs=1e-2)


# ---- score() end to end ----


def test_score_needs_manual_without_retail():
    sc = score(_lot(retail_price=None), None, CFG)
    assert sc.verdict == Verdict.NEEDS_MANUAL
    assert sc.max_bid is None


def test_score_watch_without_current_bid():
    # Resale known but live bid not fetched yet.
    sc = score(_lot(retail_price=300), 200.0, CFG)
    assert sc.verdict == Verdict.WATCH
    assert sc.max_bid == pytest.approx(114.094, abs=1e-2)


def test_score_bid_verdict_when_cheap():
    # Current bid $30, resale $200 -> huge margin, headroom -> BID.
    sc = score(_lot(retail_price=300, current_bid=30.0), 200.0, CFG)
    assert sc.verdict == Verdict.BID
    assert sc.projected_margin_at_current_bid == pytest.approx(167.10 - all_in(30, PREMIUM, TAX), abs=1e-4)
    assert sc.margin_pct is not None and sc.margin_pct >= CFG.min_margin_pct


def test_score_skip_when_bid_too_high():
    # Current bid $140 exceeds max_bid(~114) and margin negative -> SKIP.
    sc = score(_lot(retail_price=300, current_bid=140.0), 200.0, CFG)
    assert sc.verdict == Verdict.SKIP
    assert sc.max_bid < 140.0


def test_score_uses_lot_premium_over_config():
    # Lot exposes a 20% premium; scoring must use it, not the 15% default.
    lot = _lot(retail_price=300, current_bid=30.0, premium=0.20)
    sc = score(lot, 200.0, CFG)
    assert sc.premium_used == 0.20
    assert sc.all_in_at_current_bid == pytest.approx(all_in(30, 0.20, TAX), abs=1e-6)


def test_score_zero_current_bid_does_not_crash():
    # A $0 opening bid makes all-in 0 → margin_pct undefined (None). Scoring must
    # still produce a verdict + reason string without a format-on-None crash.
    sc = score(_lot(retail_price=300, current_bid=0.0), 200.0, CFG)
    assert sc.margin_pct is None
    assert sc.verdict in (Verdict.WATCH, Verdict.BID)
    assert isinstance(sc.reason, str) and sc.reason  # reason rendered, not raised


def test_margin_pct_relative_to_all_in():
    sc = score(_lot(retail_price=300, current_bid=50.0), 200.0, CFG)
    expected = sc.projected_margin_at_current_bid / sc.all_in_at_current_bid
    assert sc.margin_pct == pytest.approx(expected, abs=1e-9)
