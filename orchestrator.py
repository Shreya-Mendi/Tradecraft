"""
Orchestrator — runs the 5 agents in sequence and manages the shared bus.

Pipeline:
  [Market Event] → Researcher → SignalAgent → RiskManager → ExecutionAgent → Supervisor

Usage:
  python orchestrator.py
  LLM_PROVIDER=anthropic python orchestrator.py
  LLM_PROVIDER=openai python orchestrator.py
"""

import json
import sys
import os

# Make sure imports resolve from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.bus import MessageBus
from agents.agents import (
    ResearcherAgent,
    SignalAgent,
    RiskManager,
    ExecutionAgent,
    SupervisorAgent,
)
from analytics.performance_tracker import PerformanceTracker

_tracker = PerformanceTracker()


# ── Sample Market Events (swap these out for real data later) ─────────────────

SAMPLE_EVENTS = [
    {
        "id": "EVT-001",
        "headline": "AAPL warns of 6-8 week supply chain delays due to Taiwan fab disruption.",
        "ticker": "AAPL",
        "source": "Reuters",
        "timestamp": "2024-02-15T09:32:00Z",
        "asset_class": "US Equities",
    },
    {
        "id": "EVT-002",
        "headline": "Fed minutes signal two additional rate hikes; inflation stickier than expected.",
        "ticker": "SPY",
        "source": "Federal Reserve",
        "timestamp": "2024-02-15T14:00:00Z",
        "asset_class": "Macro / ETF",
    },
]


def run_pipeline(event: dict, verbose: bool = True) -> dict:
    """
    Run the full 5-agent pipeline for a given market event.
    Returns a summary dict with all agent outputs.
    """

    print(f"\n{'='*70}")
    print(f"  AGENTIC WALL STREET SYSTEM — PIPELINE RUN")
    print(f"  Provider: {os.getenv('LLM_PROVIDER', 'mock').upper()}")
    print(f"  Event: {event['headline']}")
    print(f"{'='*70}")

    # ── Initialize bus with shared context ───────────────────────────────────
    bus = MessageBus(log_path="logs/audit.jsonl")

    # Inject live market data if available (--live flag or live_price already in event)
    live_context = {}
    if event.get("live_price") or os.getenv("LIVE_DATA", "").lower() == "1":
        try:
            from data.market_feed import get_live_market_context
            live_context = get_live_market_context(event.get("ticker", "SPY"))
            print(f"  [live] Price: ${live_context['market_conditions']['live_price']}  "
                  f"VIX: {live_context['macro_context']['vix']}")
        except Exception as exc:
            print(f"  [live] Market feed unavailable ({exc}) — using defaults")

    # Seed shared state (live data overrides defaults where available)
    bus.set_state("market_event", json.dumps(event))
    bus.set_state("macro_context", live_context.get("macro_context", {"fed_rate": 5.25, "vix": 18.4, "regime": "LATE_CYCLE"}))
    bus.set_state("portfolio", {
        "cash_pct": 35,
        "positions": [
            {"ticker": "MSFT", "size_pct": 8, "direction": "LONG"},
            {"ticker": "NVDA", "size_pct": 6, "direction": "LONG"},
        ],
        "nav_usd": 10_000_000,
    })
    bus.set_state("risk_limits", {
        "max_position_pct": 5,
        "max_drawdown_pct": 10,
        "max_portfolio_gross_pct": 150,
    })
    bus.set_state("market_conditions", live_context.get("market_conditions", {
        "volatility": "elevated",
        "spread_bps": 5,
        "adv_30d_usd": 85_000_000,
    }))

    # ── Initialize agents ────────────────────────────────────────────────────
    agents = [
        ResearcherAgent(bus),
        SignalAgent(bus),
        RiskManager(bus),
        ExecutionAgent(bus),
        SupervisorAgent(bus),
    ]

    # ── Run pipeline ─────────────────────────────────────────────────────────
    results = {}
    for agent in agents:
        print(f"\n▶ Running {agent.name.upper()}...")
        msg = agent.run()
        results[agent.name] = msg.payload
        print(f"  ✓ Posted: {msg.message_type} [{msg.message_id}]")

        # Early exit if vetoed by risk manager
        if msg.message_type == "RISK_DECISION" and msg.payload.get("veto"):
            print(f"\n  ⛔ HARD VETO by Risk Manager: {msg.payload.get('reason')}")
            print("  Pipeline halted. Running Supervisor for audit...")
            supervisor = SupervisorAgent(bus)
            audit_msg = supervisor.run()
            results["supervisor"] = audit_msg.payload
            break

    # ── Print full thread ─────────────────────────────────────────────────────
    if verbose:
        bus.print_thread()

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  PIPELINE COMPLETE")
    print("─" * 70)

    supervisor_result = results.get("supervisor", {})
    execution_result = results.get("execution_agent", {})
    risk_result = results.get("risk_manager", {})

    print(f"  Audit Status   : {supervisor_result.get('audit_status', 'N/A')}")
    print(f"  Risk Verdict   : {risk_result.get('verdict', 'N/A')}")
    print(f"  Execution      : {execution_result.get('status', 'N/A')}")
    print(f"  Log ID         : {supervisor_result.get('log_id', 'N/A')}")
    print(f"  Human Review?  : {supervisor_result.get('human_review_required', 'N/A')}")
    print(f"  Audit file     : logs/audit.jsonl")
    print("─" * 70 + "\n")

    # ── Record to performance tracker ─────────────────────────────────────────
    import uuid
    results["event"] = event
    trade = _tracker.record(results, sim_result=None, run_id=f"cli-{uuid.uuid4().hex[:8]}")
    print(f"  Performance    : {trade.outcome}  ({trade.pnl_bps:+.1f} bps est.)")
    summary = _tracker.get_summary()
    print(f"  All-time P&L   : {summary['cum_pnl_bps']:+.1f} bps  |  Win rate: {summary['win_rate_pct']:.0f}%  |  Sharpe: {summary['sharpe_ratio']:.2f}")
    print("─" * 70 + "\n")

    return results


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser()
    _p.add_argument("event_idx", nargs="?", type=int, default=0,
                    help="Sample event index (0, 1, …)")
    _p.add_argument("--live", action="store_true",
                    help="Fetch real prices and headlines via yfinance + RSS")
    _p.add_argument("--ticker", type=str, default=None,
                    help="Override ticker for --live mode")
    _args = _p.parse_args()

    if _args.live:
        try:
            from data.market_feed import get_live_event
            _ticker = _args.ticker or SAMPLE_EVENTS[_args.event_idx % len(SAMPLE_EVENTS)]["ticker"]
            event = get_live_event(_ticker)
            print(f"  [live] Fetched event for {_ticker}: {event['headline'][:80]}")
        except ImportError:
            print("  [live] yfinance/feedparser not installed — using sample event")
            event = SAMPLE_EVENTS[_args.event_idx % len(SAMPLE_EVENTS)]
    else:
        event = SAMPLE_EVENTS[_args.event_idx % len(SAMPLE_EVENTS)]

    results = run_pipeline(event)
