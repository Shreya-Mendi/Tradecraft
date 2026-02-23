"""
RL Position Sizer — Q-table reinforcement learning for position sizing.

Replaces the LLM's hallucinated size_pct with a learned policy that
maximises realised P&L bps over time.

Architecture
────────────
  State  : (signal_bucket, regime_bucket, drawdown_bucket, vol_bucket)
  Action : position size in % of NAV — one of {1, 2, 3, 4, 5}
  Reward : realised P&L in bps from the LOB SimulationResult (or estimated)
  Update : tabular Q-learning  Q(s,a) ← Q(s,a) + α[r + γ·max Q(s',a') - Q(s,a)]

The Q-table persists to logs/q_table.json so the agent learns across
multiple sessions without any deep-learning framework dependency.

Usage
─────
    sizer = PositionSizer()
    recommended_pct = sizer.recommend(state_dict)
    # ... run pipeline, get SimulationResult ...
    sizer.update(state_dict, recommended_pct, reward_bps)
    sizer.save()
"""

import json
import math
import random
from pathlib import Path
from typing import Optional

Q_TABLE_PATH  = Path("logs/q_table.json")
ACTIONS       = [1.0, 2.0, 3.0, 4.0, 5.0]   # % of NAV — capped at risk limit
MAX_POSITION  = 5.0                            # hard cap enforced by RiskManager


