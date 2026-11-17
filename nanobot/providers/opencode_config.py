"""Phase 00 OpenCode model and execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APPROVED_FREE_MODEL = "opencode-go/glm-5.3"


@dataclass(frozen=True)
class OpenCodeEngineConfig:
    executable: str = "opencode"
    model: str = APPROVED_FREE_MODEL
    agent: str = "phase00-readonly"
    timeout_seconds: float = 120
    max_output_bytes: int = 1_048_576
    max_event_bytes: int = 65_536
    working_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.model != APPROVED_FREE_MODEL:
            raise ValueError(f"OpenCode model is not approved for free-only execution: {self.model}")
        if self.timeout_seconds <= 0:
            raise ValueError("OpenCode timeout must be positive")
        if self.max_output_bytes <= 0 or self.max_event_bytes <= 0:
            raise ValueError("OpenCode output limits must be positive")
        if self.max_event_bytes > self.max_output_bytes:
            raise ValueError("OpenCode event limit cannot exceed total output limit")
