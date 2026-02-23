"""
The 5 Wall Street Agents.

Each agent:
1. Reads relevant messages from the shared bus
2. Calls the LLM with its role-specific system prompt
3. Posts a structured message back to the bus

Pipeline order: Researcher → SignalAgent → RiskManager → ExecutionAgent → Supervisor
"""

import json
from agents.base import BaseAgent
from core.bus import Message

# RL position sizer — loaded once per process (persists Q-table across requests)
try:
    from rl.position_sizer import PositionSizer
    from analytics.performance_tracker import PerformanceTracker
    _rl_sizer   = PositionSizer()
    _perf_tracker = PerformanceTracker()
    _RL_ENABLED = True
except Exception as _rl_err:
    _RL_ENABLED = False


# ── 1. Macro / News Researcher ────────────────────────────────────────────────

class ResearcherAgent(BaseAgent):
    name = "researcher"
    system_prompt = """
You are a Macro/News Researcher at a quantitative hedge fund.
Your job: ingest news, macro data, and filings; identify market-moving signals.

Always respond in valid JSON with this exact schema:
{
  "signal": "BULLISH" | "BEARISH" | "NEUTRAL",
  "confidence": float (0–1),
  "summary": string,
  "sources": list[string],
  "regime": "RISK_ON" | "RISK_OFF" | "NEUTRAL",
  "key_risks": list[string]
}

Be concise, factual, and cite sources. Do not suggest trades.
"""

    def run(self) -> Message:
        # Read the market event from shared state
        event = self.bus.get_state("market_event", "No event provided.")

        prompt = f"""
Analyze the following market event and produce your research signal:

EVENT: {event}

Context from bus: {json.dumps(self.bus.get_state("macro_context", {}))}
"""
        result = self.think(prompt)
        return self.post("RESEARCH_SIGNAL", result)


# ── 2. Alpha Designer / Signal Agent ─────────────────────────────────────────

class SignalAgent(BaseAgent):
    name = "signal_agent"
    system_prompt = """
You are an Alpha Designer at a quantitative hedge fund.
Your job: given research signals, propose specific tradable positions with backtested rationale.

Always respond in valid JSON with this exact schema:
{
  "action": "LONG" | "SHORT" | "HOLD",
  "ticker": string,
  "size_pct": float (% of portfolio, 0–20),
  "entry_price": float,
  "stop_loss": float,
  "take_profit": float,
  "rationale": string,
  "backtest_sharpe": float,
  "expected_return_pct": float
}

Base your sizing on conviction and volatility. Never exceed 20% single position.
"""

    def run(self) -> Message:
        # Read the latest research signal
        research_msg = self.bus.latest("RESEARCH_SIGNAL")
        if not research_msg:
            return self.post("TRADE_PROPOSAL", {"error": "No research signal found."})

        prompt = f"""
Based on this research signal, propose a trade:

RESEARCH: {json.dumps(research_msg.payload)}
PORTFOLIO: {json.dumps(self.bus.get_state("portfolio", {"cash_pct": 100, "positions": []}))}
"""
        result = self.think(prompt)
        result["based_on_signal_id"] = research_msg.message_id

        # ── RL Position Sizing Override ───────────────────────────────────────
        # The LLM proposes a size_pct based on its training, but the RL agent
        # has learned from actual trade outcomes. We use the RL recommendation
        # and preserve both values in the payload for full auditability.
        if _RL_ENABLED:
            try:
                macro   = self.bus.get_state("macro_context", {})
                perf    = _perf_tracker.get_summary()
                rl_state = _rl_sizer.build_state(
                    research_payload=research_msg.payload,
                    macro_context=macro,
                    performance_summary=perf,
                )
                llm_size_pct = float(result.get("size_pct", 2.0))
                rl_size_pct  = _rl_sizer.recommend(rl_state)

                # Store both for audit transparency
                result["llm_size_pct"] = round(llm_size_pct, 2)
                result["rl_size_pct"]  = round(rl_size_pct,  2)
                result["size_pct"]     = round(rl_size_pct,  2)   # RL wins
                result["rl_state_key"] = _rl_sizer.discretise(rl_state)
                result["rl_epsilon"]   = round(_rl_sizer.epsilon, 4)
            except Exception as _e:
                # RL failure is non-fatal — keep the LLM's size
                result["rl_error"] = str(_e)

        return self.post("TRADE_PROPOSAL", result, in_reply_to=research_msg.message_id)


# ── 3. Risk Manager ───────────────────────────────────────────────────────────

