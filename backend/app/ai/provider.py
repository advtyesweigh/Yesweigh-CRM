"""AI provider abstraction — Gemini today, swappable later."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCallRequest:
    id: str
    name: str
    args: dict[str, Any]
    # Gemini thinking models require this opaque blob on functionCall parts in later turns.
    thought_signature: bytes | None = None


@dataclass
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    thoughts_tokens: int = 0


@dataclass
class ProviderResponse:
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    raw: Any = None


class AIProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        max_output_tokens: int,
    ) -> ProviderResponse:
        """Run one model turn. `messages` may include role=user|model|tool."""


def get_provider(provider_name: str, api_key: str) -> AIProvider:
    name = (provider_name or "gemini").strip().lower()
    if name == "gemini":
        from app.ai.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=api_key)
    raise ValueError(f"Unsupported AI provider: {provider_name}")
