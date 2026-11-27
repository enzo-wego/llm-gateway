"""Request-level Claude selection shared by the public generation routes."""

from typing import Annotated

from claude_agent_sdk import EffortLevel
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ClaudeModel = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=128,
        pattern=r"^(?:sonnet|opus|haiku|fable|best|claude-[a-z0-9]+(?:-[a-z0-9]+)*)(?:\[1m\])?$",
    ),
]


class ClaudeOverrideUnavailable(HTTPException):
    """An explicit Claude request cannot use the configured route."""

    def __init__(self, status_code: int, detail: str, *, reason: str) -> None:
        super().__init__(status_code, detail)
        self.reason = reason


class ClaudeOverrides(BaseModel):
    """Only non-null fields opt out of legacy provider routing."""

    model_config = ConfigDict(frozen=True)

    model: ClaudeModel | None = Field(
        default=None,
        description="Claude model override. Uses the CLI's model-specific thinking default.",
    )
    effort: EffortLevel | None = Field(
        default=None,
        description="Claude effort override. Uses the CLI's model-specific thinking default.",
    )

    @property
    def explicit(self) -> bool:
        return self.model is not None or self.effort is not None

    def require_available_seat(self, *, backend: str, seat_available: bool) -> None:
        """Reject an explicit override that cannot run on the Claude seat."""
        if not self.explicit:
            return
        if backend != "claude":
            raise ClaudeOverrideUnavailable(
                422,
                "Claude model and effort overrides are unavailable when OpenRouter is primary",
                reason="claude_override_backend",
            )
        if not seat_available:
            raise ClaudeOverrideUnavailable(
                503, "Claude seat is unavailable for this override", reason="seat_unavailable"
            )