class RiskManager(BaseAgent):
    name = "risk_manager"
    system_prompt = """
You are a Risk Manager at a quantitative hedge fund. You have HARD VETO power.
Your job: evaluate trade proposals for risk, exposure, liquidity, and drawdown.

Rules you MUST enforce:
- No single position > 5% of portfolio (veto if exceeded, suggest scaled size)
- Max portfolio drawdown limit: 10% (veto if breached)
- Minimum liquidity: position must be < 1% of 30-day ADV
- No trades during major macro announcements unless confidence > 0.85

Always respond in valid JSON with this exact schema:
{
  "verdict": "APPROVED" | "APPROVED_WITH_CONDITIONS" | "VETOED",
  "veto": bool,
  "adjusted_size_pct": float | null,
  "reason": string,
  "risk_metrics": {
    "position_limit_ok": bool,
    "drawdown_ok": bool,
    "liquidity_ok": bool
  }
}
"""

    def run(self) -> Message:
        proposal = self.bus.latest("TRADE_PROPOSAL")
        research = self.bus.latest("RESEARCH_SIGNAL")

        if not proposal:
            return self.post("RISK_DECISION", {"error": "No trade proposal to evaluate."})

        prompt = f"""
Evaluate this trade proposal for risk:

PROPOSAL: {json.dumps(proposal.payload)}
RESEARCH CONTEXT: {json.dumps(research.payload if research else {})}
CURRENT PORTFOLIO: {json.dumps(self.bus.get_state("portfolio", {}))}
RISK LIMITS: {json.dumps(self.bus.get_state("risk_limits", {"max_position_pct": 5, "max_drawdown_pct": 10}))}
"""
        result = self.think(prompt)
        return self.post("RISK_DECISION", result, in_reply_to=proposal.message_id)


# ── 4. Execution Agent ────────────────────────────────────────────────────────

class ExecutionAgent(BaseAgent):
    name = "execution_agent"
    system_prompt = """
You are an Execution Trader at a quantitative hedge fund.
Your job: translate approved trade decisions into optimal execution strategies.

Always respond in valid JSON with this exact schema:
{
  "strategy": "TWAP" | "VWAP" | "MARKET" | "LIMIT" | "ICEBERG",
  "duration_min": int,
  "child_orders": int,
  "limit_price": float | null,
  "expected_slippage_bps": float,
  "venue": string,
  "status": "SIMULATED_FILL" | "PENDING" | "REJECTED",
  "notes": string
}

This is PAPER TRADING only. All orders go to PAPER_EXCHANGE. Minimize slippage.
"""

    def run(self) -> Message:
        risk_decision = self.bus.latest("RISK_DECISION")
        proposal = self.bus.latest("TRADE_PROPOSAL")

        if not risk_decision or risk_decision.payload.get("veto"):
            return self.post("EXECUTION_PLAN", {
                "status": "REJECTED",
                "reason": "Vetoed by Risk Manager.",
            })

        prompt = f"""
Create an execution plan for this approved trade:

TRADE: {json.dumps(proposal.payload if proposal else {})}
RISK DECISION: {json.dumps(risk_decision.payload)}
MARKET CONDITIONS: {json.dumps(self.bus.get_state("market_conditions", {"volatility": "normal", "spread_bps": 3}))}

This is paper trading. Suggest realistic TWAP/VWAP parameters.
"""
        result = self.think(prompt)
        result["venue"] = result.get("venue", "PAPER_EXCHANGE")
        result["status"] = result.get("status", "SIMULATED_FILL")
        return self.post("EXECUTION_PLAN", result, in_reply_to=risk_decision.message_id)


# ── 5. Supervisor / Compliance Agent ─────────────────────────────────────────

class SupervisorAgent(BaseAgent):
    name = "supervisor"
    system_prompt = """
You are the Compliance Supervisor at a quantitative hedge fund.
Your job: audit the full decision chain, check regulatory compliance, log provenance.

Rules to enforce:
- Every trade must have a research basis
- Every trade must have passed risk review
- No trades in restricted securities
- Flag any suspicious patterns (wash trades, layering, etc.)

Always respond in valid JSON with this exact schema:
{
  "audit_status": "COMPLIANT" | "NON_COMPLIANT" | "REQUIRES_REVIEW",
  "circuit_breaker_triggered": bool,
  "human_review_required": bool,
  "flags": list[string],
  "compliance_notes": string,
  "log_id": string,
  "decision_chain_complete": bool
}
"""

    def run(self) -> Message:
        all_messages = self.bus.all_messages()

        # Build full decision chain summary for audit
        chain = [
            {"id": m.message_id, "sender": m.sender, "type": m.message_type, "payload": m.payload}
            for m in all_messages
        ]

        prompt = f"""
Audit the complete decision chain below for compliance:

FULL DECISION CHAIN:
{json.dumps(chain, indent=2)}

Check:
1. Is there a valid research basis?
2. Did risk manager review it?
3. Were risk limits respected?
4. Any regulatory red flags?
5. Is human review needed?
"""
        result = self.think(prompt)

        # Generate a deterministic log ID from message count
        result["log_id"] = result.get("log_id", f"TRD-{len(all_messages):04d}")
        result["total_messages_audited"] = len(all_messages)

        return self.post("AUDIT_COMPLETE", result)
