"""Unit tests for the pure eBay-comp helpers (query building, price parsing,
trimmed-median summary) and the EbayResaleEstimator's condition adjustment."""

from datetime import datetime, timezone

from nellis_hunter.ebay import build_query, parse_sold_prices, summarize_comps
from nellis_hunter.models import Lot
from nellis_hunter.resale import (
    CompositeResaleEstimator,
    EbayResaleEstimator,
    HeuristicResaleEstimator,
)


def _lot(**kw) -> Lot:
    base = dict(
        lot_id="1",
        title="Dell OptiPlex 3080 Micro Desktop i5",
        location="Mesa",
        url="https://www.nellisauction.com/p/x/1",
        close_time=datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Lot(**base)


# ---- build_query ----


def test_build_query_strips_condition_noise():
    lot = _lot(title="Used Open Box Logitech MX Master 3S Mouse (untested)", brand="Logitech")
    q = build_query(lot).lower()
    assert "logitech" in q and "master" in q and "3s" in q
    for noise in ("used", "open", "box", "untested"):
        assert noise not in q.split()


def test_build_query_keeps_model_numbers():
    q = build_query(_lot(title="Dell OptiPlex 3080 Micro", brand="Dell"))
    assert "3080" in q
    # Brand isn't duplicated even though it's in title + brand field.
    assert q.lower().split().count("dell") == 1


def test_build_query_respects_max_words():
    lot = _lot(title="alpha bravo charlie delta echo foxtrot golf hotel india", brand="")
    assert len(build_query(lot, max_words=4).split()) == 4


def test_build_query_empty_when_all_noise():
    assert build_query(_lot(title="used open box", brand="")) == ""


# ---- parse_sold_prices ----

_SAMPLE_HTML = """
<li class="s-item"><span class="s-item__price">Shop on eBay</span></li>
<li class="s-item"><span class="s-item__price">$24.99</span></li>
<li class="s-item"><span class="s-item__price">$1,250.00</span></li>
<li class="s-item"><span class="s-item__price">$30.00 to $45.00</span></li>
<li class="s-item"><span class="s-item__price"><span class=PRP>$19.95</span></span></li>
"""


def test_parse_sold_prices_extracts_numbers():
    prices = parse_sold_prices(_SAMPLE_HTML)
    # "Shop on eBay" has no $ → dropped. Range collapses to its low end ($30).
    assert 24.99 in prices
    assert 1250.0 in prices
    assert 30.0 in prices
    assert 19.95 in prices
    assert all(p > 0 for p in prices)


def test_parse_sold_prices_empty():
    assert parse_sold_prices("<html>no prices here</html>") == []


# ---- summarize_comps ----


def test_summarize_comps_returns_none_below_min():
    assert summarize_comps([10.0, 12.0], min_comps=3) is None


def test_summarize_comps_median_and_trim():
    prices = [5, 18, 19, 20, 21, 22, 500]  # one whale that trimming should drop
    s = summarize_comps([float(p) for p in prices], min_comps=3, trim_frac=0.15)
    assert s is not None
    assert s["n_raw"] == 7
    assert 18 <= s["median"] <= 22  # whale + floor trimmed away
    assert s["high"] <= 22


def test_summarize_comps_no_trim_when_too_small():
    s = summarize_comps([10.0, 20.0, 30.0], min_comps=3, trim_frac=0.4)
    assert s is not None and s["median"] == 20.0


# ---- EbayResaleEstimator (with a fake comps client) ----


class _FakeClient:
    def __init__(self, summary):
        self._summary = summary

    def sold_comps(self, query):
        return self._summary

    def close(self):
        pass


def test_ebay_estimator_applies_condition_multiplier():
    est = EbayResaleEstimator(_FakeClient({"median": 100.0, "n": 5}))
    # "Damaged" → 0.45 multiplier per resale.CONDITION_MULTIPLIERS.
    assert est.estimate(_lot(condition="Damaged")) == 45.0
    # "Used" → 0.90.
    assert est.estimate(_lot(condition="Used")) == 90.0


def test_ebay_estimator_none_when_no_comps():
    est = EbayResaleEstimator(_FakeClient(None))
    assert est.estimate(_lot()) is None


def test_composite_falls_back_to_heuristic():
    # eBay returns nothing → composite uses the retail heuristic instead.
    composite = CompositeResaleEstimator(
        [EbayResaleEstimator(_FakeClient(None)), HeuristicResaleEstimator()]
    )
    lot = _lot(retail_price=200.0, condition="Used", category="Computers, Laptops")
    assert composite.estimate(lot) is not None  # heuristic produced a number
