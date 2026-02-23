"""
Tradecraft — FastAPI Backend
Serves real-time agent pipeline data to the React dashboard.

Run: uvicorn server:app --reload --port 8000
"""

import json
import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.bus import MessageBus
from agents.agents import (
    ResearcherAgent, SignalAgent, RiskManager,
    ExecutionAgent, SupervisorAgent,
)
from analytics.performance_tracker import PerformanceTracker

tracker = PerformanceTracker()

app = FastAPI(title="Tradecraft API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory store for active runs ──────────────────────────────────────────
active_runs: dict[str, dict] = {}


class RunRequest(BaseModel):
    headline: str
    ticker: str
    source: str = "Manual Input"


SAMPLE_EVENTS = [
    {
        "id": "EVT-001",
        "headline": "AAPL warns of 6-8 week supply chain delays due to Taiwan fab disruption.",
        "ticker": "AAPL",
        "source": "Reuters",
    },
    {
        "id": "EVT-002",
        "headline": "Fed minutes signal two additional rate hikes; inflation stickier than expected.",
        "ticker": "SPY",
        "source": "Federal Reserve",
    },
    {
        "id": "EVT-003",
        "headline": "NVDA beats earnings by 18%; data center revenue up 3x YoY.",
        "ticker": "NVDA",
        "source": "NASDAQ Filing",
    },
]


def build_bus_for_event(event: dict) -> MessageBus:
    bus = MessageBus(log_path="logs/audit.jsonl")
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
    bus.set_state("risk_limits", {"max_position_pct": 5, "max_drawdown_pct": 10})
    bus.set_state("market_conditions", {"volatility": "elevated", "spread_bps": 5, "adv_30d_usd": 85_000_000})
    return bus


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/events")
def get_sample_events():
    return {"events": SAMPLE_EVENTS}


@app.post("/api/run")
async def run_pipeline(req: RunRequest):
    """Run the full 5-agent pipeline and return all results."""
    event = {"id": f"EVT-{datetime.now().strftime('%H%M%S')}", "headline": req.headline, "ticker": req.ticker, "source": req.source}
    bus = build_bus_for_event(event)

    agents = [
        ResearcherAgent(bus),
        SignalAgent(bus),
        RiskManager(bus),
        ExecutionAgent(bus),
        SupervisorAgent(bus),
    ]

    results = []
    for agent in agents:
        msg = agent.run()
        results.append({
            "agent": agent.name,
            "message_type": msg.message_type,
            "message_id": msg.message_id,
            "timestamp": msg.timestamp,
            "payload": msg.payload,
        })
        # Early exit on veto
        if msg.message_type == "RISK_DECISION" and msg.payload.get("veto"):
            supervisor = SupervisorAgent(bus)
            audit = supervisor.run()
            results.append({
                "agent": supervisor.name,
                "message_type": audit.message_type,
                "message_id": audit.message_id,
                "timestamp": audit.timestamp,
                "payload": audit.payload,
            })
            break

    # Build pipeline_results dict for tracker
    pipeline_map = {r["agent"]: r["payload"] for r in results}
    pipeline_map["event"] = event
    import uuid
    run_id = f"api-{uuid.uuid4().hex[:8]}"
    trade_record = tracker.record(pipeline_map, sim_result=None, run_id=run_id)

    return {
        "event": event,
        "pipeline": results,
        "message_count": len(results),
        "performance": {"run_id": run_id, "outcome": trade_record.outcome, "pnl_bps": trade_record.pnl_bps},
    }


@app.get("/api/run/stream")
async def stream_pipeline(headline: str, ticker: str, source: str = "Manual"):
    """SSE stream — sends each agent result as it completes."""
    event = {"headline": headline, "ticker": ticker, "source": source}
    bus = build_bus_for_event(event)

    agents = [
        ResearcherAgent(bus),
        SignalAgent(bus),
        RiskManager(bus),
        ExecutionAgent(bus),
        SupervisorAgent(bus),
    ]

    async def generator():
        yield f"data: {json.dumps({'type': 'start', 'event': event})}\n\n"
        for agent in agents:
            await asyncio.sleep(0.3)  # brief pause for visual effect
            msg = agent.run()
            payload = {
                "type": "agent_result",
                "agent": agent.name,
                "message_type": msg.message_type,
                "message_id": msg.message_id,
                "timestamp": msg.timestamp,
                "payload": msg.payload,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if msg.message_type == "RISK_DECISION" and msg.payload.get("veto"):
                supervisor = SupervisorAgent(bus)
                audit = supervisor.run()
                yield f"data: {json.dumps({'type': 'agent_result', 'agent': 'supervisor', 'message_type': audit.message_type, 'payload': audit.payload})}\n\n"
                break
        yield f"data: {json.dumps({'type': 'complete'})}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/api/performance")
def get_performance():
    """Return full performance summary across all pipeline runs."""
    return tracker.get_summary()


@app.get("/api/audit/log")
def get_audit_log(limit: int = 50):
    """Return the last N audit log entries."""
    log_path = "logs/audit.jsonl"
    if not os.path.exists(log_path):
        return {"entries": []}
    with open(log_path) as f:
        lines = f.readlines()
    entries = [json.loads(l) for l in lines[-limit:]]
    return {"entries": entries, "total": len(lines)}
