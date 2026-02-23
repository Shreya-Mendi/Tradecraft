"""
Offline RL Trainer — warm-starts the Q-table from logs/trades.jsonl.

Replays historical trade records to pre-train the position sizer before
it starts making live decisions. This accelerates convergence significantly
vs. learning from scratch in production.

Usage:
    python rl/train_offline.py
    python rl/train_offline.py --epochs 5 --verbose
"""

import argparse
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rl.position_sizer import PositionSizer
from analytics.performance_tracker import PerformanceTracker


def parse_args():
    p = argparse.ArgumentParser(description="Offline RL warm-start from trade history")
    p.add_argument("--epochs",  type=int, default=3,   help="Replay passes over the dataset (default: 3)")
    p.add_argument("--verbose", action="store_true",   help="Print per-trade updates")
    p.add_argument("--reset",   action="store_true",   help="Delete existing Q-table before training")
    return p.parse_args()


def load_trades() -> list[dict]:
    tracker = PerformanceTracker()
    records = tracker.load_all()
    # Only use executed (non-vetoed) trades for training
    return [r for r in records if r["outcome"] in ("WIN", "LOSS", "FLAT")]


def record_to_state(record: dict) -> dict:
    """
    Reconstruct a state dict from a persisted TradeRecord.
    We don't have full research payload stored, so we approximate:
      - signal_confidence: from pnl sign and size (winner had high confidence)
      - regime: inferred from action direction
      - drawdown_pct: 0 (not stored per-record yet — future enhancement)
      - vix: 18.4 default (not stored per-record yet)
    """
    return {
        "signal_confidence": 0.8 if record["outcome"] == "WIN" else 0.5,
        "regime":   "RISK_ON"  if record["action"] == "LONG" else "RISK_OFF",
        "drawdown_pct": 0.0,
        "vix": 18.4,
    }


def main():
    args = parse_args()

    trades = load_trades()
    if not trades:
        print("No trade records found in logs/trades.jsonl. Run some pipelines first.")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"  OFFLINE RL TRAINER")
    print(f"  Trades to replay : {len(trades)}")
    print(f"  Epochs           : {args.epochs}")
    print(f"{'='*60}\n")

    if args.reset:
        q_path = Path("logs/q_table.json")
        if q_path.exists():
            q_path.unlink()
            print("  Existing Q-table deleted.\n")

    sizer = PositionSizer()
    initial_states = sizer._step

    total_updates = 0
    total_reward  = 0.0

    for epoch in range(1, args.epochs + 1):
        epoch_reward = 0.0
        for i, record in enumerate(trades):
            state  = record_to_state(record)
            action = float(record.get("size_pct", 2.0))
            reward = float(record.get("pnl_bps", 0.0))

            # Build next_state from the following record (if available)
            next_record = trades[i + 1] if i + 1 < len(trades) else None
            next_state  = record_to_state(next_record) if next_record else None

            sizer.update(state, action, reward, next_state)
            epoch_reward  += reward
            total_reward  += reward
            total_updates += 1

            if args.verbose:
                sk = sizer.discretise(state)
                best = sizer._best_action(sk)
                print(f"  [{epoch}] {record['ticker']:6s} {record['action']:5s} "
                      f"size={action:.1f}% reward={reward:+.2f}bps → best={best:.1f}%")

        print(f"  Epoch {epoch}/{args.epochs}  avg reward: {epoch_reward / len(trades):+.2f} bps  "
              f"ε={sizer.epsilon:.4f}")

    sizer.save()

    summary = sizer.policy_summary()
    print(f"\n{'─'*60}")
    print(f"  Training complete")
    print(f"  Steps before    : {initial_states}")
    print(f"  Steps after     : {sizer._step}")
    print(f"  Total updates   : {total_updates}")
    print(f"  Avg reward      : {total_reward / total_updates:+.2f} bps")
    print(f"  States learned  : {summary['states_visited']}")
    print(f"  Final epsilon   : {summary['epsilon']:.4f}")
    print(f"  Q-table saved   : logs/q_table.json")
    print(f"\n  Greedy policy:")
    for state_key, pol in summary["policy"].items():
        print(f"    {state_key:<35}  →  {pol['best_action_pct']:.1f}%")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
