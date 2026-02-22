"""
Execution Bridge — connects ExecutionAgent's EXECUTION_PLAN to the LOB simulator.

Takes the structured plan (TWAP/VWAP, child_orders, duration_min, etc.) from
the message bus and routes each child order through the LimitOrderBook,
returning real fill stats vs. the agent's predicted slippage.
"""

import time
import random
from dataclasses import dataclass, field
from typing import Optional

from lob.lob import LimitOrderBook, MatchResult


# VWAP intraday volume curve (normalised weights by 30-min bucket, NYSE-style U-shape)
_VWAP_WEIGHTS = [
    0.085, 0.062, 0.048, 0.041, 0.038, 0.036,   # 09:30–12:00
    0.034, 0.033, 0.034, 0.036, 0.038, 0.041,   # 12:00–15:00
    0.052, 0.068, 0.094, 0.120,                  # 15:00–16:00 (close rush)
]


@dataclass
class ChildOrderResult:
    child_index: int
    side: str               # "buy" or "sell"
    target_qty: float
    fills: list            # list of Fill objects
    avg_fill_price: float
    filled_qty: float
    unfilled_qty: float
    slippage_bps: float
    timestamp_offset_sec: float   # seconds from start of execution


@dataclass
class SimulationResult:
    ticker: str
    action: str             # LONG / SHORT
    strategy: str           # TWAP / VWAP / MARKET / LIMIT
    total_target_qty: float
    total_filled_qty: float
    unfilled_qty: float
    fill_rate_pct: float
    avg_fill_price: float
    arrival_mid_price: float
    expected_slippage_bps: float   # from ExecutionAgent
    actual_slippage_bps: float     # from LOB simulation
    slippage_delta_bps: float      # actual - expected (positive = worse)
    total_notional_usd: float
    child_results: list[ChildOrderResult]
    duration_sec: float
    notes: str = ""


def _nav_to_qty(nav_usd: float, size_pct: float, price: float) -> float:
    """Convert portfolio size_pct → number of shares."""
    notional = nav_usd * size_pct / 100.0
    return notional / price if price > 0 else 0.0


def _action_to_side(action: str) -> str:
    """LONG / BUY → 'buy', SHORT / SELL → 'sell'."""
    return "buy" if action.upper() in ("LONG", "BUY") else "sell"


def run_twap(
    execution_plan: dict,
    trade_proposal: dict,
    portfolio: dict,
    lob: LimitOrderBook,
) -> SimulationResult:
    """
    Execute a TWAP strategy: split total order into N equal child orders,
    evenly spaced over duration_min.
    """
    n_children = int(execution_plan.get("child_orders", 6))
    duration_min = float(execution_plan.get("duration_min", 30))
    expected_slippage = float(execution_plan.get("expected_slippage_bps", 0.0))

    action = trade_proposal.get("action", "LONG")
    ticker = trade_proposal.get("ticker", lob.ticker)
    size_pct = float(trade_proposal.get("size_pct", execution_plan.get("adjusted_size_pct", 1.0)))
    entry_price = float(trade_proposal.get("entry_price", lob.mid_price() or 100.0))
    nav_usd = float(portfolio.get("nav_usd", 10_000_000))

    side = _action_to_side(action)
    total_qty = _nav_to_qty(nav_usd, size_pct, entry_price)
    child_qty = total_qty / n_children
    interval_sec = (duration_min * 60) / n_children
    arrival_mid = lob.mid_price() or entry_price

    child_results: list[ChildOrderResult] = []
    elapsed = 0.0

    for i in range(n_children):
        # Add small intraday noise to mid price (+/- 0.05% each slice)
        noise = random.gauss(0, entry_price * 0.0005)
        lob._arrival_mid = arrival_mid + noise

        result: MatchResult = lob.match_market_order(side, child_qty)

        # Replenish book slightly after each child so later slices have liquidity
        _replenish_book(lob, side, entry_price, child_qty * 0.3)

        child_results.append(ChildOrderResult(
            child_index=i + 1,
            side=side,
            target_qty=child_qty,
            fills=result.fills,
            avg_fill_price=result.avg_fill_price,
            filled_qty=result.total_filled_qty,
            unfilled_qty=result.unfilled_qty,
            slippage_bps=result.slippage_bps,
            timestamp_offset_sec=elapsed,
        ))
        elapsed += interval_sec

    return _aggregate(
        ticker=ticker,
        action=action,
        strategy="TWAP",
        child_results=child_results,
        arrival_mid=arrival_mid,
        total_qty=total_qty,
        expected_slippage=expected_slippage,
        duration_sec=duration_min * 60,
        nav_usd=nav_usd,
    )


