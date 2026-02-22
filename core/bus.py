"""
Message Bus — shared memory and communication backbone for all agents.

All agents read from and write to the bus. Every message is timestamped
and appended to an immutable audit log.
"""

import uuid
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from pathlib import Path


@dataclass
class Message:
    sender: str
    message_type: str          # e.g. "RESEARCH_SIGNAL", "TRADE_PROPOSAL", "VETO", "AUDIT"
    payload: dict
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    in_reply_to: Optional[str] = None  # message_id of parent message


class MessageBus:
    """
    Central shared memory. Agents post messages here; others read them.
    The bus also writes every message to an append-only JSON log file.
    """

    def __init__(self, log_path: str = "logs/audit.jsonl"):
        self._messages: list[Message] = []
        self._shared_state: dict[str, Any] = {}  # key-value store for agent state
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Messaging ─────────────────────────────────────────────────────────────

    def post(self, message: Message) -> Message:
        """Post a message to the bus and persist it to the audit log."""
        self._messages.append(message)
        self._append_to_log(message)
        return message

    def get_messages(self, message_type: Optional[str] = None, sender: Optional[str] = None) -> list[Message]:
        """Filter messages by type and/or sender."""
        msgs = self._messages
        if message_type:
            msgs = [m for m in msgs if m.message_type == message_type]
        if sender:
            msgs = [m for m in msgs if m.sender == sender]
        return msgs

    def latest(self, message_type: str) -> Optional[Message]:
        """Get the most recent message of a given type."""
        matches = self.get_messages(message_type=message_type)
        return matches[-1] if matches else None

    def all_messages(self) -> list[Message]:
        return list(self._messages)

    # ── Shared State ──────────────────────────────────────────────────────────

    def set_state(self, key: str, value: Any):
        self._shared_state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._shared_state.get(key, default)

    # ── Audit Log ─────────────────────────────────────────────────────────────

    def _append_to_log(self, message: Message):
        with open(self._log_path, "a") as f:
            f.write(json.dumps(asdict(message)) + "\n")

    def print_thread(self):
        """Pretty-print the full message thread to stdout."""
        print("\n" + "═" * 70)
        print("  AGENT MESSAGE THREAD")
        print("═" * 70)
        for msg in self._messages:
            print(f"\n[{msg.timestamp[11:19]} UTC] {msg.sender.upper()} → {msg.message_type}")
            print("─" * 50)
            for k, v in msg.payload.items():
                val = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                print(f"  {k:25s}: {val}")
        print("\n" + "═" * 70)
