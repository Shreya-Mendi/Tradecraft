"""
LOB Simulation Test Runner

Runs the full 5-agent pipeline then routes the ExecutionAgent's plan through
the Limit Order Book simulator. Prints a detailed simulation report.

Usage:
  python lob/run_lob_simulation.py
  python lob/run_lob_simulation.py --event 1
  python lob/run_lob_simulation.py --event 0 --provider mock
  ANTHROPIC_API_KEY=sk-... python lob/run_lob_simulation.py --provider anthropic
  OPENAI_API_KEY=sk-...   python lob/run_lob_simulation.py --provider openai
"""

import argparse
import json
import os
import sys
import random

# Resolve imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import run_pipeline, SAMPLE_EVENTS
from lob.lob import LimitOrderBook
from lob.execution_bridge import simulate_execution, SimulationResult


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="LOB Simulation Test Runner")
    p.add_argument("--event", type=int, default=0,
                   help="Index into SAMPLE_EVENTS (default: 0)")
    p.add_argument("--provider", type=str, default=None,
                   help="LLM provider: mock | anthropic | openai  (overrides env var)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for LOB liquidity (default: 42)")
    p.add_argument("--depth", type=int, default=5,
                   help="Book depth levels to display (default: 5)")
    p.add_argument("--verbose-children", action="store_true",
                   help="Print each child order fill breakdown")
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 30, char: str = "█") -> str:
    filled = int(round(width * min(value, max_val) / max_val)) if max_val > 0 else 0
    return char * filled + "░" * (width - filled)


def _slippage_indicator(delta: float) -> str:
    """Return a colored label for slippage delta."""
    if delta <= 0:
        return "BETTER THAN EXPECTED ✓"
    elif delta <= 2:
        return "WITHIN TOLERANCE"
    elif delta <= 5:
        return "SLIGHTLY ELEVATED !"
    else:
        return "HIGH SLIPPAGE ⚠"


def _print_book_snapshot(lob: LimitOrderBook, levels: int = 5):
    depth = lob.depth(levels)
    print(f"\n  {'─'*52}")
    print(f"  {'LIMIT ORDER BOOK':^52}")
    print(f"  {'─'*52}")
    print(f"  {'ASKS':>40}")
    for lvl in reversed(depth["asks"]):
        bar = _bar(lvl["qty"], 3000, width=20)
        print(f"  {'':20s}  {lvl['price']:>10.4f}  {bar}  {lvl['qty']:>8.0f}")
    mid = depth["mid"]
    spread = depth["spread_bps"]
    print(f"  {' '*20}  ── MID {mid:.4f}  spread {spread:.1f} bps ──")
    print(f"  {'BIDS':10}")
    for lvl in depth["bids"]:
        bar = _bar(lvl["qty"], 3000, width=20)
        print(f"  {bar}  {lvl['price']:>10.4f}  {lvl['qty']:>8.0f}")
    print(f"  {'─'*52}")


def _print_simulation_report(sim: SimulationResult, expected_slippage_agent: float):
    print(f"\n{'═'*70}")
    print(f"  LOB SIMULATION REPORT")
    print(f"{'═'*70}")

    print(f"\n  Ticker           : {sim.ticker}")
    print(f"  Action           : {sim.action}")
    print(f"  Strategy         : {sim.strategy}")
    print(f"  Duration         : {sim.duration_sec / 60:.1f} min  ({len(sim.child_results)} child orders)")

    print(f"\n  ── Fill Statistics {'─'*50}")
    print(f"  Target Qty       : {sim.total_target_qty:>12,.2f} shares")
    print(f"  Filled Qty       : {sim.total_filled_qty:>12,.2f} shares")
    print(f"  Unfilled Qty     : {sim.unfilled_qty:>12,.2f} shares")
    fill_bar = _bar(sim.fill_rate_pct, 100, width=30)
    print(f"  Fill Rate        :  [{fill_bar}]  {sim.fill_rate_pct:.1f}%")

    print(f"\n  ── Price Analysis {'─'*51}")
    print(f"  Arrival Mid      : ${sim.arrival_mid_price:>12.4f}")
    print(f"  Avg Fill Price   : ${sim.avg_fill_price:>12.4f}")
    print(f"  Total Notional   : ${sim.total_notional_usd:>12,.2f}")

    print(f"\n  ── Slippage {'─'*57}")
    print(f"  Expected (agent) :  {expected_slippage_agent:>6.2f} bps")
    print(f"  Actual (LOB)     :  {sim.actual_slippage_bps:>6.2f} bps")
    delta = sim.slippage_delta_bps
    sign = "+" if delta >= 0 else ""
    print(f"  Delta            :  {sign}{delta:.2f} bps  →  {_slippage_indicator(delta)}")

    # Market impact estimate
    impact_usd = abs(sim.avg_fill_price - sim.arrival_mid_price) * sim.total_filled_qty
    print(f"  Market Impact    : ${impact_usd:>12,.2f}")

    print(f"\n{'─'*70}\n")


