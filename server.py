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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.bus import MessageBus
from agents.agents import (
    ResearcherAgent, SignalAgent, RiskManager,
    ExecutionAgent, SupervisorAgent,
)
from analytics.performance_tracker import PerformanceTracker
from lob.lob import LimitOrderBook
from lob.execution_bridge import simulate_execution

tracker = PerformanceTracker()

# Share the same RL sizer instance that SignalAgent uses so updates persist
try:
    from agents.agents import _rl_sizer, _RL_ENABLED as _RL_FEEDBACK
except Exception:
    _RL_FEEDBACK = False

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


def _get_live_price(ticker: str, fallback: float = 100.0) -> float:
    try:
        from data.market_feed import get_price
        data = get_price(ticker)
        return float(data.get("price") or fallback)
    except Exception:
        return fallback


def build_bus_for_event(event: dict) -> MessageBus:
    bus = MessageBus(log_path="logs/audit.jsonl")
    bus.set_state("market_event", json.dumps(event))

    # Real market data with graceful fallback to hardcoded defaults
    try:
        from data.market_feed import get_macro_snapshot, get_news_headlines
        macro = get_macro_snapshot()
        news  = get_news_headlines(event.get("ticker", "SPY"))
        bus.set_state("macro_context", macro)
        bus.set_state("market_event_enriched", {
            **event,
            "live_price": _get_live_price(event.get("ticker", "SPY")),
            "news": news[:3],
        })
    except Exception:
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

    # ── LOB simulation ────────────────────────────────────────────────────────
    sim_result = None
    exec_entry = next((r for r in results if r["agent"] == "execution_agent"), None)
    if exec_entry and exec_entry["payload"].get("status") != "REJECTED":
        try:
            trade_proposal = bus.latest("TRADE_PROPOSAL").payload
            ticker         = trade_proposal.get("ticker", req.ticker)
            live_price     = _get_live_price(ticker, float(trade_proposal.get("entry_price", 100.0)))
            lob            = LimitOrderBook(ticker, mid_price=live_price, spread_bps=5.0)
            sim_result     = simulate_execution(exec_entry["payload"], trade_proposal, bus.get_state("portfolio"), lob)
            exec_entry["payload"].update({
                "actual_slippage_bps": round(sim_result.actual_slippage_bps, 2),
                "fill_rate_pct":       round(sim_result.fill_rate_pct, 1),
                "avg_fill_price":      round(sim_result.avg_fill_price, 4),
                "total_filled_qty":    round(sim_result.total_filled_qty, 2),
                "slippage_delta_bps":  round(sim_result.slippage_delta_bps, 2),
            })
        except Exception as _lob_err:
            exec_entry["payload"]["lob_error"] = str(_lob_err)

    # Build pipeline_results dict for tracker
    pipeline_map = {r["agent"]: r["payload"] for r in results}
    pipeline_map["event"] = event
    import uuid
    run_id = f"api-{uuid.uuid4().hex[:8]}"
    proposal = pipeline_map.get("signal_agent", {})
    trade_record = tracker.record(
        pipeline_map, sim_result=sim_result, run_id=run_id,
        llm_size_pct=proposal.get("llm_size_pct"),
        rl_size_pct=proposal.get("rl_size_pct"),
    )

    # ── RL feedback loop ──────────────────────────────────────────────────────
    if _RL_FEEDBACK and sim_result:
        try:
            macro    = bus.get_state("macro_context", {})
            perf     = tracker.get_summary()
            rl_state = _rl_sizer.build_state(proposal, macro, perf)
            action   = float(proposal.get("rl_size_pct") or proposal.get("size_pct", 2.0))
            _rl_sizer.update(rl_state, action, trade_record.pnl_bps)
            _rl_sizer.save()
        except Exception:
            pass

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
        stream_results = []
        sim_result = None
        yield f"data: {json.dumps({'type': 'start', 'event': event})}\n\n"
        for agent in agents:
            await asyncio.sleep(0.3)
            msg = agent.run()
            msg_payload = dict(msg.payload)

            # After execution agent fires, run LOB simulation and merge stats
            if agent.name == "execution_agent" and msg_payload.get("status") != "REJECTED":
                try:
                    trade_proposal = bus.latest("TRADE_PROPOSAL").payload
                    ticker_str     = trade_proposal.get("ticker", ticker)
                    live_price     = _get_live_price(ticker_str, float(trade_proposal.get("entry_price", 100.0)))
                    lob            = LimitOrderBook(ticker_str, mid_price=live_price, spread_bps=5.0)
                    sim_result     = simulate_execution(msg_payload, trade_proposal, bus.get_state("portfolio"), lob)
                    msg_payload.update({
                        "actual_slippage_bps": round(sim_result.actual_slippage_bps, 2),
                        "fill_rate_pct":       round(sim_result.fill_rate_pct, 1),
                        "avg_fill_price":      round(sim_result.avg_fill_price, 4),
                        "total_filled_qty":    round(sim_result.total_filled_qty, 2),
                        "slippage_delta_bps":  round(sim_result.slippage_delta_bps, 2),
                    })
                except Exception as _e:
                    msg_payload["lob_error"] = str(_e)

            payload = {
                "type": "agent_result",
                "agent": agent.name,
                "message_type": msg.message_type,
                "message_id": msg.message_id,
                "timestamp": msg.timestamp,
                "payload": msg_payload,
            }
            stream_results.append({"agent": agent.name, "payload": msg_payload})
            yield f"data: {json.dumps(payload)}\n\n"

            if msg.message_type == "RISK_DECISION" and msg.payload.get("veto"):
                supervisor = SupervisorAgent(bus)
                audit = supervisor.run()
                stream_results.append({"agent": "supervisor", "payload": audit.payload})
                yield f"data: {json.dumps({'type': 'agent_result', 'agent': 'supervisor', 'message_type': audit.message_type, 'payload': audit.payload})}\n\n"
                break

        # Record performance and feed RL reward
        try:
            import uuid
            pipeline_map = {r["agent"]: r["payload"] for r in stream_results}
            pipeline_map["event"] = event
            run_id = f"sse-{uuid.uuid4().hex[:8]}"
            proposal = pipeline_map.get("signal_agent", {})
            trade_record = tracker.record(
                pipeline_map, sim_result=sim_result, run_id=run_id,
                llm_size_pct=proposal.get("llm_size_pct"),
                rl_size_pct=proposal.get("rl_size_pct"),
            )
            if _RL_FEEDBACK and sim_result:
                macro    = bus.get_state("macro_context", {})
                perf     = tracker.get_summary()
                rl_state = _rl_sizer.build_state(proposal, macro, perf)
                action   = float(proposal.get("rl_size_pct") or proposal.get("size_pct", 2.0))
                _rl_sizer.update(rl_state, action, trade_record.pnl_bps)
                _rl_sizer.save()
        except Exception:
            pass

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


# Serve the built frontend — mounted last so API routes take priority.
# Only active when frontend/dist exists (i.e. after `npm run build`).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