def run_vwap(
    execution_plan: dict,
    trade_proposal: dict,
    portfolio: dict,
    lob: LimitOrderBook,
) -> SimulationResult:
    """
    Execute a VWAP strategy: distribute order across child slices weighted by
    a typical intraday volume curve (U-shaped, 16 buckets).
    """
    n_children = int(execution_plan.get("child_orders", len(_VWAP_WEIGHTS)))
    n_children = min(n_children, len(_VWAP_WEIGHTS))
    duration_min = float(execution_plan.get("duration_min", 390))  # full day default
    expected_slippage = float(execution_plan.get("expected_slippage_bps", 0.0))

    action = trade_proposal.get("action", "LONG")
    ticker = trade_proposal.get("ticker", lob.ticker)
    size_pct = float(trade_proposal.get("size_pct", execution_plan.get("adjusted_size_pct", 1.0)))
    entry_price = float(trade_proposal.get("entry_price", lob.mid_price() or 100.0))
    nav_usd = float(portfolio.get("nav_usd", 10_000_000))

    side = _action_to_side(action)
    total_qty = _nav_to_qty(nav_usd, size_pct, entry_price)
    arrival_mid = lob.mid_price() or entry_price

    # Use first n_children weights, renormalize
    weights = _VWAP_WEIGHTS[:n_children]
    weight_sum = sum(weights)
    weights = [w / weight_sum for w in weights]
    interval_sec = (duration_min * 60) / n_children

    child_results: list[ChildOrderResult] = []
    elapsed = 0.0

    for i, weight in enumerate(weights):
        child_qty = total_qty * weight
        noise = random.gauss(0, entry_price * 0.0005)
        lob._arrival_mid = arrival_mid + noise

        result: MatchResult = lob.match_market_order(side, child_qty)
        _replenish_book(lob, side, entry_price, child_qty * 0.3)

        child_results.append(ChildOrderResult(
            child_index=i + 1,
            side=side,
            target_qty=child_qty,
            fills=result.fills,
            avg_fill_price=result.avg_fill_price,
            filled_qty=result.total_filled_qty,
            unfilled_qty=result.unfilled_qty,
            slippage_bps=result.slippage_bps,
            timestamp_offset_sec=elapsed,
        ))
        elapsed += interval_sec

    return _aggregate(
        ticker=ticker,
        action=action,
        strategy="VWAP",
        child_results=child_results,
        arrival_mid=arrival_mid,
        total_qty=total_qty,
        expected_slippage=expected_slippage,
        duration_sec=duration_min * 60,
        nav_usd=nav_usd,
    )