def _print_child_breakdown(sim: SimulationResult):
    print(f"  Child Order Breakdown:")
    print(f"  {'#':>3}  {'Target Qty':>12}  {'Filled':>10}  {'Avg Price':>10}  {'Slip(bps)':>9}")
    print(f"  {'─'*3}  {'─'*12}  {'─'*10}  {'─'*10}  {'─'*9}")
    for c in sim.child_results:
        print(
            f"  {c.child_index:>3}  {c.target_qty:>12,.2f}  "
            f"{c.filled_qty:>10,.2f}  {c.avg_fill_price:>10.4f}  "
            f"{c.slippage_bps:>9.2f}"
        )
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    random.seed(args.seed)

    # Set LLM provider via env if --provider flag given
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    provider = os.getenv("LLM_PROVIDER", "mock")

    event = SAMPLE_EVENTS[args.event % len(SAMPLE_EVENTS)]

    print(f"\n{'═'*70}")
    print(f"  TRADECRAFT — LOB SIMULATION TEST RUNNER")
    print(f"  LLM Provider : {provider.upper()}")
    print(f"  Event        : {event['headline']}")
    print(f"  Ticker       : {event['ticker']}")
    print(f"  Random Seed  : {args.seed}")
    print(f"{'═'*70}")

    # ── Step 1: Run 5-agent pipeline ──────────────────────────────────────────
    print("\n[Step 1/3] Running 5-agent pipeline...")
    pipeline_results = run_pipeline(event, verbose=False)

    risk_result = pipeline_results.get("risk_manager", {})
    execution_result = pipeline_results.get("execution_agent", {})
    proposal_result = pipeline_results.get("signal_agent", {})

    # Check for veto
    if risk_result.get("veto"):
        print(f"\n  ⛔ Pipeline VETOED by Risk Manager: {risk_result.get('reason')}")
        print("  LOB simulation skipped — no approved trade to execute.")
        print(f"\n  Supervisor Audit: {pipeline_results.get('supervisor', {}).get('audit_status', 'N/A')}")
        sys.exit(0)

    if execution_result.get("status") == "REJECTED":
        print("\n  ⛔ Execution Agent REJECTED the order.")
        sys.exit(0)

    print(f"\n  ✓ Pipeline complete")
    print(f"  Risk Verdict : {risk_result.get('verdict', 'N/A')}")
    print(f"  Strategy     : {execution_result.get('strategy', 'N/A')}")
    print(f"  Child Orders : {execution_result.get('child_orders', 'N/A')}")
    print(f"  Est. Slippage: {execution_result.get('expected_slippage_bps', 'N/A')} bps")

    # ── Step 2: Build LOB ─────────────────────────────────────────────────────
    print("\n[Step 2/3] Initialising Limit Order Book...")

    # Use entry_price from trade proposal; fall back to a reasonable default
    entry_price = float(proposal_result.get("entry_price", 150.0))
    spread_bps = float(pipeline_results.get("market_conditions", {}).get("spread_bps", 5.0))
    # market_conditions is in shared state — grab from execution context
    spread_bps = float(execution_result.get("expected_slippage_bps", 5.0))
    spread_bps = max(spread_bps, 2.0)  # floor at 2 bps

    lob = LimitOrderBook(
        ticker=event["ticker"],
        mid_price=entry_price,
        spread_bps=spread_bps,
    )

    print(f"\n  Initial book snapshot for {event['ticker']}:")
    _print_book_snapshot(lob, levels=args.depth)

    # ── Step 3: Simulate Execution ────────────────────────────────────────────
    print("\n[Step 3/3] Simulating order execution through LOB...")

    # Resolve adjusted size from risk manager
    adjusted_size = risk_result.get("adjusted_size_pct") or proposal_result.get("size_pct", 2.0)
    if adjusted_size is None:
        adjusted_size = proposal_result.get("size_pct", 2.0)

    # Inject adjusted size into proposal for bridge
    effective_proposal = dict(proposal_result)
    effective_proposal["size_pct"] = float(adjusted_size)

    # Portfolio state (matches orchestrator defaults)
    portfolio = {"nav_usd": 10_000_000}

    sim: SimulationResult = simulate_execution(
        execution_plan=execution_result,
        trade_proposal=effective_proposal,
        portfolio=portfolio,
        lob=lob,
    )

    # ── Report ────────────────────────────────────────────────────────────────
    _print_simulation_report(sim, expected_slippage_agent=float(execution_result.get("expected_slippage_bps", 0.0)))

    if args.verbose_children:
        _print_child_breakdown(sim)

    # Supervisor audit status
    supervisor = pipeline_results.get("supervisor", {})
    if supervisor:
        print(f"  Compliance Audit : {supervisor.get('audit_status', 'N/A')}")
        print(f"  Log ID           : {supervisor.get('log_id', 'N/A')}")
        print(f"  Human Review?    : {supervisor.get('human_review_required', 'N/A')}")

    print(f"\n{'═'*70}\n")


if __name__ == "__main__":
    main()
