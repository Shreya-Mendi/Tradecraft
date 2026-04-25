# Tradecraft — System Overview & Build Plan

---

## What Is Tradecraft?

Tradecraft is a **multi-agent paper trading simulation** modelled on how a quantitative hedge fund desk operates. Five AI agents collaborate in a fixed pipeline to evaluate a market event, propose a trade, check risk, execute it, and audit the result.

It is **not** a live trading system. No real money moves. Every "execution" is simulated against a synthetic limit order book. The point is to build a realistic, observable pipeline where each agent has a defined role, limited authority, and leaves a traceable audit trail.

---

## The 5 Agents

```
Market Event
     │
     ▼
┌──────────────┐    reads news, macro data, filings
│  Researcher  │──► RESEARCH_SIGNAL  { signal, confidence, regime }
└──────────────┘
     │
     ▼
┌──────────────┐    proposes a specific trade
│ Signal Agent │──► TRADE_PROPOSAL   { ticker, action, size_pct, entry, SL, TP }
└──────────────┘    RL sizer overrides size_pct with a learned policy
     │
     ▼
┌──────────────┐    hard veto power — enforces position + drawdown limits
│ Risk Manager │──► RISK_DECISION    { verdict, adjusted_size_pct, veto }
└──────────────┘
     │
     ▼
┌──────────────┐    translates approved trade into TWAP/VWAP/MARKET plan
│  Execution   │──► EXECUTION_PLAN   { strategy, child_orders, slippage }
│    Agent     │    LOB bridge runs the actual fill simulation
└──────────────┘
     │
     ▼
┌──────────────┐    audits the full decision chain for compliance
│  Supervisor  │──► AUDIT_COMPLETE   { status, flags, log_id }
└──────────────┘
     │
     ▼
Performance Tracker  (Sharpe, drawdown, win rate across all runs)
```

Each agent only reads what it needs from the **message bus** — a shared in-memory store that also appends every message to an immutable JSONL audit log.

---

## The RL Position Sizer — Should We Keep It?

**Yes. It's the most interesting part of the system.**

The SignalAgent embeds a tabular Q-learning agent that decides how big the position should be. Instead of trusting the LLM's hallucinated `size_pct`, the RL agent observes a state tuple:

```
state = (signal_bucket, macro_regime, drawdown_bucket, vol_bucket)
action = position size ∈ {1%, 2%, 3%, 4%, 5%} of NAV
reward = realised P&L in bps from the LOB fill
```

The Q-table persists to `logs/q_table.json` so it learns across sessions. Right now it runs but **never learns** because `sim_result=None` — the reward signal is always missing. Wiring the LOB fixes this automatically.

---

## The Limit Order Book (LOB)

`lob/lob.py` is a real price-time priority matching engine:
- Bids = max-heap (best bid = highest price)
- Asks = min-heap (best ask = lowest price)
- Supports: limit orders, market orders, synthetic liquidity seeding

`lob/execution_bridge.py` routes the ExecutionAgent's plan through it:
- **TWAP**: splits total order into N equal child orders over `duration_min`
- **VWAP**: distributes children across a U-shaped intraday volume curve
- **MARKET**: single immediate fill

The bridge returns a `SimulationResult` with real fill stats: avg fill price, actual slippage bps, fill rate, notional. These are what the performance tracker needs to compute meaningful Sharpe/drawdown numbers.

**Currently: the bridge is never called. The pipeline passes `sim_result=None`.**

---

## Current Architecture Diagram

```
Browser (React/Vite)
        │
        │  POST /api/run  or  GET /api/run/stream (SSE)
        ▼
FastAPI server.py
        │
        ├── build_bus_for_event()   ← hardcoded macro_context (BUG: should use market_feed)
        │
        ├── ResearcherAgent.run()
        ├── SignalAgent.run()       ← RL sizer runs but reward never fed back (BUG)
        ├── RiskManager.run()
        ├── ExecutionAgent.run()    ← LLM invents slippage number (BUG: LOB never called)
        ├── SupervisorAgent.run()
        │
        └── tracker.record(sim_result=None)  ← performance stats all zero (BUG)
```

---

## What Needs to Be Built — Prioritised

### Step 1 — Wire the LOB to the execution pipeline  *(core fix)*

**File:** `server.py` — after ExecutionAgent posts its plan:

