"""Request-level Claude selection shared by the public generation routes."""

from typing import Annotated, Literal

from claude_agent_sdk import EffortLevel
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter, ValidationInfo, field_validator

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

    backend: Literal["openrouter"] | None = None
    model: str | None = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if info.data.get("backend") == "openrouter":
            return _OR_MODEL.validate_python(value)
        return _CLAUDE_MODEL.validate_python(value)

    @field_validator("effort")
    @classmethod
    def validate_effort(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None:
            if info.data.get("backend") == "openrouter":
                if value == "max":
                    raise ValueError("unsupported OpenRouter effort")
            else:
                _CLAUDE_EFFORT.validate_python(value)
        return value

    @property
    def explicit(self) -> bool:
        return self.model is not None or self.effort is not None

    def require_available_seat(self, *, backend: str, seat_available: bool) -> None:
        """Reject an explicit override that cannot run on the Claude seat."""
        if not self.explicit or self.backend == "openrouter":
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


_CLAUDE_MODEL = TypeAdapter(ClaudeModel)
_CLAUDE_EFFORT = TypeAdapter(EffortLevel)
_OR_MODEL = TypeAdapter(Annotated[str, StringConstraints(
    max_length=160, pattern=r"^~?[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$",
)])
