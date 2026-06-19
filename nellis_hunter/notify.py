"""Output sinks. `Notifier.send(scored_lots)` is the seam; Discord is the v1 impl.

Reference shape: aaronata/AuctionChecker (keyword monitor -> Discord webhook,
scheduled, ignore-list dedup). Same idea, adapted to a ranked flip digest.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx

from .models import ScoredLot, Verdict

_VERDICT_EMOJI = {
    Verdict.BID: "🟢 BID",
    Verdict.WATCH: "🟡 WATCH",
    Verdict.SKIP: "⚪ SKIP",
    Verdict.NEEDS_MANUAL: "🔵 MANUAL",
}


def format_line(scored: ScoredLot) -> str:
    """One compact human line per lot for the digest."""
    lot, sc = scored.lot, scored.scoring
    tag = _VERDICT_EMOJI.get(sc.verdict, sc.verdict.value)
    margin = (
        f"${sc.projected_margin_at_current_bid:.0f} margin"
        if sc.projected_margin_at_current_bid is not None
        else "no live bid"
    )
    title = lot.title.replace("\n", " ").strip()
    if len(title) > 70:
        title = title[:67] + "…"
    local = lot.close_time_local
    # %-I isn't portable (no Windows support); strip the leading zero manually.
    closes = local.strftime("%I:%M%p").lstrip("0").lower() if local else "?"

    cur = f"${lot.current_bid:.0f}" if lot.current_bid is not None else "—"
    mb = f"${sc.max_bid:.0f}" if sc.max_bid is not None else "—"
    retail = f"${lot.retail_price:.0f}" if lot.retail_price is not None else "—"

    head = f"[{tag} ▸ {margin}] {title} — {lot.location}"
    body = f"  current {cur} | max bid {mb} | retail {retail} | closes {closes}"
    return f"{head}\n{body}\n  {lot.url}"


class Notifier(ABC):
    @abstractmethod
    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None: ...


class ConsoleNotifier(Notifier):
    """Prints the digest to stdout. Default when no webhook is configured."""

    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None:
        if header:
            print(header)
        if not scored_lots:
            print("(no lots surfaced)")
            return
        for s in scored_lots:
            print(format_line(s))
            print()


class DiscordNotifier(Notifier):
    """Posts the digest to a Discord webhook, chunked under the 2000-char limit."""

    MAX_CHARS = 1900

    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        if not webhook_url:
            raise ValueError("DiscordNotifier requires a webhook URL")
        self.webhook_url = webhook_url
        self._client = client or httpx.Client(timeout=30.0)
        self._owns = client is None

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None:
        lines = [format_line(s) for s in scored_lots] or ["(no lots surfaced today)"]
        chunks = self._chunk([header] + lines if header else lines)
        for chunk in chunks:
            self._post(chunk)

    def send_text(self, content: str) -> None:
        """Post a single arbitrary message (used by the webhook self-test)."""
        self._post(content[: self.MAX_CHARS])

    def _post(self, content: str, *, max_retries: int = 3) -> None:
        """POST one message, honoring Discord's 429 rate limit (Retry-After)."""
        for attempt in range(max_retries):
            resp = self._client.post(self.webhook_url, json={"content": content})
            if resp.status_code == 429 and attempt < max_retries - 1:
                # Discord tells us exactly how long to wait, in seconds.
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except (ValueError, KeyError, TypeError):
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                time.sleep(min(retry_after, 10.0))
                continue
            resp.raise_for_status()
            return

    def _chunk(self, blocks: list[str]) -> list[str]:
        chunks, cur = [], ""
        for block in blocks:
            piece = (block + "\n\n")
            if len(cur) + len(piece) > self.MAX_CHARS:
                if cur:
                    chunks.append(cur.rstrip())
                cur = piece
            else:
                cur += piece
        if cur.strip():
            chunks.append(cur.rstrip())
        return chunks


# Stubs for the other sinks named in the brief — same interface, drop-in later.
class TelegramNotifier(Notifier):  # pragma: no cover - stub
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token, self.chat_id = bot_token, chat_id

    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None:
        raise NotImplementedError("TelegramNotifier is a stub; implement Bot API sendMessage.")


class GoogleSheetNotifier(Notifier):  # pragma: no cover - stub
    def __init__(self, sheet_id: str, creds_path: str):
        self.sheet_id, self.creds_path = sheet_id, creds_path

    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None:
        raise NotImplementedError("GoogleSheetNotifier is a stub; append rows via Sheets API.")


class EmailNotifier(Notifier):  # pragma: no cover - stub
    def __init__(self, smtp_url: str, to_addr: str):
        self.smtp_url, self.to_addr = smtp_url, to_addr

    def send(self, scored_lots: list[ScoredLot], *, header: str = "") -> None:
        raise NotImplementedError("EmailNotifier is a stub; send via SMTP.")


def build_notifier(discord_webhook_url: str) -> Notifier:
    """Pick the sink from config: Discord if a webhook is set, else console."""
    if discord_webhook_url:
        return DiscordNotifier(discord_webhook_url)
    return ConsoleNotifier()


def _cli() -> None:
    """`python -m nellis_hunter.notify --test` — post a sample digest to the
    configured Discord webhook to confirm it's wired up correctly."""
    import argparse
    import sys
    from datetime import datetime, timezone

    from .config import load_config

    ap = argparse.ArgumentParser(description="Notifier self-test")
    ap.add_argument("--test", action="store_true", help="Send a test message to the Discord webhook")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    config = load_config()
    if not config.discord_webhook_url:
        print("No DISCORD_WEBHOOK_URL set in .env — nothing to test. Add it and retry.")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).astimezone()
    notifier = DiscordNotifier(config.discord_webhook_url)
    try:
        notifier.send_text(
            f"✅ **Nellis Hunter** webhook test — {now:%a %b %d, %I:%M%p}\n"
            "If you can read this in Discord, the daily digest will post here."
        )
        print("Sent a test message to your Discord channel. Go check it.")
    except httpx.HTTPStatusError as exc:
        print(f"Discord rejected the post ({exc.response.status_code}). "
              "Double-check the webhook URL in .env.")
        raise SystemExit(1)
    finally:
        notifier.close()


if __name__ == "__main__":
    _cli()
