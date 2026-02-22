"""
pipeline_runner.py — GitHub Actions entry point.

Runs the 5-agent trading pipeline using GitHub Models as the LLM backend.
Called by .github/workflows/pipeline-run.yml with inputs passed as CLI flags.

Writes a JSON result file to --output for the frontend to poll.

GitHub Models API is OpenAI-compatible:
  endpoint: https://models.github.ai/inference
  auth:     Bearer $GITHUB_TOKEN  (auto-provided in Actions)
  model:    openai/gpt-4o, anthropic/claude-3-5-sonnet, etc.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Resolve project root imports
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # GitHub Models uses OpenAI-compatible SDK


# ── GitHub Models client ──────────────────────────────────────────────────────

def make_gh_client() -> OpenAI:
    """Return an OpenAI client pointed at GitHub Models."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN not set. This runner must be called from GitHub Actions."
        )
    return OpenAI(
        base_url="https://models.github.ai/inference",
        api_key=token,
    )


def call_github_model(client: OpenAI, model: str, system: str, user: str) -> dict:
    """
    Call GitHub Models and return parsed JSON payload.
    Retries once on rate-limit (HTTP 429).
    """
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                temperature=0.3,        # deterministic enough for trading decisions
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
            return json.loads(raw)
        except Exception as exc:
            if attempt == 0 and "429" in str(exc):
                print(f"  Rate limited — waiting 30s before retry…")
                time.sleep(30)
            else:
                raise


# ── Agent system prompts (same as Python backend) ────────────────────────────

RESEARCHER_SYSTEM = """
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

SIGNAL_SYSTEM = """
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

RISK_SYSTEM = """
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

EXECUTION_SYSTEM = """
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

SUPERVISOR_SYSTEM = """
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

MACRO_CONTEXT = {
    "fed_rate": 5.25,
    "vix": 18.4,
    "regime": "LATE_CYCLE",
}

PORTFOLIO = {
    "cash_pct": 35,
    "positions": [
        {"ticker": "MSFT", "size_pct": 8, "direction": "LONG"},
        {"ticker": "NVDA", "size_pct": 6, "direction": "LONG"},
    ],
    "nav_usd": 10_000_000,
}

RISK_LIMITS = {
    "max_position_pct": 5,
    "max_drawdown_pct": 10,
    "max_portfolio_gross_pct": 150,
}

MARKET_CONDITIONS = {
    "volatility": "elevated",
    "spread_bps": 5,
    "adv_30d_usd": 85_000_000,
}


# ── Pipeline orchestrator ─────────────────────────────────────────────────────

def run_pipeline(client: OpenAI, model: str, event: dict, run_id: str) -> dict:
    results = {}
    ts = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*60}")
    print(f"  PIPELINE RUN — {run_id}")
    print(f"  Model: {model}")
    print(f"  Event: {event['headline']}")
    print(f"{'='*60}\n")

    # ── 1. Researcher ──────────────────────────────────────────────────────────
    print("▶ Researcher…")
    researcher = call_github_model(
        client, model,
        system=RESEARCHER_SYSTEM,
        user=f"Analyze this event:\n\nEVENT: {json.dumps(event)}\nMACRO: {json.dumps(MACRO_CONTEXT)}",
    )
    results["researcher"] = researcher
    print(f"  Signal: {researcher.get('signal')}  Confidence: {researcher.get('confidence')}")

    # ── 2. Signal Agent ────────────────────────────────────────────────────────
    print("▶ Signal Agent…")
    signal_agent = call_github_model(
        client, model,
        system=SIGNAL_SYSTEM,
        user=f"Propose a trade:\n\nRESEARCH: {json.dumps(researcher)}\nPORTFOLIO: {json.dumps(PORTFOLIO)}",
    )
    signal_agent["ticker"] = event["ticker"].upper()  # enforce correct ticker
    results["signal_agent"] = signal_agent
    print(f"  Action: {signal_agent.get('action')}  Size: {signal_agent.get('size_pct')}%")

    # ── 3. Risk Manager ────────────────────────────────────────────────────────
    print("▶ Risk Manager…")
    risk_manager = call_github_model(
        client, model,
        system=RISK_SYSTEM,
        user=(
            f"Evaluate this trade proposal:\n\n"
            f"PROPOSAL: {json.dumps(signal_agent)}\n"
            f"RESEARCH: {json.dumps(researcher)}\n"
            f"PORTFOLIO: {json.dumps(PORTFOLIO)}\n"
            f"RISK LIMITS: {json.dumps(RISK_LIMITS)}"
        ),
    )
    results["risk_manager"] = risk_manager
    print(f"  Verdict: {risk_manager.get('verdict')}  Veto: {risk_manager.get('veto')}")

    if risk_manager.get("veto"):
        print("  ⛔ HARD VETO — running Supervisor for audit…")
        supervisor = _run_supervisor(client, model, results, event)
        results["supervisor"] = supervisor
        return _wrap_result(results, run_id, event, ts, vetoed=True)

    # ── 4. Execution Agent ─────────────────────────────────────────────────────
    print("▶ Execution Agent…")
    execution_agent = call_github_model(
        client, model,
        system=EXECUTION_SYSTEM,
        user=(
            f"Build execution plan:\n\n"
            f"TRADE: {json.dumps(signal_agent)}\n"
            f"RISK DECISION: {json.dumps(risk_manager)}\n"
            f"MARKET CONDITIONS: {json.dumps(MARKET_CONDITIONS)}\n\n"
            "This is paper trading. All orders go to PAPER_EXCHANGE."
        ),
    )
    execution_agent.setdefault("venue",  "PAPER_EXCHANGE")
    execution_agent.setdefault("status", "SIMULATED_FILL")
    results["execution_agent"] = execution_agent
    print(f"  Strategy: {execution_agent.get('strategy')}  Slippage: {execution_agent.get('expected_slippage_bps')} bps")

    # ── 5. Supervisor ──────────────────────────────────────────────────────────
    print("▶ Supervisor…")
    supervisor = _run_supervisor(client, model, results, event)
    results["supervisor"] = supervisor
    print(f"  Audit: {supervisor.get('audit_status')}  Log ID: {supervisor.get('log_id')}")

    return _wrap_result(results, run_id, event, ts, vetoed=False)


def _run_supervisor(client, model, results, event):
    supervisor = call_github_model(
        client, model,
        system=SUPERVISOR_SYSTEM,
        user=f"Audit the complete decision chain:\n\n{json.dumps(results, indent=2)}",
    )
    import random, string
    supervisor.setdefault(
        "log_id",
        f"TRD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-"
        + "".join(random.choices(string.digits, k=4))
    )
    return supervisor


def _wrap_result(results, run_id, event, ts, vetoed):
    return {
        "status":    "complete",
        "run_id":    run_id,
        "event":     event,
        "timestamp": ts,
        "vetoed":    vetoed,
        "model":     os.environ.get("GH_MODEL", "unknown"),
        **results,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--headline", required=True)
    p.add_argument("--ticker",   required=True)
    p.add_argument("--source",   default="Unknown")
    p.add_argument("--run-id",   required=True, dest="run_id")
    p.add_argument("--model",    default="openai/gpt-4o")
    p.add_argument("--output",   required=True)
    return p.parse_args()


def main():
    args = parse_args()

    event = {
        "headline": args.headline,
        "ticker":   args.ticker.upper(),
        "source":   args.source,
    }

    client = make_gh_client()
    result = run_pipeline(client, args.model, event, args.run_id)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\n✓ Result written to {out_path}")


if __name__ == "__main__":
    main()