def run_market(
    execution_plan: dict,
    trade_proposal: dict,
    portfolio: dict,
    lob: LimitOrderBook,
) -> SimulationResult:
    """Execute a single market order immediately."""
    expected_slippage = float(execution_plan.get("expected_slippage_bps", 0.0))
    action = trade_proposal.get("action", "LONG")
    ticker = trade_proposal.get("ticker", lob.ticker)
    size_pct = float(trade_proposal.get("size_pct", execution_plan.get("adjusted_size_pct", 1.0)))
    entry_price = float(trade_proposal.get("entry_price", lob.mid_price() or 100.0))
    nav_usd = float(portfolio.get("nav_usd", 10_000_000))

    side = _action_to_side(action)
    total_qty = _nav_to_qty(nav_usd, size_pct, entry_price)
    arrival_mid = lob.mid_price() or entry_price

    result: MatchResult = lob.match_market_order(side, total_qty)

    child_results = [ChildOrderResult(
        child_index=1,
        side=side,
        target_qty=total_qty,
        fills=result.fills,
        avg_fill_price=result.avg_fill_price,
        filled_qty=result.total_filled_qty,
        unfilled_qty=result.unfilled_qty,
        slippage_bps=result.slippage_bps,
        timestamp_offset_sec=0.0,
    )]

    return _aggregate(
        ticker=ticker,
        action=action,
        strategy="MARKET",
        child_results=child_results,
        arrival_mid=arrival_mid,
        total_qty=total_qty,
        expected_slippage=expected_slippage,
        duration_sec=0.0,
        nav_usd=nav_usd,
    )


# ── Routing ───────────────────────────────────────────────────────────────────

def simulate_execution(
    execution_plan: dict,
    trade_proposal: dict,
    portfolio: dict,
    lob: LimitOrderBook,
) -> SimulationResult:
    """
    Route to the correct execution strategy based on ExecutionAgent's plan.
    Falls back to TWAP for ICEBERG/LIMIT since those map naturally to it.
    """
    strategy = execution_plan.get("strategy", "TWAP").upper()

    if strategy == "VWAP":
        return run_vwap(execution_plan, trade_proposal, portfolio, lob)
    elif strategy == "MARKET":
        return run_market(execution_plan, trade_proposal, portfolio, lob)
    else:
        # TWAP, LIMIT, ICEBERG all use TWAP splitting logic
        return run_twap(execution_plan, trade_proposal, portfolio, lob)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _replenish_book(lob: LimitOrderBook, side: str, price: float, qty: float):
    """Add a small resting order back to the opposite side after a fill."""
    half_spread = price * lob._spread_bps / 2 / 10_000
    if side == "buy":
        # We just lifted asks — replenish ask side slightly above mid
        lob.add_limit_order("ask", round(price + half_spread, 4), qty)
    else:
        # We just hit bids — replenish bid side slightly below mid
        lob.add_limit_order("bid", round(price - half_spread, 4), qty)


def _aggregate(
    ticker: str,
    action: str,
    strategy: str,
    child_results: list[ChildOrderResult],
    arrival_mid: float,
    total_qty: float,
    expected_slippage: float,
    duration_sec: float,
    nav_usd: float,
) -> SimulationResult:
    total_filled = sum(c.filled_qty for c in child_results)
    unfilled = total_qty - total_filled
    fill_rate = total_filled / total_qty * 100 if total_qty > 0 else 0.0

    # Quantity-weighted average fill price
    weighted_sum = sum(c.avg_fill_price * c.filled_qty for c in child_results if c.filled_qty > 0)
    avg_fill = weighted_sum / total_filled if total_filled > 0 else arrival_mid

    # Actual slippage vs arrival mid
    actual_slippage = abs(avg_fill - arrival_mid) / arrival_mid * 10_000 if arrival_mid > 0 else 0.0

    return SimulationResult(
        ticker=ticker,
        action=action,
        strategy=strategy,
        total_target_qty=total_qty,
        total_filled_qty=total_filled,
        unfilled_qty=unfilled,
        fill_rate_pct=fill_rate,
        avg_fill_price=avg_fill,
        arrival_mid_price=arrival_mid,
        expected_slippage_bps=expected_slippage,
        actual_slippage_bps=actual_slippage,
        slippage_delta_bps=actual_slippage - expected_slippage,
        total_notional_usd=avg_fill * total_filled,
        child_results=child_results,
        duration_sec=duration_sec,
    )
