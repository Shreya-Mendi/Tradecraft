"""
LLM abstraction — swap providers by changing LLM_PROVIDER env var.
Supported: "anthropic", "openai", "mock" (default for dev/testing)
"""

import os
import json
from typing import Optional

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")


def call_llm(system_prompt: str, user_message: str, model: Optional[str] = None) -> str:
    """Unified LLM call. Returns the assistant's text response."""

    if LLM_PROVIDER == "anthropic":
        return _call_anthropic(system_prompt, user_message, model)
    elif LLM_PROVIDER == "openai":
        return _call_openai(system_prompt, user_message, model)
    else:
        return _call_mock(system_prompt, user_message)


# ── Anthropic ────────────────────────────────────────────────────────────────

def _call_anthropic(system_prompt: str, user_message: str, model: Optional[str]) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=model or "claude-opus-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ── OpenAI ───────────────────────────────────────────────────────────────────

def _call_openai(system_prompt: str, user_message: str, model: Optional[str]) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model or "gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


# ── Mock (deterministic, no API key needed) ──────────────────────────────────

_MOCK_RESPONSES = {
    "researcher": json.dumps({
        "signal": "BEARISH",
        "confidence": 0.78,
        "summary": "AAPL supply chain disruption reported. Taiwan fab delays 6–8 weeks.",
        "sources": ["Reuters headline", "Apple 8-K filing"],
        "regime": "RISK_OFF",
    }),
    "signal_agent": json.dumps({
        "action": "SHORT",
        "ticker": "AAPL",
        "size_pct": 8.0,
        "entry_price": 189.50,
        "stop_loss": 193.00,
        "take_profit": 182.00,
        "rationale": "Momentum + news event; 5 similar events → -1.2% median 3-day move.",
        "backtest_sharpe": 1.4,
    }),
    "risk_manager": json.dumps({
        "verdict": "APPROVED_WITH_CONDITIONS",
        "adjusted_size_pct": 4.0,
        "reason": "Original size exceeds 5% single-stock limit. Scaled to 4%. Drawdown headroom: 3.8%.",
        "veto": False,
    }),
    "execution_agent": json.dumps({
        "strategy": "TWAP",
        "duration_min": 30,
        "child_orders": 6,
        "expected_slippage_bps": 4.2,
        "venue": "PAPER_EXCHANGE",
        "status": "SIMULATED_FILL",
    }),
    "supervisor": json.dumps({
        "audit_status": "COMPLIANT",
        "circuit_breaker_triggered": False,
        "provenance_hash": "a3f9c12d",
        "flags": [],
        "human_review_required": False,
        "log_id": "TRD-20240215-0042",
    }),
}


def _call_mock(system_prompt: str, user_message: str) -> str:
    """Returns deterministic mock responses based on agent role in system prompt."""
    sp = system_prompt.lower()
    if "researcher" in sp:
        return _MOCK_RESPONSES["researcher"]
    elif "signal" in sp or "alpha" in sp:
        return _MOCK_RESPONSES["signal_agent"]
    elif "risk" in sp:
        return _MOCK_RESPONSES["risk_manager"]
    elif "execution" in sp:
        return _MOCK_RESPONSES["execution_agent"]
    elif "supervisor" in sp or "compliance" in sp:
        return _MOCK_RESPONSES["supervisor"]
    return json.dumps({"response": "Agent processed request.", "raw_prompt": user_message[:100]})
