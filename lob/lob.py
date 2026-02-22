"""
Limit Order Book (LOB) Simulator

Price-time priority matching engine.
- Bids: max-heap (best bid = highest price)
- Asks: min-heap (best ask = lowest price)

Supports:
  - Limit orders (add_limit_order)
  - Market orders (match_market_order)
  - Book queries: best_bid, best_ask, mid_price, spread_bps
  - Synthetic liquidity seeding around a mid price
"""

import heapq
import time
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Fill:
    price: float
    qty: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class MatchResult:
    fills: list[Fill]
    avg_fill_price: float       # quantity-weighted average
    total_filled_qty: float
    unfilled_qty: float
    slippage_bps: float         # vs arrival mid-price


class LimitOrderBook:
    """
    Simple price-time priority LOB.

    Internally:
      _bids: max-heap stored as min-heap of (-price, seq, qty)
      _asks: min-heap of (price, seq, qty)
    """

    def __init__(self, ticker: str, mid_price: float, spread_bps: float = 5.0):
        self.ticker = ticker
        self._arrival_mid = mid_price   # mid at order arrival (for slippage calc)
        self._spread_bps = spread_bps
        self._bids: list[tuple] = []    # (-price, seq, qty)
        self._asks: list[tuple] = []    # ( price, seq, qty)
        self._seq = 0
        self._seed_book(mid_price, spread_bps)

    # ── Book seeding ──────────────────────────────────────────────────────────

    def _seed_book(self, mid: float, spread_bps: float):
        """Populate synthetic liquidity around mid price."""
        half_spread = mid * (spread_bps / 2) / 10_000
        best_bid = mid - half_spread
        best_ask = mid + half_spread

        # Seed 8 bid levels below best bid, decreasing in size
        for i in range(8):
            price = round(best_bid - i * mid * 0.0005, 4)   # 5 bps apart
            qty = random.uniform(500, 3000) * (1 / (i + 1)) ** 0.5
            self.add_limit_order("bid", price, qty)

        # Seed 8 ask levels above best ask
        for i in range(8):
            price = round(best_ask + i * mid * 0.0005, 4)
            qty = random.uniform(500, 3000) * (1 / (i + 1)) ** 0.5
            self.add_limit_order("ask", price, qty)

    # ── Order management ──────────────────────────────────────────────────────

    def add_limit_order(self, side: str, price: float, qty: float):
        """Add a resting limit order to the book."""
        self._seq += 1
        if side == "bid":
            heapq.heappush(self._bids, (-price, self._seq, qty))
        elif side == "ask":
            heapq.heappush(self._asks, (price, self._seq, qty))
        else:
            raise ValueError(f"Invalid side: {side!r}. Use 'bid' or 'ask'.")

    def match_market_order(self, side: str, qty: float) -> MatchResult:
        """
        Fill a market order against the resting book.

        side="buy"  → lifts the ask side
        side="sell" → hits the bid side
        """
        arrival_mid = self.mid_price()
        fills: list[Fill] = []
        remaining = qty

        book = self._asks if side == "buy" else self._bids

        while remaining > 0 and book:
            if side == "buy":
                neg_or_pos_price, seq, level_qty = book[0]
                fill_price = neg_or_pos_price          # ask: price stored as-is
            else:
                neg_price, seq, level_qty = book[0]
                fill_price = -neg_price                # bid: stored as negative

            fill_qty = min(remaining, level_qty)
            fills.append(Fill(price=fill_price, qty=fill_qty))
            remaining -= fill_qty

            if fill_qty >= level_qty:
                heapq.heappop(book)                    # level fully consumed
            else:
                # Replace top entry with reduced qty
                heapq.heappop(book)
                if side == "buy":
                    heapq.heappush(book, (fill_price, seq, level_qty - fill_qty))
                else:
                    heapq.heappush(book, (-fill_price, seq, level_qty - fill_qty))

        total_filled = sum(f.qty for f in fills)
        if total_filled == 0:
            avg_fill = 0.0
            slippage_bps = 0.0
        else:
            avg_fill = sum(f.price * f.qty for f in fills) / total_filled
            if arrival_mid and arrival_mid > 0:
                slippage_bps = abs(avg_fill - arrival_mid) / arrival_mid * 10_000
            else:
                slippage_bps = 0.0

        return MatchResult(
            fills=fills,
            avg_fill_price=avg_fill,
            total_filled_qty=total_filled,
            unfilled_qty=remaining,
            slippage_bps=slippage_bps,
        )

    # ── Book queries ──────────────────────────────────────────────────────────

    def best_bid(self) -> Optional[float]:
        if not self._bids:
            return None
        return -self._bids[0][0]

    def best_ask(self) -> Optional[float]:
        if not self._asks:
            return None
        return self._asks[0][0]

    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def spread_bps(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        mid = (bb + ba) / 2
        return (ba - bb) / mid * 10_000

    def depth(self, levels: int = 5) -> dict:
        """Return top N bid/ask levels for inspection."""
        bids = sorted([(-p, q) for p, _, q in self._bids], reverse=True)[:levels]
        asks = sorted([(p, q) for p, _, q in self._asks])[:levels]
        return {
            "bids": [{"price": p, "qty": round(q, 2)} for p, q in bids],
            "asks": [{"price": p, "qty": round(q, 2)} for p, q in asks],
            "mid": self.mid_price(),
            "spread_bps": self.spread_bps(),
        }
