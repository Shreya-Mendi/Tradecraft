"""
Base Agent — all 5 agents inherit from this.
Each agent has: a role, a system prompt, access to the bus, and a run() method.
"""

import json
from abc import ABC, abstractmethod
from core.bus import MessageBus, Message
from core.llm import call_llm


class BaseAgent(ABC):
    name: str = "base_agent"
    system_prompt: str = "You are a financial agent."

    def __init__(self, bus: MessageBus):
        self.bus = bus

    def think(self, user_message: str) -> dict:
        """Call the LLM and parse JSON response."""
        raw = call_llm(self.system_prompt, user_message)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Gracefully handle non-JSON responses
            return {"raw_response": raw}

    def post(self, message_type: str, payload: dict, in_reply_to: str = None) -> Message:
        """Post a message to the shared bus."""
        msg = Message(
            sender=self.name,
            message_type=message_type,
            payload=payload,
            in_reply_to=in_reply_to,
        )
        return self.bus.post(msg)

    @abstractmethod
    def run(self) -> Message:
        """Each agent implements its own run() logic."""
        pass