```python
from data.market_feed import get_price
from lob.lob import LimitOrderBook
from lob.execution_bridge import simulate_execution

# After ExecutionAgent runs:
execution_plan = bus.latest("EXECUTION_PLAN").payload
trade_proposal = bus.latest("TRADE_PROPOSAL").payload
portfolio      = bus.get_state("portfolio")
ticker         = trade_proposal.get("ticker", req.ticker)
live_price     = get_price(ticker)["price"]

lob        = LimitOrderBook(ticker, mid_price=live_price, spread_bps=5.0)
sim_result = simulate_execution(execution_plan, trade_proposal, portfolio, lob)

# Merge real fill stats back into the execution plan payload on the bus
execution_plan["actual_slippage_bps"] = sim_result.actual_slippage_bps
execution_plan["fill_rate_pct"]       = sim_result.fill_rate_pct
execution_plan["avg_fill_price"]      = sim_result.avg_fill_price

tracker.record(pipeline_map, sim_result=sim_result, run_id=run_id)
```

This also closes the RL reward loop — the tracker computes `pnl_bps` from the fill, which feeds back into `_rl_sizer.update()`.

---

### Step 2 — Wire real market data into the bus  *(correctness)*

**File:** `server.py::build_bus_for_event()` — replace hardcoded macro context:

```python
from data.market_feed import get_price, get_macro_snapshot, get_news_headlines

def build_bus_for_event(event: dict) -> MessageBus:
    bus = MessageBus(log_path="logs/audit.jsonl")
    bus.set_state("market_event", json.dumps(event))

    macro   = get_macro_snapshot()          # real VIX + fed funds via yfinance
    price   = get_price(event["ticker"])    # real quote
    news    = get_news_headlines(event["ticker"])  # RSS headlines

    bus.set_state("macro_context", macro)
    bus.set_state("market_event_enriched", {**event, "live_price": price["price"], "news": news})
    bus.set_state("portfolio", { "cash_pct": 35, "nav_usd": 10_000_000, "positions": [] })
    bus.set_state("risk_limits", {"max_position_pct": 5, "max_drawdown_pct": 10})
    return bus
```

---

### Step 3 — Add Groq as the free LLM provider  *(for Railway deployment)*

Groq is free (14,400 req/day), uses an OpenAI-compatible API, and is fast enough for streaming.
Model: `llama-3.3-70b-versatile` — strong enough for structured JSON agent outputs.

**File:** `core/llm.py` — add Groq provider:

```python
elif LLM_PROVIDER == "groq":
    return _call_groq(system_prompt, user_message, model)

def _call_groq(system_prompt: str, user_message: str, model: Optional[str]) -> str:
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=model or "llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content
```

Add `groq` to `requirements.txt`.

---

### Step 4 — RL reward feedback loop  *(closes the learning cycle)*

After `tracker.record()`, feed the realised P&L back to the RL sizer:

```python
if _RL_ENABLED and sim_result:
    state_key = bus.get_state("rl_state_key")
    action     = bus.get_state("rl_action")
    reward     = trade_record.pnl_bps
    _rl_sizer.update(state_key, action, reward)
    _rl_sizer.save()
```

This makes the Q-table actually learn from each run.

---

### Step 5 — Railway deployment

**Files needed:**
- `Dockerfile` — containerise the FastAPI app
- `railway.toml` — declare start command and port
- Serve the React frontend as static files from FastAPI (no separate deployment needed)

**Environment variables on Railway:**
```
LLM_PROVIDER=groq
GROQ_API_KEY=<your key>
PORT=8000
```

**Dockerfile:**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

For the frontend — mount it as static files in `server.py`:
```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="static")
```
(Run `npm run build` in `frontend/` first; Railway can do this in the Dockerfile.)

---

## Build Order Summary

| # | What | Why | File(s) |
|---|---|---|---|
| 1 | Wire LOB to execution pipeline | Closes the core simulation gap | `server.py` |
| 2 | Wire real market data to bus | Agents see real prices/VIX | `server.py` |
| 3 | Add Groq LLM provider | Free, fast, Railway-ready | `core/llm.py`, `requirements.txt` |
| 4 | RL reward feedback | Q-table actually learns | `server.py` |
| 5 | Dockerfile + railway.toml | Deploy | `Dockerfile`, `railway.toml` |
| 6 | Serve frontend from FastAPI | Single Railway service | `server.py`, `frontend/` |

Steps 1–4 are all `server.py` changes or small additions — low blast radius, no new architecture. Step 5–6 is pure deployment plumbing.

---

## What Tradecraft Will Look Like When Done

A single Railway URL. Open it, enter a ticker + headline, click Run. Five agents fire in sequence (streamed live via SSE). The execution agent runs a real TWAP/VWAP simulation through the LOB using the live price. The RL sizer sets the position size based on what it's learned from prior runs. Every decision is logged with a full audit trail. The performance tab shows real Sharpe and drawdown computed from actual fill data.

Free to run. No paid APIs required.
