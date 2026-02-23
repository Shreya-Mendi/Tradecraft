"""
Performance Tracker — persistent P&L and trading metrics across all runs.

Every completed pipeline + LOB run is appended to logs/trades.jsonl.
Summary stats (win rate, Sharpe, drawdown, avg slippage) are computed
on demand from that log — no in-memory state lost between restarts.

Usage:
    tracker = PerformanceTracker()
    tracker.record(pipeline_results, sim_result)   # after each run
    summary = tracker.get_summary()                # for /api/performance
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

TRADES_LOG = Path("logs/trades.jsonl")


@dataclass
class TradeRecord:
    run_id:           str
    timestamp:        str
    ticker:           str
    action:           str          # LONG / SHORT / HOLD
    entry_price:      float
    take_profit:      float
    stop_loss:        float
    size_pct:         float        # % of NAV actually used (after risk adjustment)
    nav_usd:          float
    strategy:         str          # TWAP / VWAP / MARKET
    fill_rate_pct:    float        # from LOB sim
    avg_fill_price:   float        # from LOB sim
    actual_slippage_bps: float     # from LOB sim
    expected_slippage_bps: float   # from ExecutionAgent
    notional_usd:     float        # total fill value
    pnl_bps:          float        # estimated P&L vs entry in bps (positive = win)
    pnl_usd:          float        # estimated P&L in USD
    outcome:          str          # WIN / LOSS / FLAT (based on direction vs price target)
    risk_verdict:     str          # APPROVED / APPROVED_WITH_CONDITIONS / VETOED
    audit_status:     str          # COMPLIANT / NON_COMPLIANT
    log_id:           str
    llm_size_pct:     Optional[float] = None   # original LLM suggestion before RL override
    rl_size_pct:      Optional[float] = None   # RL sizer recommendation


class PerformanceTracker:
    """
    Append-only trade log with on-demand summary statistics.
    Thread-safe for single-process use (FastAPI is single-threaded by default).
    """

    def __init__(self, log_path: str = str(TRADES_LOG)):
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record(
        self,
        pipeline_results: dict,
        sim_result=None,         # lob.execution_bridge.SimulationResult or None
        run_id: str = "",
        llm_size_pct: float = None,
        rl_size_pct: float = None,
    ) -> TradeRecord:
        """
        Build a TradeRecord from pipeline + LOB outputs and persist it.
        Works even when LOB sim is skipped (sim_result=None).
        """
        proposal  = pipeline_results.get("signal_agent", {})
        risk      = pipeline_results.get("risk_manager", {})
        execution = pipeline_results.get("execution_agent", {})
        supervisor= pipeline_results.get("supervisor", {})
        event     = pipeline_results.get("event", {})

        ticker       = (event.get("ticker") or proposal.get("ticker", "UNKNOWN")).upper()
        action       = proposal.get("action", "HOLD")
        entry_price  = float(proposal.get("entry_price", 0))
        take_profit  = float(proposal.get("take_profit", entry_price))
        stop_loss    = float(proposal.get("stop_loss",   entry_price))
        size_pct     = float(risk.get("adjusted_size_pct") or proposal.get("size_pct", 0))
        nav_usd      = 10_000_000   # TODO: pull from bus state when real portfolio exists

        # LOB fill stats — use actuals if available, fall back to agent estimates
        if sim_result:
            fill_rate        = sim_result.fill_rate_pct
            avg_fill         = sim_result.avg_fill_price
            actual_slip      = sim_result.actual_slippage_bps
            notional         = sim_result.total_notional_usd
            strategy         = sim_result.strategy
        else:
            fill_rate        = 100.0 if risk.get("verdict") != "VETOED" else 0.0
            avg_fill         = entry_price
            actual_slip      = float(execution.get("expected_slippage_bps", 0))
            notional         = nav_usd * size_pct / 100 if size_pct else 0
            strategy         = execution.get("strategy", "UNKNOWN")

        expected_slip = float(execution.get("expected_slippage_bps", 0))

        # Estimated P&L in bps: how far is take_profit from entry?
        # For LONG:  (tp - entry) / entry * 10000
        # For SHORT: (entry - tp) / entry * 10000
        if entry_price > 0 and action in ("LONG", "SHORT"):
            if action == "LONG":
                pnl_bps = (take_profit - entry_price) / entry_price * 10_000
            else:
                pnl_bps = (entry_price - take_profit) / entry_price * 10_000
        else:
            pnl_bps = 0.0

        pnl_bps -= actual_slip                  # subtract slippage cost
        pnl_usd  = notional * pnl_bps / 10_000
        outcome  = "WIN" if pnl_bps > 0 else ("LOSS" if pnl_bps < 0 else "FLAT")

        if risk.get("veto"):
            outcome = "VETOED"
            pnl_bps = 0.0
            pnl_usd = 0.0

        record = TradeRecord(
            run_id=run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            ticker=ticker,
            action=action,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            size_pct=size_pct,
            nav_usd=nav_usd,
            strategy=strategy,
            fill_rate_pct=fill_rate,
            avg_fill_price=avg_fill,
            actual_slippage_bps=actual_slip,
            expected_slippage_bps=expected_slip,
            notional_usd=notional,
            pnl_bps=round(pnl_bps, 4),
            pnl_usd=round(pnl_usd, 2),
            outcome=outcome,
            risk_verdict=risk.get("verdict", "UNKNOWN"),
            audit_status=supervisor.get("audit_status", "UNKNOWN"),
            log_id=supervisor.get("log_id", ""),
            llm_size_pct=llm_size_pct,
            rl_size_pct=rl_size_pct,
        )

        self._append(record)
        return record

    def _append(self, record: TradeRecord):
        with open(self._log_path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    # ── Loading ───────────────────────────────────────────────────────────────

    def load_all(self) -> list[dict]:
        if not self._log_path.exists():
            return []
        records = []
        with open(self._log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records

    def load_last_n(self, n: int) -> list[dict]:
        return self.load_all()[-n:]

    # ── Summary stats ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """
        Compute and return full performance summary.
        Suitable for the /api/performance endpoint.
        """
        records = self.load_all()
        if not records:
            return self._empty_summary()

        # Split by outcome
        executed = [r for r in records if r["outcome"] not in ("VETOED", "FLAT")]
        wins     = [r for r in executed if r["outcome"] == "WIN"]
        losses   = [r for r in executed if r["outcome"] == "LOSS"]
        vetoes   = [r for r in records  if r["outcome"] == "VETOED"]

        total    = len(records)
        n_exec   = len(executed)
        win_rate = len(wins) / n_exec * 100 if n_exec else 0

        pnl_series = [r["pnl_bps"] for r in executed]
        cum_pnl_bps = sum(pnl_series)
        cum_pnl_usd = sum(r["pnl_usd"] for r in executed)

        avg_win_bps  = sum(r["pnl_bps"] for r in wins)  / len(wins)  if wins  else 0
        avg_loss_bps = sum(r["pnl_bps"] for r in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win_bps / avg_loss_bps) if avg_loss_bps else float("inf")

        avg_slippage = sum(r["actual_slippage_bps"] for r in records) / total

        # Sharpe (annualised, assuming each run = 1 day, 252 trading days)
        sharpe = self._sharpe(pnl_series)

        # Max drawdown from cumulative P&L series
        max_dd_bps = self._max_drawdown(pnl_series)

        # RL impact: compare rl_size_pct vs llm_size_pct when both exist
        rl_records  = [r for r in records if r.get("rl_size_pct") is not None]
        rl_adoption = len(rl_records) / total * 100 if total else 0

        # Ticker breakdown
        by_ticker: dict[str, dict] = {}
        for r in executed:
            tk = r["ticker"]
            if tk not in by_ticker:
                by_ticker[tk] = {"trades": 0, "wins": 0, "pnl_bps": 0.0}
            by_ticker[tk]["trades"] += 1
            if r["outcome"] == "WIN":
                by_ticker[tk]["wins"] += 1
            by_ticker[tk]["pnl_bps"] += r["pnl_bps"]

        return {
            "total_runs":        total,
            "executed_trades":   n_exec,
            "vetoed_trades":     len(vetoes),
            "win_rate_pct":      round(win_rate, 1),
            "cum_pnl_bps":       round(cum_pnl_bps, 2),
            "cum_pnl_usd":       round(cum_pnl_usd, 2),
            "avg_win_bps":       round(avg_win_bps, 2),
            "avg_loss_bps":      round(avg_loss_bps, 2),
            "profit_factor":     round(profit_factor, 2),
            "sharpe_ratio":      round(sharpe, 3),
            "max_drawdown_bps":  round(max_dd_bps, 2),
            "avg_slippage_bps":  round(avg_slippage, 2),
            "rl_adoption_pct":   round(rl_adoption, 1),
            "by_ticker":         by_ticker,
            "recent_trades":     records[-10:],   # last 10 for dashboard
        }

    # ── Math helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _sharpe(pnl_series: list[float], risk_free: float = 0.0) -> float:
        """Annualised Sharpe from a series of per-trade P&L in bps."""
        n = len(pnl_series)
        if n < 2:
            return 0.0
        mean = sum(pnl_series) / n
        variance = sum((x - mean) ** 2 for x in pnl_series) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 0
        if std == 0:
            return 0.0
        daily_sharpe = (mean - risk_free) / std
        return daily_sharpe * math.sqrt(252)   # annualise

    @staticmethod
    def _max_drawdown(pnl_series: list[float]) -> float:
        """Max peak-to-trough drawdown in bps from cumulative P&L series."""
        if not pnl_series:
            return 0.0
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_series:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _empty_summary() -> dict:
        return {
            "total_runs": 0, "executed_trades": 0, "vetoed_trades": 0,
            "win_rate_pct": 0, "cum_pnl_bps": 0, "cum_pnl_usd": 0,
            "avg_win_bps": 0, "avg_loss_bps": 0, "profit_factor": 0,
            "sharpe_ratio": 0, "max_drawdown_bps": 0, "avg_slippage_bps": 0,
            "rl_adoption_pct": 0, "by_ticker": {}, "recent_trades": [],
        }
