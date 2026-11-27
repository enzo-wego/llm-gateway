"""Translate the opt-in query parameter to native Gemini generation settings."""

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final, Never

from pydantic import JsonValue


class _Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EffortError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_GENERATION_ACTIONS: Final = frozenset({"generateContent", "streamGenerateContent"})
# Exact model IDs; aliases and specialized variants do not inherit capabilities.
_THREE_LEVEL_MODELS: Final = frozenset({
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
})
_LOW_HIGH_MODEL: Final = "gemini-3-pro-preview"
_BUDGETS: Final[Mapping[str, Mapping[_Effort, int]]] = {
    "gemini-2.5-flash": {_Effort.LOW: 256, _Effort.MEDIUM: 4096, _Effort.HIGH: 16384},
    "gemini-2.5-flash-lite": {_Effort.LOW: 512, _Effort.MEDIUM: 4096, _Effort.HIGH: 16384},
    "gemini-2.5-pro": {_Effort.LOW: 1024, _Effort.MEDIUM: 8192, _Effort.HIGH: 24576},
}
_THOUGHT_FLAGS: Final = frozenset({"includeThoughts", "include_thoughts"})
_THINKING_CONTROLS: Final = frozenset({
    "thinkingBudget", "thinking_budget", "thinkingLevel", "thinking_level",
})


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise EffortError("ambiguous_effort_body")
    return fields


def _reject_constant(_: str) -> Never:
    raise EffortError("invalid_effort_body")


def _config_key(parent: Mapping[str, JsonValue], camel: str, snake: str) -> str:
    if camel in parent and snake in parent:
        raise EffortError("ambiguous_effort_body")
    return snake if snake in parent else camel


def _setting(model: str, effort: _Effort) -> tuple[str, str | int]:
    if model in _THREE_LEVEL_MODELS or model == _LOW_HIGH_MODEL:
        if model == _LOW_HIGH_MODEL and effort is _Effort.MEDIUM:
            raise EffortError("unsupported_effort_level")
        return "thinkingLevel", effort.name
    budgets = _BUDGETS.get(model)
    if budgets is None:
        raise EffortError("unsupported_effort_model")
    return "thinkingBudget", budgets[effort]


def apply_effort(model: str, action: str, efforts: Sequence[str], body: bytes) -> bytes:
    """Merge the requested effort without replacing any native thinking controls."""
    if len(efforts) != 1:
        raise EffortError("invalid_effort")
    try:
        effort = _Effort(efforts[0])
    except ValueError:
        raise EffortError("invalid_effort") from None
    if action not in _GENERATION_ACTIONS:
        raise EffortError("unsupported_effort_action")
    name, value = _setting(model, effort)

    try:
        payload: JsonValue = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        if not isinstance(payload, dict):
            raise EffortError("invalid_effort_body")
        if "model" in payload and payload["model"] not in (model, f"models/{model}"):
            raise EffortError("conflicting_effort_model")
        generation_key = _config_key(payload, "generationConfig", "generation_config")
        generation = payload.setdefault(generation_key, {})
        if not isinstance(generation, dict):
            raise EffortError("invalid_generation_config")
        thinking_key = _config_key(generation, "thinkingConfig", "thinking_config")
        thinking = generation.setdefault(thinking_key, {})
        if not isinstance(thinking, dict):
            raise EffortError("invalid_thinking_config")
        if _THINKING_CONTROLS.intersection(thinking):
            raise EffortError("conflicting_thinking_config")
        if len(thinking) > 1 or any(
            key not in _THOUGHT_FLAGS or not isinstance(flag, bool)
            for key, flag in thinking.items()
        ):
            raise EffortError("invalid_thinking_config")
        thinking[name] = value
        return json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, RecursionError):
        raise EffortError("invalid_effort_body") from None
