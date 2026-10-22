"""Explicit adapter contract for text replies that drive shell actions.

The mini-SWE-agent v1 parser extracts exactly one bash fence and strips the
outer command whitespace. Compare that command exactly, without executing it.
Settings bind the pinned parser and execution configuration to the native
profile identity; ordinary text calls keep their existing behavior.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

COMPARATOR_VERSION = "mini-swe-bash-v1"
SETTINGS_PREFIX = "text-action-settings-v1:"


@dataclass(frozen=True)
class TextActionContract:
    settings_version: str
    comparator_version: str = COMPARATOR_VERSION

    def __post_init__(self) -> None:
        if self.comparator_version != COMPARATOR_VERSION or not re.fullmatch(
            SETTINGS_PREFIX + r"[0-9a-f]{64}", self.settings_version
        ):
            raise ValueError("unsupported text action comparison contract")

    def request_supported(self, request: dict[str, Any], protocol: str) -> bool:
        count, stream, tools = (
            request.get("n"),
            request.get("stream"),
            request.get("tools"),
        )
        return (
            protocol in {"openai.chat.completions.v1", "litellm.completion.v1"}
            and (count is None or (type(count) is int and count == 1))
            and (stream is None or (type(stream) is bool and not stream))
            and (tools is None or (isinstance(tools, list) and not tools))
            and all(
                request.get(name) is None
                for name in (
                    "response_format",
                    "output_format",
                    "output_config",
                    "tool_choice",
                    "parallel_tool_calls",
                    "functions",
                    "function_call",
                )
            )
        )

    def action(self, text: str) -> str | None:
        actions = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
        return actions[0].strip() if len(actions) == 1 else None


_current: ContextVar[TextActionContract | None] = ContextVar(
    "agentc_text_action_contract", default=None
)


def current_text_action_contract() -> TextActionContract | None:
    return _current.get()


@contextmanager
def text_action_scope(contract: TextActionContract) -> Iterator[None]:
    if type(contract) is not TextActionContract:
        raise ValueError("text action scope needs an explicit supported contract")
    token = _current.set(contract)
    try:
        yield
    finally:
        _current.reset(token)
