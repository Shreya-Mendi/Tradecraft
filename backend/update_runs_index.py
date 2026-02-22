"""
update_runs_index.py — Maintains the runs-index.json file.

Appends the latest run's metadata to an index file so the frontend
can display a history of past pipeline runs.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--run-id",   required=True, dest="run_id")
    p.add_argument("--ticker",   required=True)
    p.add_argument("--headline", required=True)
    p.add_argument("--index",    required=True)
    return p.parse_args()


def main():
    args = parse_args()
    index_path = Path(args.index)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing index
    if index_path.exists():
        entries = json.loads(index_path.read_text())
    else:
        entries = []

    # Prepend new entry
    entries.insert(0, {
        "run_id":    args.run_id,
        "ticker":    args.ticker.upper(),
        "headline":  args.headline,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Keep last 50 runs
    entries = entries[:50]
    index_path.write_text(json.dumps(entries, indent=2))
    print(f"✓ Runs index updated ({len(entries)} entries)")


if __name__ == "__main__":
    main()