class PositionSizer:
    """
    Tabular ε-greedy Q-learning agent for position sizing.

    Hyperparameters chosen conservatively for financial RL:
      alpha (learning rate) = 0.1  — slow, stable updates
      gamma (discount)      = 0.9  — future rewards matter but not dominantly
      epsilon               = 0.15 — 15% exploration; decays over time
    """

    def __init__(
        self,
        q_path:  str   = str(Q_TABLE_PATH),
        alpha:   float = 0.10,
        gamma:   float = 0.90,
        epsilon: float = 0.15,
        epsilon_decay: float = 0.995,
        epsilon_min:   float = 0.02,
    ):
        self._q_path    = Path(q_path)
        self.alpha      = alpha
        self.gamma      = gamma
        self.epsilon    = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min   = epsilon_min
        self._step      = 0

        # Q-table: dict  state_key → {action_str → q_value}
        self._q: dict[str, dict[str, float]] = {}
        self._load()

    # ── State discretisation ──────────────────────────────────────────────────

    @staticmethod
    def discretise(state: dict) -> str:
        """
        Convert continuous state dict to a hashable string key.

        State inputs:
          signal_confidence : float 0–1    → 3 buckets (low/med/high)
          regime            : str           → 3 values  (risk_on/neutral/risk_off)
          drawdown_pct      : float 0–100  → 3 buckets  (ok/caution/danger)
          vix               : float         → 3 buckets  (calm/normal/stressed)
        """
        conf = float(state.get("signal_confidence", 0.5))
        if conf < 0.5:
            conf_b = "low"
        elif conf < 0.75:
            conf_b = "med"
        else:
            conf_b = "high"

        regime_raw = str(state.get("regime", "NEUTRAL")).upper()
        if "ON" in regime_raw or "BULL" in regime_raw:
            regime_b = "on"
        elif "OFF" in regime_raw or "BEAR" in regime_raw:
            regime_b = "off"
        else:
            regime_b = "neutral"

        dd = float(state.get("drawdown_pct", 0))
        if dd < 3:
            dd_b = "ok"
        elif dd < 7:
            dd_b = "caution"
        else:
            dd_b = "danger"

        vix = float(state.get("vix", 18.4))
        if vix < 15:
            vix_b = "calm"
        elif vix < 25:
            vix_b = "normal"
        else:
            vix_b = "stressed"

        return f"{conf_b}|{regime_b}|{dd_b}|{vix_b}"

    # ── Q-table helpers ───────────────────────────────────────────────────────

    def _q_row(self, state_key: str) -> dict[str, float]:
        if state_key not in self._q:
            # Initialise with small optimistic values (encourages exploration)
            self._q[state_key] = {str(a): 0.5 for a in ACTIONS}
        return self._q[state_key]

    def _best_action(self, state_key: str) -> float:
        row = self._q_row(state_key)
        return float(max(row, key=row.get))

    # ── Policy ────────────────────────────────────────────────────────────────

    def recommend(self, state: dict) -> float:
        """
        Return the recommended position size (% of NAV) for the given state.
        Uses ε-greedy: with probability ε explore randomly, else exploit.

        Always clamps output to [1.0, MAX_POSITION].
        """
        state_key = self.discretise(state)

        if random.random() < self.epsilon:
            action = float(random.choice(ACTIONS))
        else:
            action = self._best_action(state_key)

        return min(action, MAX_POSITION)

    # ── Learning ──────────────────────────────────────────────────────────────

    def update(
        self,
        state:        dict,
        action_taken: float,
        reward_bps:   float,
        next_state:   Optional[dict] = None,
    ):
        """
        Q-learning update after observing the reward.

        reward_bps: realised P&L in basis points (from SimulationResult.pnl_bps
                    or estimated by PerformanceTracker).
                    Positive = win, negative = loss.
        """
        state_key  = self.discretise(state)
        action_str = str(float(action_taken))

        # Ensure the action is in our discrete set (snap to nearest)
        if action_str not in {str(a) for a in ACTIONS}:
            action_str = str(min(ACTIONS, key=lambda a: abs(a - action_taken)))

        # Reward shaping: scale bps to a [-1, 1] range for stable learning
        # Clip at ±50 bps (extreme outliers don't destabilise the table)
        r = max(-50, min(50, reward_bps)) / 50.0

        # Max future Q
        if next_state:
            next_key  = self.discretise(next_state)
            max_next_q = max(self._q_row(next_key).values())
        else:
            max_next_q = 0.0

        # Q update
        row = self._q_row(state_key)
        old_q = row.get(action_str, 0.0)
        row[action_str] = old_q + self.alpha * (r + self.gamma * max_next_q - old_q)

        # Decay epsilon
        self._step += 1
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self):
        self._q_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "q_table": self._q,
            "epsilon": self.epsilon,
            "step":    self._step,
            "actions": ACTIONS,
        }
        self._q_path.write_text(json.dumps(data, indent=2))

    def _load(self):
        if not self._q_path.exists():
            return
        try:
            data = json.loads(self._q_path.read_text())
            self._q       = data.get("q_table", {})
            self.epsilon  = data.get("epsilon",  self.epsilon)
            self._step    = data.get("step",     0)
        except Exception as exc:
            print(f"  [rl] Could not load Q-table ({exc}) — starting fresh")

    # ── Introspection ─────────────────────────────────────────────────────────

    def policy_summary(self) -> dict:
        """
        Return the greedy policy (best action per state) for all known states.
        Useful for debugging and dashboard display.
        """
        policy = {}
        for state_key, row in self._q.items():
            best = max(row, key=row.get)
            policy[state_key] = {
                "best_action_pct": float(best),
                "q_values": {k: round(v, 4) for k, v in row.items()},
            }
        return {
            "step":    self._step,
            "epsilon": round(self.epsilon, 4),
            "states_visited": len(self._q),
            "policy": policy,
        }

    def build_state(
        self,
        research_payload: dict,
        macro_context:    dict,
        performance_summary: dict,
    ) -> dict:
        """
        Build the state dict from pipeline outputs + macro + performance history.
        Call this inside SignalAgent before calling recommend().
        """
        return {
            "signal_confidence": float(research_payload.get("confidence", 0.5)),
            "regime":            research_payload.get("regime", macro_context.get("regime", "NEUTRAL")),
            "drawdown_pct":      float(performance_summary.get("max_drawdown_bps", 0)) / 100,
            "vix":               float(macro_context.get("vix", 18.4)),
        }
