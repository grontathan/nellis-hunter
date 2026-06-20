"""Tests for the Discord embed digest — verify each lot becomes one self-contained
embed (image welded to its numbers) and that batching honors Discord's 10-per-message
cap. This is the fix for the old plain-text digest whose auto-unfurled images drifted
away from the text describing them."""

from datetime import datetime, timezone

from nellis_hunter.models import Lot, Scoring, ScoredLot, Verdict
from nellis_hunter.notify import DiscordNotifier, InteractionNotifier, format_embed


def _scored(**kw) -> ScoredLot:
    lot = Lot(
        lot_id=kw.get("lot_id", "1"),
        title=kw.get("title", "Dell OptiPlex 3080 Micro Desktop"),
        location="Mesa",
        url="https://www.nellisauction.com/p/x/1",
        retail_price=260.0,
        current_bid=40.0,
        bid_count=3,
        condition="Used",
        category="Computers, Laptops",
        image_url=kw.get("image_url", "https://m.media-amazon.com/images/I/61q2eL0jlyL.jpg"),
        close_time=datetime(2026, 6, 20, 2, 14, tzinfo=timezone.utc),
    )
    sc = Scoring(
        verdict=kw.get("verdict", Verdict.BID),
        resale_estimate=180.0,
        max_bid=120.0,
        projected_margin_at_current_bid=34.0,
        margin_pct=0.41,
    )
    return ScoredLot(lot=lot, scoring=sc)


def test_format_embed_has_image_and_pricing_fields():
    e = format_embed(_scored())
    assert e["url"].endswith("/p/x/1")
    assert e["image"]["url"].startswith("https://")
    field_names = {f["name"] for f in e["fields"]}
    assert {"Current bid", "Max bid", "Margin", "Retail"} <= field_names
    # Verdict drives the accent color (green for BID).
    assert e["color"] == 0x2ECC71


def test_format_embed_handles_missing_image_and_bid():
    s = _scored(image_url=None)
    s.lot.current_bid = None
    s.lot.bid_count = None
    s.scoring.projected_margin_at_current_bid = None
    e = format_embed(s)
    assert "image" not in e  # no image key when the lot has no photo
    margin = next(f["value"] for f in e["fields"] if f["name"] == "Margin")
    assert margin == "no live bid"


def test_format_embed_truncates_long_title():
    e = format_embed(_scored(title="X" * 400))
    assert len(e["title"]) <= 256


class _CaptureClient:
    def __init__(self):
        self.payloads = []

    def post(self, url, json):
        self.payloads.append(json)
        return _Resp()

    def close(self):
        pass


class _Resp:
    status_code = 204

    def raise_for_status(self):
        pass


def test_send_batches_embeds_under_ten_per_message():
    cap = _CaptureClient()
    notifier = DiscordNotifier("https://discord/webhook", client=cap)
    lots = [_scored(lot_id=str(i)) for i in range(23)]
    notifier.send(lots, header="**digest**")

    # 23 lots → 10 + 10 + 3 across three messages.
    assert len(cap.payloads) == 3
    assert [len(p["embeds"]) for p in cap.payloads] == [10, 10, 3]
    # Header rides on the first message only.
    assert cap.payloads[0]["content"] == "**digest**"
    assert "content" not in cap.payloads[1]


def test_send_empty_posts_placeholder():
    cap = _CaptureClient()
    DiscordNotifier("https://discord/webhook", client=cap).send([], header="**digest**")
    assert len(cap.payloads) == 1
    assert "no lots surfaced" in cap.payloads[0]["content"]


class _CaptureRequestClient:
    """Captures (method, url, json) for the request()-based InteractionNotifier."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, json):
        self.calls.append((method, url, json))
        return _Resp()

    def close(self):
        pass


def test_interaction_edits_original_then_followups():
    cap = _CaptureRequestClient()
    notifier = InteractionNotifier("app123", "tok456", client=cap)
    lots = [_scored(lot_id=str(i)) for i in range(23)]
    notifier.send(lots, header="**Tools — 23 surfaced**")

    methods = [c[0] for c in cap.calls]
    urls = [c[1] for c in cap.calls]
    payloads = [c[2] for c in cap.calls]
    # First call edits the deferred placeholder; overflow batches are followups.
    assert methods == ["PATCH", "POST", "POST"]
    assert urls[0].endswith("/webhooks/app123/tok456/messages/@original")
    assert urls[1] == "https://discord.com/api/v10/webhooks/app123/tok456"
    assert [len(p["embeds"]) for p in payloads] == [10, 10, 3]
    # Header rides on the first (edited) message only.
    assert payloads[0]["content"] == "**Tools — 23 surfaced**"
    assert "content" not in payloads[1]


def test_interaction_empty_edits_placeholder_with_message():
    cap = _CaptureRequestClient()
    InteractionNotifier("app123", "tok456", client=cap).send([], header="**Tools**")
    assert len(cap.calls) == 1
    method, url, payload = cap.calls[0]
    assert method == "PATCH"
    assert url.endswith("/messages/@original")
    assert "no lots surfaced" in payload["content"]
