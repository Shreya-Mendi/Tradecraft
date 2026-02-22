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

    # Seed shared state
    bus.set_state("market_event", json.dumps(event))
    bus.set_state("macro_context", {"fed_rate": 5.25, "vix": 18.4, "regime": "LATE_CYCLE"})
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
    bus.set_state("market_conditions", {
        "volatility": "elevated",
        "spread_bps": 5,
        "adv_30d_usd": 85_000_000,
    })

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

    return results


if __name__ == "__main__":
    # Run the pipeline on the first sample event
    event_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    event = SAMPLE_EVENTS[event_idx % len(SAMPLE_EVENTS)]
    results = run_pipeline(event)
