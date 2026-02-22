# Tradecraft — Agentic Trading System v2.0

> Five specialized AI agents that research, signal, risk-check, execute, and audit trades — with a polished dark-luxury dashboard.

---

## Project Structure

```
tradecraft/
├── server.py                # FastAPI backend (Phase 2)
├── orchestrator.py          # CLI pipeline runner (Phase 1)
├── requirements.txt
├── core/
│   ├── bus.py               # Message bus + audit log
│   └── llm.py               # LLM abstraction (Claude / OpenAI / Mock)
├── agents/
│   ├── base.py              # BaseAgent
│   └── agents.py            # All 5 agents
├── frontend/
│   └── index.html           # Dashboard (zero build step — open directly)
├── logs/
│   └── audit.jsonl          # Append-only audit log
└── data/                    # Market data (Phase 3)
```

---

## Quickstart

### Option A — Dashboard only (no backend needed)
```bash
open frontend/index.html
# or: python -m http.server 3000 --directory frontend
```
Works fully with mock data. No API keys required.

### Option B — Full stack with FastAPI backend
```bash
pip install -r requirements.txt

# Start backend
uvicorn server:app --reload --port 8000

# Open dashboard
open frontend/index.html
```

### Option C — Real LLMs
```bash
export ANTHROPIC_API_KEY=your_key
LLM_PROVIDER=anthropic uvicorn server:app --reload --port 8000
```

---

## Phase 2 — What's New

- **FastAPI backend** with SSE streaming (agents report in real-time)
- **Polished dashboard** — dark luxury editorial design
  - Live agent thread with animated state transitions
  - Expandable payload cards per agent
  - Immutable audit table
  - Pipeline metrics strip
- **REST API** endpoints: `/api/run`, `/api/run/stream`, `/api/audit/log`

---

## Phase 3 Roadmap

- [ ] Connect Polygon.io / NewsAPI for real market data
- [ ] Add simple LOB (limit order book) simulator
- [ ] RL-based position sizing in SignalAgent
- [ ] Agent debate protocol (agents can challenge each other)
- [ ] Redis message bus for async multi-agent communication
- [ ] Backtesting module with Sharpe/drawdown reporting

---

## The 5 Agents

| # | Agent | Role | Hard Power |
|---|---|---|---|
| 1 | Researcher | News + macro signal | — |
| 2 | Signal Agent | Trade proposal + backtest | — |
| 3 | Risk Manager | Position/drawdown check | **Hard Veto** |
| 4 | Execution Agent | TWAP/VWAP paper order | — |
| 5 | Supervisor | Compliance audit + log | — |
